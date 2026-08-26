"""Tier 3 Claude judge — calls Anthropic API to interpret tricky rulings.

When tier 1 (hardcoded handlers), tier 1.5 (templates), and tier 2
(SpellResolver) all miss an effect, the engine escalates to tier 3:
ask Claude to interpret the oracle text and return JSON actions that
mtg.actions.execute_action_on_state can apply.

Public free functions (each takes a RulesEngine instance as first arg):

    ask_judge(rules, game, question, context='')
        Plain text answer. No state changes — just a ruling.

    ask_judge_with_fix(rules, game, question, ...)
        Returns ruling + JSON actions. The actions get applied via
        rules._execute_action_on_state().

    resolve_effect(rules, game, effect_description, ...)
        Primary tier 3 entry point. Sends oracle text + game state to
        Claude, parses returned JSON actions, applies them. Used by
        cast_spell_async, trigger handlers, !resolve command.

    describe_game_for_judge(rules, game)
        Format game state into a compact string for Claude's prompt.

State touched on `rules`:

    rules.client                 — anthropic.Anthropic client
    rules.model                  — model name string
    rules.usage_callback         — token tracker
    rules.game_log               — game event log
    rules._cached_judge_desc     — judge description cache
    rules._cached_judge_fingerprint
    rules._resolve_dedupe        — per-turn dedupe cache
    rules._execute_action_on_state — applies returned JSON actions
    rules._strip_think_tags      — pre-existing duck-typed call (only
                                   used when raw_text contains <think>;
                                   ClaudePlayer has this method, RulesEngine
                                   doesn't — same as before refactor)

Extracted from mtg/rules_engine.py during the Phase 2 OSS-readability
refactor (Phase 2B).
"""

import asyncio
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from mtg.constants import Phase, Zone
from mtg.helpers import response_text
from mtg.models import Card, Player, GameState
from rules.effect_templates import has_residual_clause_beyond_library_look


# May 20 audit: the AI sometimes attaches a synthetic combat description to a
# `resolve` action after a cast — game_1506604518098342018 planned
# ["resolve: Craterhoof enters, pump team for +10/+10 trample. Attack for
# lethal."], which fired in MAIN1 as a sorcery-speed -999 life event and
# violated CR 510.1 (combat damage happens only in the combat damage step).
#
# Aug 2 batch-14 audit (R-M2, CRITICAL): the guard used to match the
# SUBSTRING "combat damage" anywhere, including text that merely REFERENCES
# combat damage as the CONDITION of a future replacement effect. Jeska,
# Thrice Reborn's [0] — "Choose target creature. Until your next turn, if
# that creature would deal combat damage to one of your opponents, it deals
# triple that damage to that player instead" — is a legal sorcery-speed
# loyalty ability (CR 606.3) that deals nothing at all when it resolves, so
# it was refused on EVERY activation in every game and the card was
# permanently non-functional. A conditional/future framing sets damage up;
# it does not deal it now.
#
# Module-level (not inline in resolve_effect) so the predicate is shared with
# its tests rather than mirrored by them — a mirrored copy passes no matter
# what production does, which is how the first version of this pin survived
# its own mutant.
_SETS_UP_FUTURE_DAMAGE_RE = re.compile(
    r'\b(?:if|whenever|when)\b[^.]*\bwould deal\b|\buntil your next turn\b')
_ATTACK_TRIGGER_CLAUSE_RE = re.compile(
    r'\b(?:when|whenever)\b[^,.;]*\battacks?\b\s*,?', re.IGNORECASE)
_COMBAT_SHAPED_RE = re.compile(
    r'\b(attack(?:s|ing|ed)?|combat damage|deals? lethal|'
    r'deal lethal damage|for lethal)\b')
# Aug 10 deferred (D): an effect whose ENTIRE printed instruction is dealing
# damage. FULL match by construction — the guard that consumes it refuses a
# direct kill/exile from such an effect (CR 704.5g says state-based actions
# decide lethality, not the resolver), and a full anchor is what makes that
# refusal have no legitimate counterexample. Module scope so the pin drives
# THIS object rather than re-expressing it — a mirrored predicate is a comment,
# not a test (this project has shipped that mistake repeatedly).
_DAMAGE_ONLY_EFFECT = re.compile(
    r'^[^.]*?\bdeals?\s+(?:\d+|x|that much)\s+damage\s+to\s+[^.]*?\.?\s*$',
    re.IGNORECASE)


def is_combat_shaped_resolve(effect_description: str) -> bool:
    """Whether a free-text resolve claims to deal combat damage RIGHT NOW.

    True  -> refuse it (CR 510.1): "Attack for lethal", "deals combat damage
             to each opponent".
    False -> allow it: text that merely sets up or references future combat
             damage ("until your next turn, if that creature would deal
             combat damage ... instead").
    """
    lowered = (effect_description or "").lower()
    if _SETS_UP_FUTURE_DAMAGE_RE.search(lowered):
        return False
    # "Whenever this creature ... attacks, search your library" is a legal
    # attack-triggered effect, not an attempt to manufacture combat damage in
    # a resolve action. Remove only the trigger condition before applying the
    # combat guard; any combat claim in the effect itself remains visible.
    lowered = _ATTACK_TRIGGER_CLAUSE_RE.sub('', lowered)
    return bool(_COMBAT_SHAPED_RE.search(lowered))


async def ask_judge(rules, game: GameState, question: str, context: str = "") -> str:
    """
    Ask Claude to rule on a complex interaction.
    Used for triggered abilities, stack resolution, and edge cases.
    """
    if not rules.client:
        return "⚠️ No rules judge available (Claude client not configured)"
    
    # Build game state context (cached for batched trigger resolution)
    current_fp = game._state_fingerprint()
    if current_fp == rules._cached_judge_fingerprint and rules._cached_judge_desc:
        state_desc = rules._cached_judge_desc
        print("[RESOLVE-CACHE] HIT — reusing judge description")
    else:
        state_desc = rules._describe_game_for_judge(game)
        rules._cached_judge_desc = state_desc
        rules._cached_judge_fingerprint = current_fp

    # Add recent game log
    recent_log = "\n".join(rules.game_log[-10:]) if rules.game_log else "No recent events"

    prompt = f"""You are an expert Magic: The Gathering judge. Answer the following rules question based on the comprehensive rules.

CURRENT GAME STATE:
{state_desc}

RECENT EVENTS:
{recent_log}

ADDITIONAL CONTEXT:
{context}

QUESTION:
{question}

Provide a clear, concise ruling. If there are triggered abilities that need to go on the stack, list them in order (APNAP if relevant). If the answer depends on player choices, explain the options.

Be specific about:
1. What happens mechanically
2. In what order things resolve
3. Any player choices required
4. Relevant comprehensive rules citations (just numbers, e.g., CR 603.3c)"""

    try:
        _judge_kwargs = dict(
            model=rules.model,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        if hasattr(rules.client.messages, '_log_tag'):
            _judge_kwargs['purpose'] = 'ask_judge'
        response = await asyncio.to_thread(
            rules.client.messages.create,
            **_judge_kwargs,
        )
        if rules.usage_callback and hasattr(response, 'usage'):
            rules.usage_callback(response.usage, rules.model)
        return response_text(response)
    except Exception as e:
        return f"⚠️ Judge error: {e}"


async def ask_judge_with_fix(rules, game: GameState, question: str,
                              controller: str = "") -> str:
    """
    Ask Claude to rule on a question AND generate executable fix commands.

    Returns a ruling string that includes both the explanation and any
    game state changes that were applied. This is the "judge with hands"
    mode — the judge doesn't just explain what should happen, it actually
    does it.
    """
    if not rules.client:
        return "⚠️ No rules judge available (Claude client not configured)"

    # Build game state context (cached for batched trigger resolution)
    current_fp = game._state_fingerprint()
    if current_fp == rules._cached_judge_fingerprint and rules._cached_judge_desc:
        state_desc = rules._cached_judge_desc
        print("[RESOLVE-CACHE] HIT — reusing judge description")
    else:
        state_desc = rules._describe_game_for_judge(game)
        rules._cached_judge_desc = state_desc
        rules._cached_judge_fingerprint = current_fp
    recent_log = "\n".join(rules.game_log[-10:]) if rules.game_log else "No recent events"

    # Get both: a ruling AND structured actions
    prompt = f"""You are an expert Magic: The Gathering judge resolving a game situation.

CURRENT GAME STATE:
{state_desc}

RECENT EVENTS:
{recent_log}

SITUATION TO RESOLVE:
{question}

Controller of the effect: {controller}

You MUST respond in TWO parts:

PART 1 - RULING: Brief explanation of what happens according to the rules (2-3 sentences max).

PART 2 - ACTIONS: A JSON array of game state changes to apply. Use these action formats:
- {{"action": "deal_damage", "amount": N, "target_player": "name"}}
- {{"action": "draw_cards", "player": "name", "amount": N}}
- {{"action": "gain_life", "player": "name", "amount": N}}
- {{"action": "lose_life", "player": "name", "amount": N}}
- {{"action": "destroy", "card": "Card Name"}}
- {{"action": "move_card", "card": "X", "from_zone": "zone", "to_zone": "zone", "player": "name"}}
- {{"action": "move_card", "card": "X", "from_zone": "graveyard", "to_zone": "library", "position": "top", "player": "name"}}
- {{"action": "create_token", "player": "name", "name": "N", "power": P, "toughness": T, "types": "...", "count": N, "keywords": ["defender", "flying"]}} — ALWAYS include the token's keywords (defender, flying, etc.); omitting them creates a token WITHOUT those abilities
- {{"action": "prevent_next_damage", "card": "Name", "amount": N}} — "prevent the next N damage that would be dealt to X this turn" (Eiganjo Castle). A consumable shield, NOT a full prevention: it absorbs N and lets the rest through.
- {{"action": "add_counters", "card": "X", "counter_type": "+1/+1", "amount": N}}
- {{"action": "remove_keywords", "card": "X", "keywords": ["Hexproof"]}} — a permanent LOSES keywords until end of turn; use "player": "name" (omit "card") for "permanents your opponents control lose ..." effects
- {{"action": "steal_permanent", "player": "thief", "from_player": "current controller", "card": "Card Name"}} — PERMANENT control change (Agent of Treachery). For "gain control ... UNTIL END OF TURN" (Act of Treason family) you MUST add "until_end_of_turn": true — control then reverts automatically at end of turn; add "untap": true and "gain_haste": true when the printed effect untaps it / grants haste
- {{"action": "pump_all_creatures", "player": "name", "power": N, "toughness": N}} — TEMPORARY +N/+N until end of turn (NOT counters). Add "card": "Name" to pump ONLY that one creature — "this creature gets +N/+N" MUST be scoped with "card", never applied to the whole team
- {{"action": "scry", "player": "name", "amount": N}}
- {{"action": "tap", "card": "X"}} — add "skip_next_untap": true for "doesn't untap during its controller's next untap step" riders (Frost Lynx family); omitting it silently drops the printed rider
- {{"action": "untap", "card": "X"}}
- {{"action": "add_mana", "player": "name", "color": "C", "amount": N}}
- {{"action": "discard", "player": "name", "card": "Card Name"}}
- {{"action": "no_action", "reason": "why"}}

IMPORTANT: Use "add_counters" ONLY for permanent +1/+1 counters (e.g., "enters with X +1/+1 counters").
For "gets +N/+N until end of turn" effects, use "pump_all_creatures" instead — these are TEMPORARY boosts.

If no game state changes are needed, use: [{{"action": "no_action", "reason": "explanation"}}]

Format your response EXACTLY like this:
RULING: [your ruling text]
ACTIONS: [your JSON array]"""

    try:
        _judge_fix_kwargs = dict(
            model=rules.model,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        if hasattr(rules.client.messages, '_log_tag'):
            _judge_fix_kwargs['purpose'] = 'ask_judge_with_fix'
        response = await asyncio.to_thread(
            rules.client.messages.create,
            **_judge_fix_kwargs,
        )
        if rules.usage_callback and hasattr(response, 'usage'):
            rules.usage_callback(response.usage, rules.model)

        text = response_text(response).strip()
        # May 7 audit fix #10: collapse multi-line JSON so the log preview
        # stays one line instead of dumping a 20-line indented block.
        try:
            _judge_preview = re.sub(r'\s+', ' ', text).strip()
        except Exception:
            _judge_preview = text.replace('\n', ' ')
        print(f"[JUDGE-FIX] Raw response: {_judge_preview[:300]}")

        # Parse ruling and actions
        ruling_text = text
        actions_applied = []

        # Handle JSON-formatted responses: {"RULING": "...", "ACTIONS": [...]}
        if text.startswith('{'):
            try:
                judge_json = json.loads(text)
                if isinstance(judge_json, dict):
                    if 'RULING' in judge_json:
                        ruling_text = judge_json['RULING']
                    if 'ACTIONS' in judge_json and isinstance(judge_json['ACTIONS'], list):
                        for action in judge_json['ACTIONS']:
                            if isinstance(action, dict) and action.get("action") != "no_action":
                                msg = rules._execute_action_on_state(game, action)
                                if msg:
                                    actions_applied.append(msg)
                                    print(f"[JUDGE-FIX] Applied: {msg}")
                        # Build response and skip the regex path below
                        parts = [f"📜 **Judge Ruling:**\n{ruling_text}"]
                        if actions_applied:
                            parts.append("\n**Applied changes:**")
                            for msg in actions_applied:
                                parts.append(f"  {msg}")
                        return "\n".join(parts)
            except json.JSONDecodeError:
                pass  # Fall through to regex parsing

        # Extract ACTIONS: section (text format: RULING: ... ACTIONS: [...])
        actions_match = re.search(r'ACTIONS:\s*(\[.*\])', text, re.DOTALL)
        if actions_match:
            try:
                actions_json = json.loads(actions_match.group(1))
                # Execute each action
                for action in actions_json:
                    if action.get("action") == "no_action":
                        continue
                    msg = rules._execute_action_on_state(game, action)
                    if msg:
                        actions_applied.append(msg)
                        print(f"[JUDGE-FIX] Applied: {msg}")
            except json.JSONDecodeError as e:
                print(f"[JUDGE-FIX] Failed to parse actions JSON: {e}")

        # Extract RULING: section
        ruling_match = re.search(r'RULING:\s*(.+?)(?=\nACTIONS:|\Z)', text, re.DOTALL)
        if ruling_match:
            ruling_text = ruling_match.group(1).strip()

        # Sanitize: strip leaked JSON from ruling text before posting to Discord
        # Raw JSON like {"RULING": ..., "ACTIONS": [...]} should not appear in messages
        # Check both uppercase and lowercase keys (DeepSeek uses lowercase)
        ruling_lower = ruling_text.lower() if ruling_text else ''
        if ruling_text and ('"ruling"' in ruling_lower or '"actions"' in ruling_lower
                            or '"action"' in ruling_lower or '"mechanical_steps"' in ruling_lower
                            or '"relevant_rules"' in ruling_lower):
            try:
                # Try to extract just the ruling from JSON structure
                parsed = json.loads(ruling_text)
                if isinstance(parsed, dict):
                    # Try both cases
                    ruling_text = parsed.get('RULING') or parsed.get('ruling') or ''
                    if not ruling_text:
                        # Fall back to any string value
                        for v in parsed.values():
                            if isinstance(v, str) and len(v) > 20:
                                ruling_text = v
                                break
            except (json.JSONDecodeError, TypeError):
                pass
            # Strip any remaining JSON arrays/objects from the text
            ruling_text = re.sub(r'\s*"(?:ACTIONS|actions)"\s*:\s*\[.*?\]', '', ruling_text, flags=re.DOTALL)
            ruling_text = re.sub(r'\s*"(?:mechanical_steps|relevant_rules|resolution_order|player_choices|relevant_rules_citations|order_of_resolution)"\s*:\s*\[.*?\]', '', ruling_text, flags=re.DOTALL)
            ruling_text = re.sub(r'^\s*\{\s*"(?:RULING|ruling)"\s*:\s*"?', '', ruling_text)
            ruling_text = re.sub(r'"?\s*\}\s*$', '', ruling_text)
            ruling_text = ruling_text.strip().strip('"').strip()
            # If still looks like JSON, just extract readable text
            if ruling_text.startswith('{') or ruling_text.startswith('['):
                ruling_text = re.sub(r'[{}\[\]"]', '', ruling_text)
                ruling_text = ruling_text.strip()

        # Also strip unquoted action-dict prose that DeepSeek sometimes emits
        # as RULING content, e.g.:
        #   "action: move_card, card: X, from_zone: library, to_zone: battlefield, player: Claude"
        # This is not JSON — it's the action schema rendered as prose. Scrub it
        # so only genuine ruling language reaches Discord.
        if ruling_text:
            action_prose = re.compile(
                r'action\s*:\s*\w+(?:\s*,\s*(?:card|target_card|target_player|target_controller|'
                r'player|from_zone|to_zone|amount|counter_type|color|name|power|toughness|'
                r'types|count|position|keywords|reason|min_power|type)\s*:\s*[^,\n]+)+',
                re.IGNORECASE,
            )
            ruling_text = action_prose.sub('', ruling_text)
            # Apr 29 audit: also catch the action-prose-without-`action:`-prefix
            # form, e.g. "Eternities Crafter, from_zone: library, to_zone: battlefield, player: Claude".
            # DeepSeek emits this when it skips the "action: move_card" header.
            # Match a leading bareword/card name followed by 2+ schema-key: value pairs.
            bare_action_prose = re.compile(
                r'(?m)^\s*[A-Z][\w\'\-,/ ]{0,80}?'
                r'(?:\s*,\s*(?:card|target_card|target_player|target_controller|'
                r'player|from_zone|to_zone|amount|counter_type|color|name|power|toughness|'
                r'types|count|position|keywords|reason|min_power|type)\s*:\s*[^,\n]+){2,}',
                re.IGNORECASE,
            )
            ruling_text = bare_action_prose.sub('', ruling_text)
            # Collapse leftover commas/whitespace from the excision.
            ruling_text = re.sub(r',\s*,', ',', ruling_text)
            ruling_text = re.sub(r'^\s*,\s*|\s*,\s*$', '', ruling_text, flags=re.MULTILINE)
            ruling_text = re.sub(r'\n\s*\n', '\n', ruling_text).strip()

        # May 20 audit fix: pass through the centralized prose sanitizer to
        # drop ACTIONS:/RULING:/Applied changes: scaffolding headers, orphan
        # conjunction-leading sentences (May 19's hedge-stripper sometimes
        # eats subjects, leaving "but no specific card is named..."), and
        # unmatched close-paren fragments like "controller mills 3)".
        from mtg.helpers import sanitize_judge_prose
        if ruling_text:
            ruling_text = sanitize_judge_prose(ruling_text)

        # Final fallback: if scrubbing emptied the ruling (DeepSeek returned
        # only action prose with no actual explanation), substitute a neutral
        # message that points at the applied-changes list below.
        if not ruling_text or len(ruling_text) < 8:
            ruling_text = "Effect resolved." if actions_applied else "No game state change."

        # Build combined response
        parts = [f"📜 **Judge Ruling:**\n{ruling_text}"]
        if actions_applied:
            parts.append("\n**Applied changes:**")
            for msg in actions_applied:
                parts.append(f"  {msg}")
        return "\n".join(parts)

    except Exception as e:
        print(f"[JUDGE-FIX] Error: {e}")
        # Fall back to text-only ruling
        return await rules.ask_judge(game, question)


async def resolve_effect(rules, game: GameState, effect_description: str,
                          source_card: str = "", controller: str = "",
                          context: str = "") -> Tuple[List[str], List[Dict]]:
    """
    Ask Claude to resolve an effect and return EXECUTABLE ACTIONS.
    
    Unlike ask_judge (text-only ruling), this returns structured actions
    that can be applied to the game state.
    
    Returns:
        Tuple of (display_messages, actions_executed)
    """
    # July 24 batch-6 audit (reviewer D1, CRITICAL): type-restricted graveyard
    # returns resolve deterministically instead of via the LLM. Bruna, the
    # Fading Light's "return target Angel or Human creature card from your
    # graveyard" came back from Tier 3 naming Balan (a Cat Knight) while three
    # legal Humans sat in the same graveyard (game_1529985418743910420,
    # CR 601.2c). Same shortcut shape as the Hidetsugu guard below: compute
    # the choice ourselves, bypass the hallucination class entirely. Sits
    # BEFORE the no-client return — it needs no LLM.
    _gy_ret = re.search(
        r'return (?:up to \w+ )?target ([\w\s\']*?)\s*creature card'
        r'(?:s)? from your graveyard (?:on)?to the battlefield',
        (effect_description or "").lower())
    if _gy_ret and controller:
        _ctrl_p = next((p for p in game.players if p.name == controller), None)
        if _ctrl_p is not None:
            _restrict = [t.strip() for t in
                         re.split(r'\s+or\s+|,\s*', _gy_ret.group(1))
                         if t.strip() and t.strip() not in ('a', 'an', 'another', 'target')]
            _best = None
            _best_cmc = -1
            for _c in _ctrl_p.graveyard:
                _tl = (_c.type_line or '').lower()
                if 'creature' not in _tl:
                    continue
                if _restrict and not any(t in _tl for t in _restrict):
                    continue
                _cmc = int(_c.cmc) if getattr(_c, 'cmc', None) else 0
                if _cmc > _best_cmc:
                    _best_cmc = _cmc
                    _best = _c
            _r_desc = ' or '.join(_restrict) if _restrict else 'creature'
            if _best is not None:
                _ret = {"action": "reanimate", "player": controller,
                        "card": _best.name, "own_graveyard": True,
                        "allow_types": _restrict or ["creature"],
                        "_source_card_name": source_card}
                _ret_actions = [_ret]
                # July 30 batch-9 audit: Whip of Erebos went through this
                # guard with 1 action — the "It gains haste. Exile it at the
                # beginning of the next end step." riders were DROPPED,
                # turning a temporary reanimation permanent
                # (game_1532236167368544388, Phyrexian Obliterator). Parse
                # the two rider shapes from the effect text; the delayed
                # exile rides schedule_delayed_trigger like Puppeteer
                # Clique ("your next end step" = phase_of-gated; "the next
                # end step" = ungated, per the July 23 Necropotence rule).
                # Whip's "if it would leave the battlefield, exile it
                # instead" replacement stays unmodeled.
                _desc_l = (effect_description or "").lower()
                if re.search(r'\bgains? haste\b', _desc_l):
                    _ret["haste"] = True
                _dx = re.search(r'exile (?:it|that (?:card|creature)) at the '
                                r'beginning of (the|your) next end step',
                                _desc_l)
                if _dx:
                    _sched = {"action": "schedule_delayed_trigger",
                              "trigger_at": "end_step", "turn_delay": 0,
                              "source": source_card or "Delayed exile",
                              "actions": [{"action": "move_card",
                                           "card": _best.name,
                                           "from_zone": "battlefield",
                                           "to_zone": "exile",
                                           "player": controller}]}
                    if _dx.group(1) == "your":
                        _sched["phase_of"] = controller
                    _ret_actions.append(_sched)
                print(f"[RESOLVE-GY-RETURN] {source_card}: returning {_best.name} "
                      f"(restriction: {_r_desc}) deterministically — Tier 3 bypassed"
                      + (" +haste" if _ret.get('haste') else "")
                      + (" +delayed exile at end step" if _dx else ""))
                return ([], _ret_actions)
            print(f"[RESOLVE-GY-RETURN] {source_card}: no legal {_r_desc} "
                  f"creature card in {controller}'s graveyard — declining")
            return ([f"📜 {source_card or 'Effect'}: no {_r_desc} creature "
                     f"card in graveyard to return"], [])

    # July 30 batch-9 reviewer audit: self-only pump activations ("{R}: This
    # creature gets +1/+0 until end of turn") resolved via Tier 3 spread to
    # EVERY creature the controller owned — the documented action vocabulary's
    # only pump was player-scoped, so the LLM had no way to say "just this
    # one" (Inferno Titan's pump buffed Shardless Agent too,
    # game_1532232990367682571 — recurrence of the class behind the July 21
    # display-only fix). Deterministic, like the Hidetsugu/GY-return guards:
    # compute it ourselves, scoped to the source, no LLM call.
    # July 31 batch-11: the AI activation path (mtg/engine.py ~4292) passes
    # "<player> activated <source>'s ability: <text>" — every ^-anchored
    # deterministic guard below (self-pump, fog, bounce-attackers) silently
    # never matched on that path. Spore Frog's fog was refused AFTER its
    # sacrifice cost was paid (batch 15325 ×2 — the exact batch-10 bug the
    # fog guard was built for; its pin tested the bare text, which the live
    # path never sends), and [RESOLVE-SELF-PUMP] has never fired live.
    # Strip the prefix ONCE for guard matching; the LLM path keeps the full
    # description (the prefix is useful judge context).
    _guard_desc = re.sub(
        r"^\s*[^:]{1,80}? activated [^:]{1,80}?'s ability:\s*", '',
        effect_description or '', count=1)

    if source_card and controller:
        _self_pump = re.match(
            r'^\s*(?:this (?:creature|permanent)|'
            + re.escape(source_card.lower())
            + r')\s+gets\s+([+-]\d+)/([+-]\d+) until end of turn\.?\s*$',
            _guard_desc.lower())
        if _self_pump:
            _pp, _tt = int(_self_pump.group(1)), int(_self_pump.group(2))
            print(f"[RESOLVE-SELF-PUMP] {source_card}: {_pp:+d}/{_tt:+d} "
                  f"scoped to itself — Tier 3 bypassed")
            return ([], [{"action": "pump_all_creatures", "player": controller,
                          "card": source_card, "power": _pp, "toughness": _tt}])

    # July 31 batch-10: two deterministic carve-outs that must run BEFORE the
    # combat-shape guard below, whose broad regex ("attack|combat damage")
    # otherwise refuses them. Both were observed silently no-oping in batch
    # 15324: Spore Frog's activation was refused AFTER its sacrifice cost was
    # paid (creature gone, fog never applied — twice), and Aetherize's
    # "Return all attacking creatures" resolve was refused outright. Neither
    # is a synthetic kill description — they're the cards' real text, and
    # both compute deterministically from game state.
    _det_desc = _guard_desc.lower().strip()
    if re.match(r'^prevent all combat damage that would be dealt this turn\.?$',
                _det_desc):
        print(f"[RESOLVE-FOG] {source_card or 'effect'}: prevent all combat "
              f"damage this turn — Tier 3 bypassed")
        return ([], [{"action": "prevent_combat_damage", "scope": "all"}])
    if re.match(r"^return all attacking creatures to their owner(?:'s|s'|s)? hands?\.?$",
                _det_desc):
        _bounce_actions = []
        for _p in game.players:
            for _c in list(_p.battlefield):
                if getattr(_c, 'attacking', False):
                    _owner_idx = getattr(_c, 'owner_index', -1)
                    _owner = (game.players[_owner_idx].name
                              if 0 <= _owner_idx < len(game.players) else _p.name)
                    _bounce_actions.append({
                        "action": "move_card", "card": _c.name,
                        "from_zone": "battlefield", "to_zone": "hand",
                        "player": _owner})
        print(f"[RESOLVE-BOUNCE-ATTACKERS] {source_card or 'effect'}: "
              f"returning {len(_bounce_actions)} attacking creature(s) to hand "
              f"— Tier 3 bypassed")
        return ([], _bounce_actions)

    if not rules.client:
        return [f"⚠️ Unresolved effect: {effect_description}"], []

    # May 20 audit fix: reject "attack for lethal" / "combat damage" /
    # synthetic-kill descriptions that the AI sometimes attaches to a resolve
    # action after a cast. game_1506604518098342018:2287 had a plan
    # ["resolve: Craterhoof enters, pump team for +10/+10 trample. Attack for
    # lethal."] that fired in MAIN1 as a sorcery-speed -999 life event,
    # violating CR 510.1 (combat damage only in the combat damage step).
    # Same pattern killed Rick in game_1506604605327282176:741-749.
    if is_combat_shaped_resolve(effect_description):
        print(f"[RESOLVE-REFUSED] Combat-shaped resolve rejected: '{effect_description[:80]}' — "
              f"combat damage must happen in the combat damage step (CR 510.1)")
        # June 10 audit: emit the player-facing hint once per source per game.
        # The AI re-proposed combat-shaped resolves for the same card (Etali)
        # 4× across turns and the identical ⚠️ line posted every time — the
        # per-turn dedup can't catch cross-turn repeats.
        _hint_key = f"combat-resolve:{source_card}"
        _hints = getattr(game, '_judge_hints_emitted', None)
        if _hints is None:
            game._judge_hints_emitted = set()
            _hints = game._judge_hints_emitted
        if _hint_key in _hints:
            return ([], [])
        _hints.add(_hint_key)
        return ([
            f"⚠️ **{source_card}**: combat actions can't resolve at sorcery speed. "
            f"Use `!attack` to declare attackers in the declare-attackers step."
        ], [])

    # June 10 deep-dive (CRITICAL): Tier 3 fabricated an impossible mana
    # payment — Leyline Tyrant's "you may pay any amount of {R}" resolved as
    # a 5-damage payout while the controller had ZERO untapped sources and an
    # empty pool. If the effect hinges on an optional mana payment and no
    # mana is available at all, resolve it as a decline instead of letting
    # the model invent the payment.
    # Aug 7 batch audit (A-1a): multikicker's REMINDER text ("You may pay an
    # additional {1} any number of times AS YOU CAST this spell") matched this
    # guard at RESOLUTION time — by then the kick count is fixed (CR 601.2b)
    # and there is nothing left to pay, so an unkicked Comet Storm with zero
    # untapped sources was refused wholesale: full X paid, zero damage dealt
    # (game_1535051230815064206). Remove parentheticals that describe a
    # CAST-time payment before running the guard; extort's reminder ("you may
    # pay {W/B}", no cast-time phrase) is a genuine resolution-time payment
    # and stays guarded, as does all body-text "you may pay" (Leyline Tyrant,
    # the guard's origin).
    def _strip_cast_time_pay_parens(text):
        # Order-agnostic within the parenthetical: multikicker puts "you may
        # pay" BEFORE "as you cast this spell"; suspend puts "rather than
        # cast this card" first.
        def _repl(m):
            body = m.group(1)
            if ('you may pay' in body
                    and ('as you cast this spell' in body
                         or 'rather than cast this card' in body)):
                return ''
            return m.group(0)
        return re.sub(r'\(([^)]*)\)', _repl, text)
    _guard_pay_text = _strip_cast_time_pay_parens(_guard_desc.lower())
    if re.search(r'\byou may pay\b', _guard_pay_text) and controller:
        _ctrl_p = next((p for p in game.players if p.name == controller), None)
        if _ctrl_p is not None:
            _pool_total = sum((getattr(_ctrl_p, 'mana_pool', {}) or {}).values())
            _untapped_srcs = len(_ctrl_p.untapped_mana_sources() or [])
            if _pool_total == 0 and _untapped_srcs == 0:
                print(f"[RESOLVE-REFUSED] Optional-payment effect with zero available mana "
                      f"for {controller}: '{effect_description[:80]}'")
                return ([f"📜 {source_card or 'Effect'}: optional cost declined (no mana available)"], [])

    # May 20 audit fix: reject equip/attach descriptions that reference a
    # source not on the battlefield. game_1506623352381509733:718 had a plan
    # `resolve: Equip Sword of Body and Mind to White Spirit` where the Sword
    # was still in hand. The judge happily resolved it as a +2/+2 team pump.
    # When the description starts with "equip" / "attach" + a card name,
    # verify that card name is actually on the battlefield under any player's
    # control. If not, refuse and emit a hint.
    _equip_match = re.match(
        r'^\s*(?:equip|attach)\s+([A-Z][\w\',\-\s]{2,40}?)\s+(?:to|onto)\s+',
        effect_description or "",
    )
    if _equip_match:
        _equip_name = _equip_match.group(1).strip()
        _equip_lower = _equip_name.lower()
        _found_on_bf = False
        try:
            for _p in game.players:
                for _c in _p.battlefield:
                    if _c.name.lower() == _equip_lower:
                        _found_on_bf = True
                        break
                if _found_on_bf:
                    break
        except Exception:
            pass
        if not _found_on_bf:
            print(f"[RESOLVE-REFUSED] Equip-shaped resolve rejected: "
                  f"'{_equip_name}' is not on the battlefield (in hand / graveyard / library)")
            return ([
                f"⚠️ **{source_card}**: cannot equip/attach **{_equip_name}** — "
                f"it's not on the battlefield. Cast it first."
            ], [])


    # May 25 audit (F28): hardcoded shortcuts for specific cards where Tier 3
    # has been observed to hallucinate the rules (rounding direction, target
    # restriction, etc.). The LLM has no canonical Hidetsugu template and got
    # contaminated by Gisela's "rounded up" prompting in the same game —
    # rounded Hidetsugu's `floor(life/2)` UP, so Rick at 5 life took 3
    # damage (should be 2). Bypass the LLM with a deterministic JSON action
    # for each player. The damage actions still flow through the replacement
    # engine, so Gisela's halving + Furnace's doubling apply correctly when
    # the card is in play. Keyed on source_card to avoid matching false-
    # positive descriptions that just MENTION Hidetsugu.
    src_lower = (source_card or "").lower()
    if "heartless hidetsugu" in src_lower:
        actions = []
        msgs = []
        for p in game.players:
            half_dmg = p.life // 2  # floor division per oracle "rounded down"
            if half_dmg > 0:
                actions.append({
                    "action": "deal_damage",
                    "amount": half_dmg,
                    "target_player": p.name,
                    "source": "Heartless Hidetsugu",
                })
        # Execute the actions immediately so the messages reflect the real
        # post-replacement damage (Gisela halving / Furnace doubling).
        executed = []
        _saved_source_ctx = getattr(game, '_current_resolution_source', None)
        game._current_resolution_source = ("Heartless Hidetsugu", controller or "")
        try:
            for action in actions:
                try:
                    msg = rules._execute_action_on_state(game, action)
                    if msg:
                        msgs.append(msg)
                        executed.append(action)
                except Exception as e:
                    print(f"[RESOLVE-HIDETSUGU] Failed action: {e}")
        finally:
            game._current_resolution_source = _saved_source_ctx
        msgs.insert(0, f"⚡ **Heartless Hidetsugu** triggers")
        print(f"[RESOLVE-HIDETSUGU] Bypassed Tier 3 — dealt floor(life/2) to each player")
        return msgs, executed

    # Library-look short-circuit (CR 701.18 Scry, "look at the top N cards",
    # tutoring effects, reveal-top triggers). The engine doesn't model
    # library order, so these effects can't change visible game state and
    # always return no-op from Tier 3. Skip the API call entirely — costs
    # ~$0.005 per call × 17/batch = ~$0.085 saved per Apr-28-scale batch.
    effect_lower = (effect_description or "").lower()
    library_look_phrases = (
        "scry ", "look at the top", "look at the next", "reveal the top",
        "search your library and reveal", "rashmi",
    )
    is_library_look = (
        any(phrase in effect_lower for phrase in library_look_phrases)
        # A top-library instruction is not necessarily a pure reorder. Keep
        # compound effects in the normal resolver whenever they also move
        # cards or otherwise mutate state (Risen Reef is the live exemplar).
        and not has_residual_clause_beyond_library_look(
            effect_description or "")
        and "draw" not in effect_lower
        and "destroy" not in effect_lower
        and "exile" not in effect_lower
        and "deal" not in effect_lower
        # July 27 fanout: the exclusion list checked draw/destroy/exile/deal but
        # not the disruption verbs, so Thought Erasure — whose SURVEIL REMINDER
        # text contains "look at the top card of your library" — tripped this
        # gate and its entire hand-disruption half (reveal, choose a nonland,
        # discard it) was skipped as an unmodellable library look.
        and "discard" not in effect_lower
        and "reveals their hand" not in effect_lower
        and "you choose" not in effect_lower
        and "sacrifice" not in effect_lower
    )
    if is_library_look:
        # May 17 audit: minimal library-order modeling — actually execute
        # fateseal (look at top card of target opponent's library, bottom
        # if it's bad for them) when the effect targets an opponent.
        # Strategically correct heuristic: bottom a high-CMC non-land
        # (denies them their best draw), keep lands in flooded boards, etc.
        # Doesn't handle Brainstorm-style put-back-from-hand because that
        # has its own dedicated action (`put_back_from_hand`).
        msg_lines = []
        fateseal_applied = False
        if "top card of target player" in effect_lower or "top card of target opponent" in effect_lower:
            # Pick the opponent's library (the controller is the activator)
            try:
                opp = None
                ctrl_name = (controller or '').lower()
                for p in game.players:
                    if (p.name or '').lower() != ctrl_name:
                        opp = p
                        break
                if opp and opp.library:
                    top_card = opp.library[0]
                    is_land = top_card.is_land() if hasattr(top_card, 'is_land') else False
                    cmc = getattr(top_card, 'cmc', 0) or 0
                    opp_land_count = sum(1 for c in opp.battlefield if c.is_land())
                    # Bottom if: high-CMC non-land (deny draw), OR land when
                    # opponent already flooded (waste their next draw on
                    # something more valuable to them).
                    should_bottom = (
                        (not is_land and cmc >= 3)
                        or (is_land and opp_land_count >= 5)
                    )
                    if should_bottom:
                        opp.library.pop(0)
                        opp.library.append(top_card)
                        msg_lines.append(
                            f"🔮 **{source_card}** fatesealed {opp.name} "
                            f"— bottomed top card"
                        )
                    else:
                        msg_lines.append(
                            f"🔮 **{source_card}** fatesealed {opp.name} "
                            f"— left top card in place"
                        )
                    fateseal_applied = True
            except Exception as _fs_err:
                print(f"[FATESEAL] Error: {_fs_err}")
        if not fateseal_applied:
            print(f"[RESOLVE-SHORTCUT] Library-look effect '{source_card}' — no-op (library order not modeled)")
            # Honest fallback for effects we still can't model (Brainstorm
            # put-back is handled separately, scry/surveil have their own
            # actions, so this catches the residual long tail like
            # Telling Time, Mishra's Bauble, etc.)
            honest_msg = (
                f"🔮 **{source_card}** resolves — library reordering is not modeled "
                f"by the engine, so no top-of-library effect takes place."
            )
            return [honest_msg], []
        return msg_lines, []

    # Per-turn dedupe: if the same (source_card, effect) was already
    # resolved this turn, skip the API call and suppress the Discord
    # re-post. Prevents Temple/Viscera Seer scry ETBs from printing 3×
    # when the trigger fires from multiple tiers and prevents burning
    # tokens on back-to-back identical requests.
    # Aug 10 card-targeted wave (CRITICAL): the key carried no GAME
    # identifier while `rules` is a singleton shared by every concurrent game
    # in the cog (one GameEngine -> one RulesEngine -> games: Dict[int,
    # GameState]), so a turn-N resolution in one game silently cancelled the
    # same card's resolution in EVERY other game on its turn N. Measured over
    # the 160-game batch: all five [RESOLVE-DEDUP] fires had no genuine
    # earlier resolve in their own log — cross-game suppression was the
    # guard's ONLY observed effect. It cost a fully-paid Killing Wave
    # (game_1536028980996472842, X=2, three sources tapped, no effect).
    # Same class as the Apr-6 parallel-game strategist-memo contamination.
    dedupe_key = (
        int(getattr(game, 'thread_id', 0) or 0),
        (source_card or "").strip().lower(),
        (effect_description or "").strip().lower(),
        int(getattr(game, 'turn_number', 0) or 0),
    )
    if dedupe_key[0] and dedupe_key[1] and rules._resolve_dedupe.get(dedupe_key):
        print(f"[RESOLVE-DEDUP] Skipped duplicate '{source_card}' / '{effect_description[:60]}...' this turn")
        return [], []
    # Flag immediately so re-entrant calls during our own API round-trip
    # can't race past the guard.
    if dedupe_key[0] and dedupe_key[1]:
        rules._resolve_dedupe[dedupe_key] = True

    # Build game state context (cached for batched trigger resolution)
    current_fp = game._state_fingerprint()
    if current_fp == rules._cached_judge_fingerprint and rules._cached_judge_desc:
        state_desc = rules._cached_judge_desc
        print("[RESOLVE-CACHE] HIT — reusing judge description")
    else:
        state_desc = rules._describe_game_for_judge(game)
        rules._cached_judge_desc = state_desc
        rules._cached_judge_fingerprint = current_fp
    recent_log = "\n".join(rules.game_log[-10:]) if rules.game_log else "No recent events"
    
    # Build a multiplayer-safe player mapping. Eliminated seats remain in
    # GameState for stable ownership/indexing, but they are not opponents and
    # must never be offered as action targets to Tier 3.
    controller_name = controller or (game.active_player.name if game.players else "Unknown")
    controller_player = next(
        (p for p in game.players
         if p.name == controller_name and not getattr(p, 'eliminated', False)),
        None,
    )
    if controller_player is not None:
        opponent_names = [p.name for p in game.opponents_of(controller_player)]
    else:
        opponent_names = [
            p.name for p in game.players
            if p.name != controller_name and not getattr(p, 'eliminated', False)
        ]
    living_player_names = [
        p.name for p in game.players if not getattr(p, 'eliminated', False)
    ]
    opponents_label = ", ".join(opponent_names) or "None"
    living_names_label = '\", \"'.join(living_player_names)
    
    prompt = f"""You are an MTG rules engine. Resolve this effect by returning JSON actions that modify the game state.

GAME STATE:
{state_desc}

RECENT EVENTS:
{recent_log}

EFFECT TO RESOLVE:
Source: {source_card}
Controller: {controller_name}
Living opponent(s): {opponents_label}
Effect text: {effect_description}
{f"Additional context: {context}" if context else ""}

Return a JSON object with:
- "explanation": Brief 1-2 sentence explanation of what happens. Plain English, no rules pedantry.
   - DO write: "Mulldrifter's controller draws two cards."
   - DON'T write: "Mulldrifter, controlled by its controller, has its enters-the-battlefield ability resolve, allowing it to be a new object that can trigger again, drawing two cards per CR 603.x."
   - No CR citations, no "(per the rules)", no "(dies due to 1 damage on it)".
- "actions": Array of game state mutations to execute

AVAILABLE ACTIONS (use ONLY these exact types):
- {{"action": "deal_damage", "amount": N, "target_player": "name"}} — damage to a player
- {{"action": "deal_damage", "amount": N, "target_card": "Card Name", "target_controller": "name"}} — damage to a creature/planeswalker
- {{"action": "gain_life", "player": "name", "amount": N}}
- {{"action": "lose_life", "player": "name", "amount": N}}
- {{"action": "draw_cards", "player": "name", "amount": N}}
- {{"action": "discard", "player": "name", "card": "Card Name"}} — specific card, or "random" for random
- {{"action": "move_card", "card": "Card Name", "from_zone": "zone", "to_zone": "zone", "player": "name"}}
zones: hand, battlefield, graveyard, exile, library
- {{"action": "add_counters", "card": "Card Name", "counter_type": "+1/+1", "amount": N}} — PERMANENT counters only
- {{"action": "remove_counters", "card": "Card Name", "counter_type": "+1/+1", "amount": N}}
- {{"action": "remove_keywords", "card": "Card Name", "keywords": ["Hexproof", "Indestructible"]}} — permanent LOSES keywords until end of turn; "player": "name" without "card" = all permanents that player's OPPONENTS control lose them
- {{"action": "steal_permanent", "player": "thief", "from_player": "current controller", "card": "Card Name"}} — PERMANENT control change (Agent of Treachery). For "gain control ... UNTIL END OF TURN" (Act of Treason family) you MUST add "until_end_of_turn": true — control then reverts automatically at end of turn; add "untap": true and "gain_haste": true when the printed effect untaps it / grants haste
- {{"action": "pump_all_creatures", "player": "name", "power": N, "toughness": N}} — TEMPORARY +N/+N until end of turn. Add "card": "Name" to pump ONLY that one creature — "this creature gets +N/+N" MUST be scoped with "card", never applied to the whole team
- {{"action": "scry", "player": "name", "amount": N}} — scry N (look at top N, reorder/bottom)
- {{"action": "create_token", "player": "name", "name": "Token Name", "power": N, "toughness": N, "types": "Creature — Type", "count": N, "keywords": ["defender", "flying"]}} — ALWAYS include the token's printed keywords; a "0/4 Wall with defender" created without "keywords" can illegally attack
- {{"action": "prevent_next_damage", "card": "Name", "amount": N}} — "prevent the next N damage that would be dealt to X this turn" (Eiganjo Castle). A consumable shield, NOT a full prevention: it absorbs N and lets the rest through.
- {{"action": "tap", "card": "Card Name"}} — add "skip_next_untap": true for "doesn't untap during its controller's next untap step" riders (Frost Lynx family); omitting it silently drops the printed rider
- {{"action": "untap", "card": "Card Name"}}
- {{"action": "add_mana", "player": "name", "color": "R", "amount": N}} — colors: W/U/B/R/G/C
- {{"action": "destroy", "card": "Card Name"}} — destroy (goes to graveyard, respects indestructible)
- {{"action": "grant_keywords", "player": "name", "target_card": "Card Name", "keywords": ["keyword1", "keyword2"]}} — grant keywords (vigilance, reach, haste, trample, etc.) to a specific creature until end of turn
- {{"action": "grant_keywords", "player": "name", "target": "all_own_creatures", "keywords": ["keyword"]}} — grant keywords to all of a player's creatures until end of turn
- {{"action": "no_action", "reason": "why"}} — effect doesn't apply or is optional and declined

IMPORTANT: "add_counters" is for PERMANENT +1/+1 counters. For "gets +N/+N until end of turn", use "pump_all_creatures". For "target creature gains [keyword] until end of turn", use "grant_keywords" with "target_card".

CRITICAL RULES:
- You MUST return valid JSON with "explanation" and "actions" keys
- Use exact card names as shown in the game state
- Use exact living player names as shown: "{living_names_label}"
- "Each opponent" means every player in Living opponent(s); never target an eliminated player
- For "any target" effects, pick the strategically best target for the controller
- If the effect is optional ("may"), assume the controller chooses to do it unless clearly disadvantageous
- If a trigger doesn't apply (e.g., "whenever another creature enters" but no creature entered), return no_action
- Do NOT explain rules or cite CR numbers. Just execute the effect.

Respond with ONLY the JSON object, no markdown, no backticks, no preamble."""

    try:
        _resolve_kwargs = dict(
            model=rules.model,
            max_tokens=2000,  # Was 1200 (originally 600) — complex effects like Worldgorger Dragon, Cyclonic Rift need 8+ actions
            messages=[{"role": "user", "content": prompt}],
        )
        if hasattr(rules.client.messages, '_log_tag'):
            _resolve_kwargs['purpose'] = 'resolve_effect'
        response = await asyncio.to_thread(
            rules.client.messages.create,
            **_resolve_kwargs,
        )
        if rules.usage_callback and hasattr(response, 'usage'):
            rules.usage_callback(response.usage, rules.model)

        raw_text = response_text(response).strip()
        # Strip <think> scratchpad if present (DeepSeek reasoning)
        text = rules._strip_think_tags(raw_text, context="resolve") if '<think>' in raw_text else raw_text
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)

        # Bug #11: Detect truncated JSON and retry with larger token limit
        if text and not (text.rstrip().endswith('}') or text.rstrip().endswith(']')):
            print(f"[RESOLVE] Truncated JSON detected, retrying with 4000 tokens")
            _resolve_kwargs2 = dict(
                model=rules.model,
                max_tokens=4000,
                messages=[{"role": "user", "content": prompt}],
            )
            if hasattr(rules.client.messages, '_log_tag'):
                _resolve_kwargs2['purpose'] = 'resolve_effect_retry'
            response2 = await asyncio.to_thread(
                rules.client.messages.create,
                **_resolve_kwargs2,
            )
            if rules.usage_callback and hasattr(response2, 'usage'):
                rules.usage_callback(response2.usage, rules.model)
            text = response_text(response2).strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)

        # Parse tolerantly: json.loads first, then raw_decode to salvage
        # valid JSON with trailing garbage, then give up (caller handles).
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            try:
                result, _end = json.JSONDecoder().raw_decode(text)
                print(f"[RESOLVE] raw_decode salvaged JSON (ignored trailing {len(text) - _end} chars)")
            except (json.JSONDecodeError, ValueError):
                raise  # Re-raise; outer handler prints [RESOLVE] JSON parse error
        explanation = result.get("explanation", "Effect resolved")
        # Strip AI strategy reasoning from player-visible explanations
        # (The prompt tells Claude to pick "strategically best" targets, but
        # that reasoning shouldn't leak to Discord)
        strategy_phrases = [
            r'\bstrategically?\b[^.]*',
            r'\bmost beneficial\b[^.]*',
            r'\bmost disruptive\b[^.]*',
            r'\bbest (?:choice|option|target)\b[^.]*',
            r'\bsince the (?:game state|controller|board)\b[^.]*',
        ]
        # Apr 29 audit: trim verbose rules-narration that DeepSeek tacks onto
        # ETB explanations. These are correct rules text but redundant in a
        # Discord trigger message ("controller, Rick Deckard" when the line
        # already names Rick, "(dies due to 1 damage on it)" when SBA-deaths
        # are obvious, "allowing it to be a new object that can trigger
        # again" — pure rules pedantry).
        narration_phrases = [
            r',?\s*(?:controlled|owned)\s+by\s+(?:its|their)\s+(?:controller|owner)[,.][^.]*?(?=\.|,|$)',
            r',?\s*allowing it to (?:be|become) a new object[^.]*?(?=\.|$)',
            r',?\s*\(?dies due to \d+ damage[^)]*\)?',
            r',?\s*as the new object can trigger[^.]*?(?=\.|$)',
            r',?\s*per (?:CR|comprehensive rule)[^.]*?(?=\.|$)',
        ]
        for pattern in strategy_phrases + narration_phrases:
            explanation = re.sub(pattern, '', explanation, flags=re.IGNORECASE).strip()
        # May 16 audit: the strategy_phrases regex leaves orphan articles when
        # it strips "The strategically best target is X" → "The ." Same for
        # "A strategically best...", "An strategically best...". Detect and
        # remove these dangling article-only fragments so we don't ship
        # "...returns a creature card from his graveyard. The" to Discord.
        explanation = re.sub(
            r'(?:^|(?<=[.!?]))\s*\b(?:The|A|An)\s*[.!?]?(?=\s*(?:[A-Z]|$))',
            ' ',
            explanation,
        )
        # May 17 audit: also catch "The . However" and "The , However" forms
        # where the dangling article + period appears mid-sentence (the
        # sentence-boundary regex above missed this when the next clause
        # started with "However" / "But" / continuation markers). Match a
        # 1-3 character article followed by a period (with surrounding
        # whitespace) when the next word is one of the common continuations.
        explanation = re.sub(
            r'\b(?:The|A|An)\s+\.\s+(?=(?:However|But|And|Then|So|Therefore|Thus|Yet)\b)',
            '',
            explanation,
        )
        explanation = re.sub(r'\s+\.', '.', explanation)
        # Strip unquoted action-dict prose DeepSeek sometimes dumps into
        # the explanation field, e.g. "action: move_card, card: X, from_zone: ..."
        _action_prose = re.compile(
            r'action\s*:\s*\w+(?:\s*,\s*(?:card|target_card|target_player|target_controller|'
            r'player|from_zone|to_zone|amount|counter_type|color|name|power|toughness|'
            r'types|count|position|keywords|reason|min_power|type)\s*:\s*[^,\n]+)+',
            re.IGNORECASE,
        )
        explanation = _action_prose.sub('', explanation)
        explanation = re.sub(r'\s{2,}', ' ', explanation).strip(' .,')
        if not explanation:
            explanation = "Effect resolved"
        actions = result.get("actions", [])

        # Q-J slice 1: persist the plan BEFORE any of it runs. Recovery
        # must never re-query Tier 3 — a second call can legitimately
        # return a DIFFERENT plan, and applying half of one and half of
        # another is not a resolution of anything.
        _coord = None
        _job_id = getattr(game, '_active_resolution_job_id', None)
        if _job_id and actions:
            try:
                from mtg.resolution import ResolutionCoordinator
                _coord = ResolutionCoordinator.for_game(
                    getattr(rules, 'engine_ref', None), game)
                _coord.record_plan(_job_id, actions, tier="tier3")
            except (AttributeError, TypeError, ValueError, KeyError,
                    OSError) as _e:
                # Narrow on purpose: the ratchet's point is that a bare
                # `except Exception` here would turn a real resolution bug
                # into a silently unpersisted plan. OSError is in the tuple
                # because record_plan's persist writes the save file.
                _coord = None
                print(f"[RESOLVE-PLAN] plan not persisted: {_e}")

        if not actions:
            return [f"📜 {explanation}"], []
        
        # Execute each action
        messages = []
        executed = []

        # May 25 audit (F16): set _current_resolution_source for the duration of
        # the action-loop. The damage / counter / life action handlers in
        # mtg/actions.py fall back to game._current_resolution_source when an
        # action dict omits `source`. Tier 3 judge JSON commonly omits the
        # field, producing `[SPELL-DAMAGE] (unknown source)` lines — 18 events
        # in the May 25 batch, mostly Heartless Hidetsugu activations. Save
        # and restore so we don't clobber an outer caller's context (e.g.
        # nested triggers fired by these actions).
        _saved_source_ctx = getattr(game, '_current_resolution_source', None)
        if source_card:
            game._current_resolution_source = (source_card, controller or "")
        try:
            for _action_index, action in enumerate(actions):
                # Q-J slice 1: claim the action before executing it. A
                # key already in the ledger means a previous process
                # owned this action; at-most-once is the deliberate
                # direction (see mtg/resolution.py module docstring).
                if _coord is not None:
                    _should_apply, _ = _coord.claim_action(
                        _job_id, _action_index, action)
                    if not _should_apply:
                        continue
                action_type = action.get("action", "")

                if action_type == "no_action":
                    reason = action.get("reason", "no effect")
                    print(f"[RESOLVE] No action: {reason}")
                    continue

                # Aug 7 batch audit (A-4): the Tier-3 judge placed a -1/-1
                # counter on SKULLCLAMP (an Equipment) for Yawgmoth's "Put a
                # -1/-1 counter on up to one target creature"
                # (game_1535060075683651725) — nothing validated the emitted
                # target against the ability's printed restriction. The guard
                # lives HERE and not in the add_counters handler because that
                # handler legitimately serves non-creatures (charge counters
                # on artifacts, loyalty, lore, +1/+1 on a Vehicle).
                # Aug 10 deferred (D): DAMAGE AUTHORITY. Tier 3 resolved Goblin
                # Bombardment's "deals 1 damage to any target" by emitting a
                # move_card battlefield->graveyard for an UNDAMAGED 5/4 — it
                # decided lethality itself instead of dealing damage and
                # letting CR 704.5g decide. Anchored on a FULL match of the
                # effect text, deliberately, not a substring: an ability whose
                # ENTIRE printed effect is "deal N damage" cannot legitimately
                # require a direct kill, so there is no counterexample to
                # misfire on. A COMPOUND ability (a damage clause plus a real
                # destroy clause) does not full-match and passes ungoverned —
                # that is under-refusal, which is the safe direction here and
                # matches this file's refuse-only-when-certain convention.
                if action_type in ("destroy", "exile", "sacrifice_permanent") \
                        or (action_type == "move_card"
                            and str(action.get("to_zone", "")).lower()
                            in ("graveyard", "exile")):
                    if _DAMAGE_ONLY_EFFECT.match(_guard_desc.strip()):
                        print(f"[RESOLVE-REFUSED] {action_type} from an effect "
                              f"whose only printed instruction is damage — "
                              f"lethality is decided by state-based actions "
                              f"(CR 704.5g), not by the resolver")
                        continue

                if (action_type == "add_counters"
                        and 'target creature' in _guard_desc.lower()
                        and action.get("card")):
                    _tgt_nm = str(action.get("card")).lower()
                    _tgt_obj = None
                    for _gp in game.players:
                        for _gc in _gp.battlefield:
                            if _gc.name.lower() == _tgt_nm:
                                _tgt_obj = _gc
                                break
                        if _tgt_obj:
                            break
                    if _tgt_obj is not None and not _tgt_obj.is_creature(game):
                        print(f"[RESOLVE-REFUSED] add_counters on "
                              f"{_tgt_obj.name} — the ability targets a "
                              f"CREATURE (CR 601.2c/608.2b); dropping the "
                              f"illegal action")
                        continue

                try:
                    msg = rules._execute_action_on_state(game, action)
                    if msg:
                        messages.append(msg)
                        executed.append(action)
                except Exception as e:
                    print(f"[RESOLVE] Failed to execute action {action}: {e}")
                    # User-facing message intentionally omits the raw exception
                    # (KeyError, AttributeError details leak internal dict keys
                    # and object layout that aren't meaningful to players).
                    messages.append(f"⚠️ Part of the effect couldn't resolve.")
        finally:
            game._current_resolution_source = _saved_source_ctx
        
        if messages:
            # Prepend the explanation (use source card name or extract from explanation).
            # May 23 audit (CRITICAL #5): when source_card is unknown, "Effect"
            # as a fallback header is uninformative and reads as if a card
            # named "Effect" is triggering. Try to infer from the effect
            # description first (looking for a Title-Cased card-shaped name);
            # fall back to a generic "Triggered ability" header otherwise.
            display_name = source_card
            if not display_name:
                # Look for a Title-Cased multi-word phrase that's likely a card name.
                # Avoid quoting raw card names from `for a X` / `to the Y` patterns.
                title_match = re.search(
                    r"\b([A-Z][a-zA-Z]+(?:[\s,\-'][A-Z][a-zA-Z]+){1,4})\b",
                    effect_description or "",
                )
                if title_match:
                    candidate = title_match.group(1).strip()
                    # Skip player names + common scaffolding
                    skip_words = {"Rick Deckard", "Claude", "Each Player",
                                  "Target Player", "Active Player"}
                    if candidate not in skip_words:
                        display_name = candidate
            if not display_name:
                display_name = "Triggered ability"
            # Apr 30 audit: skip the message entirely when the explanation says
            # the trigger doesn't actually fire ("Smothering Tithe does not trigger
            # here"). Otherwise we leak the model's chain-of-thought reasoning into
            # a player-facing line that announces a non-event.
            if explanation:
                exp_lower = explanation.lower()
                if any(phrase in exp_lower for phrase in (
                    "does not trigger", "doesn't trigger", "no trigger fires",
                    "no effect fires", "no effect triggers", "effect does not apply",
                    "does not apply here", "this does not trigger",
                )):
                    print(f"[RESOLVE] Suppressing non-trigger explanation for {display_name}: {explanation[:120]}")
                    return messages, executed
            # May 17 audit: AI judge sometimes attributes an unrelated card's
            # effect to the source (e.g. Phyrexian Arena getting credit for
            # Reclamation Sage's ETB). When the explanation names ANOTHER
            # battlefield card that isn't the source, suppress the explanation
            # — we can't tell which one is correct so it's safer to emit
            # "<source> triggers" than to misattribute.
            if explanation and source_card:
                src_lower = (source_card or "").lower()
                explanation_lower = explanation.lower()
                try:
                    bf_names = []
                    for _p in game.players:
                        for _c in _p.battlefield:
                            nm = (_c.name or "").lower()
                            if nm and nm != src_lower:
                                bf_names.append(nm)
                    # Look for another battlefield card mentioned before the
                    # source in the explanation. The most-frequent leak is
                    # "Phyrexian Arena destroys ... Reclamation Sage" where the
                    # OTHER card's name precedes the source.
                    src_idx = explanation_lower.find(src_lower)
                    for other in bf_names:
                        if not other or len(other) < 5:
                            continue
                        other_idx = explanation_lower.find(other)
                        if other_idx >= 0 and (src_idx < 0 or other_idx < src_idx):
                            print(f"[RESOLVE] Suppressing misattributed explanation for "
                                  f"{display_name}: mentions '{other}' before/instead of source")
                            messages.insert(0, f"⚡ **{display_name}** triggers")
                            return messages, executed
                except Exception:
                    pass
            # Strip AI strategic reasoning from player-visible messages.
            # Claude's explanation sometimes includes deliberation like
            # "The best strategic target is X because..." which is internal
            # reasoning that should not appear in Discord output.
            safe_explanation = explanation
            # May 16 audit: 53% of games had `⚡ <source> — <prose>` lines
            # containing LLM hedging like "(likely Cloudblazer)" or "since it
            # has summoning sickness and can be flickered for value". Strip
            # parentheticals that contain speculation/reasoning before the
            # marker-based truncation runs.
            safe_explanation = re.sub(
                r'\s*\([^)]*\b(?:likely|probably|presumably|possibly|maybe|perhaps|'
                r'because|since\s+it|since\s+the|since\s+they|for\s+value|'
                r'best\s+target|best\s+choice|chosen\s+by|to\s+trigger|'
                r'but\s+note\s+that|note\s+that|going\s+to\s+graveyard)\b[^)]*\)',
                '',
                safe_explanation,
                flags=re.IGNORECASE,
            )
            # Strip generic "via X's +Y ability" / "via X's [+N] ability" prose
            # — these phrasings describe HOW an effect happened rather than
            # the player-visible outcome.
            safe_explanation = re.sub(
                r'\s+via\s+[A-Z][\w\',\s\-]*?(?:\'s)?\s+(?:\[?[+\-]?\d+\]?\s+)?ability\b',
                '',
                safe_explanation,
            )
            # Collapse "is destroyed and goes to the graveyard" → just "is
            # destroyed" — Discord already emits a separate 💀 line for the
            # graveyard transition, so the wordy form duplicates the outcome.
            safe_explanation = re.sub(
                r'\bis\s+destroyed\s+and\s+goes\s+to\s+the\s+graveyard\b',
                'is destroyed',
                safe_explanation,
                flags=re.IGNORECASE,
            )
            # Strip parenthetical re-reasoning patterns: "(but ...)" / "(...)
            # where the parens contain a clarifying explanation rather than
            # a real game-state note.
            safe_explanation = re.sub(
                r'\s*\(but\b[^)]*\)',
                '',
                safe_explanation,
                flags=re.IGNORECASE,
            )
            reasoning_markers = [
                "the best strategic target",
                "the best target",
                "strategically",
                "the best choice",
                "i chose",
                "i selected",
                # Apr 30 audit additions: chain-of-thought hedging words that
                # leak into trigger announcements (Smothering Tithe case).
                "but since",
                "however,",
                "the effect is triggered by",
                "since the effect",
                "but the effect",
                # May 16 audit: speculation phrases from V4-Pro reasoning leak.
                # "likely X" / "presumably Y" / "for value" at the SENTENCE
                # level (parenthetical version was stripped above).
                ", likely ", " likely a ", " likely the ",
                ", presumably",
                "for value",
                "since it has",
                "since they have",
                "can be flickered",
                "can be blinked",
                # May 24 audit additions: first-person engine voice + action-
                # narration patterns from "Triggered ability —" prose leaks
                # (68 instances across 40 games). These describe HOW or WHY
                # the action happened in prose that Discord's per-action
                # messages already render more cleanly.
                "as instructed by",
                "sending it to the graveyard",
                "putting the top",
                "wait, that's a land",
                "wait, lands are excluded",
                "since we can't see",
                "we simulate this",
                "the source is not specified",
                "but actually there are no",
                "but actually there is no",
                "the effect is drawing",
                "the effect is destroying",
                "the effect is exiling",
                "the effect is milling",
                "the effect is",
                "the only creature card",
            ]
            for marker in reasoning_markers:
                idx = safe_explanation.lower().find(marker)
                if idx > 0:
                    # Truncate at the SENTENCE containing the marker, not at
                    # the marker itself. Otherwise "Claude puts a counter ...
                    # trigger. The strategically best creature..." would chop
                    # at "strategically" and leave a dangling "The". Walk
                    # back to the previous sentence boundary (period+space or
                    # newline), then trim trailing punctuation/whitespace.
                    boundary = max(
                        safe_explanation.rfind(". ", 0, idx),
                        safe_explanation.rfind(".\n", 0, idx),
                        safe_explanation.rfind("\n", 0, idx),
                        safe_explanation.rfind("! ", 0, idx),
                        safe_explanation.rfind("? ", 0, idx),
                    )
                    if boundary > 0:
                        # Keep the period/punctuation that ended the prior sentence
                        cut_at = boundary + 1
                        safe_explanation = safe_explanation[:cut_at].rstrip()
                    else:
                        # Marker is in the first sentence — drop everything
                        safe_explanation = ""
                    break
            # Cap verbose multi-sentence explanations (e.g. Emrakul "This means..." pedagogy).
            # Keep only the first sentence when the explanation runs over 120 characters.
            if len(safe_explanation) > 120:
                period_idx = safe_explanation.find(". ", 40)
                if period_idx != -1:
                    safe_explanation = safe_explanation[:period_idx + 1].rstrip()
            # May 16 audit: if the sanitizer stripped the explanation down to
            # nothing or a fragment, emit just the source name. The downstream
            # 📦 zone-change lines convey the actual state change.
            safe_explanation = safe_explanation.strip()
            # May 20 audit fix: route through the centralized prose sanitizer
            # so scaffolding headers / orphan conjunctions / unmatched close
            # parens get dropped. If subject-validation returns empty, fall
            # back to the same clean form the < 10 chars branch uses.
            from mtg.helpers import sanitize_judge_prose
            safe_explanation = sanitize_judge_prose(safe_explanation)
            # May 24 audit: when we have NO source attribution (display_name
            # fell back to "Triggered ability") AND the explanation still
            # contains action-narration prose, suppress the explanation
            # entirely. The downstream action lines (📦 zone-change, ⚔️
            # damage, 🩸 life-loss) carry the actual state change cleaner
            # than a narrated paragraph. 68 such leaks in the May 24 batch.
            if display_name == "Triggered ability" and safe_explanation:
                lower = safe_explanation.lower()
                action_narration_markers = (
                    " scries ", " mills ", " draws ", " discards ", " gains ",
                    " loses ", " puts a ", " puts the top", " puts onto",
                    " destroys ", " exiles ", " sacrifices ", " creates ",
                    " returns ", " bounces ", " taps ", " untaps ",
                )
                if any(m in lower for m in action_narration_markers):
                    print(f"[RESOLVE] Suppressing action-narration prose for "
                          f"unattributed trigger: '{safe_explanation[:80]}...'")
                    safe_explanation = ""
            # May 25 audit (F6 Option D — architectural): always emit the
            # source-only form, never the `— prose` suffix. The previous
            # turn of fixes layered six different sanitizers on top of the
            # LLM prose (hedge stripper, action-narration filter,
            # hallucination-marker filter, misattribution suppressor,
            # reasoning-marker truncation, parenthetical reasoning strip),
            # and each new audit batch found a new fluent-hallucination
            # shape that slipped past them all. Brago "causes each creature
            # to get a +1/+1 counter" — Brago doesn't grant counters; the
            # grammar is clean, no marker catches it. Rather than chasing
            # an infinite tail of regex-patches, drop the channel: the
            # downstream action emits (📦 zone, ⚔️ damage, 🩸 life,
            # ⭕ counters, ✨ flicker, 💀 destroy) describe what the engine
            # actually did — which is what players (and the strategist's
            # [CONV-DELTA] readback) need.
            #
            # Trade-off: lose the explanatory bridge for complex effects
            # where the action line isn't self-explanatory. For those,
            # the source name + downstream action line still convey the
            # essentials. Raw LLM prose is preserved in the console log
            # so post-mortem grep can recover it for audit work.
            #
            # The earlier sanitization passes above (lines ~770-952) are
            # now mostly dead code for the trigger-emit path but kept for
            # the misattribution-suppression early-return at line 800-801
            # which can still short-circuit BEFORE we reach this point.
            if explanation:
                # Single-line preview for log grep; trim aggressively.
                _prose_one_line = re.sub(r'\s+', ' ', explanation).strip()
                print(f"[RESOLVE-PROSE-DROPPED] {display_name}: {_prose_one_line[:200]}")
            messages.insert(0, f"⚡ **{display_name}** triggers")
        
        return messages, executed
        
    except json.JSONDecodeError as e:
        # May 7 audit fix #10: one-line preview avoids dumping indented JSON
        # blocks into the console log.
        try:
            _resolve_preview = re.sub(r'\s+', ' ', text).strip() if text else ""
        except Exception:
            _resolve_preview = (text or "").replace('\n', ' ')
        print(f"[RESOLVE] JSON parse error: {e} | Raw: {_resolve_preview[:200]}")
        # Suppress the user-facing prompt when the raw response actually
        # *describes* a no-op (the second copy of a duplicated trigger,
        # e.g. two Worldgorger Dragon LTB triggers where the first
        # already returned everything from exile, leaving the second
        # nothing to do). The model often emits a coherent prose
        # explanation that just isn't valid JSON in those cases.
        no_op_markers = (
            "no further action", "already returned", "already resolved",
            "nothing to return", "no effect", "does not change",
            "no game state change", "already on the battlefield",
        )
        text_lower = text.lower() if text else ""
        if any(marker in text_lower for marker in no_op_markers):
            print(f"[RESOLVE] Suppressing no-op response (JSON malformed but content is no-op)")
            return [], []
        # May 7 audit fix #5: in autoplay mode, suppress the user-facing
        # "Effect didn't resolve cleanly" prompt. It used to leak through
        # the trigger-drain path (triggers.py unconditionally extends
        # `messages` with resolve_msgs, ignoring the empty-actions
        # signal). Console log still captures the parse failure for audit.
        # May 7 audit fix #7: also truncate the [200+ char] ability text
        # in the user-facing message so the Discord card doesn't blow up.
        if getattr(game, 'is_autoplay', False):
            print(f"[RESOLVE] Autoplay: suppressing JSON-parse-error prompt for '{effect_description[:60]}'")
            return [], []
        truncated_desc = effect_description
        if effect_description and len(effect_description) > 60:
            truncated_desc = effect_description[:60].rstrip() + "..."
        return [f"⚠️ Effect didn't resolve cleanly: *{truncated_desc}*. "
                f"Use `!judge resolve` or `!fix` if needed."], []
    except Exception as e:
        print(f"[RESOLVE] Error: {e}")
        # API outage / billing failures (Insufficient Balance, 402, 429,
        # 503, ConnectionError) shouldn't spam Discord with useless
        # `!judge resolve` prompts that nobody can act on during autoplay
        # or after-hours. Suppress the user-facing message; the console
        # log + circuit breaker already make it visible to the operator.
        err_text = str(e).lower()
        api_outage_markers = (
            "insufficient balance", "invalid_request_error",
            "402", "429", "503", "504",
            "connection", "timeout", "rate limit",
            "circuit breaker", "api disabled",
        )
        if any(marker in err_text for marker in api_outage_markers):
            return [], []
        # May 7 audit fix #5: autoplay suppression for general resolution errors,
        # mirroring the JSON-parse-error path above. Also truncate the description.
        if getattr(game, 'is_autoplay', False):
            print(f"[RESOLVE] Autoplay: suppressing resolution-error prompt for '{effect_description[:60]}'")
            return [], []
        truncated_desc = effect_description
        if effect_description and len(effect_description) > 60:
            truncated_desc = effect_description[:60].rstrip() + "..."
        return [f"⚠️ Effect resolution error for *{truncated_desc}*. "
                f"Use `!judge resolve` or `!fix`. (details logged)"], []


def describe_game_for_judge(rules, game: GameState) -> str:
    """Build a description of game state for the judge."""
    lines = [
        f"Turn {game.turn_number}, Phase: {game.phase.value}",
        f"Active player: {game.active_player.name}",
        ""
    ]
    
    for i, player in enumerate(game.players):
        if getattr(player, 'eliminated', False):
            lines.append(
                f"=== {player.name} (Player {i+1}, ELIMINATED) ===")
            lines.append(
                f"Status: Eliminated ({getattr(player, 'loss_reason', '') or 'lost the game'})")
            lines.append("")
            continue
        lines.append(f"=== {player.name} (Player {i+1}, LIVING) ===")
        lines.append(f"Life: {player.life}, Poison: {player.poison}")
        
        if player.battlefield:
            lines.append("Battlefield:")
            for card in player.battlefield:
                state = []
                if card.tapped:
                    state.append("tapped")
                if card.summoning_sick and card.is_creature():
                    state.append("summoning sick")
                if card.counters:
                    state.append(f"counters: {card.counters}")
                state_str = f" ({', '.join(state)})" if state else ""
                lines.append(f"  - {card.name}{state_str}")
        
        lines.append(f"Hand: {len(player.hand)} cards")
        lines.append(f"Graveyard: {[c.name for c in player.graveyard]}")
        lines.append("")
    
    if game.stack:
        lines.append(f"Stack: {game.stack}")
    
    return "\n".join(lines)
