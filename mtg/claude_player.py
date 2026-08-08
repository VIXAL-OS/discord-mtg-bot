"""ClaudePlayer — AI logic for Claude playing MTG.

This is the AI decision-making layer. The strategist + actor split (Phase 2
of Parallel CoT, see CLAUDE.md) lives here: a background "strategist" model
produces a strategy memo each turn, and a foreground "actor" model uses that
memo + the current board state to plan the turn as a JSON action array.

Helpers in this module:

    _check_color_castable — checks whether a mana cost is payable given the
                            currently available mana pool, accounting for
                            color requirements vs flexible/any-color mana.
                            Used exclusively by ClaudePlayer's castable-list
                            calculations.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
"""

import asyncio
import json
import random
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import anthropic
import discord

from mtg.constants import (
    Phase, Zone, COMMAND_ZONE_FORMATS, FORMAT_DECK_SIZE,
    FORMAT_STARTING_LIFE, MANA_COLOR_IDENTITY, PHASE_NAMES, PHASE_ORDER,
)
from mtg.helpers import response_text
from mtg.models import Card, Player, GameState, FormatValidator

# Optional: structured mana cost parser
try:
    from rules.mana import ManaCost
    HAS_MANA_ENGINE = True
except ImportError:
    ManaCost = None
    HAS_MANA_ENGINE = False


STRATEGY_REJECTION_THRESHOLD = 2
STRATEGY_BACKOFF_TURNS = 3


def _strategy_call_due(game: GameState) -> bool:
    """Consume one per-game backoff turn and report whether to call strategist."""
    remaining = max(0, getattr(game, '_strategy_backoff_turns', 0))
    if not remaining:
        return True
    game._strategy_backoff_turns = remaining - 1
    print(f"[STRATEGIST-BACKOFF] Skipping strategist call "
          f"({game._strategy_backoff_turns} skipped turn(s) remain)")
    return False


def _record_strategy_memo_result(game: GameState, accepted: bool) -> None:
    """Update the per-game rejection circuit after memo validation."""
    if accepted:
        game._strategy_rejection_streak = 0
        game._strategy_backoff_turns = 0
        return
    game._strategy_rejection_streak = (
        getattr(game, '_strategy_rejection_streak', 0) + 1)
    if game._strategy_rejection_streak >= STRATEGY_REJECTION_THRESHOLD:
        game._strategy_backoff_turns = STRATEGY_BACKOFF_TURNS
        print(f"[STRATEGIST-BACKOFF] {game._strategy_rejection_streak} "
              f"consecutive memo rejections; skipping next "
              f"{STRATEGY_BACKOFF_TURNS} strategist turns")


# July 30, 2026: the castability computation moved to mtg/legal_actions.py —
# the ONE provider for the prompt builders here AND the future frontend
# (the two in-file castable builders had already diverged; see that
# module's docstring). Re-imported here because ~10 call sites in this
# file use it directly.
from mtg.legal_actions import _check_color_castable, castable_labels  # noqa: E402

# Aug 2 batch-14: module-level so the density gate is testable — the
# nested _sanitize_memo closure reads this tuple; tests import it and
# run the same >=2-hit rule against observed leak samples.
_MEMO_SCAFFOLDING_MARKERS = (
    'concrete board observation',
    '500 characters',
    'tight prose',
    'do not narrate',
    'hard limit',
    'hard cap',
    'maximum 500',
    'naming specific cards',
    'stable strategy reference',
    'win conditions by archetype',
    'prioritize in this order',
    'threat evaluation',
    'interaction rules',
    'combat math:',
    'control deck play',
    # May 25 audit (F12): V4-Pro pattern caught in 1100/1501 memos —
    # "Win condition: We need to output exactly four labeled lines.
    # Let's assess the current board state to fill each line."
    # The prefill guarantees `has_win`, so the positive-validator
    # was useless against this. These markers catch the scaffolding
    # paraphrase shape so density-check trips first.
    'we need to output',
    'we need to produce',
    'we need to generate',
    'we need to fill',
    "let's assess",
    "let's parse",
    "let's fill",
    "let's evaluate",
    'labeled lines',
    'labeled format',
    'four labeled',
    'fill each line',
    'fill each label',
    'to fill each',
    # June 10 audit: 238 of 543 ACCEPTED memos carried
    # scaffolding hidden BEHIND the required label — "Win
    # condition: We need to produce exactly 4 lines as
    # instructed: …" passed positive-validation (has the
    # label) and density (those phrases weren't markers).
    'we need to produce',
    'we need to output',
    'we must produce',
    'as instructed',
    'exactly 4 lines',
    'exactly four lines',
    'four lines as',
    'the format is',
    'per the format',
    'the required format',
    # Aug 2 batch-14 (strategist A/B): V4-Flash thinking mode
    # leaks its format meta-reasoning in TELEGRAPHIC style —
    # "We need produce exactly four lines. Need obey labels.
    # Need analyze board." — the "to"-less variants matched
    # NO existing marker, so 113/365 accepted memos were this
    # garbage (each displacing the previous GOOD memo, which
    # a nuke would have kept). All variants observed live in
    # batch 15334.
    'we need produce',
    'we need output',
    'we need answer',
    'we need respond',
    'we need craft',
    'we need write',
    'need obey',
    'need craft',
    'need analyze',
    'need assess',
    'need be specific',
    'need exact',
    'need output',
    'need produce',
    'four lines',
    'under 800',
    '800 chars',
    'exact format',
    'we are claude',
    'we have game context',
)


_MEMO_LABEL_TASK_REFERENCE_RE = re.compile(
    r'(?i)(?:^|[^a-z])(?:win condition|this turn|'
    r'opp threats(?:[ \t]*&[ \t]*answers)?|hold for opp turn)'
    r'[ \t]*:[ \t]*(?:'
    r'(?:the[ \t]+)?(?:user|prompt|task|request|instruction)s?'
    r'|(?:we|i)[ \t]+(?:need|must|should)[ \t]+(?:to[ \t]+)?'
    r'(?:answer|output|respond|provide|produce|follow))'
    r'(?=$|[^a-z])'
)


def _memo_has_labeled_task_reference(cleaned: str) -> bool:
    # Labels are not strategic evidence when their content describes the task.
    return bool(_MEMO_LABEL_TASK_REFERENCE_RE.search(cleaned or ""))


# May 7 audit: helper for surfacing legality context to the actor's prompt.
# The AI repeatedly planned Counterspell/Mana Leak/Dovin's Veto when the
# stack was empty and targeted removal when the opponent had 0 creatures —
# PLAN-VALIDATE caught them but the actor never learned because it never saw
# WHY a card was unplayable. Exposing stack_size and a per-card legality
# annotation lets the prompt teach the actor in-band.
def _card_legality_note(card, game, opp_player) -> str:
    """Return a short legality annotation for a card in hand, or '' if no issue.

    Detects two patterns flagged in the May 7 audit:
    - Counterspells (or other stack-dependent spells) when game.stack is empty
    - Targeted creature removal (instant/sorcery) when opp has 0 creatures

    Conservative: only fires on instants/sorceries whose primary effect needs
    the missing target. Doesn't reject creatures/PWs/etc with conditional
    target text — their bodies/abilities still matter on cast.
    """
    if card.is_land():
        return ""
    oracle = (card.oracle_text or '').lower()
    type_line = (getattr(card, 'type_line', '') or '').lower()
    is_instant_or_sorcery = 'instant' in type_line or 'sorcery' in type_line

    if ('aura' in type_line
            and ('enchant creature' in oracle or 'enchant permanent' in oracle)):
        caster = next((p for p in game.players if p is not opp_player), None)
        if caster is not None:
            from rules.targeting_helpers import aura_has_legal_target
            if not aura_has_legal_target(game, card, caster):
                zone = ("in any graveyard" if 'in a graveyard' in oracle
                        else "on the battlefield")
                return f"no legal Aura targets {zone}"

    # June 11 audit: the prompt advertised untapped Chainer as a target for
    # Murderous Compulsion ("target tapped creature"), inducing repeated
    # illegal casts in game 1514629178413154325. Ask the same full validator
    # used by the cast path before adding card-specific heuristics below.
    if is_instant_or_sorcery and opp_player is not None:
        caster = next((p for p in game.players if p is not opp_player), None)
        if caster is not None:
            from rules.targeting_helpers import (
                _find_any_valid_target, _spell_requires_targets)
            if (_spell_requires_targets(card)
                    and not _find_any_valid_target(game, card, caster.name)):
                return "no legal targets"

    # Counterspell detection — only an issue when the stack is empty.
    # Counters on creatures (Mystic Snake, Frilled Mystic) and modal spells
    # with non-counter modes are excluded — they still have value on empty stack.
    stack_size = len(getattr(game, 'stack', []) or [])
    if stack_size == 0 and 'counter target' in oracle and 'spell' in oracle:
        is_creature_etb = card.is_creature()
        has_other_mode = '•' in oracle and any(
            kw in oracle for kw in ('draw a card', 'return target', 'tap target',
                                    'gain', 'destroy target', 'create')
        )
        if not is_creature_etb and not has_other_mode:
            return "no legal targets — stack is empty"

    # Targeted removal: instant/sorcery with "destroy/exile target creature"
    # and opponent has zero creatures on board. Skip pump/return-to-hand-own
    # variants (they don't need an opposing creature).
    if is_instant_or_sorcery and opp_player is not None:
        opp_creatures = [c for c in opp_player.battlefield if c.is_creature()]
        if not opp_creatures:
            targets_creature = (
                ('destroy target' in oracle and 'creature' in oracle)
                or ('exile target' in oracle and 'creature' in oracle)
            )
            # Spells that target own gy/own creatures (Reanimate, pump) are fine.
            targets_own_gy = 'your graveyard' in oracle
            targets_own_creature = 'creature you control' in oracle
            # Spells with any-permanent flexibility (Vindicate, Anguished Unmaking)
            # may still have a non-creature target available.
            targets_any_permanent = 'target permanent' in oracle
            if (targets_creature and not targets_own_gy
                    and not targets_own_creature
                    and not targets_any_permanent):
                # Aug 2 (hint-scoping, July-29 carry): own creatures ARE
                # CR-legal targets, so "no legal targets" was a false claim
                # whenever the caster had a board (Fatal Push read
                # unplayable while the caster's own Dragon's Rage Channeler
                # was legal). Keep the deterrent, drop the lie.
                caster2 = next(
                    (p for p in game.players if p is not opp_player), None)
                if caster2 is not None and any(
                        c.is_creature() for c in caster2.battlefield):
                    return ("only your own creatures are legal targets — "
                            "usually a waste")
                return "no legal targets — opponent has 0 creatures"
    return ""


def prose_hold_veto(raw_text: str, action: dict) -> bool:
    """Aug 2 (the prose-says-pass class): True when the model's own prose
    says to HOLD the exact card its JSON action casts (batch evidence:
    Teferi's Protection burned against the model's own written reasoning —
    the F13 pass-intent check existed only in decide_response).

    Conservative on purpose: the hold marker must PRECEDE the card's name
    within the same sentence fragment (<=40 chars between), so "cast Bolt
    and hold Counterspell" vetoes only a Counterspell action, never the
    Bolt. Applies to cast/activate actions with a real card name.
    """
    if not raw_text or not isinstance(action, dict):
        return False
    if action.get('type') not in ('cast', 'activate'):
        return False
    name = (action.get('card') or action.get('permanent') or '')
    if not name or len(name) < 4:
        return False
    pat = re.compile(
        r"\b(?:hold(?:ing)?|sav(?:e|ing)|keep(?:ing)?|don'?t cast|"
        r"do not cast|shouldn'?t cast|wait(?:ing)? (?:on|to cast))\b"
        r"[^.!?\n]{0,40}?" + re.escape(name.lower()))
    return bool(pat.search(raw_text.lower()))


def _card_target_hint(card, game, opp_player) -> str:
    """Return a short hint about an available legal target for this card,
    or '' if no useful hint applies.

    May 20 audit: the May 7 `[unplayable: ...]` annotation surfaced the
    NEGATIVE case (no legal targets), but the POSITIVE case stayed
    invisible. In game_1506209151464767648 Jund held 3 Fatal Push for 9
    turns against an active Goblin Guide because the castable line said
    `Fatal Push ({B})` with no hint that Goblin Guide was a legal target.

    Conservative: only annotates instant/sorcery cards with destroy-target/
    exile-target/deal-damage-to-target oracle text, and only points at the
    cheapest opponent creature. Doesn't try to be smart about Fatal Push's
    `mana value 2 or less` clause — listing any creature is enough of a
    nudge to get the actor to plan the cast.
    """
    if card.is_land() or opp_player is None:
        return ""
    oracle = (card.oracle_text or '').lower()
    type_line = (getattr(card, 'type_line', '') or '').lower()
    if not ('instant' in type_line or 'sorcery' in type_line):
        return ""
    targets_creature = (
        ('destroy target' in oracle and 'creature' in oracle)
        or ('exile target' in oracle and 'creature' in oracle)
        or ('deal' in oracle and 'damage to target' in oracle and 'creature' in oracle)
    )
    if not targets_creature:
        return ""
    caster = next((p for p in game.players if p is not opp_player), None)
    if caster is None:
        return ""
    from rules.targeting_helpers import _validate_target_for_action
    opp_creatures = [
        c for c in opp_player.battlefield
        if c.is_creature()
        and _validate_target_for_action(
            game, c, opp_player, card, caster.name)[0]
    ]
    if not opp_creatures:
        return ""
    # Pick the cheapest legal target as the hint (more likely to actually
    # be in-range for restricted removal like Fatal Push / Doom Blade).
    target = min(opp_creatures, key=lambda c: getattr(c, 'cmc', 0) or 0)
    return f"target available: {target.name}"


def _annotate_castable_with_legality(castable_cards: list, hand: list,
                                     game, opp_player) -> list:
    """Append "(no legal targets — ...)" or "(target available: ...)"
    suffixes to castable labels. Returns a new list. May 7 audit fix #2;
    May 20 audit added the positive-target case (Fatal Push held all game
    because the AI didn't realize Goblin Guide was a legal target)."""
    if not castable_cards:
        return castable_cards
    name_to_unplayable_note = {}
    name_to_target_hint = {}
    for c in hand:
        note = _card_legality_note(c, game, opp_player)
        if note:
            name_to_unplayable_note[c.name.lower()] = note
            continue
        hint = _card_target_hint(c, game, opp_player)
        if hint:
            name_to_target_hint[c.name.lower()] = hint
    annotated = []
    for label in castable_cards:
        # label format: "Card Name ({W}{U})" or "Card Name (cost) [TAG]"
        # extract leading name up to first " ("
        head = label.split(' (', 1)[0].strip().lower()
        if head in name_to_unplayable_note:
            annotated.append(f"{label} [unplayable: {name_to_unplayable_note[head]}]")
        elif head in name_to_target_hint:
            annotated.append(f"{label} [{name_to_target_hint[head]}]")
        else:
            annotated.append(label)
    return annotated


# =============================================================================
# CLAUDE PLAYER AI
# =============================================================================

def _resolve_annotated_card_name(name_str, name_map):
    """Resolve a (possibly annotated) name through a disambiguation map.

    July 20 batch-3 audit (reviewer A4): block prompts show attackers as
    "Name(P/T)[kw1,kw2]"; the old end-anchored parenthetical strip failed
    whenever a [keywords] suffix followed the (P/T) group, so the echoed
    descriptor never matched the bare-name map and that block was silently
    dropped ("Could not resolve attacker 'Faerie Rogue(1/1)[flying]'").
    Strip trailing (…) / […] groups until stable.
    """
    clean = name_str
    while True:
        _stripped = re.sub(r'\s*(?:\([^)]*\)|\[[^\]]*\])\s*$', '', clean)
        if _stripped == clean:
            break
        clean = _stripped
    clean = clean.strip()
    # Exact match (handles disambiguated names like Plant_1)
    if clean in name_map:
        return name_map[clean]
    # Case-insensitive match
    for k, v in name_map.items():
        if k.lower() == clean.lower():
            return v
    # Match by base card name (AI dropped the _N suffix)
    for k, v in name_map.items():
        if v.name.lower() == clean.lower():
            return v
    return None


class ClaudePlayer:
    """AI logic for Claude playing MTG."""

    def __init__(self, client: anthropic.Anthropic, usage_callback=None):
        self.client = client
        self.model = "claude-sonnet-5"  # Game decisions use Sonnet (Opus reserved for emotional support)
        self.last_error = None  # Track errors for debugging
        self.usage_callback = usage_callback  # Callback for token tracking
        self.engine_ref = None  # Set by the active GameEngine (for plan_turn PW access)
        # Circuit breaker: track consecutive API failures to detect credit exhaustion
        self._consecutive_failures = 0
        self._api_disabled = False  # Set True after too many consecutive failures
        # State description cache — avoid recomputing when board hasn't changed
        self._cached_state_desc = None
        self._cached_state_fingerprint = None
        self._cached_hand_desc = None
        self._cached_hand_hash = None
        # Phase 2: Strategist+Actor — background strategy memo
        self._strategy_memo = ""  # Updated every turn by background strategist
        self._strategy_task = None  # asyncio.Task for background strategist call
        # Phase 3: split Actor (self.client) from Strategist (strategist_client).
        # None = fall back to self.client / self.model (Anthropic or single-model DeepSeek).
        # Set by autoplay when a separate strategist provider is available.
        self.strategist_client = None   # the deep-reasoning strategist (v4-flash THINKING since the Aug 2 A/B; was v4-pro)
        self.strategist_model = None    # model name string for the strategist client

    @property
    def provider_tag(self):
        """Console log tag that reflects actual provider.

        Aug 7 batch audit (C-2): Qwen had no branch, so all 50 Qwen-wave
        games logged [CLAUDE AI] console tags — a provider-tag grep by
        future audit tooling would silently miss them (Discord seat names
        were correct throughout; this is console-only).
        """
        model_lower = self.model.lower()
        if 'deepseek' in model_lower:
            return '[DEEPSEEK AI]'
        if 'qwen' in model_lower:
            return '[QWEN AI]'
        if 'openrouter/' in model_lower or self.model.startswith('openrouter/'):
            short = self.model.split('/')[-1]
            return f'[OPENROUTER:{short} AI]'
        return '[CLAUDE AI]'

    @property
    def turn_tag(self):
        """Console log tag for turn-level logging."""
        model_lower = self.model.lower()
        if 'deepseek' in model_lower:
            return '[DEEPSEEK TURN]'
        if 'qwen' in model_lower:
            return '[QWEN TURN]'
        if 'openrouter/' in model_lower or self.model.startswith('openrouter/'):
            short = self.model.split('/')[-1]
            return f'[OPENROUTER:{short} TURN]'
        return '[CLAUDE TURN]'
    
    def _track_usage(self, response, model_override: Optional[str] = None):
        """Track token usage if callback is set. Resets failure counter on success.

        `model_override` lets the strategist path report its own model
        (deepseek-v4-pro) instead of self.model (the actor's deepseek-v4-flash),
        so V4-Pro tokens get priced at strategist rates instead of being lumped
        with actor rates in the lifetime cost summary.
        """
        self._consecutive_failures = 0  # Successful API call — reset circuit breaker
        # June 10 audit (V30): usage can legitimately be None (mid-stream
        # error after partial text — the final usage chunk never arrived).
        # Passing None into the callback raised AttributeError and the outer
        # catch threw away the memo; skip accounting instead.
        if self.usage_callback and getattr(response, 'usage', None) is not None:
            self.usage_callback(response.usage, model_override or self.model)

    async def _update_strategy(self, game: 'GameState', player_index: int):
        """[STRATEGIST] Background strategy advisor — runs once per turn.

        Produces a 3-5 sentence strategy memo analyzing the board state.
        The memo is used by plan_turn() on the NEXT turn (one-turn stale
        is fine — MTG game plans don't change every action).

        Phase 2 of the Parallel CoT Architecture (see CLAUDE.md).
        """
        try:
            player = game.players[player_index]
            opponent = game.players[1 - player_index]

            # Build compact board state for strategist (smaller than full state).
            # Use effective P/T so variable creatures (Beanstalk Giant, Mortivore,
            # Tarmogoyf) are represented accurately — the strategist otherwise
            # under-rates */* and X/X creatures and skips useful blocks.
            def _pt(c):
                try:
                    p = c.get_effective_power(game)
                    t = c.get_effective_toughness(game)
                    return f"{p}/{t}"
                except Exception:
                    return f"{c.power}/{c.toughness}"

            # May 14 audit (A6): build a TYPED permanents table with flags
            # (commander/token/source/loyalty) so the strategist doesn't
            # hallucinate ("Athreos isn't a creature", "Bird (likely a Bird
            # token from something)", "Phoenix of Ash is a 2/2 flyer").
            def _describe_creature(c):
                tags = []
                if getattr(c, 'is_commander', False):
                    tags.append("COMMANDER")
                if getattr(c, 'token', False) or 'token' in (c.type_line or '').lower():
                    tags.append("token")
                # Surface key keywords so strategist doesn't re-derive from name
                keywords = list(getattr(c, 'keywords', []) or [])
                kw_show = [kw for kw in keywords if kw.lower() in {
                    'flying', 'trample', 'lifelink', 'deathtouch', 'first strike',
                    'double strike', 'vigilance', 'menace', 'haste', 'reach',
                    'indestructible', 'hexproof', 'shroud', 'unblockable',
                    'defender', 'flash'
                }]
                if kw_show:
                    tags.append(" ".join(kw_show).lower())
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                return f"{c.name} {_pt(c)}{tag_str}"

            def _describe_perm(c):
                tags = []
                type_l = (c.type_line or '').lower()
                if 'planeswalker' in type_l:
                    loyalty = getattr(c, 'loyalty_counters', None) or getattr(c, 'loyalty', None)
                    if loyalty:
                        tags.append(f"PW loyalty={loyalty}")
                elif 'enchantment' in type_l:
                    tags.append("enchantment")
                elif 'artifact' in type_l:
                    tags.append("artifact")
                if getattr(c, 'is_commander', False):
                    tags.append("COMMANDER")
                tag_str = f" [{', '.join(tags)}]" if tags else ""
                return f"{c.name}{tag_str}"

            my_creatures = [_describe_creature(c) for c in player.creatures()[:8]]
            opp_creatures = [_describe_creature(c) for c in opponent.creatures()[:8]]
            my_other = [_describe_perm(c) for c in player.active_battlefield()
                        if not c.is_creature() and not c.is_land()][:6]
            opp_other = [_describe_perm(c) for c in opponent.active_battlefield()
                         if not c.is_creature() and not c.is_land()][:6]
            my_hand_summary = [f"{c.name} ({c.mana_cost})" for c in player.hand if not c.is_land()][:6]
            lands_count = len(player.lands())
            opp_lands = len(opponent.lands())

            # Show already-activated permanents so strategist knows what's used up
            activated_pws_str = ""
            if hasattr(game, '_activation_counts') and game._activation_counts:
                activated_names = [
                    k.split(":", 1)[-1] if ":" in k else k
                    for k, v in game._activation_counts.items() if v >= 1
                ]
                if activated_names:
                    activated_pws_str = f"\nAlready activated this turn (cannot use again): {', '.join(activated_names)}"

            # Lethal-on-board detection: sum unblocked-able attack power vs opp life.
            # Why: strategist sometimes recommends activating planeswalkers / casting
            # value spells when just attacking would win the game this turn. Surface
            # this as a top-line flag so the memo prioritises the kill.
            lethal_flag = ""
            try:
                attack_ready = [
                    c for c in player.creatures()
                    if not c.tapped
                    and not getattr(c, 'summoning_sick', False)
                    and 'defender' not in (getattr(c, 'oracle_text', '') or '').lower()
                ]
                # Conservative: assume opp can block with tapped-or-not creatures
                opp_blockers = [c for c in opponent.creatures() if not getattr(c, 'tapped', False)]
                total_power = sum(max(0, c.get_effective_power(game)) for c in attack_ready)
                evasion_power = sum(
                    max(0, c.get_effective_power(game)) for c in attack_ready
                    if any(kw in (getattr(c, 'oracle_text', '') or '').lower()
                           for kw in ['flying', 'unblockable', 'menace', 'trample',
                                      "can't be blocked", 'shadow', 'intimidate'])
                )
                # If unblockable/evasive power alone is lethal, definitely lethal.
                # If raw power >> blockers, also likely lethal.
                is_lethal = (
                    evasion_power >= opponent.life
                    or (total_power >= opponent.life and len(attack_ready) > len(opp_blockers))
                )
                if is_lethal:
                    lethal_flag = (
                        f"\n⚠ LETHAL ON BOARD: you have {total_power} power "
                        f"({evasion_power} evasive) vs opp {opponent.life} life, "
                        f"{len(attack_ready)} attackers vs {len(opp_blockers)} untapped blockers. "
                        f"ATTACK — do not waste the turn on planeswalker loyalty or value spells."
                    )
            except Exception:
                pass

            # May 14 audit (A6): explicit format header + named perspective so
            # the strategist doesn't hedge ("scenario doesn't specify format")
            # or flip controller attribution ("opponent has Solitary Confinement"
            # when it's the player's). Format string was completely absent
            # before; perspective was inferred from "Your/Opponent" framing
            # which let the model swap when oracle text says "target opponent".
            fmt = getattr(game, 'format', 'unknown')
            format_header = f"FORMAT: {fmt}"
            try:
                if fmt in ('commander', 'edh', 'brawl', 'oathbreaker'):
                    format_header += f" | starting life {player.life if game.turn_number == 0 else '40' if fmt in ('commander','edh') else '25' if fmt=='brawl' else '20'}"
            except Exception:
                pass
            perspective = (
                f"PERSPECTIVE: You = {player.name} (planning their turn). "
                f"Opponent = {opponent.name}. "
                f"When the memo says 'opponent', it means {opponent.name} — do not flip."
            )

            # May 14 audit (A7): surface opponent's spell-type history so the
            # strategist sees "opponent has cast 0 noncreature spells in 8
            # turns — counterspells are dead in this matchup, pivot." Without
            # this, control decks die holding all their answers.
            spell_count_section = ""
            counts_by_player = getattr(game, '_spell_counts_by_player', {})
            opp_counts = counts_by_player.get(opponent.name, {})
            you_counts = counts_by_player.get(player.name, {})
            if opp_counts or you_counts:
                opp_total = opp_counts.get('total', 0)
                opp_nc = opp_counts.get('noncreature', 0)
                opp_inst = opp_counts.get('instant', 0)
                you_total = you_counts.get('total', 0)
                counters_in_hand = sum(
                    1 for c in player.hand
                    if not c.is_land()
                    and 'counter target' in (c.oracle_text or '').lower()
                    and 'spell' in (c.oracle_text or '').lower()
                    and '•' not in (c.oracle_text or '').lower()
                    and not c.is_creature()
                )
                dead_counter_note = ""
                if counters_in_hand >= 2 and game.turn_number >= 4 and opp_nc == 0:
                    dead_counter_note = (
                        f" — ⚠ HARD COUNTERS ARE DEAD: opp has cast 0 noncreature spells; "
                        f"you hold {counters_in_hand} hard counterspell(s). Pivot to threats/draw "
                        f"or cycle counters via cycling/discard outlets."
                    )
                spell_count_section = (
                    f"\nMATCHUP: opp cast totals = {opp_total} ({opp_nc} noncreature, "
                    f"{opp_inst} instant). You cast totals = {you_total}.{dead_counter_note}"
                )

            # Modal-counterspell visibility: Mystic Confluence / Archmage's Charm
            # have non-counter modes that work on empty stack. Without this hint
            # the strategist treats them as dead like vanilla counters.
            modal_counter_in_hand = [
                c.name for c in player.hand
                if not c.is_land()
                and 'counter target' in (c.oracle_text or '').lower()
                and 'spell' in (c.oracle_text or '').lower()
                and '•' in (c.oracle_text or '').lower()
            ]
            modal_counter_section = ""
            if modal_counter_in_hand:
                modal_counter_section = (
                    f"\nMODAL COUNTERS IN HAND (have non-counter modes that work "
                    f"on empty stack): {', '.join(modal_counter_in_hand[:3])}."
                )

            # Commander damage tracker for Commander-class formats. The 21-from-
            # one-commander loss condition is invisible without this.
            cmd_damage_section = ""
            if fmt in ('commander', 'edh'):
                you_taken = 0
                opp_taken = 0
                try:
                    your_idx = game.players.index(player)
                    opp_idx = 1 - your_idx
                    you_taken = max(player.commander_damage.values()) if player.commander_damage else 0
                    opp_taken = max(opponent.commander_damage.values()) if opponent.commander_damage else 0
                except Exception:
                    pass
                cmd_damage_section = (
                    f"\nCOMMANDER DAMAGE: you taken {you_taken}/21, opp taken {opp_taken}/21"
                )

            # May 23 audit (Hypothesis #1): cache hit rate stuck at ~68.8%
            # because only ~200 chars of user-message prefix are stable
            # (format_header + perspective). Inject a STABLE game-context
            # block FIRST so the cache prefix extends through ~500+
            # additional tokens before hitting volatile turn-by-turn state.
            # Only include fields that DON'T change after game start:
            # player names, commander names (commander stays in command zone
            # name-stable even when bouncing between command zone and other
            # zones), format. Deck zone counts CAN'T go here — tokens
            # enter/leave constantly.
            try:
                your_cmdr_name = getattr(player, '_starting_commander_name', None)
                if not your_cmdr_name:
                    cmdr_list = list(getattr(player, 'command_zone', []) or [])
                    your_cmdr_name = cmdr_list[0].name if cmdr_list else 'none'
                    setattr(player, '_starting_commander_name', your_cmdr_name)
                opp_cmdr_name = getattr(opponent, '_starting_commander_name', None)
                if not opp_cmdr_name:
                    cmdr_list = list(getattr(opponent, 'command_zone', []) or [])
                    opp_cmdr_name = cmdr_list[0].name if cmdr_list else 'none'
                    setattr(opponent, '_starting_commander_name', opp_cmdr_name)
            except Exception:
                your_cmdr_name = opp_cmdr_name = 'n/a'

            # May 23 follow-up: cache the STARTING DECK LIST per-player as
            # a sorted, deduplicated string of card names. This snapshots
            # ALL cards in the deck at game-start (library + opening hand)
            # and never changes — so it adds ~1500 stable tokens to the
            # cache prefix. Pushes cache hit median from ~68.8% → 80%+.
            # Stored on the player object the first time we see them so
            # subsequent turns don't recompute. The snapshot is taken on
            # FIRST CALL of the game (after mulligans, before turn 1 of
            # planning) which captures the post-mulligan deck contents.
            def _snapshot_deck(p):
                cached = getattr(p, '_starting_decklist_str', None)
                if cached is not None:
                    return cached
                names = []
                for c in list(p.library) + list(p.hand) + list(p.battlefield) + list(p.command_zone) + list(p.graveyard) + list(p.exile):
                    if getattr(c, 'is_token', False):
                        continue  # tokens enter/leave; don't include
                    n = getattr(c, 'name', None)
                    if n:
                        names.append(n)
                names.sort()
                # Deduplicate while preserving counts: "Forest x4, Llanowar Elves, …"
                from collections import Counter
                counts = Counter(names)
                parts = []
                for n in sorted(counts.keys()):
                    cnt = counts[n]
                    parts.append(f"{n} x{cnt}" if cnt > 1 else n)
                snapshot = ', '.join(parts)
                setattr(p, '_starting_decklist_str', snapshot)
                return snapshot
            try:
                your_deck = _snapshot_deck(player)
                opp_deck = _snapshot_deck(opponent)
            except Exception:
                your_deck = opp_deck = 'n/a'

            stable_game_context = (
                f"=== GAME CONTEXT (stable for this game) ===\n"
                f"Players: {player.name} vs {opponent.name}\n"
                f"Your commander: {your_cmdr_name}\n"
                f"Opp commander: {opp_cmdr_name}\n"
                f"\n"
                f"Your deck (cards by name, includes hand+library+battlefield+grave+exile):\n"
                f"{your_deck}\n"
                f"\n"
                f"Opp deck (same):\n"
                f"{opp_deck}\n"
                f"=== END GAME CONTEXT ===\n"
            )
            board_summary = f"""{stable_game_context}
{format_header}
{perspective}
Life: You {player.life}, Opponent {opponent.life}{lethal_flag}{cmd_damage_section}{spell_count_section}{modal_counter_section}
Your creatures: {', '.join(my_creatures) if my_creatures else 'none'}
Your other permanents: {', '.join(my_other) if my_other else 'none'}
Your lands: {lands_count} | Your hand (non-land): {', '.join(my_hand_summary) if my_hand_summary else 'none'}
Opponent creatures: {', '.join(opp_creatures) if opp_creatures else 'none'}
Opponent other permanents: {', '.join(opp_other) if opp_other else 'none'}
Opponent lands: {opp_lands} | Opponent hand size: {len(opponent.hand)}{activated_pws_str}"""

            # May 7 audit: V4-Pro with reasoning_effort=high frequently echoed
            # the instructional rules back as the memo body ("Output ONLY the
            # memo prose. No JSON...", "The memo must cover: win condition...")
            # in 4/5 sampled games. Rewriting the system prompt to be brief
            # and action-oriented — no "rules" section the model can quote.
            # The defensive sanitizer below strips any meta-instruction that
            # still leaks through.
            #
            # May 13 audit: 0% prompt-cache hit rate on the strategist (~1300
            # reasoning-mode calls per batch, all uncached → top cost lever).
            # Expanded the system prompt with a long, STABLE strategic-context
            # section so DeepSeek's auto-cache has a >=512-token prefix to
            # hit across calls in the same game session. Per-turn dynamic
            # content stays in the user message. The new sections are pure
            # MTG strategy heuristics — they apply every game, every turn —
            # so cache investment pays off after the first call.
            # May 18 audit: reorder so the LARGE stable block (~3000 tokens of
            # MTG reference material) sits AT THE FRONT of the system prompt,
            # then the small per-coach instruction follows. DeepSeek's
            # prefix-based auto-cache starts from token 0 of the system, so
            # putting the biggest stable content there maximises the cached
            # token count. The header + instructions are also stable across
            # calls in one game session, so order shouldn't matter for cache
            # purposes — but in case DeepSeek's cache has a minimum-prefix
            # threshold, fronting the long block gives the best odds. May 17
            # batch ran cache_hit ~65.8% (target 60-89%); this aims for 80%+.
            system = """=== STABLE STRATEGY REFERENCE (do not echo back, do not paraphrase) ===

WIN CONDITIONS by archetype:
- Aggro/Stompy/Voltron: attack to reduce opp to 0 (or 21 commander damage). Curve out, swing every turn, protect threats with combat tricks/removal.
- Control: stabilize via wipes + counterspells + spot removal. Win in the late game with a single threat (Shark Typhoon, Aetherflux, Approach, Mastermind).
- Combo: assemble specific cards (Thassa's Oracle + library-empty, Kiki + Felidar, Heliod + Walking Ballista). Tutor + protect.
- Aristocrats/Drain: incremental damage via dies-triggers + sacrifice loops. Blood Artist, Zulaport, Korvold value.
- Reanimator: dump fatties to graveyard, return them cheap. Animate Dead, Reanimate, Persist, Meren.
- Ramp: get to expensive threats fast. Cultivate, Kodama's Reach, Rampant Growth → 6+ CMC bombs.

PRIORITIZE in this order each turn:
1. LETHAL: if you can win this turn, attack — don't waste mana on value.
2. LAND DROP: missing a land drop is the biggest tempo loss in MTG.
3. ANSWER OPP THREAT: if opp has a creature you can't race, kill it.
4. PRESSURE OPP LIFE: deploy threats / attack into chump blocks.
5. DRAW / RAMP: only if no threat or removal needed.
6. PASS / HOLD: only if holding instant-speed interaction matters.

COMMANDER DEPLOYMENT: in commander formats your commander is usually your engine or win condition — cast it when mana allows. Don't bank it in the command zone for many turns; the only good reasons to hold it are an expected board wipe or protecting a combo line. (June 10 audit: replacement_chain lost 2 of 4 games partly because Gisela was never deployed.)
PLANESWALKER ABILITIES: read ALL loyalty modes, not just [+1]. Minus abilities are usually the removal/value mode (Teferi -3 bounces a threat). If loyalty meets the ultimate's cost and the ultimate wins or locks the game, USE IT — clicking [+1] forever with a game-winning -8 available is a misplay.
SACRIFICE DISCIPLINE: never sacrifice more than one creature per turn to a free outlet (Viscera Seer, Carrion Feeder, Altars) unless a death-payoff permanent (Blood Artist, Zulaport Cutthroat, Korvold, Mayhem Devil, Bastion of Remembrance) is on YOUR battlefield, or you're dodging exile/theft. Sacrificing your board — especially your commander — for scry value loses games.
EQUIPMENT: an Equipment in play but unattached does nothing. If you control unattached Equipment and a creature, equipping is usually better than casting another Equipment.
CARD-ADVANTAGE ENGINES: a repeatable draw/loot/scry activation you can afford with SPARE mana (Anje Falkenrath's {T} discard-draw, Thrasios's {4}, Azcanta) is almost always worth using every turn — leaving it idle is discarding a card a turn. A CHEAP value commander (Tymna at 3 mana, any 2-3 mana commander with a triggered engine) should hit the battlefield in the first few turns, not sit in the command zone; its value compounds per turn on the battlefield and is zero in the zone. If your deck has a COMPANION (Lurrus), pay the {3} to bring it to hand as soon as you have spare mana — it's a free extra card you already paid for in deckbuilding. (Aug 2: batch audits show Tymna/Anje/Lurrus fully offered and never chosen, entire games.)

THREAT EVALUATION:
- Indestructible/hexproof/protection creatures need bounce/exile/sacrifice.
- A creature with lifelink can race aggro.
- A creature with deathtouch threatens every attacker — chump or sacrifice.
- Anthems (Cathar's Crusade, Honor of the Pure, Mirari's Wake) are the highest-priority removal — they snowball.
- ETB-recurring creatures (Mulldrifter, Eternal Witness, Sun Titan) gain value when blinked.

INTERACTION RULES:
- Counterspells need a spell on the stack. NEVER cast Counterspell at sorcery speed.
- Burn at the face is usually worse than burn at creatures unless lethal.
- Holding removal "for something better" lets opp set up. If a threat is killing you, kill it now.
- Board wipes are the answer to wide boards; spot removal is the answer to single fatties.
- "Free" interaction (Force of Negation, Solitude evoke, Ephemerate) should be deployed liberally when the alternative is losing.

CONTROL DECK PLAY (UW Control, Esper, etc.):
- Don't sandbag answers when you're dying — Solitude evokes for free and chumps lethal.
- Snapcaster Mage is a 2/1 BODY with flashback upside; cast it as a flash blocker if life total demands.
- Pass with mana up even on safe turns to threaten counterspells.
- Wrath of God / Supreme Verdict only when opp board > your board — empty-board wipes are 4 mana for nothing.
- Counterspell what wins the game; let cantrips through.
- MATCHUP AWARENESS (May 14 audit): if MATCHUP line shows opponent has cast 0
  noncreature spells over many turns, your hard counterspells (Counterspell,
  Mana Drain, Negate, etc.) are DEAD this game. Pivot: use modal counters'
  draw/bounce modes, dig with Brainstorm/Ponder, deploy threats, or pitch
  counters to discard outlets (Looting, Faithless Looting). Don't hold
  reactive cards across 15+ turns waiting for a target that won't appear.
- If MODAL COUNTERS line is present, those cards have non-counter modes
  (Mystic Confluence = bounce/draw, Archmage's Charm = draw/exile) that
  work fine on empty stack. Use those modes when no spell threatens.

COMBAT MATH:
- Trample: assign lethal to each blocker, rest hits the player.
- Deathtouch: 1 damage is lethal to any creature — assign 1 to each blocker.
- First strike attacker vs vanilla blocker: blocker dies before dealing damage.
- Lifelink: lifegain happens simultaneously with damage. Race math changes a lot.
- Indestructible doesn't ignore 0 toughness or -X/-X counters.

=== END STABLE REFERENCE ===

You are an MTG coach producing a structured strategy memo for the active player.

Your output must be EXACTLY four labeled lines, in this order, with these four labels (no other text — no preamble, no closing, no headers, no analysis):

Win condition: ...
This turn: ...
Opp threats & answers: ...
Hold for opp turn: ...

EXAMPLE output (study the FORMAT — your output mirrors this exact shape):

Win condition: Reduce Rick to 0 with Korvold sacrifice triggers + token swarm; Goldspan Dragon closes in 2 attacks.
This turn: Cast Korvold (he's in command zone, 6 mana available, current tax 0). Sac the 1/1 Treasure to draw + lose Rick 1. Attack with Goldspan Dragon (5/5 flying) for 5.
Opp threats & answers: Rick has 2 untapped lands + Counterspell in hand from earlier scry. Your Vexing Shusher protects Korvold's cast.
Hold for opp turn: Hold Assassin's Trophy for Rick's Smothering Tithe — taxing his next spell is more value than killing a land now.

RULES (apply to your output, not your reasoning):
- Be specific to the actual board state. Name real cards on both sides.
- Total output must be UNDER 800 characters. Cut padding, not specifics.
- Output nothing except those four labeled lines — no scaffolding, no "First, assess…", no analysis text. The four labels are non-negotiable; emit them even when a section is short.
- Do not echo or paraphrase the STABLE STRATEGY REFERENCE section above."""

            # Phase 3: use the dedicated strategist client/model if wired
            # (deepseek-v4-pro for deep reasoning). Falls back to actor client if not set.
            strat_client = self.strategist_client or self.client
            strat_model = self.strategist_model or self.model

            # May 20 audit (#20): one-shot cache-prefix diagnostic per game so
            # the next batch can grep for cache-warmup drift. May 19's
            # "STABLE STRATEGY REFERENCE move-to-front" change only moved the
            # cache_hit median 65.8% → 68.1% (target 80%+). Surface the
            # actual prefix lengths + previews so we can compare across games.
            # May 23 audit (CRITICAL #6): previously `_cache_prefix_logged_for_game`
            # was on `self` (ClaudePlayer instance) which persists across games in
            # autoplay — the diagnostic fired once per PROCESS, not once per GAME.
            # Per-game scope: track which game ids we've already logged for.
            _game_key = id(game)
            _logged_games = getattr(self, '_cache_prefix_logged_games', set())
            if _game_key not in _logged_games:
                try:
                    sys_len = len(system)
                    user_len = len(board_summary)
                    sys_preview = (system[:80] or '').replace('\n', ' ')
                    user_preview = (board_summary[:80] or '').replace('\n', ' ')
                    print(f"[CACHE-PREFIX] strategist game-first call: "
                          f"system_len={sys_len} user_len={user_len} "
                          f"sys_first80='{sys_preview}' user_first80='{user_preview}'")
                except Exception:
                    pass
                _logged_games.add(_game_key)
                self._cache_prefix_logged_games = _logged_games
            # May 24 audit fix (CRITICAL #2): response-prefix injection.
            # Previous batches showed 60% of V4-Pro memos failed the labeled-
            # format positive-validation (953 nukes / 645 accepted in May 24
            # batch). Even after dropping reasoning_effort=high → medium in
            # May 23, V4-Pro keeps producing 4000+ char rambling prose that
            # doesn't lead with "Win condition:".
            #
            # The OpenAI-compat / DeepSeek "prefill" pattern: append an
            # assistant message that starts with the required prefix. The
            # model continues from that prefix, guaranteeing the first
            # label is present. We then prepend the prefix back to the
            # returned text so the validator sees "Win condition: ..." even
            # if the model only returned "..." (some providers echo the
            # prefix back, others don't — defensive prepend handles both).
            _PREFILL = "Win condition:"
            # Note: DeepSeek's beta endpoint supports a `prefix: True` field
            # on the assistant message to guarantee the model continues
            # rather than starting a new turn — but the OpenAI Python
            # client's ChatCompletionAssistantMessageParam doesn't accept
            # `prefix`, and the regular `/v1` endpoint we use already treats
            # a trailing assistant message as a prefill. So we omit it; the
            # defensive prepend at the response site covers any case where
            # the provider doesn't echo the prefix back in `content`.
            #
            # May 25 audit (CRITICAL F3): the unconditional assistant-tail
            # prefill broke in 4 Aminatou games when the strategist routed to
            # a native Anthropic client (no DeepSeek key configured) —
            # Anthropic returned `400 invalid_request_error: This model does
            # not support assistant message prefill. The conversation must
            # end with a user message.` Gate the prefill behind the same
            # adapter-shape check used for json_mode. The defensive prepend
            # at line ~902 still tags the returned memo with "Win condition:"
            # so the validator behaves the same either way.
            _adapter_supports_prefill = hasattr(strat_client.messages, '_log_tag')
            strat_messages = [{"role": "user", "content": board_summary}]
            if _adapter_supports_prefill:
                strat_messages.append({"role": "assistant", "content": _PREFILL})
            strat_kwargs = dict(
                model=strat_model,
                # V4-Pro with reasoning_effort=high spends most of the budget
                # on internal reasoning (returned in `reasoning_content`, not
                # `content`). At 500 tokens the prose memo was routinely
                # empty; 2000 leaves headroom for both the reasoning trace
                # and a 3-5 sentence memo. The retry path bumps to 4000.
                #
                # May 20 audit (#13): V4-Pro at max_tokens=2000 produced 7700-
                # 8200 char memos in 7 cases despite the "Aim for ~800 max"
                # prompt phrasing. The soft prompt cap doesn't constrain the
                # model; the server-side budget does. Drop to 1200 to enforce
                # a tighter ceiling while still leaving meaningful room for
                # V4-Pro's reasoning trace (which typically eats 600-900
                # tokens). Watch [STRATEGIST-LEN] cap_binding= ratio next
                # batch — if pure empty-memo rate spikes, bump back up.
                max_tokens=1200,
                system=system,
                messages=strat_messages,
            )
            # json_mode=False is an OpenAICompatibleAdapter-specific kwarg — only
            # pass it when the client is our adapter (has _log_tag on messages).
            # Anthropic's native client doesn't accept unknown kwargs.
            if hasattr(strat_client.messages, '_log_tag'):
                strat_kwargs['json_mode'] = False
                strat_kwargs['purpose'] = 'strategist'
                # July 20: adaptive degrade — after 2 deadman/hard-cap fires
                # in THIS game, remaining strategist calls run at
                # reasoning_effort=low. On a bad-DeepSeek day (July 12-13:
                # 248 fires across 139 games vs the 0-2 healthy baseline)
                # the deadman caps each hang at 90s, but without this the
                # game keeps re-paying that tax every strategist turn.
                # Per-game state, not adapter state — 25 concurrent games
                # share one adapter and a good game must not be degraded by
                # a bad one.
                # Aug 2 (the flash A/B): reasoning_effort is a V4-PRO knob —
                # the flash strategist would reject/ignore it, so the degrade
                # is model-gated. On flash, the deadman/hard-cap remains the
                # only (and hypothesis: rarely needed) backstop.
                if (game._strategist_fires >= 2
                        and 'pro' in (self.strategist_model or '')):
                    strat_kwargs['reasoning_effort'] = 'low'
                    if not game._strategist_degraded:
                        game._strategist_degraded = True
                        print(f"[STRATEGIST-DEGRADE] {game._strategist_fires} "
                              f"deadman/hard-cap fires this game — remaining "
                              f"strategist calls drop to reasoning_effort=low")
            # May 18 audit: route the strategist call through streaming when
            # the adapter supports it, with a deadman-timer watchdog that
            # closes the stream if no token arrives in DEADMAN_S seconds.
            # The crashed game's 28-min hang showed that non-streaming has
            # no kill switch — once a non-streaming request is in flight,
            # the server can think for an arbitrary time and we can't tell
            # it to stop. With streaming we close the socket on the deadman
            # timeout, which propagates back as an HTTP request-cancel.
            # Heartbeat note: streaming naturally keeps the event loop free
            # (we await each chunk), so we no longer need the to_thread
            # wrapper that the non-streaming path used to dodge Discord's
            # gateway heartbeat.
            # May 25 audit (F14): 8 fire events in batch (7 Hard cap + 1 Deadman)
            # vs the 0-2/batch healthy baseline — 4× overage. reasoning_effort was
            # already dropped high→medium on May 23, so the natural next lever is
            # the budget itself. 7 of 8 fires were Hard cap, meaning the model
            # was producing chunks steadily but the TOTAL duration exceeded 120s
            # (i.e. slow-but-not-stuck, not a reasoning death loop). Cut the
            # hard cap from 120s → 90s so tail-of-distribution slow calls
            # fail-fast at the cost of one degraded memo instead of soaking
            # the full 2-minute budget. Deadman stays at 60s — that one's
            # tuned for "model stopped responding," not slow throughput.
            # If the next batch shows fires DROPPING below 2 but memo
            # quality regressing, the call is to drop reasoning_effort
            # further (medium → low) rather than raise the cap back.
            DEADMAN_S = 60.0   # No chunk in 60s → kill the stream.
            STREAM_HARD_CAP_S = 90.0  # Total stream duration cap.

            async def _strategist_via_stream(kwargs_in):
                """Open a streaming call, watch with a deadman timer, return text/reasoning."""
                stream = await asyncio.to_thread(
                    lambda: strat_client.messages.stream(**kwargs_in)
                )

                async def _deadman_watchdog():
                    """Background task: poll inter-chunk delay, close on stall."""
                    while not getattr(stream, '_closed', False):
                        await asyncio.sleep(5)
                        try:
                            stalled = stream.seconds_since_last_chunk
                        except Exception:
                            return
                        if stalled > DEADMAN_S:
                            print(f"[STRATEGIST] Deadman fired: no chunk in "
                                  f"{stalled:.0f}s — closing stream")
                            game._strategist_fires += 1
                            await stream.close()
                            return

                watchdog = asyncio.create_task(_deadman_watchdog())
                stream_start = asyncio.get_event_loop().time()
                try:
                    async with stream:
                        async for _chunk in stream.text_chunks():
                            # Hard outer cap: even if chunks keep arriving but
                            # the total stream duration exceeds the budget,
                            # bail. Defends against a slow-but-steady drip
                            # that could otherwise tie up the strategist for
                            # the entire game.
                            if asyncio.get_event_loop().time() - stream_start > STREAM_HARD_CAP_S:
                                print(f"[STRATEGIST] Hard cap fired: {STREAM_HARD_CAP_S}s "
                                      f"exceeded — closing stream")
                                game._strategist_fires += 1
                                break
                finally:
                    watchdog.cancel()
                    try:
                        await watchdog
                    except (asyncio.CancelledError, Exception):
                        pass
                # June 10 audit (V30): a mid-stream error used to look like a
                # clean exhaustion — usage=None then crashed the usage
                # callback and the WHOLE memo (partial text included) was
                # discarded via the outer "graceful degradation" catch,
                # 14×/batch. Now: no text accumulated → raise so the
                # documented non-streaming fallback at the call site actually
                # fires; partial text → keep it (usage stays None; the
                # null-check in _track_usage skips accounting).
                _stream_err = getattr(stream, 'stream_error', None)
                if _stream_err is not None and not stream.full_text and not stream.full_reasoning:
                    raise RuntimeError(f"mid-stream error with no text accumulated: {_stream_err}")
                if _stream_err is not None:
                    print(f"[STRATEGIST] Mid-stream error after partial text "
                          f"({len(stream.full_text)} chars) — using partial memo, usage unavailable")
                return stream.full_text, stream.full_reasoning, stream.usage

            # Streaming path: use it when the adapter supports it (i.e. the
            # OpenAICompatibleAdapter which has `stream()` on its messages
            # namespace). Native Anthropic clients use a different streaming
            # interface — keep the old non-streaming path for them.
            #
            # May 23 audit (MAJOR #16): the bare `hasattr(messages, 'stream')`
            # check let Anthropic SDK calls through too. Anthropic's
            # `messages.stream()` returns a MessageStreamManager which doesn't
            # support `async with` (only sync `with`) — every call hit
            # "MessageStreamManager object does not support the asynchronous
            # context manager protocol" and fell back to non-streaming
            # (game_1507596001329025045:2220,2503). Detect the OpenAI-compat
            # adapter shape positively instead: it sets `_StreamingResponse`
            # attribute and we check for the `_OpenAIMessagesNamespace` class
            # name pattern.
            use_streaming = (
                hasattr(strat_client.messages, 'stream')
                and type(strat_client.messages).__name__ in ('_MessagesNamespace', 'OpenAIMessages')
            )
            if use_streaming:
                try:
                    text, reasoning, usage_obj = await _strategist_via_stream(strat_kwargs)
                    # Build an _AdaptedResponse-shaped object so downstream
                    # code (the empty-retry block, _track_usage) doesn't have
                    # to branch on streaming vs non-streaming.
                    class _StreamResponseShim:
                        def __init__(s, t, r, u):
                            class _Block:
                                def __init__(sb, tx): sb.text = tx
                            s.content = [_Block(t or r or "")]
                            s.reasoning_content = r or ""
                            s.usage = u
                    response = _StreamResponseShim(text, reasoning, usage_obj)
                except Exception as stream_err:
                    # If streaming itself errors out (network blip, server
                    # rejected stream=true, deadman closed the socket), fall
                    # back to non-streaming for THIS call. Better degraded
                    # output than no output.
                    print(f"[STRATEGIST] Streaming failed ({stream_err}); "
                          f"falling back to non-streaming for this call")
                    response = await asyncio.to_thread(
                        lambda: strat_client.messages.create(**strat_kwargs)
                    )
            else:
                # Non-streaming fallback (native Anthropic client).
                response = await asyncio.to_thread(
                    lambda: strat_client.messages.create(**strat_kwargs)
                )
            self._track_usage(response, model_override=strat_model)
            memo = response_text(response).strip()
            # Legacy V3-style <think>...</think> stripping. V4 doesn't emit
            # those tags (its reasoning lives in `message.reasoning_content`,
            # which the adapter already separates), but we keep this for any
            # third-party model that does inline thinking-mode markup.
            memo = re.sub(r'<think>.*?</think>\s*', '', memo, flags=re.DOTALL).strip()
            if '<think>' in memo and '</think>' not in memo:
                memo = re.sub(r'<think>.*$', '', memo, flags=re.DOTALL).strip()
            # May 24 audit fix: paired with the prefill at strat_messages —
            # prepend the prefix back if the provider didn't echo it.
            # DeepSeek's prefix-completion echoes the assistant prefix in
            # the response; OpenAI-vanilla doesn't. Detection: if the memo
            # doesn't already start with the prefix (after whitespace
            # stripping), prepend it so the validator's substring check at
            # _sanitize_memo (line ~1107) finds "win condition:".
            if memo and not memo.lower().lstrip().startswith(_PREFILL.lower()):
                memo = f"{_PREFILL} {memo}".strip()

            # If memo is still empty, retry once with a much larger budget.
            # V4-Pro with reasoning_effort=high spends most of the budget on
            # internal reasoning before producing prose, so a 500-token cap
            # often returns "" in `.content`. Bumping to 4000 leaves room for
            # both the reasoning and the actual memo.
            if not memo:
                raw_len = len(response_text(response))
                reasoning_len = len(getattr(response, 'reasoning_content', '') or "")
                print(f"[STRATEGIST] Empty memo (raw={raw_len}, reasoning={reasoning_len}) "
                      f"— retrying with larger budget")
                strat_kwargs['max_tokens'] = 4000
                try:
                    # Retry also uses streaming + deadman when available, so a
                    # second call won't re-stall for 28 minutes.
                    if use_streaming:
                        text, reasoning, usage_obj = await _strategist_via_stream(strat_kwargs)
                        class _StreamResponseShim:
                            def __init__(s, t, r, u):
                                class _Block:
                                    def __init__(sb, tx): sb.text = tx
                                s.content = [_Block(t or r or "")]
                                s.reasoning_content = r or ""
                                s.usage = u
                        response = _StreamResponseShim(text, reasoning, usage_obj)
                    else:
                        response = await asyncio.to_thread(
                            lambda: strat_client.messages.create(**strat_kwargs)
                        )
                    self._track_usage(response, model_override=strat_model)
                    memo = response_text(response).strip()
                    memo = re.sub(r'<think>.*?</think>\s*', '', memo, flags=re.DOTALL).strip()
                    if '<think>' in memo and '</think>' not in memo:
                        memo = re.sub(r'<think>.*$', '', memo, flags=re.DOTALL).strip()
                    # Retry path: same prefix defense as first call.
                    if memo and not memo.lower().lstrip().startswith(_PREFILL.lower()):
                        memo = f"{_PREFILL} {memo}".strip()
                except Exception as retry_e:
                    print(f"[STRATEGIST] Retry failed: {retry_e}")
                    memo = ""

            # Sanitize strategist output before injecting into actor prompt.
            # The strategist sometimes returns JSON-shaped prose, markdown
            # fences, or headers that the actor can echo back into its JSON
            # action stream (leaking to Discord as a fake action bullet).
            # Defence-in-depth: strip anything that looks like executable JSON
            # or code, normalise whitespace, cap length.
            def _sanitize_memo(raw: str) -> str:
                # May 18 audit (density-check guard): if the memo contains 2+
                # distinct system-prompt scaffolding phrases, the model is
                # paraphrasing/regurgitating the prompt instead of producing
                # strategic content. In the May 17 crashed-game log, the
                # memo "Start with a concrete board observation. Max 500
                # characters. Write tight prose. Let's analyze the board
                # state. We are pl…" contained 4 distinct scaffolding tokens
                # but slipped past the per-line and per-sentence passes below
                # (V4-Pro emitted them as one long period-separated line, and
                # the leading "Start with…" prefix got stripped while later
                # scaffolding survived). This density gate fires BEFORE the
                # other passes — if it trips, nuke the memo entirely and let
                # the caller fall back to the previous turn's memo.
                lowered = (raw or '').lower()
                scaffolding_markers = _MEMO_SCAFFOLDING_MARKERS
                hits = sum(1 for m in scaffolding_markers if m in lowered)
                if hits >= 2:
                    print(f"[STRATEGIST] Density-check nuke: {hits} scaffolding "
                          f"markers in {len(raw)}-char memo — discarding")
                    return ""
                cleaned = raw
                # Strip fenced code blocks entirely — don't let actor echo them.
                cleaned = re.sub(r'```[\s\S]*?```', '', cleaned)
                # Strip bare JSON objects/arrays (defensive — they should not
                # appear in a prose memo; if they do, they are probably a
                # model-confusion artifact).
                cleaned = re.sub(r'\{\s*"[\s\S]*?\}\s*', '', cleaned)
                cleaned = re.sub(r'\[\s*\{[\s\S]*?\}\s*\]', '', cleaned)
                # Apr 29 audit: strip meta-preamble that V4-Pro reasoning_effort=high
                # sometimes emits ("We need to produce a concise strategy memo
                # for the active player. Given the board state...", "Here is the
                # strategy:", etc.). These waste 200-1200 chars of memo budget
                # and confuse the actor.
                meta_patterns = [
                    r'^\s*we (?:need to|should|must|will) (?:produce|write|create|provide).*?\.',
                    r'^\s*(?:here is|here\'?s)\s+(?:the|a|my)\s+(?:strategy|memo|plan|analysis).*?:',
                    r'^\s*(?:let me|i\'?ll|i will)\s+(?:think|analyze|consider|reason).*?\.',
                    r'^\s*(?:the|this)\s+memo\s+(?:should|will|covers).*?\.',
                    r'^\s*okay,?\s+',
                    r'^\s*(?:first|to start|to begin),?\s+(?:i\'?ll|let\'?s|we\'?ll)\s+',
                    r'^\s*(?:strategy|memo|analysis)\s*:\s*',
                ]
                for pat in meta_patterns:
                    cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE | re.DOTALL).lstrip()

                # May 7 audit: strategist echoed system prompt rules verbatim
                # in 4/5 sampled games ("Output ONLY the memo prose. No JSON,
                # no markdown headers, no list bullets...", "The memo must
                # cover: win condition, priority plays..."). Strip these meta-
                # instruction lines wherever they appear. Apply BEFORE the
                # whitespace collapse so we can match line-anchored patterns.
                #
                # Pattern: any line starting with one of these instructional
                # phrases is discarded entirely. The strategist's actual
                # strategic prose never starts with these phrases.
                rule_leak_starts = [
                    'output only', 'output ',
                    'the memo must', 'the memo should', 'this memo',
                    'no json', 'no markdown', 'no list', 'no bullets',
                    'do not ', "don't ",
                    'cover exactly', 'cover the following',
                    'be specific about',
                    'begin with', 'start with',
                    'rules:', 'output rules', 'response format',
                    'instructions:',
                    'write tight prose',
                    # May 16 audit: V4-Pro echoes the prompt's HARD LIMIT line
                    # and the section headers from the STABLE STRATEGY REFERENCE
                    # back as memo content (~8% of strategist outputs).
                    'hard limit', 'hard cap', 'maximum 500',
                    'first, assess', 'first assess',
                    'win conditions by archetype', 'win conditions:',
                    'prioritize in this order', 'prioritize:',
                    'threat evaluation:', 'interaction rules:',
                    'control deck play', 'combat math:',
                    'stable strategy reference', 'stable reference',
                    '=== ',  # any section header echo
                ]
                kept_lines = []
                for line in cleaned.split('\n'):
                    stripped = line.strip()
                    if not stripped:
                        kept_lines.append(line)
                        continue
                    # Strip leading markdown / bullets before checking the prefix.
                    bare = re.sub(r'^[\s\-\*•\d\.\)]+', '', stripped).lower()
                    if any(bare.startswith(prefix) for prefix in rule_leak_starts):
                        continue
                    kept_lines.append(line)
                cleaned = '\n'.join(kept_lines).strip()
                # Collapse whitespace
                cleaned = re.sub(r'\s+', ' ', cleaned).strip()

                # May 13 audit: V4-Pro with reasoning_effort=high frequently
                # emits its entire chain-of-thought as one long paragraph
                # ending with the actual strategy. The line-anchored passes
                # above don't help because the whole memo is one line. Strip
                # leading *sentences* that look like meta-commentary about
                # the task. The first ~50% of memos in the May 11 batch
                # started with "We are asked...", "We need to give...", "We
                # are instructed...", "The briefing should...", "Tight prose,
                # naming specific cards. Cover win condition..." etc.
                meta_sentence_re = re.compile(
                    r'^\s*('
                    r'we\s+(?:are|need|must|should|will|have|can|may)\b'
                    r'|we\'(?:re|ve|ll|d)\b'
                    r'|i\s+(?:need|will|am|have|should|must|think|would|can)\b'
                    r'|i\'(?:ll|m|d|ve|re)\b'
                    r'|let(?:\s+me|\s+us)\b'
                    r'|let\'s\b'
                    r'|okay,?\s'
                    r'|alright,?\s'
                    r'|first,?\s+(?:i|let|we|to|i\'ll)\b'
                    # May 16 audit: V4-Pro restates the task as "First, assess
                    # the board state" / "First, consider win conditions" /
                    # similar imperatives. These leak the prompt's PRIORITIZE
                    # ordering back as memo content.
                    r'|first,?\s+(?:assess|consider|evaluate|note|observe|review|determine|identify|address)\b'
                    r'|to\s+(?:start|begin|address|tackle|approach),?\b'
                    r'|the\s+(?:briefing|prompt|instruction|memo|format|user|task|request|coach|response|output)\b'
                    r'|the\s+active\s+player\s+is\b'
                    r'|this\s+memo\b'
                    r'|here(?:\s+is|\s+are)\b'
                    r'|here\'s\b'
                    r'|so,?\s+(?:i|let|we|now)\b'
                    r'|tight\s+prose\b'
                    # May 14 audit (A8): V4-Pro echos these system-prompt
                    # imperatives as the memo body 10-15% of the time. The
                    # original regex caught lowercase first-person but missed
                    # the imperative second-person leak.
                    r'|write\s+tight\s+prose\b'
                    r'|cover\s+(?:win\s+condition|the\s+following|specific|exactly|what)\b'
                    r'|cover(?:ing)?\s+win\s+condition\b'
                    r'|begin\s+with\s+a?\s*concrete\b'
                    r'|focus\s+on\s+win\s+condition\b'
                    r'|use\s+tight\s+prose\b'
                    r'|the\s+given\s+state\s*:?'
                    r'|the\s+(?:board|scenario|situation|provided\s+board)\s+(?:state|is|provides|gives)\b'
                    r'|naming\s+specific\b'
                    r'|active\s+player(?:\s+is|\'s)\b'
                    r')',
                    re.IGNORECASE,
                )
                # Sentence split: keep "Mr. X" intact-ish by splitting on
                # period+space+capital, which is the common case here.
                sentences = re.split(r'(?<=[\.\!\?])\s+(?=[A-Z])', cleaned)
                while sentences and meta_sentence_re.match(sentences[0]):
                    sentences.pop(0)
                # May 18 audit: the original loop only popped LEADING meta
                # sentences. V4-Pro embeds the prompt-echo mid-paragraph
                # ("…Now let's analyze. Start with a concrete board
                # observation. Max 500 characters. The opponent has 3
                # Islands…"). Apply the rule_leak_starts prefix filter
                # per-sentence — same prefix list, just one-sentence-at-
                # a-time instead of one-newline-at-a-time. Catches the
                # case where V4-Pro emits one line of period-separated
                # sentences and the line-anchored pass misses interior
                # scaffolding.
                filtered_sentences = []
                for s in sentences:
                    s_stripped = s.strip()
                    if not s_stripped:
                        continue
                    s_bare = re.sub(r'^[\s\-\*•\d\.\)]+', '', s_stripped).lower()
                    if any(s_bare.startswith(prefix) for prefix in rule_leak_starts):
                        continue
                    filtered_sentences.append(s)
                cleaned = ' '.join(filtered_sentences).strip()
                if _memo_has_labeled_task_reference(cleaned):
                    print("[STRATEGIST] Labeled task-reference nuke \u2014 "
                          f"discarding {len(cleaned)}-char memo")
                    return ""
                # May 20 audit (#12) — POSITIVE validation. Previous code only
                # rejected long essay-mode memos (>600 chars) without labels.
                # The May 20 batch showed 30% of memos still lacked the labeled
                # format even when short, leaking scaffolding-paraphrase content
                # like "We see the board state. First, determine what we can do
                # this turn." (game_1506604518060367912 etc.). Switch to a
                # positive-validation rule: REQUIRE `Win condition:` AND
                # `This turn:` together. If either is missing, return empty so
                # the caller falls back to the previous labeled memo.
                cleaned_lower = cleaned.lower()
                has_win = 'win condition:' in cleaned_lower
                has_turn = 'this turn:' in cleaned_lower
                section_labels_present = sum(1 for label in
                                              ('win condition:', 'this turn:',
                                               'opp threats', 'hold for opp turn:')
                                              if label in cleaned_lower)
                # May 25 audit fix (F12): the May 24 loosening to `has_win`-only
                # was too permissive — the prefill GUARANTEES has_win=True for
                # every non-empty memo, so the check was effectively "memo is
                # non-empty." 1100/1501 memos in the May 25 batch leaked
                # scaffolding ("Win condition: We need to output exactly four
                # labeled lines. Let's assess the current board state to fill
                # each line.") past this gate. New rule: require Win condition:
                # AND at least one more section label. That tolerates the case
                # where V4-Pro continues from the prefill with substantive prose
                # under a second header (the May 24 motivating case), but
                # rejects memos that ONLY have the prefilled label, which are
                # almost always scaffolding regurgitation. The density-check
                # above catches the most common shapes before we get here; this
                # is the second line of defense.
                if not has_win or section_labels_present < 2:
                    print(f"[STRATEGIST] Positive-validation nuke: insufficient labels "
                          f"(win={has_win}, this_turn={has_turn}, total_labels={section_labels_present}) "
                          f"— discarding {len(cleaned)}-char memo")
                    return ""
                # May 18 audit: bumped cap from 600 → 800 chars to give the new
                # structured 4-section format room (Win / This turn / Opp
                # threats / Hold). V4-Pro at 600 was clustering at the cap
                # exactly because the prompt's "HARD LIMIT 500" reframed as a
                # target to hit; the new prompt asks for "~800 max" so the
                # cap is intentionally a ceiling, not a target.
                #
                # Aug 8 (queue R1): 800 → 1000. Two consecutive flash-
                # strategist batches ran cap_binding=yes at 41.5% / 42.7% —
                # and unlike V4-Pro's 4.2-4.6k scaffolding rambles, the
                # flash memos running 800-1000 raw are GOOD content being
                # chopped (board-grounded, format-perfect on eyeball). The
                # prompt phrasing stays at "~800" deliberately (the model's
                # target is fine; only the truncation moves). A/B
                # expectation for the next batch: cap_binding=yes (raw >
                # 1000) drops below ~15% with no scaffolding-nuke increase;
                # if quality visibly degrades instead, revert this cap.
                return cleaned[:1000]

            # May 18 audit: capture pre-sanitize and pre-truncate lengths so a
            # post-batch grep can answer "is the 800-char cap binding?". If
            # `raw_len > 800` regularly across the batch, the model is
            # producing useful content we're chopping → consider bumping the
            # cap. If `raw_len` clusters under 800, the cap isn't bottlenecked.
            _memo_raw_len = len(memo or '')
            memo = _sanitize_memo(memo)
            _memo_post_sanitize_len = len(memo or '')

            # Only overwrite the existing memo if we got a non-empty new one —
            # stale strategy beats empty strategy. May 14 audit (A8): also
            # reject memos that survived sanitizing but are below ~80 chars
            # (truncated or sanitizer-stripped to almost nothing). Examples
            # from the May 14 batch: 16-char "Opponent has a 4...", 44-char
            # fragments. These are useless to the actor and worse than the
            # stale memo from last turn.
            if memo and len(memo) >= 80:
                _record_strategy_memo_result(game, accepted=True)
                self._strategy_memo = memo[:1000]  # Cap length (matches sanitizer; R1 Aug 8)
                game._strategy_memo = self._strategy_memo
                # Emit the kept memo update + a separate measurement line so a
                # batch-level grep for [STRATEGIST-LEN] can build a histogram.
                # raw_len = model's raw output (pre-sanitize). post_sanitize =
                # after sanitizer (pre-cap). final = what the actor will see.
                # If raw > 800 we're chopping useful content; if raw ≤ 800
                # the cap is non-binding and the model is hitting whatever
                # target the prompt sets.
                _final_len = len(self._strategy_memo)
                print(f"[STRATEGIST] Strategy memo updated ({_final_len} chars): {self._strategy_memo[:120]}...")
                # May 20 audit: post_sanitize is captured AFTER the sanitizer
                # truncate so it can never exceed the cap — cap_binding=yes
                # was mathematically impossible. The cap binds whenever the
                # model's raw output exceeds it (1000 since Aug 8 / R1),
                # regardless of sanitizer outcome.
                print(f"[STRATEGIST-LEN] raw={_memo_raw_len} "
                      f"post_sanitize={_memo_post_sanitize_len} "
                      f"final={_final_len} "
                      f"cap_binding={'yes' if _memo_raw_len > 1000 else 'no'}")
            else:
                # June 11 audit: game 1514621840440561704 paid for another
                # 4,521-character memo that density validation discarded.
                # Back off after repeated rejects instead of buying the same
                # unusable result every turn.
                _record_strategy_memo_result(game, accepted=False)
                reason = "empty" if not memo else f"too short ({len(memo)} chars after sanitize)"
                print(f"[STRATEGIST] Memo rejected ({reason}) — keeping previous "
                      f"({len(self._strategy_memo)} chars)")
                print(f"[STRATEGIST-LEN] raw={_memo_raw_len} "
                      f"post_sanitize={_memo_post_sanitize_len} "
                      f"final=0 cap_binding=rejected")
        except Exception as e:
            print(f"[STRATEGIST] Strategy update failed (graceful degradation): {e}")
            # Don't clear old memo — stale strategy is better than no strategy

    def _strip_think_tags(self, text: str, context: str = "action") -> str:
        """Strip <think>...</think> scratchpad from DeepSeek responses.

        DeepSeek likes to reason before answering. We tell it to put reasoning
        in <think> tags, then return only the JSON after. This method strips
        the thinking and returns just the actionable text.
        Also strips markdown reasoning that appears before any JSON.

        context — controls the prose-to-pass fallback format when no JSON is found:
          "action"    → '{"type": "pass"}'
          "plan"      → '[{"type": "pass"}]'
          "resolve"   → '{"explanation": "No action determined.", "actions": [{"action": "no_action", "reason": "AI returned prose only"}]}'
          "attackers" → '[]'
          "blocks"    → '{}'
        """
        # Strip ALL <think>...</think> blocks (there may be multiple)
        think_matches = list(re.finditer(r'<think>(.*?)</think>\s*', text, re.DOTALL))
        if think_matches:
            for m in think_matches:
                thinking = m.group(1).strip()
                print(f"{self.provider_tag} [THINK] {thinking[:150]}...")
            text = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()
        # Handle unclosed <think> tag (DeepSeek sometimes forgets </think>)
        elif '<think>' in text:
            think_start = text.index('<think>')
            thinking = text[think_start + 7:].strip()
            print(f"{self.provider_tag} [THINK-UNCLOSED] {thinking[:150]}...")
            # Try to find JSON after the unclosed think block
            json_after = re.search(r'\{"\w', text[think_start:])
            if json_after:
                text = text[think_start + json_after.start():].strip()
            else:
                text = ""  # No JSON found — will fall through to JSON-FAIL

        # Find real JSON objects/arrays — NOT mana cost braces like {3}{G}.
        # JSON objects start with {" (property name in quotes).
        # JSON arrays start with [ followed by " or { or [.
        # MTG mana costs are {W}, {U}, {3}, etc. — single char/digit, no quotes.

        # Try to find a JSON object: {"type": ... — handles whitespace between { and "
        json_obj = re.search(r'\{\s*"[a-zA-Z]', text)
        # JSON array: [ followed by [, {, ", or digit
        json_arr = re.search(r'\[\s*[\["{0-9]', text)

        candidates = []
        if json_obj:
            candidates.append(json_obj.start())
        if json_arr:
            candidates.append(json_arr.start())

        if candidates:
            start = min(candidates)
            if start > 0:
                preamble = text[:start].strip()
                if preamble:
                    print(f"{self.provider_tag} [STRIP] Removed {len(preamble)} chars of preamble before JSON")
                text = text[start:]
        else:
            # No JSON found at all — DeepSeek returned pure prose.
            # Log for debugging, then return a safe fallback so the parse succeeds
            # and the turn advances instead of wasting a retry cycle.
            print(f"{self.provider_tag} [STRIP] No JSON object/array found in response: {text[:150]}")
            print(f"{self.provider_tag} [STRIP] Falling back to pass action (context={context})")
            if context == "plan":
                return '[{"type": "pass"}]'
            elif context == "resolve":
                return '{"explanation": "No action determined.", "actions": [{"action": "no_action", "reason": "AI returned prose only"}]}'
            elif context == "attackers":
                return '[]'
            elif context == "blocks":
                return '{}'
            else:  # "action" and any unknown context
                return '{"type": "pass"}'

        return text

    def _safe_json_loads(self, text: str, fallback=None):
        """Parse JSON tolerantly — handles 'Extra data' from DeepSeek.

        DeepSeek often outputs valid JSON followed by explanation text:
            {"type": "cast", "card": "Signet"} because I need mana...
        json.loads() throws 'Extra data' on this. raw_decode() reads just
        the first complete JSON object/array and ignores the rest.

        Returns parsed object, or fallback if parsing fails entirely.

        When fallback is a list (plan_turn context) and DeepSeek returns a single
        action dict like {"reasoning": "...", "type": "pass"}, wrap it in a list
        so plan_turn gets the expected format without a retry cycle.
        """
        text = text.strip()
        if not text:
            return fallback

        # Fast path: standard json.loads (handles 90% of cases)
        try:
            result = json.loads(text)
            # [FIX-1] DeepSeek sometimes returns a single-action dict when plan_turn
            # expects a list. If caller expects a list (fallback is list) and result
            # is a dict with a "type" key, wrap it — saves ~9.5 retry cycles per game.
            if isinstance(fallback, list) and isinstance(result, dict) and 'type' in result:
                print(f"{self.provider_tag} [PLAN-WRAP] Single-action dict wrapped in list: type={result.get('type')}")
                return [result]
            return result
        except json.JSONDecodeError as e:
            if 'Extra data' not in str(e):
                # Not an extra-data error — try raw_decode as last resort
                pass
            # Fall through to raw_decode

        # Slow path: raw_decode reads first complete JSON value
        try:
            decoder = json.JSONDecoder()
            result, end_idx = decoder.raw_decode(text)
            trailing = text[end_idx:].strip()
            if trailing:
                print(f"{self.provider_tag} [JSON-TRIM] Ignored {len(trailing)} chars after JSON")
            # [FIX-1] Same single-action dict wrapping for raw_decode path
            if isinstance(fallback, list) and isinstance(result, dict) and 'type' in result:
                print(f"{self.provider_tag} [PLAN-WRAP] Single-action dict wrapped in list (raw_decode): type={result.get('type')}")
                return [result]
            return result
        except (json.JSONDecodeError, ValueError):
            pass

        # Last resort: if text starts with { but isn't valid JSON,
        # it might be truncated. Don't error silently.
        if fallback is not None:
            print(f"{self.provider_tag} [JSON-FAIL] Could not parse: {text[:100]}")
            return fallback
        raise json.JSONDecodeError(f"No valid JSON found", text, 0)

    def _check_circuit_breaker(self, error: Exception) -> bool:
        """Track API failures and trip circuit breaker after 5 consecutive failures.
        Returns True if the circuit breaker has tripped (API is disabled)."""
        self._consecutive_failures += 1
        error_str = str(error)
        if self._consecutive_failures >= 5 and not self._api_disabled:
            self._api_disabled = True
            print(f"[CIRCUIT-BREAKER] API disabled after {self._consecutive_failures} consecutive failures. "
                  f"Last error: {error_str[:200]}")
        return self._api_disabled
    
    def _build_decision_system_prompt(self) -> str:
        """Static instruction text for decide_action — sent once as system message
        in conversation mode, or inlined in single-shot mode."""
        return """RESPOND WITH JSON ONLY. Your first character must be `{`. No explanation, no preamble, no text after the object.

You are playing Magic: The Gathering. Analyze the game state and decide your action.

MANA COSTS - THIS IS IMPORTANT:
- Generic mana (numbers like {2}, {3}, {4}) can be paid with ANY color!
- Colored mana ({W}, {U}, {B}, {R}, {G}) MUST be that specific color
- Example: Simic Signet costs {2} - you can pay this with 2 red mana, 2 green mana, or any mix!
- Example: Kodama's Reach costs {2}{G} - you need 3 total mana, but at least 1 MUST be green
- If you have 2{R} available, you CAN cast a {2} cost card!

STRATEGIC ADVICE - DON'T WASTE CARDS:
- DON'T cast pump spells (+X/+X effects like Overrun, Return of the Wildspeaker's second mode) if you control NO creatures!
- DON'T cast draw spells that depend on creature power (like Return of the Wildspeaker's first mode) if you have no creatures with power!
- Hold value cards until they'll actually do something meaningful
- Ramp and card draw are usually better early; big creatures are better once you have mana
- Equipment is useless without creatures to equip
- Permanents marked (T) on the battlefield are TAPPED — don't try to activate them again this turn!

DECISION QUALITY — AVOID COMMON MISTAKES:
- BOARD WIPES (Wrath, Damnation, Blasphemous Act): only cast if opponent's board is stronger than yours, OR if you can rebuild faster. Wiping your own better board is a big swing against you.
- TARGETED REMOVAL (Doom Blade, Path, Swords): only cast if there is a meaningful target — never burn removal on a 1/1 token if a real threat is on the board, and never cast removal with NO opposing creatures.
- COUNTERSPELLS: only legal in response to a spell on the stack. NEVER include a counterspell in your main-phase plan — the stack is empty during your main phase, so the spell has no legal target and the engine will reject it. This applies to Counterspell, Swan Song, Mana Leak, Fierce Guardianship, Negate, Force of Will, Cyclonic Rift's normal mode, and every other "counter target spell" card. Hold {U}{U} open for opponent's turn instead. (You'll get a separate prompt at instant speed if a stack response is needed.)
- FLASH CREATURES WITH "EXILE/COUNTER ON ETB" (Spell Queller, Mystic Snake, Frilled Mystic, Voidmage Husher): these are designed to be cast as a *response* to an opponent's spell. Casting them in your own main phase with an empty stack still puts the body onto the battlefield, but the ETB trigger fizzles — you've paid the higher mana cost for a vanilla flash creature when you could have held them for an opposing spell. Prefer to keep mana up and respond at instant speed unless you genuinely need the body on board this turn (lethal pressure, blocker for a flying threat next turn, etc.).
- X-COST SPELLS (Walking Ballista, Hydroid Krasis, Blue Sun's Zenith): set X to a meaningful value. Casting X=0 wastes the card. If you can only afford X=1 and the effect needs more to matter, hold it.
- DISCARD (Liliana of the Veil, Smallpox, end-step discard): when forced to discard, drop your highest-CMC unplayable card or a redundant land — never discard your only win condition or a card you can cast next turn.
- LIFE AS A RESOURCE: at low life (≤5) treat your life total like cards in hand. Skip optional life payments (fetchlands, shocklands' 2-life option, Dark Confidant flips, Phyrexian mana) unless the payoff is decisive.
- TUTORS (Demonic Tutor, Vampiric, Mystical): include `"tutor_card": "<exact card name>"`; name a card that wins or stabilizes immediately, not generic ramp. SPLIT-DESTINATION tutors (Jarad's Orders — one card to hand, one to graveyard): use `"tutor_to_hand": "<name>"` and `"tutor_to_graveyard": "<name>"` so each search gets its own choice (put the reanimation target in the graveyard, the enabler in hand).
- FLASHBACK GRANTERS (Snapcaster Mage, Lurrus, Past in Flames, Mizzix's Mastery): when you grant flashback to an instant/sorcery in your graveyard, follow through THIS TURN — include the flashback cast in the same plan if you have mana for both. Snapcaster {1}{U} + flashback Lightning Bolt {R} = 3 mana for 3 damage and a body. Skipping the flashback wastes the Snapcaster's whole reason to exist (granted ability ends at end of turn, the spell stays in your graveyard but unused). If you can't afford both, hold the granter for a turn you can. Same logic for Lurrus's once-per-turn permanent recursion — recur a value piece, don't waste the slot.
- DON'T SANDBAG WHEN YOU'RE DYING (control decks especially): if your life is ≤5 and you have a body that can BLOCK in hand (Snapcaster Mage, Spell Queller, Solitude evoke, Watcher in the Mist, Reflector Mage — any flash creature, any 0-mana evoke), DEPLOY IT. Holding "for value" while taking lethal next turn is a known control-deck losing pattern. Evoke costs are FREE alternative casts — Solitude evokes for {0} and exiles a creature; Endurance evokes to shuffle graveyards; Grief evokes to discard. At 2 life vs a 4-power attacker, evoking Solitude to exile the attacker IS the play. Free spells (Force of Negation, Foil, alt-cost counterspells) are also free chumpings if cast on creatures, and free protection on your turn.
- WHEN HOLDING REMOVAL: ask "what gets worse if I wait one turn?" If opp has lethal damage on board or a snowballing anthem (Cathar's Crusade, Sword of Feast and Famine), removal NOW is correct. If opp is empty-handed and tapped out, holding can be fine. Default to action-now over inaction.
- BURN SPELLS: burn at the face is the right call when you can kill the opponent within 2-3 turns. Burn at a creature is the right call when (a) the creature kills you faster than your clock kills them, or (b) the creature has lifelink/recurring value (Vendilion Clique). NEVER face-burn when a single creature can kill you in 1 turn unblocked.

Also consider:
- Cards in hand that you can actually cast with your available colors
- Whether you've already played a land this turn
- Board state and threats

STRATEGIST MEMO INTEGRATION:
- A separate strategist analyzed the board state on the previous turn and produced a memo (shown in the user message between `=== STRATEGY ===` or `=== STRATEGIST MEMO ===` markers if present).
- The memo is a structured 4-section briefing with labeled lines:
    Win condition: <how this deck closes the game>
    This turn: <what to cast / activate / attack with this turn>
    Opp threats & answers: <opponent threats and your answers>
    Hold for opp turn: <what to keep mana up for>
  Read each label and prefer the plays named in `This turn:`. Prefer holds named in `Hold for opp turn:` (don't burn mana on a card it tells you to save).
- If the memo names a specific card to cast/activate THIS TURN and that card appears in CASTABLE NOW or ACTIVATABLE, prefer that play.
- Specifically watch for ACTIVATION instructions like "tap Priest of Forgotten Gods sacrificing Reassembling Skeleton", "activate Yawgmoth sacrificing X", "use Korvold's tap ability". When the memo names a specific activation, include that action in your plan with `{"type": "activate", "permanent": "<name>", "ability": <idx>}` — don't just cast and pass.
- If the memo says "hold X" or "save Y for next turn", don't waste X/Y this turn.
- If the memo's recommendation conflicts with CASTABLE NOW (memo names a card that isn't castable), prefer CASTABLE NOW — the memo may be one-turn stale.
- May 16 audit: the strategist's tactical recommendations were being ignored in ~30% of turns where they were given. Treat memo instructions as HIGH-PRIORITY guidance, not optional advice.

TURN STRUCTURE:
- During Main Phase, you can play lands, cast spells, and activate abilities
- When you're done casting spells, pass to move to combat
- You'll be asked separately which creatures to attack with (don't worry about attacking now)
- After combat, you get another Main Phase to cast more spells

Respond with a JSON action. Examples:
{"type": "play_land", "card": "Forest"}
{"type": "cast", "card": "Lightning Bolt", "target": "opponent"}
{"type": "cast", "card": "Counterspell", "target": "stack_top"}
{"type": "cast", "card": "Beanstalk Giant", "adventure": "Fertile Footsteps"}
{"type": "cast", "card": "Demonic Tutor", "tutor_card": "Craterhoof Behemoth"}
{"type": "cast", "card": "Primal Command", "modes": [2, 4], "target": ["Sol Ring"], "tutor_card": "Craterhoof Behemoth"}
{"type": "suspend", "card": "Rift Bolt"}
{"type": "foretell", "card": "Quakebringer"}
{"type": "graveyard_activate", "card": "Angel of Sanctions", "mechanic": "embalm"}
{"type": "activate", "permanent": "Chandra, Pyromaster", "ability": 0}
{"type": "activate", "permanent": "Nicol Bolas, Dragon-God", "ability": 1}
{"type": "resolve", "description": "Mystic Sanctuary ETB — put Counterspell on top of library"}
{"type": "pass"}

IMPORTANT ACTIONS:
- ACTIVATE: Use this for planeswalker loyalty abilities! "ability": 0 = first ability (usually +), 1 = second, 2 = ultimate. Also use for any permanent with activated abilities (tap abilities, sacrifice abilities, etc.)
- RESOLVE: Use this when an ETB trigger, ability, or effect needs manual resolution. If you see a message like "Use !resolve" or "Use !judge" for an unresolved effect, use the resolve action to handle it. Describe what should happen.

MANA-COST AWARENESS FOR ACTIVATIONS:
- When planning a turn, each cast/activate action consumes mana from your remaining pool. Do NOT plan multiple activations whose combined cost exceeds your total available mana.
- Example: with 4 total mana, activating Rhys the Redeemed ({2}{G/W}, {T}: create token) takes 3 mana. You can't ALSO activate it again in the same plan unless you have 7+ mana. The engine will reject the second activation.
- This applies especially to repeatable token-makers (Rhys, Westvale Abbey, Bitterblossom isn't activated, but tokens-from-graveyard cards are): plan ONE activation per turn unless you have explicit mana for more.

NOTE: For adventure cards, use the "adventure" key to cast the adventure half (sorcery/instant). The adventure half has a different (usually cheaper) mana cost.

RESPONSE FORMAT — a single JSON object with the action keys only (no "reasoning" field — keep output minimal):
{"type": "play_land", "card": "Forest"}

ONLY output cast/play cards that are listed in YOUR HAND above. A `tutor_card` is the explicit exception: it must name a real card you intend to find in your library. Do NOT invent card names.

CRITICAL: You MUST end your response with a valid JSON object. If you are uncertain what to do, output {"type": "pass"} — never output prose without JSON. A response with no JSON action will be treated as a pass automatically, wasting your turn.

IMPORTANT: Output ONLY the JSON object. No text before or after it."""

    def _build_state_message(self, game: GameState, player_index: int,
                             mana_str: str, total_mana: int,
                             castable_hint: str, castable_section: str,
                             state_desc: str, hand_desc: str,
                             last_error: str = None) -> str:
        """Build a full state user message for the first call in a conversation,
        or for single-shot mode."""
        player = game.players[player_index]
        error_section = ""
        if last_error:
            error_section = f"\n\u26a0\ufe0f YOUR PREVIOUS ACTION FAILED: {last_error}\nPlease try a different action or pass if no valid plays are available.\n"

        # May 14 audit (A8): inject strategist memo into the per-action
        # decide_action path. Previously the memo only reached plan_turn,
        # so when plan_turn fell back to per-action mode (~20-25% of turns
        # based on the May 14 batch), the actor had no strategic context.
        strategy_memo = getattr(game, '_strategy_memo', '') or getattr(self, '_strategy_memo', '')
        memo_section = ""
        if strategy_memo:
            memo_section = (
                f"\n=== STRATEGIST MEMO (from earlier turn \u2014 may be stale) ===\n"
                f"{strategy_memo}\n"
                f"==========================================================\n"
            )

        # Build explicit hand list for prominence (reduces hallucinated card names)
        hand_card_names = [c.name for c in player.hand]
        hand_list = ", ".join(hand_card_names) if hand_card_names else "(empty)"

        # Surface persistent illegal-cast blocks so the AI doesn't keep
        # proposing the same color-identity-violating card every turn.
        # Cleared at end-of-game; persists across turns since the violation
        # doesn't go away.
        blocklist = getattr(player, '_color_id_blocklist', None)
        block_section = ""
        if blocklist:
            block_section = (
                "\n⛔ ILLEGAL THIS GAME (color identity / banned — do NOT propose): "
                f"{', '.join(sorted(blocklist))}\n"
            )

        # May 13 audit: PWs that already used a loyalty ability this turn must
        # not be re-activated (CR 606.3). Plan_turn already surfaces this but
        # decide_action (per-action / conversation mode) did not, so the AI
        # repeatedly proposed `{type:activate, permanent:<PW>}` and ate three
        # retries per turn × several turns per game. Same prominence as the
        # color-identity blocklist — it's a hard constraint, not a heuristic.
        # `_activation_counts` is a per-permanent counter used for ALL
        # activations (PW + creature/artifact). We filter to PWs only so
        # the section doesn't say "Thrasios already activated" when Thrasios
        # can in fact still activate again (creatures with no per-turn limit).
        pw_used = []
        if hasattr(game, '_activation_counts') and game._activation_counts:
            own_pw_names = {c.name.lower() for c in player.battlefield if c.is_planeswalker()}
            for k, v in game._activation_counts.items():
                if v >= 1 and k.split(":")[-1] in own_pw_names:
                    pw_used.append(k.split(":")[-1])
        pw_used_section = ""
        if pw_used:
            pw_used_section = (
                "\n⛔ PWs ALREADY ACTIVATED THIS TURN (CR 606.3 — cannot use again until next turn): "
                f"{', '.join(sorted(set(pw_used)))}\n"
            )

        return f"""{state_desc}

{hand_desc}

=== YOUR HAND (ONLY these cards exist — do NOT use any other card names) ===
{hand_list}
============================================================================={block_section}{pw_used_section}
{memo_section}
Current phase: {game.phase.value}
Available mana by color: {mana_str} (total: {total_mana})
{castable_hint}
{castable_section}
Lands played this turn: {player.lands_played_this_turn}/{player.max_lands_per_turn}
{error_section}
What is your best play? Respond with a JSON action."""

    def _build_delta_message(self, action_result: str, game: GameState,
                             player_index: int, mana_str: str,
                             total_mana: int, castable_section: str,
                             last_error: str = None) -> str:
        """Build a compact delta message for subsequent calls in a conversation.
        Instead of re-sending full state, describe what changed."""
        player = game.players[player_index]
        opponent = game.players[1 - player_index]

        parts = [f"Action result: {action_result}"]

        # Compact state update
        parts.append(f"Your life: {player.life}. Opponent life: {opponent.life}.")
        parts.append(f"Hand: {len(player.hand)} cards. Library: {len(player.library)} cards.")
        parts.append(f"Available mana: {mana_str} (total: {total_mana})")
        parts.append(f"Lands played: {player.lands_played_this_turn}/{player.max_lands_per_turn}")

        if castable_section:
            parts.append(castable_section)

        # PWs that already activated this turn (CR 606.3) \u2014 also surface in
        # delta mode so the AI doesn't retry a previously-failed PW ability.
        # Filter `_activation_counts` (generic counter) to actual PWs only.
        if hasattr(game, '_activation_counts') and game._activation_counts:
            own_pw_names = {c.name.lower() for c in player.battlefield if c.is_planeswalker()}
            pw_used = sorted({
                k.split(":")[-1] for k, v in game._activation_counts.items()
                if v >= 1 and k.split(":")[-1] in own_pw_names
            })
            if pw_used:
                parts.append(f"\u26d4 PWs already activated this turn (cannot use again): {', '.join(pw_used)}")

        # Show pending resolves if any
        if game.pending_resolves:
            parts.append("\u26a0\ufe0f PENDING EFFECTS:")
            for pr in game.pending_resolves:
                parts.append(f"  - {pr}")

        if last_error:
            parts.append(f"\u26a0\ufe0f ACTION FAILED: {last_error}")

        parts.append("What is your next action? Respond with a JSON action.")
        return "\n".join(parts)

    async def decide_action(self, game: GameState, player_index: int,
                           last_error: str = None,
                           conversation: List[Dict] = None,
                           action_result: str = None) -> Tuple[Dict, Optional[List[Dict]]]:
        """
        Have Claude decide what to do in the current game state.

        Args:
            game: Current game state
            player_index: Which player Claude is
            last_error: If provided, feedback about why the previous action failed
            conversation: If provided, accumulated message history for this phase.
                Empty list = first call (builds full state). Non-empty = subsequent
                call (builds delta from action_result).
            action_result: Description of what the last action did (for delta messages
                in conversation mode). e.g. "Played Forest", "Cast Lightning Bolt
                dealing 3 damage to Claude"

        Returns:
            Tuple of (action_dict, updated_conversation). If conversation was None,
            returns (action_dict, None) for backward compatibility.
        """
        # Circuit breaker: skip API call if credits are exhausted
        if self._api_disabled:
            return {"type": "pass"}, conversation
        player = game.players[player_index]
        opponent = game.players[1 - player_index]
        
        # Calculate available mana BY COLOR (not just total)
        # Track 'any'-color mana separately — it can fill colored requirements
        mana_by_color = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        any_color_mana = 0  # Flexible mana (Command Tower, Arcane Signet, etc.)
        for land in player.untapped_lands():
            mana = player._get_mana_production(land)
            for color, amt in mana.items():
                if color == 'any':
                    any_color_mana += amt
                elif color in mana_by_color:
                    mana_by_color[color] += amt

        # Also check mana rocks and other sources
        # CR 302.6: a summoning-sick creature's {T} mana ability is not
        # available. untapped_mana_sources() is the authority (it also drops
        # tapped and non-producing permanents); re-expressing the rule here
        # is how this loop came to advertise mana the payment engine then
        # refuses. Lands are excluded because the loop above counted them.
        _usable_sources = {id(c) for c in player.untapped_mana_sources()}
        for perm in player.battlefield:
            if not perm.is_land() and id(perm) in _usable_sources:
                mana = player._get_mana_production(perm)
                for color, amt in mana.items():
                    if color == 'any':
                        any_color_mana += amt
                    elif color in mana_by_color:
                        mana_by_color[color] += amt

        # NOTE: do NOT bucket 'any' mana into C — "any color" mana cannot pay
        # {C} colorless requirements (Eldrazi). _check_color_castable already
        # tracks any_color_mana separately as flexible mana that fills colored
        # shortfalls AND counts toward total_mana via the explicit total below.
        
        # Debug: Log what Claude sees WITH color breakdown
        if any_color_mana > 0:
            print(f"{self.provider_tag} Flexible 'any' mana sources: {any_color_mana} (can fill colored reqs)")
        lands_in_hand = [c.name for c in player.hand if c.is_land()]
        nonlands_in_hand = [(c.name, c.mana_cost) for c in player.hand if not c.is_land()]
        lands_on_bf = len([c for c in player.battlefield if c.is_land()])
        print(f"{self.provider_tag} Hand: {len(player.hand)} cards - Lands: {lands_in_hand}, Nonlands: {nonlands_in_hand[:5]}...")
        print(f"{self.provider_tag} Battlefield: {lands_on_bf} lands, Lands played this turn: {player.lands_played_this_turn}")
        print(f"{self.provider_tag} Mana by color: {mana_by_color}")
        
        # Build game state description for Claude (cached — skip rebuild if board unchanged)
        current_fp = game._state_fingerprint()
        if current_fp == self._cached_state_fingerprint and self._cached_state_desc:
            state_desc = self._cached_state_desc
            print(f"{self.provider_tag} [CACHE-HIT] State unchanged (fp={current_fp})")
        else:
            state_desc = self._describe_game_state(game, player_index)
            self._cached_state_desc = state_desc
            self._cached_state_fingerprint = current_fp
            print(f"{self.provider_tag} [CACHE-MISS] Rebuilt state desc (fp={current_fp})")

        # Cache hand description (keyed by card names, not fingerprint — hand changes independently)
        hand_hash = "|".join(sorted(c.name for c in player.hand))
        if hand_hash == self._cached_hand_hash and self._cached_hand_desc:
            hand_desc = self._cached_hand_desc
        else:
            hand_desc = self._describe_hand(player)
            self._cached_hand_desc = hand_desc
            self._cached_hand_hash = hand_hash
        
        # Build mana string showing available colors (include flexible mana breakdown)
        mana_parts = []
        for c, amt in mana_by_color.items():
            if amt > 0:
                mana_parts.append(f"{amt}{{{c}}}")
        if any_color_mana > 0:
            mana_parts.append(f"{any_color_mana}{{any color}}")
        mana_str = ", ".join(mana_parts) if mana_parts else "No mana available"
        # Total includes flexible 'any' mana (counts toward CMC even though it's
        # tracked separately for colored-shortfall fills).
        total_mana = sum(mana_by_color.values()) + any_color_mana

        # Build a quick "castable" hint based on total mana
        castable_hint = f"You can pay up to {{{total_mana}}} total mana." if total_mana > 0 else ""
        if any_color_mana > 0:
            castable_hint += f" ({any_color_mana} of that is flexible and can be any color.)"
        
        # July 30, 2026: the castability computation lives in
        # mtg/legal_actions.py — the ONE provider (hand incl. split/
        # adventure/cycling, command zone, companion, free-cast effects,
        # graveyard, exiled adventure halves). The labels are byte-identical
        # to what this builder used to produce inline.
        castable_cards = castable_labels(
            game, player, mana_by_color, any_color_mana, total_mana)

        # May 7 audit: annotate cards that PLAN-VALIDATE would reject so the
        # actor sees "Counterspell ({U}{U}) [unplayable: no legal targets —
        # stack is empty]" instead of just "Counterspell ({U}{U})". Teaches
        # the actor in-band; previously it would re-plan the same spell every
        # turn until PLAN-VALIDATE filtered it out.
        castable_cards = _annotate_castable_with_legality(
            castable_cards, player.hand, game, opponent
        )

        # Expose stack size so the actor knows the priority context — empty
        # main-phase stack means counterspells fizzle. Non-empty (instant-
        # speed response window) means counters are LIVE.
        stack_size = len(getattr(game, 'stack', []) or [])

        castable_section = ""
        if castable_cards:
            castable_section = f"CASTABLE NOW: {', '.join(castable_cards)}"
        elif total_mana > 0:
            castable_section = "CASTABLE NOW: None (you don't have the right colors for any spells in hand)"

        # Surface stack size + reading guide (May 7 audit fix #2).
        castable_section += (
            f"\nSTACK: {stack_size} item(s) on the stack."
            f"\nREADING THE CASTABLE LIST: any card tagged [unplayable: ...] "
            f"will be REJECTED by the planner — do NOT include it in your plan. "
            f"Pick a different action."
        )

        # Apr 30 audit fix #25: AI repeatedly hallucinated graveyard-zone cards
        # as if they were in hand (Momentary Blink 7+ times in one game).
        gy_or_exile_castable = [c for c in castable_cards
                                if 'FLASHBACK' in c or 'ESCAPE' in c or 'COMPANION' in c
                                or 'TOP OF LIBRARY' in c or 'DRAUGR' in c]
        if gy_or_exile_castable:
            castable_section += (
                "\nNOTE: cards tagged [FLASHBACK from graveyard], [ESCAPE], "
                "[COMPANION], [TOP OF LIBRARY], or [DRAUGR — cast from "
                "opponent's exile] are NOT in your hand — they are castable "
                "anyway, so the hand-only rule above does not forbid them. "
                "Each can be cast at most ONCE this turn (then they change "
                "zones). Don't plan multiple casts of the same one."
            )

        # Bug fix: explicit land-drop-used warning. Deepseek ignores subtle hints like "Lands: 1/1".
        # 359 failed land-play attempts across 66 games.
        if player.lands_played_this_turn >= player.max_lands_per_turn:
            castable_section += "\n⚠️ LAND DROP ALREADY USED — do NOT play a land this turn."

        # Bug fix: warn about counterspells/stack-dependent cards during main phase.
        # AI repeatedly tries to cast Mystic Snake, Wash Away, etc. with empty stack.
        if game.phase in (Phase.MAIN1, Phase.MAIN2):
            stack_dependent = []
            for card in player.hand:
                oracle = (card.oracle_text or '').lower()
                name_lower = card.name.lower()
                # Counterspells (require stack target)
                if ('counter target' in oracle and ('spell' in oracle or 'activated' in oracle)):
                    stack_dependent.append(card.name)
                # Mystic Snake (flash counter creature)
                elif name_lower == 'mystic snake' or name_lower == 'frilled mystic':
                    stack_dependent.append(card.name)
            if stack_dependent:
                castable_section += (
                    f"\n⚠️ CANNOT CAST during your main phase (need spell on stack): "
                    f"{', '.join(stack_dependent)}"
                )

        # July 30 (batch-9 reviewer R2): surface Suspend. Before the suspend
        # executor branch existed the mechanic was structurally unreachable —
        # the strategist recommended "Suspend Rift Bolt for {R}" and the
        # actor could only hardcast. Mox Tantalite-class cards (no mana
        # cost) can ONLY enter play this way.
        if game.phase in (Phase.MAIN1, Phase.MAIN2):
            from mtg.helpers import parse_suspend
            _suspendable = []
            for c in player.hand:
                _s = parse_suspend(c.oracle_text)
                if not _s:
                    continue
                _sn, _scost = _s
                if _check_color_castable(_scost, mana_by_color, any_color_mana, total_mana):
                    _suspendable.append((c.name, _sn, _scost))
            if _suspendable:
                castable_section += (
                    "\n⏳ SUSPEND available (pay a cheap cost NOW, exile with time "
                    "counters, casts FREE when the last one is removed — the "
                    "efficient line when you can't afford the full cost): "
                    + ", ".join(f"{n} (Suspend {k} for {c})"
                                for n, k, c in _suspendable)
                    + f"\n  Use: {{\"type\": \"suspend\", \"card\": \"{_suspendable[0][0]}\"}}"
                )

            # Aug 2 (corners-of-corners): CREW hint — an uncrewed Vehicle is
            # not a creature (CR 301.6) and does nothing until crewed. Offer
            # it whenever untapped creature power can cover the crew cost.
            from mtg.helpers import parse_crew as _parse_crew
            _crewable = []
            _untapped_power = 0
            for c in player.battlefield:
                if c.is_creature(game=game) and not c.tapped:
                    try:
                        _untapped_power += c.get_effective_power(game)
                    except Exception:
                        try:
                            _untapped_power += int(c.power or 0)
                        except (TypeError, ValueError):
                            pass
            for c in player.battlefield:
                if ('vehicle' in (c.type_line or '').lower()
                        and not c.is_creature(game=game)):
                    _cn2 = _parse_crew(c.oracle_text)
                    if _cn2 is not None and _untapped_power >= _cn2:
                        _crewable.append((c.name, _cn2))
            if _crewable:
                castable_section += (
                    "\n🚗 CREW available (tap creatures with total power ≥ N; "
                    "the Vehicle becomes an artifact creature THIS turn — crew "
                    "before declaring attackers to attack with it): "
                    + ", ".join(f"{n} (crew {k})" for n, k in _crewable)
                    + f"\n  Use: {{\"type\": \"crew\", \"vehicle\": \"{_crewable[0][0]}\"}}"
                )

        # Fetchland activation hint — uncracked fetchlands produce 0 mana and should be activated.
        # At critical life totals (≤1), cracking a fetch costs 1 life and would kill us — suppress
        # the suggestion entirely. At low life (≤5) downgrade to a warning rather than a directive.
        uncracked_fetches = []
        for c in player.battlefield:
            if (c.is_land() and not c.tapped
                    and 'search your library' in (c.oracle_text or '').lower()
                    and 'sacrifice' in (c.oracle_text or '').lower()):
                uncracked_fetches.append(c.name)
        if uncracked_fetches:
            if player.life <= 1:
                castable_section += (
                    f"\n⚠️ DO NOT crack fetchlands ({', '.join(uncracked_fetches)}): "
                    f"you are at {player.life} life and the 1-life cost would be lethal."
                )
            elif player.life <= 5:
                castable_section += (
                    f"\n🔍 FETCHLANDS available ({', '.join(uncracked_fetches)}) — only crack if "
                    f"the mana fix is critical this turn; you are at {player.life} life and each crack costs 1 life."
                )
            else:
                castable_section += (
                    f"\n🔍 FETCHLANDS TO CRACK (activate to search for a land — they produce NO mana until cracked): "
                    f"{', '.join(uncracked_fetches)}"
                    f"\n  Use: {{\"type\": \"activate\", \"permanent\": \"{uncracked_fetches[0]}\", \"ability\": 1}}"
                )

        print(f"{self.provider_tag} Castable: {castable_cards if castable_cards else 'none'}")
        
        # ---- Build messages for API call ----
        # Conversation mode: use accumulated history with system prompt + deltas
        # Single-shot mode (legacy): inline everything in one user message
        if conversation is not None:
            # Conversation mode — append a new user message
            if len(conversation) == 0:
                # First call in this phase: full state message
                user_msg = self._build_state_message(
                    game, player_index, mana_str, total_mana,
                    castable_hint, castable_section, state_desc, hand_desc,
                    last_error
                )
                conversation.append({"role": "user", "content": user_msg})
                print(f"{self.provider_tag} [CONV-START] Phase {game.phase.value} — full state ({len(conversation)} msgs)")
            elif action_result is not None:
                # Subsequent call: build delta with fresh mana info
                delta = self._build_delta_message(
                    action_result, game, player_index,
                    mana_str, total_mana, castable_section, last_error
                )
                conversation.append({"role": "user", "content": delta})
                print(f"{self.provider_tag} [CONV-DELTA] {action_result[:80]} ({len(conversation)} msgs)")
            else:
                # Subsequent call but no action_result — retry with error feedback
                retry_msg = f"Please try again. {last_error or 'Pick a different action.'}"
                conversation.append({"role": "user", "content": retry_msg})
            api_messages = list(conversation)
            system_prompt = self._build_decision_system_prompt()
        else:
            # Legacy single-shot mode: inline everything in one prompt
            error_section = ""
            if last_error:
                error_section = (
                    f"\n\u26a0\ufe0f YOUR PREVIOUS ACTION FAILED: {last_error}\n"
                    "Please try a different action or pass if no valid plays are available.\n"
                )
            prompt = f"""You are playing Magic: The Gathering. Analyze the game state and decide your action.

{state_desc}

{hand_desc}

Current phase: {game.phase.value}
Available mana by color: {mana_str} (total: {total_mana})
{castable_hint}
{castable_section}
Lands played this turn: {player.lands_played_this_turn}/{player.max_lands_per_turn}
{error_section}
Based on this game state, what is your best play?

{self._build_decision_system_prompt()}"""
            api_messages = [{"role": "user", "content": prompt}]
            system_prompt = None

        try:
            # Run in thread pool to avoid blocking Discord's event loop
            # 1200 tokens: ~800 for <think> scratchpad + ~100 for JSON action
            create_kwargs = dict(
                model=self.model,
                max_tokens=1200,
                messages=api_messages,
            )
            if system_prompt:
                create_kwargs["system"] = system_prompt
            # May 16 audit: tag for [CALL-BREAKDOWN] grep.
            if hasattr(self.client.messages, '_log_tag'):
                create_kwargs['purpose'] = 'plan_turn'
            # May 17 audit: instrument plan_turn calls with wall-clock + cost
            # so we can debug the "actor calls 2,500-3,100 per game" regression
            # without re-running the whole batch. The numbers go into
            # [PLAN-TURN-PROFILE] which is grep-friendly.
            import time as _time_mod
            _plan_start = _time_mod.monotonic()
            response = await asyncio.to_thread(
                self.client.messages.create,
                **create_kwargs
            )
            _plan_elapsed_ms = int((_time_mod.monotonic() - _plan_start) * 1000)
            try:
                _prompt_tok = getattr(response.usage, 'input_tokens', 0)
                _comp_tok = getattr(response.usage, 'output_tokens', 0)
            except Exception:
                _prompt_tok = _comp_tok = 0
            # Approximate cost ($0.27/M miss + $1.10/M output for V4-Flash;
            # this is over-stated when cache hits but good enough for
            # ranking which call paths dominate spend).
            _approx_cost = _prompt_tok * 0.27e-6 + _comp_tok * 1.10e-6
            # May 17 audit named this `[PLAN-TURN-PROFILE]` but the emit is
            # actually inside `decide_action`, not `plan_turn`. May 19
            # regression: an earlier edit threaded `source={call_source}`
            # in here, but `call_source` is only a `plan_turn` parameter
            # and isn't in `decide_action`'s scope → NameError → every
            # decide_action call returned "pass" via the bare except below,
            # producing 28 all-pass games. Hardcode the source label here
            # and add a real `[PLAN-TURN-PROFILE] source=...` emit inside
            # `plan_turn` proper (where call_source IS in scope).
            print(f"[PLAN-TURN-PROFILE] source=decide_action_inline "
                  f"elapsed_ms={_plan_elapsed_ms} "
                  f"prompt={_prompt_tok} completion={_comp_tok} "
                  f"approx_cost=${_approx_cost:.5f}")

            # Track usage
            self._track_usage(response)

            # Parse JSON from response — strip <think> scratchpad first
            raw_text = response_text(response).strip()
            # May 7 audit fix #10: collapse whitespace in the log line so
            # multi-line pretty-printed JSON doesn't bloat the console
            # (was ~25% of all log lines for DeepSeek runs). Cap at 300 chars
            # post-collapse. Keep the full raw_text for parsing below.
            try:
                _log_preview = re.sub(r'\s+', ' ', raw_text).strip()
            except Exception:
                _log_preview = raw_text.replace('\n', ' ')
            print(f"{self.provider_tag} Raw response: {_log_preview[:300]}")

            # Strip <think> tags and leading non-JSON text
            text = self._strip_think_tags(raw_text, context="action")

            # Try to extract JSON if wrapped in code blocks
            if "```" in text:
                match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
                text = match.group(1) if match else "{\"type\": \"pass\"}"

            action = self._safe_json_loads(text, fallback={"type": "pass"})
            # Normalize: LLM sometimes returns {} or a list. Guarantee a dict with 'type'.
            if not isinstance(action, dict):
                print(f"{self.provider_tag} Non-dict action {type(action).__name__}; defaulting to pass")
                action = {"type": "pass"}
            elif "type" not in action:
                # Infer from keys if possible (e.g. {card: X} → cast)
                if action.get("card") and action.get("zone") not in ("hand_to_battlefield", None):
                    action["type"] = "pass"
                elif action.get("card"):
                    action["type"] = "cast"
                else:
                    print(f"{self.provider_tag} Action missing 'type' key ({list(action.keys())}); defaulting to pass")
                    action = {"type": "pass"}
            # Normalize singular identity fields emitted by some providers.
            # ``target`` may be a legitimate cast target array.
            for _k in ("card", "permanent"):
                _v = action.get(_k)
                if isinstance(_v, (list, tuple)):
                    action[_k] = _v[0] if _v else None
            # Log and strip reasoning key (DeepSeek puts thinking here in JSON mode)
            if isinstance(action, dict) and 'reasoning' in action:
                print(f"{self.provider_tag} [REASONING] {action['reasoning'][:200]}")
                del action['reasoning']  # Don't pass to game engine
            print(f"{self.provider_tag} Parsed action: {action}")
            # Aug 2 (prose-says-pass): veto a cast the model's own prose says
            # to hold. raw_text carries the think-tag content and the
            # stripped 'reasoning' value, so the check sees everything the
            # model wrote.
            if prose_hold_veto(raw_text, action):
                print(f"{self.provider_tag} [PASS-INTENT] prose says to hold "
                      f"{action.get('card') or action.get('permanent')} — "
                      f"vetoing to pass")
                action = {"type": "pass"}
            # Aug 7 confirmation-batch audit (CO-4): the PLAN path rejects
            # board wipes on creatureless boards (_validate_plan_mana), but
            # this inline fallback had no guard — the two-path divergence:
            # in game_1535228649845030952 the plan cast was rejected and the
            # inline path then cast Day of Judgment into a 0-creature board
            # anyway (card + 4 mana for nothing). Same predicate as the plan
            # guard, veto to pass.
            if action.get("type") == "cast" and action.get("card"):
                try:
                    from mtg.helpers import names_match, board_wipe_on_empty_board
                    _wc = next((c for c in game.players[player_index].hand
                                if names_match(c.name, action["card"])), None)
                    if board_wipe_on_empty_board(game, player_index, _wc):
                        print(f"{self.provider_tag} [WIPE-HOLD] "
                              f"{_wc.name}: board wipe on an empty "
                              f"board — vetoing to pass (inline twin "
                              f"of the plan-validate guard)")
                        action = {"type": "pass"}
                except (AttributeError, TypeError, IndexError):
                    pass

            # Append assistant response to conversation if in conversation mode
            if conversation is not None:
                conversation.append({"role": "assistant", "content": text})
                # Trim conversation if too long (keep system context fresh)
                if len(conversation) > 30:
                    # Keep first message (full state) + last 20 messages
                    conversation[:] = conversation[:1] + conversation[-20:]
                    print(f"{self.provider_tag} [CONV-TRIM] Trimmed to {len(conversation)} messages")

            return action, conversation

        except Exception as e:
            print(f"{self.provider_tag} Decision error: {e}")
            self.last_error = str(e)  # Store for debugging
            self._check_circuit_breaker(e)
            return {"type": "pass"}, conversation

    async def plan_turn(self, game: GameState, player_index: int,
                        call_source: str = "unknown") -> List[Dict]:
        """Plan an entire main phase as a single API call.

        Instead of calling decide_action 3-5 times per phase (each taking 3-5s),
        this asks the model to plan the whole sequence at once:
            [{"type": "play_land", "card": "Forest"},
             {"type": "cast", "card": "Arcane Signet"},
             {"type": "pass"}]

        Returns a list of actions to execute in order. Falls back to
        [{"type": "pass"}] on any error. The caller should fall back to the
        per-action decide_action loop if any action in the plan fails.

        May 18 audit: `call_source` identifies which code path triggered this
        plan_turn so [PLAN-TURN-PROFILE] log lines surface "ai_turn:main1",
        "autoplay:main1", "autoplay:main2", etc. The May 17 batch found
        plan_turn called 569-1214 times per game (vs ~150 target). Need
        per-source distribution to localize the inner loop.
        """
        if self._api_disabled:
            return [{"type": "pass"}]

        player = game.players[player_index]
        opponent = game.players[1 - player_index]

        # --- Reuse the same state-building as decide_action ---
        mana_by_color = {'W': 0, 'U': 0, 'B': 0, 'R': 0, 'G': 0, 'C': 0}
        any_color_mana = 0
        for land in player.untapped_lands():
            mana = player._get_mana_production(land)
            for color, amt in mana.items():
                if color == 'any':
                    any_color_mana += amt
                elif color in mana_by_color:
                    mana_by_color[color] += amt
        # CR 302.6: a summoning-sick creature's {T} mana ability is not
        # available. untapped_mana_sources() is the authority (it also drops
        # tapped and non-producing permanents); re-expressing the rule here
        # is how this loop came to advertise mana the payment engine then
        # refuses. Lands are excluded because the loop above counted them.
        _usable_sources = {id(c) for c in player.untapped_mana_sources()}
        for perm in player.battlefield:
            if not perm.is_land() and id(perm) in _usable_sources:
                mana = player._get_mana_production(perm)
                for color, amt in mana.items():
                    if color == 'any':
                        any_color_mana += amt
                    elif color in mana_by_color:
                        mana_by_color[color] += amt
        mana_by_color['C'] += any_color_mana

        mana_parts = []
        for c, amt in mana_by_color.items():
            if amt > 0:
                if c == 'C' and any_color_mana > 0:
                    fixed_c = amt - any_color_mana
                    if fixed_c > 0:
                        mana_parts.append(f"{fixed_c}{{C}}")
                    mana_parts.append(f"{any_color_mana}{{any color}}")
                else:
                    mana_parts.append(f"{amt}{{{c}}}")
        mana_str = ", ".join(mana_parts) if mana_parts else "No mana available"
        total_mana = sum(mana_by_color.values())

        # July 30, 2026: same provider as decide_action — the former inline
        # copy here had ALREADY diverged (no split halves, no adventure
        # halves, no cycling, no token skip, no free-cast effects — the
        # July 29 split fix never reached plan_turn, so Commit // Memory
        # was invisible to plans while visible to inline decisions).
        # plan_turn GAINS those branches by consuming the shared list:
        # the two paths must agree, that is the point.
        castable_cards = castable_labels(
            game, player, mana_by_color, any_color_mana, total_mana)

        # May 7 audit: annotate cards that PLAN-VALIDATE would reject so the
        # actor sees "Counterspell ({U}{U}) [unplayable: no legal targets —
        # stack is empty]" instead of just "Counterspell ({U}{U})". Same
        # treatment as decide_action.
        castable_cards = _annotate_castable_with_legality(
            castable_cards, player.hand, game, opponent
        )

        # Expose stack size to the actor (May 7 audit fix #2).
        stack_size = len(getattr(game, 'stack', []) or [])

        castable_section = ""
        if castable_cards:
            castable_section = f"CASTABLE NOW: {', '.join(castable_cards)}"

        # Surface stack size + reading guide so the actor self-filters.
        castable_section += (
            f"\nSTACK: {stack_size} item(s) on the stack."
            f"\nREADING THE CASTABLE LIST: any card tagged [unplayable: ...] "
            f"will be REJECTED by the planner — do NOT include it in your plan. "
            f"Pick a different action."
        )
        # May 14 audit: 23 counterspell-on-empty-stack vetoes across 6 games
        # (avg 3.8/game). Engine catches them but each is a wasted API call.
        # Add a louder directive when the situation is acute.
        if stack_size == 0:
            counters_in_hand = [
                c.name for c in player.hand
                if not c.is_land()
                and 'counter target' in (c.oracle_text or '').lower()
                and 'spell' in (c.oracle_text or '').lower()
                and not c.is_creature()
                # Skip modal counterspells (Archmage's Charm, Mystic Confluence)
                and '•' not in (c.oracle_text or '').lower()
            ]
            if counters_in_hand:
                castable_section += (
                    f"\n⚠️ STACK IS EMPTY: you cannot cast counterspells this turn "
                    f"({', '.join(counters_in_hand[:5])}). Hold them for your "
                    f"opponent's turn. Do NOT propose them — every attempt wastes "
                    f"a planning cycle."
                )

        # Apr 30 audit fix #25: graveyard / exile zone reminder (mirrors decide_action).
        gy_or_exile_castable = [c for c in castable_cards
                                if 'FLASHBACK' in c or 'ESCAPE' in c or 'COMPANION' in c
                                or 'TOP OF LIBRARY' in c or 'DRAUGR' in c]
        if gy_or_exile_castable:
            castable_section += (
                "\nNOTE: cards tagged [FLASHBACK from graveyard], [ESCAPE], "
                "[COMPANION], [TOP OF LIBRARY], or [DRAUGR — cast from "
                "opponent's exile] are NOT in your hand — they are castable "
                "anyway, so the hand-only rule above does not forbid them. "
                "Cast each at most ONCE per turn."
            )

        # Build state description (cached)
        current_fp = game._state_fingerprint()
        if current_fp == self._cached_state_fingerprint and self._cached_state_desc:
            state_desc = self._cached_state_desc
        else:
            state_desc = self._describe_game_state(game, player_index)
            self._cached_state_desc = state_desc
            self._cached_state_fingerprint = current_fp

        hand_hash = "|".join(sorted(c.name for c in player.hand))
        if hand_hash == self._cached_hand_hash and self._cached_hand_desc:
            hand_desc = self._cached_hand_desc
        else:
            hand_desc = self._describe_hand(player)
            self._cached_hand_desc = hand_desc
            self._cached_hand_hash = hand_hash

        lands_in_hand = [c.name for c in player.hand if c.is_land()]
        land_drop_available = player.lands_played_this_turn < player.max_lands_per_turn

        # Check for activatable planeswalkers. Belt-and-suspenders: also
        # cross-check has_activated_this_turn() so an already-used PW isn't
        # offered even if can_activate() returns true for some other reason.
        pw_section = ""
        for perm in player.battlefield:
            if perm.is_planeswalker() and self.engine_ref and hasattr(self.engine_ref, 'planeswalker_manager'):
                pw_mgr = self.engine_ref.planeswalker_manager
                if pw_mgr:
                    if hasattr(pw_mgr, 'has_activated_this_turn') and pw_mgr.has_activated_this_turn(game, perm):
                        pw_section += f'\nUNAVAILABLE: {perm.name} (already activated this turn)'
                        continue
                    abilities = pw_mgr.parse_abilities(perm)
                    for i, ab in enumerate(abilities):
                        can_act, _ = pw_mgr.can_activate(game, player, perm, i)
                        if can_act:
                            cost_str = f"+{ab.loyalty_cost}" if ab.loyalty_cost > 0 else str(ab.loyalty_cost)
                            pw_section += f'\nACTIVATABLE: {perm.name} ability {i} [{cost_str}]: {ab.text[:80]}'

        # Non-PW activated abilities — hint about strategic vs mana abilities
        # May 20 audit (Bug H): previously skipped lands and tapped permanents
        # entirely, so manlands (Celestial Colonnade, Mishra's Factory,
        # Mutavault) and tapped activated permanents (Castle Vantress scry
        # while tapped from making mana) never surfaced. game_1506623255119925278
        # had 4+ Celestial Colonnades on board across 46 turns, never animated.
        # Drop the is_land() and tapped filters here — the cost-parsing below
        # already skips mana abilities (which is what makes most basic lands
        # uninteresting), and tap-as-part-of-cost will be enforced at
        # activation time by can_activate().
        for perm in player.battlefield:
            if perm.is_planeswalker():
                continue
            if not perm.oracle_text or ':' not in perm.oracle_text:
                continue
            for line in perm.oracle_text.split('\n'):
                if ':' not in line or line.strip().startswith('('):
                    continue
                parts = line.split(':', 1)
                if len(parts) != 2:
                    continue
                cost_part = parts[0].strip().lower()
                effect_part = parts[1].strip()
                if any(kw in cost_part for kw in ['when', 'whenever', 'at the beginning']):
                    continue
                if re.match(r'^[+-]?\d+$', cost_part):
                    continue  # PW loyalty ability, already listed
                # Classify: mana ability vs strategic
                effect_lower = effect_part.lower()
                if 'add' in effect_lower and ('mana' in effect_lower or '{' in effect_lower):
                    continue  # Skip mana abilities — they're auto-used
                # Skip Cycling parenthetical reminder text mentioning ability
                # (cycling already surfaced via the castable list path above).
                # July 31 batch-11 (madness reviewer): the old check was
                # 'discard' in cost_part — which ALSO matched Anje
                # Falkenrath's real battlefield ability ("{T}, Discard a
                # card: Draw a card."), so the madness deck's commander was
                # never offered a single activation in 25 turns
                # (game_1532532252825616466). Cycling's reminder cost is
                # "discard THIS card" — match that exact shape only.
                if 'discard this card' in cost_part and 'draw a card' in effect_lower:
                    continue
                # Manland-style "becomes a N/N creature" — special-case the
                # display label to make the AI's win-path obvious.
                _is_manland = perm.is_land() and re.search(
                    r'becomes a \d+/\d+\b|\bbecomes a .{0,30}creature\b',
                    effect_lower,
                )
                if _is_manland:
                    pw_section += (
                        f'\nACTIVATABLE: {perm.name} [{cost_part.upper()}]: '
                        f'{effect_part[:80]} [manland — animate for combat]'
                    )
                else:
                    pw_section += f'\nACTIVATABLE: {perm.name} ability: {effect_part[:80]} [strategic]'
                break  # Show one non-mana ability per permanent

        hand_card_names = [c.name for c in player.hand]
        hand_list = ", ".join(hand_card_names) if hand_card_names else "(empty)"

        # Persistent block-list (cards we've already learned can't legally
        # be cast — color identity violations). Surface here too so the
        # plan_turn batch doesn't keep listing them.
        blocklist = getattr(player, '_color_id_blocklist', None)
        block_section = ""
        if blocklist:
            block_section = (
                "\n⛔ ILLEGAL THIS GAME (do NOT include in plan): "
                f"{', '.join(sorted(blocklist))}\n"
            )

        # [STRATEGIST] Phase 2: Inject last turn's strategy memo into plan prompt
        # Strategy memo is stored per-game (on GameState) to prevent cross-contamination
        # in parallel autoplay. Falls back to ClaudePlayer._strategy_memo for single games.
        strategy_memo = getattr(game, '_strategy_memo', '') or self._strategy_memo
        strategy_section = ""
        if strategy_memo:
            strategy_section = f"\n=== STRATEGY (from last turn's analysis) ===\n{strategy_memo}\n{'='*50}\n"

        # May 14 audit (A5): surface recent PLAN-VALIDATE rejections so the AI
        # learns from them instead of re-proposing the same illegal action
        # turn after turn. _validate_plan_mana records (card_name, reason,
        # turn) on game._recent_plan_rejections (bounded list of 10 entries,
        # deduped by card_name, auto-pruned after 3 turns). The reasons are
        # crisp action items ("X-cost spell with X<=0", "counterspell with
        # empty stack — cast at instant speed during opponent's turn instead").
        recent_rejections = getattr(game, '_recent_plan_rejections', []) or []
        rejection_section = ""
        if recent_rejections:
            rej_lines = [
                f"  - {cn}: {r} (turn {t})"
                for cn, r, t in recent_rejections[-6:]
            ]
            rejection_section = (
                "\n⚠️ RECENT REJECTED ACTIONS (planner refused these in prior turns — "
                "do NOT propose again unless the game state has changed to fix the "
                "specific problem):\n"
                + "\n".join(rej_lines)
                + "\n"
            )

        # Real-time activation state — not stale like the strategy memo.
        # Prevents the AI from planning activations for PWs already used this turn.
        # May 13: filter `_activation_counts` (per-permanent counter shared by
        # PWs AND creatures/artifacts) to PWs only. Listing a creature here
        # was confusingly off-limits because most creatures can re-activate.
        already_activated_section = ""
        if hasattr(game, '_activation_counts') and game._activation_counts:
            own_pw_names = {c.name.lower() for c in player.battlefield if c.is_planeswalker()}
            already_used = sorted({
                k.split(":")[-1] for k, v in game._activation_counts.items()
                if v >= 1 and k.split(":")[-1] in own_pw_names
            })
            if already_used:
                already_activated_section = f"\nALREADY ACTIVATED THIS TURN (cannot use again): {', '.join(already_used)}"

        # May 7 audit fix #3b/#4: board-wipe priority heuristic.
        # Aminatou (game 1501940665351934122 t11) sat on Terminus with 6 mana
        # while opponent had a full token board, cast Dimir Signet (ramp)
        # instead, then discarded Terminus to hand size. Surface a "you have
        # a wipe + opponent has way more board than you" signal so the actor
        # prioritises the wipe THIS turn.
        wipe_recommendations: List[str] = []
        try:
            my_creatures = [c for c in player.battlefield if c.is_creature()]
            opp_creatures = [c for c in opponent.battlefield if c.is_creature()]
            opp_creature_count = len(opp_creatures)
            my_creature_count = len(my_creatures)
            # Compute power totals using get_effective_power so anthems/counters count.
            def _eff_p(c):
                try:
                    return max(0, c.get_effective_power(game))
                except Exception:
                    return max(0, int(c.power or 0))
            opp_power = sum(_eff_p(c) for c in opp_creatures)
            my_power = sum(_eff_p(c) for c in my_creatures)
            # "Significantly larger" = opp has 4+ creatures AND opp has more
            # creatures than us (or significantly more power).
            opp_dominant = (
                opp_creature_count >= 4
                and (opp_creature_count > my_creature_count
                     or opp_power >= my_power + 5)
            )
            if opp_dominant:
                # Scan hand for board wipes we can afford.
                for card in player.hand:
                    if card.is_land():
                        continue
                    oracle = (card.oracle_text or '').lower()
                    is_wipe = (
                        'destroy all creatures' in oracle
                        or 'exile all creatures' in oracle
                        or 'all creatures get -' in oracle
                        or 'destroy each creature' in oracle
                        or 'exile each creature' in oracle
                        or 'put all creatures' in oracle  # Terminus, Hour of Reckoning's cousins
                    )
                    if not is_wipe or card.is_creature():
                        continue
                    # Affordability check (matches castable_cards logic).
                    if _check_color_castable(card.mana_cost, mana_by_color,
                                             any_color_mana, total_mana):
                        wipe_recommendations.append(card.name)
                        print(f"[PLAN-VALIDATE] Recommended wipe: {card.name} "
                              f"(opp {opp_creature_count}c/{opp_power}p vs you "
                              f"{my_creature_count}c/{my_power}p)")
        except Exception:
            pass
        wipe_section = ""
        if wipe_recommendations:
            wipe_section = (
                f"\n🎯 BOARD-WIPE OPPORTUNITY: You have wipe(s) in hand "
                f"({', '.join(wipe_recommendations)}) and opponent's board "
                f"dwarfs yours. Cast a wipe THIS TURN — sitting on it across "
                f"turns hoping for a better moment is a known misplay. "
                f"Prefer the wipe over ramp/draw spells.\n"
            )

        # May 16 audit: restructured user_msg to put STABLE content first so
        # DeepSeek's server-side prompt cache hits a longer prefix. Each call
        # used to start with state_desc (changes per-action), defeating the
        # cache after the first ~50 tokens. New order: static task header →
        # rules reminders → game-stable info → volatile state at the bottom.
        # The cache key matches the longest common prefix; pushing volatile
        # text to the suffix lets DeepSeek cache the first ~600 tokens.
        STATIC_TASK_HEADER = """=== PLAN-TURN TASK ===
Plan your ENTIRE main phase as a JSON array of actions, executed in order.

CRITICAL: After each cast, subtract its mana cost from your available mana. Do NOT plan spells you can't afford.

ACTION GRAMMAR:
- {"type": "play_land", "card": "Forest"}
- {"type": "cast", "card": "Arcane Signet"}
- {"type": "cast", "card": "Demonic Tutor", "tutor_card": "Craterhoof Behemoth"}
- {"type": "cast", "card": "Primal Command", "modes": [2, 4], "target": ["Sol Ring"], "tutor_card": "Craterhoof Behemoth"}
- {"type": "suspend", "card": "Rift Bolt"} — pay the Suspend cost, exile with time counters, casts free later (the ⏳ hint lists candidates)
- {"type": "crew", "vehicle": "Smuggler's Copter"} — tap creatures with total power ≥ the crew cost; the Vehicle becomes an artifact creature until end of turn (the 🚗 hint lists candidates; crew BEFORE attacking)
- {"type": "foretell", "card": "Quakebringer"} — pay {2} to exile it face down; cast it on a LATER turn for its (cheaper) foretell cost. The castable list marks these [FORETELL ...] and, once foretold, [FORETOLD — cast from exile]
- {"type": "graveyard_activate", "card": "Angel of Sanctions", "mechanic": "embalm"} — embalm / eternalize / unearth a card in your GRAVEYARD (mechanic is one of those three). Sorcery speed only. The castable list marks these [EMBALM/ETERNALIZE/UNEARTH from graveyard]
- {"type": "cast", "card": "Commit // Memory", "adventure": "Memory"} — cast one half of a split card. An [AFTERMATH from graveyard] half is cast from the GRAVEYARD; name the half
- {"type": "activate", "permanent": "Chandra, Pyromaster", "ability": 0}
- {"type": "resolve", "description": "<one short imperative clause, no reasoning>"}
- {"type": "pass"}

`resolve` is a FALLBACK — use it only when no other action type fits. The most
common misuse the engine sees in audits (May 24): the AI emits
`{"type": "resolve", "description": "Equip Sword of Sinew and Steel to Danitha"}`
when the correct form is `{"type": "activate", "permanent": "Sword of Sinew and
Steel", "ability": 0, "target": "Danitha"}`. `resolve` escalates to a Tier 3
Claude API call (slow, expensive); `activate` runs through the dedicated
equipment handler (fast, deterministic).

WHEN TO USE `activate` INSTEAD OF `resolve`:
- Equipment attach: `{"type": "activate", "permanent": "<Equipment>", "ability": 0, "target": "<creature>"}`
  NOT `{"type": "resolve", "description": "Equip X to Y"}`
- Aura attach to existing creature: cast the aura with a `target` field, don't `resolve` an attach
- Permanent activated abilities (Sol Ring tap, Walking Ballista pump): `activate` with the ability index
- Planeswalker loyalty abilities: `activate` with ability index 0 / 1 / 2

WHEN TO USE `cast` INSTEAD OF `resolve`:
- Reanimate spells (Animate Dead, Reanimate, Victimize, Stitch Together): `{"type": "cast", "card": "Animate Dead", "target": "<creature in graveyard>"}`
  NOT `{"type": "resolve", "description": "Return Blood Artist from graveyard"}`
- Tutor spells: `{"type": "cast", "card": "Demonic Tutor", "tutor_card": "Craterhoof Behemoth"}` — always name the exact card to find
- Split-destination tutors (Jarad's Orders): `{"type": "cast", "card": "Jarad's Orders", "tutor_to_hand": "Sakura-Tribe Elder", "tutor_to_graveyard": "Kokusho, the Evening Star"}`
- Spell-from-graveyard effects (Yawgmoth's Will, Past in Flames): `cast` the spell

`resolve` description field rules (only when truly needed): ≤80 chars, imperative
form ("Mill 3 to opponent", "Discard Forest, draw a card"). Do NOT include
reasoning, alternatives, self-talk, or "but/however/so/because" clauses. The
description is parsed verbatim — chain-of-thought leakage produces JSON
corruption (May 20 audit: V4-Flash leaked a 113-char truncated reasoning
monologue into this field in game_1506209051812302938).

ORDERING RULE:
1. play_land (if available and you have one)
2. cast cheapest ramp first
3. cast threats / answers
4. activate abilities
5. pass (always end with this)
=== END TASK HEADER ===
"""
        user_msg = STATIC_TASK_HEADER + f"""{block_section}{strategy_section}{rejection_section}
=== CURRENT TURN STATE ===
Phase: {game.phase.value}
Available mana: {mana_str} (total: {total_mana})
Lands played: {player.lands_played_this_turn}/{player.max_lands_per_turn}
{castable_section}
{pw_section}{already_activated_section}{wipe_section}

{hand_desc}

=== YOUR HAND (ONLY these cards exist — do NOT reference any other card names) ===
{hand_list}
==================================================================================

=== GAME STATE ===
{state_desc}

{"⚠️ LAND DROP ALREADY USED — do NOT include any play_land actions." if not land_drop_available else f"Land drop available. Lands in hand: {', '.join(lands_in_hand[:3]) if lands_in_hand else 'NONE — skip play_land'}"}
{"" if land_drop_available and lands_in_hand else "Do NOT include play_land — " + ("already used." if not land_drop_available else "no lands in hand.")}
If nothing to do, return [{{"type": "pass"}}]."""

        system_prompt = """RESPOND WITH JSON ONLY. Your first character must be `[`. No explanation, no preamble, no text after the array.

You are playing Magic: The Gathering. Plan your entire main phase as a JSON array.

MANA ARITHMETIC — RUN A RUNNING TOTAL:
- Generic mana ({2}, {3}) can be paid with ANY color
- Colored mana ({W}, {U}, {B}, {R}, {G}) MUST be that specific color
- After EACH cast in your plan, subtract its full cost (CMC) from your available mana
- Before adding the next cast, verify the new running total >= the next spell's cost
- Tapping a creature for convoke or an artifact for improvise also reduces effective cost — check the CASTABLE NOW list for the actual castable subset
- A plan that totals more than your available mana will be REJECTED action-by-action and you'll lose the rest of the turn — be conservative
- If you're not 100% sure two specific spells fit, plan only the first one and pass

STATE STALENESS — A SPELL YOU CAST THIS TURN IS NO LONGER IN HAND:
- After {"type": "cast", "card": "Sol Ring"}, Sol Ring is on the battlefield, not in hand
- Do NOT plan to cast a card twice in the same plan unless your deck has multiple copies
- Same goes for play_land — once a land is played it's gone from hand

NO-TARGET CHECKS — DON'T PLAN A SPELL THAT WILL FIZZLE:
- Targeted removal (Doom Blade, Swords to Plowshares, Ravenous Chupacabra, Path to Exile) needs a legal opposing creature. If opponent has 0 creatures, skip the spell entirely
- Counterspells (Negate, Fierce Guardianship, Mana Drain, Swan Song, Counterspell, Force of Will) ONLY counter a spell on the stack. The stack is empty during your main phase, so they will fizzle in a main-phase plan — do not include them
- Board wipes (Wrath of God, Supreme Verdict, Damnation, Blasphemous Act) wipe the board. Don't plan one if the board is empty, or if your own creatures are stronger than opponent's (you're killing your own better board)
- Pump spells (Overrun, Craterhoof, Return of the Wildspeaker pump mode) need creatures of yours on the battlefield
- Equipment is useless without creatures to equip — don't auto-equip the moment you cast equipment if you have no creatures yet

X-COST SPELLS — X=0 IS WASTED:
- Walking Ballista, Hangarback Walker, Hydroid Krasis, Blue Sun's Zenith, Comet Storm, Genesis Wave: X scales with mana paid
- If you can only afford X=1 and the effect needs more to matter, hold the spell rather than casting for X=0 or X=1
- Specifically: don't include X-cost casts in your plan unless you have at least 4-5 mana to spend on X (or unless X=2 deals lethal)

CASTABLE NOW LIST IS GROUND TRUTH:
- ONLY cast spells from the CASTABLE NOW list — if a card isn't listed there, you can't afford its mana cost (this is computed by the engine, not guessed)
- The list updates per-call so it always reflects this exact moment's mana
- If the list is empty, your only legal action is play_land (if available) and pass

PLANESWALKER LOYALTY:
- NEVER activate a planeswalker that already used a loyalty ability this turn (CR 606.3). Most creatures/artifacts can activate repeatedly if you can pay the cost (e.g. Thrasios can keep paying {4} as long as mana lasts), but the list under "ALREADY ACTIVATED THIS TURN" tracks hard per-turn limits — treat listed names as off-limits for the rest of the turn

OTHER STRATEGIC ADVICE:
- Play a land first (if you have one and haven't played one this turn)
- Cast mana ramp (Arcane Signet, Sol Ring, mana dorks) before expensive spells
- Hold instant-speed spells for the opponent's turn when possible
- Save premium removal for real threats, not 1/1 tokens
- Thassa's Oracle only wins if your devotion to blue >= cards in library — don't waste it
- BOARD WIPE HEURISTIC: if you have a board wipe in hand, sufficient mana, and opponent's board (creatures + total power) is significantly larger than yours, prioritize the wipe THIS TURN over ramp/draw. Don't sit on it across turns hoping for a better moment — discard-to-hand-size will eat the wipe and you'll have lost it for nothing.

STRATEGIST MEMO (if present in the user message):
- A separate strategist analyzed the board on the previous turn. Look for a `=== STRATEGY ===` block in the user message.
- The memo is a labeled 4-section briefing: `Win condition:`, `This turn:`, `Opp threats & answers:`, `Hold for opp turn:`.
- The plays named in `This turn:` are the highest-priority actions for this plan. Include them if they're in CASTABLE NOW or ACTIVATABLE.
- The cards named in `Hold for opp turn:` should NOT be cast this main phase — keep mana up for them. Treat that line as a no-cast list for this plan.
- If the memo's recommendation conflicts with CASTABLE NOW (the named card isn't castable this turn), prefer CASTABLE NOW — the memo may be one turn stale.

RESPONSE FORMAT — JSON array only:
[{"type": "play_land", "card": "Forest"},
 {"type": "cast", "card": "Arcane Signet"},
 {"type": "activate", "permanent": "Chandra", "ability": 0},
 {"type": "pass"}]

CRITICAL: You MUST respond with a valid JSON array. If you are uncertain what to do, output [{"type": "pass"}] — never output prose without a JSON array. A response with no JSON array will be treated as a pass automatically, wasting your turn.

IMPORTANT: Always end with {"type": "pass"}. No text outside the JSON array."""

        try:
            print(f"{self.provider_tag} [PLAN] Planning {game.phase.value} "
                  f"(hand={len(player.hand)}, mana={total_mana}, castable={len(castable_cards)})")

            create_kwargs = dict(
                model=self.model,
                # May 23 audit (MAJOR #18): plan_turn was hitting completion=800
                # on slow turns (Agent #1 found this in game_1507596001329025045
                # and others) — when the JSON plan gets truncated mid-action,
                # the engine falls back to decide_action_inline retries, which
                # was 44.2% of plan_turn calls in the May 23 batch (regression
                # from May 20's 43.1%). Bumping to 1200 gives the plan room to
                # complete; the actor's user-facing cost is bounded by the
                # actual completion length, not the cap.
                max_tokens=1200,
                messages=[{"role": "user", "content": user_msg}],
                system=system_prompt,
            )
            # May 17 audit: tag for [CALL-BREAKDOWN]. Without this, decide_action
            # fallback calls (when plan_turn fails or returns "pass" too early)
            # landed in the 'uncategorized' bucket — ~37% of total calls.
            # Note: this is INSIDE plan_turn but the May 17 audit's tagging
            # called it 'decide_action' for historical reasons. The function
            # surrounding this block is `plan_turn`.
            if hasattr(self.client.messages, '_log_tag'):
                create_kwargs['purpose'] = 'plan_turn'
            # May 19: profile the real plan_turn API call with the
            # `call_source` label so [PLAN-TURN-PROFILE] grep can localize
            # which path is responsible for the 569-1214 calls/game.
            import time as _t_pt
            _pt_start = _t_pt.monotonic()
            response = await asyncio.to_thread(
                self.client.messages.create,
                **create_kwargs
            )
            _pt_elapsed_ms = int((_t_pt.monotonic() - _pt_start) * 1000)
            try:
                _pt_prompt_tok = getattr(response.usage, 'input_tokens', 0)
                _pt_comp_tok = getattr(response.usage, 'output_tokens', 0)
            except Exception:
                _pt_prompt_tok = _pt_comp_tok = 0
            _pt_approx_cost = _pt_prompt_tok * 0.27e-6 + _pt_comp_tok * 1.10e-6
            print(f"[PLAN-TURN-PROFILE] source={call_source} "
                  f"elapsed_ms={_pt_elapsed_ms} "
                  f"prompt={_pt_prompt_tok} completion={_pt_comp_tok} "
                  f"approx_cost=${_pt_approx_cost:.5f}")
            self._track_usage(response)

            raw_text = response_text(response).strip()
            print(f"{self.provider_tag} [PLAN] Raw: {raw_text[:300]}")

            text = self._strip_think_tags(raw_text, context="plan")
            if "```" in text:
                match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
                text = match.group(1) if match else '[{"type": "pass"}]'

            plan = self._safe_json_loads(text, fallback=[{"type": "pass"}])

            if not isinstance(plan, list):
                print(f"{self.provider_tag} [PLAN] Not a list, wrapping: {type(plan)}")
                plan = [plan] if isinstance(plan, dict) else [{"type": "pass"}]

            # Normalize each step: drop non-dicts and guarantee a 'type' key.
            # Cast target arrays remain intact for multi-target spells.
            normalized = []
            for step in plan:
                if not isinstance(step, dict):
                    continue
                if "type" not in step:
                    step["type"] = "cast" if step.get("card") else "pass"
                # Aug 2 (prose-says-pass): drop a planned cast the model's
                # own prose says to hold (same veto as decide_action).
                if prose_hold_veto(raw_text, step):
                    print(f"{self.provider_tag} [PASS-INTENT] plan prose says "
                          f"to hold {step.get('card') or step.get('permanent')}"
                          f" — dropping the step")
                    continue
                normalized.append(step)
            plan = normalized or [{"type": "pass"}]

            # Ensure plan ends with pass
            if not plan or plan[-1].get("type") != "pass":
                plan.append({"type": "pass"})

            print(f"{self.provider_tag} [PLAN] {len(plan)} actions: "
                  f"{[a.get('type') + ':' + str(a.get('card', a.get('permanent', '')))[:15] for a in plan]}")
            return plan

        except Exception as e:
            print(f"{self.provider_tag} [PLAN] Error: {e}")
            self._check_circuit_breaker(e)
            return [{"type": "pass"}]

    async def decide_attackers(self, game: GameState, player_index: int) -> List[str]:
        """Have Claude decide which creatures to attack with during DECLARE_ATTACKERS.

        Returns list of creature names to attack with, or empty list for no attacks.
        """
        if self._api_disabled:
            return []  # Circuit breaker tripped — no API calls
        player = game.players[player_index]
        opponent = game.players[1 - player_index]

        # Find eligible attackers (untapped, non-summoning-sick, non-phased-out creatures)
        eligible = []
        for c in player.creatures():  # Uses creatures() which excludes phased-out
            if c.tapped:
                continue
            # Summoning sick creatures can't attack unless they have haste
            # Use has_haste() which checks keywords, temp_keywords, _granted_keywords, AND equipment
            if c.summoning_sick and not c.has_haste():
                continue
            eligible.append(c)

        if not eligible:
            print(f"{self.provider_tag} No eligible attackers, skipping combat")
            return []

        state_desc = self._describe_game_state(game, player_index)

        # Disambiguation: when multiple eligible attackers share a name (e.g.
        # several Plant tokens), give each a unique "_1"/"_2" suffix so the AI
        # can signal which ones to attack with. Without this, the AI says
        # "Plant" once and only one of N tokens attacks.
        from collections import Counter as _Counter
        _atk_counts = _Counter(c.name for c in eligible)
        atk_disamb_map: Dict[str, Card] = {}
        _idx_per_name: Dict[str, int] = {}
        for _c in eligible:
            if _atk_counts[_c.name] > 1:
                _i = _idx_per_name.get(_c.name, 1)
                _idx_per_name[_c.name] = _i + 1
                atk_disamb_map[f"{_c.name}_{_i}"] = _c
            else:
                atk_disamb_map[_c.name] = _c

        def _eff_pt(c):
            try:
                return f"{c.get_effective_power(game)}/{c.get_effective_toughness(game)}"
            except Exception:
                return f"{c.power}/{c.toughness}"
        creature_desc = "\n".join([
            f"- {dn} ({_eff_pt(c)}){' [token]' if getattr(c, 'is_token', False) else ''}"
            f"{' [keywords: ' + ', '.join(c.keywords) + ']' if c.keywords else ''}"
            for dn, c in atk_disamb_map.items()
        ])
        # Give the AI an explicit hint when multiple same-named creatures exist,
        # so it lists each one (Shark_1, Shark_2, Shark_3) instead of "Shark".
        multi_name_hint = ""
        _dup_names = [n for n, count in _atk_counts.items() if count > 1]
        if _dup_names:
            multi_name_hint = (
                f"\nIMPORTANT: Multiple creatures share a name ({', '.join(_dup_names)}). "
                f"Each has a unique suffix (e.g. {_dup_names[0]}_1, {_dup_names[0]}_2). "
                f"List EACH attacker separately in your attackers array, using the suffixed names."
            )

        opp_creatures = [c for c in opponent.battlefield if c.is_creature()]
        opp_desc = ""
        if opp_creatures:
            opp_desc = "\nOPPONENT'S CREATURES (potential blockers):\n" + "\n".join([
                f"- {c.name} ({_eff_pt(c)}){'(T)' if c.tapped else ''}"
                for c in opp_creatures
            ])

        # Lethal-attack heuristic: if total power of unblockable attackers is
        # >= opponent's life, broadcast a clear "ATTACK FOR LETHAL" nudge so
        # the AI doesn't hold creatures back and drag the game out (bug #34).
        try:
            opp_life = opponent.life
            untapped_opp = [c for c in opp_creatures if not c.tapped]
            unblockable_power = 0
            total_power = 0
            for c in eligible:
                _p = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else (c.power or 0)
                try:
                    _p = int(_p)
                except Exception:
                    _p = 0
                total_power += _p
                # A creature is unblockable here only if opponent has zero
                # untapped creatures. Flying/menace matching is expensive
                # to compute cleanly; leave that to the AI.
                if not untapped_opp:
                    unblockable_power += _p
            lethal_hint = ""
            if unblockable_power >= opp_life and opp_life > 0:
                lethal_hint = (
                    f"\n🚨 ATTACK FOR LETHAL: Opponent has {opp_life} life and NO "
                    f"untapped creatures. Unblockable attackers total {unblockable_power} "
                    f"power. You MUST attack with every non-vigilance creature unless you "
                    f"need them to block a known lethal threat on their turn — this ends the game."
                )
            elif total_power >= opp_life and opp_life <= 10:
                lethal_hint = (
                    f"\n⚔️ POTENTIAL LETHAL: Opponent at {opp_life} life; your attackers "
                    f"total {total_power} power. If blocks can't save them, attack with "
                    f"everything that doesn't need to stay back."
                )
            elif not untapped_opp and total_power > 0:
                # May 20 audit (Bug D): the May 20 batch had 2 stagnation-draw
                # games because UW Control sat on 2/1 Snapcasters for 40+ turns
                # while Rick had ZERO creatures in play. The lethal hints
                # above only fire when damage ≥ opp_life, but chip damage
                # against an empty board is risk-free even at low power: any
                # attacker > 0 power deals damage to face with no risk of
                # losing the creature (no blockers exist).
                lethal_hint = (
                    f"\n💥 EASY DAMAGE: Opponent has ZERO creatures in play. ANY attacker "
                    f"with power ≥ 1 deals chip damage with no risk of being blocked. "
                    f"Attack with EVERY creature unless it has a defensive purpose "
                    f"(needs to flash-block a known threat on their turn, or to untap "
                    f"for Vigilance / mana-source activation). Refusing to swing 2/1s "
                    f"into an empty board is the #1 cause of stagnation draws — "
                    f"chip damage compounds: a 2-power attacker brings a 20-life "
                    f"opponent to 0 in 10 turns, a 1-power in 20 turns."
                )
        except Exception:
            lethal_hint = ""

        # May 14 audit (A8): wire strategist memo into attack decisions too.
        # The strategist often identifies attack pressure as the key win path
        # or a key hold-back (e.g. "leave Tymna home to draw on hit"), and the
        # actor wasn't seeing it.
        strategy_memo = getattr(game, '_strategy_memo', '') or self._strategy_memo
        memo_section = ""
        if strategy_memo:
            memo_section = (
                f"\n=== STRATEGIST MEMO (from earlier turn) ===\n"
                f"{strategy_memo}\n"
                f"==========================================\n"
                f"The memo is a labeled 4-section briefing. For ATTACK decisions, "
                f"weight `This turn:` (does it tell you to swing or hold?) and "
                f"`Hold for opp turn:` (creatures named there should stay back to "
                f"untap and block / activate instants).\n"
            )

        prompt = f"""You are playing Magic: The Gathering. It's the DECLARE ATTACKERS step.

{state_desc}

YOUR CREATURES THAT CAN ATTACK (already filtered — all listed creatures are untapped, not summoning-sick, and legally able to attack this turn):
{creature_desc}
{opp_desc}
{lethal_hint}
{memo_section}
Decide which creatures to attack with. Consider:
- Opponent's life total and your damage potential
- Opponent's untapped creatures that could block
- Whether you need creatures untapped for defense
- Trample, flying, and other evasion keywords
- Risk of losing creatures to blocks
- ALL listed creatures above CAN attack — they have been pre-validated (haste creatures are included even if just played)
- Do NOT attack with 0-power creatures (they deal no damage and just die to blocks)
- Do NOT send small creatures into obviously larger untapped blockers unless you have a trick or need to trigger "whenever attacks" abilities
- If your attacks have been prevented for several turns (Teferi's Protection, Fog effects), consider NOT attacking until the effect expires
- ARISTOCRATS / SACRIFICE DECKS: if your board has 5+ creatures and most are token-shaped (Saproling, Servo, Thrull, Soldier) or sacrifice-outlet-shaped (Carrion Feeder, Viscera Seer, Yawgmoth, Korvold), bias toward SWINGING WIDE. Tokens that get blocked-and-die are FUEL for Blood Artist / Zulaport Cutthroat / Mayhem Devil / Pitiless Plunderer triggers — the death is the value, not the damage. A 4-token swing into 1 blocker = 3 damage + a creature dying for your engine. The May 15 audit found aristocrats decks attacked only 10/67 turns with 5+ creatures down because the AI defaulted to "hold the engine" — the engine IS the attack.

{multi_name_hint}

RESPONSE FORMAT — a JSON object with just the attackers list (no "reasoning" field — keep output minimal):
{{"attackers": ["Goblin Guide", "Monastery Swiftspear"]}}
{{"attackers": []}}

IMPORTANT: Output ONLY the JSON object. No text before or after."""

        try:
            # No prefill — Sonnet 4.6+ doesn't support ending with assistant message
            _atk_kwargs = dict(
                model=self.model,
                max_tokens=600,  # Bumped for <think> scratchpad
                messages=[
                    {"role": "user", "content": prompt},
                ],
            )
            if hasattr(self.client.messages, '_log_tag'):
                _atk_kwargs['purpose'] = 'decide_attackers'
            response = await asyncio.to_thread(
                self.client.messages.create,
                **_atk_kwargs,
            )

            self._track_usage(response)

            raw_text = response_text(response).strip()
            print(f"{self.provider_tag} Attackers response: {raw_text[:200]}")

            # Strip <think> scratchpad
            text = self._strip_think_tags(raw_text, context="attackers")

            if "```" in text:
                match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
                text = match.group(1) if match else "[]"

            # Sanitize common JSON issues from Claude responses
            text = text.strip()
            text = re.sub(r',\s*\]', ']', text)  # trailing commas
            text = text.replace('&quot;', '"').replace('&#39;', "'")
            # Handle set notation: {"name"} → ["name"]
            if text.startswith('{') and '"' in text and ':' not in text:
                text = text.replace('{', '[').replace('}', ']')
            # Handle {"attackers": [...], "reasoning": "..."} wrapper
            if text.startswith('{') and ':' in text:
                try:
                    wrapper = json.loads(text)
                    if isinstance(wrapper, dict):
                        # Log reasoning if present
                        if 'reasoning' in wrapper:
                            print(f"{self.provider_tag} [REASONING] Attackers: {wrapper['reasoning'][:150]}")
                        # Extract the list from any key (attackers, creatures, attack, etc.)
                        for v in wrapper.values():
                            if isinstance(v, list):
                                text = json.dumps(v)
                                break
                except json.JSONDecodeError:
                    pass
            # Extract array if wrapped in extra text
            if not text.startswith('['):
                arr_match = re.search(r'\[.*\]', text, re.DOTALL)
                if arr_match:
                    text = arr_match.group(0)
                else:
                    # Handle single card name without brackets (e.g., just "Goblin Guide")
                    # or comma-separated names without brackets
                    if '"' in text or any(c.name.lower() in text.lower() for c in eligible):
                        # Try to extract quoted names
                        quoted = re.findall(r'"([^"]+)"', text)
                        if quoted:
                            text = json.dumps(quoted)
                        else:
                            # Try comma-separated unquoted names
                            text = '[]'
                    else:
                        text = '[]'

            # Fix truncated JSON (missing closing bracket)
            if text.startswith('[') and not text.endswith(']'):
                # Remove trailing partial entry and close
                text = re.sub(r',\s*"[^"]*$', '', text)  # remove partial quoted string
                text = text.rstrip(', ') + ']'

            attackers = self._safe_json_loads(text, fallback=[])
            if isinstance(attackers, list):
                # Resolve each entry to a specific eligible creature by id so
                # multiple same-named tokens each get a slot. Priority order:
                # disambiguated-name exact ("Plant_2") → base-name exact
                # (consumes next unused same-named card) → fuzzy partial.
                valid: List[str] = []
                used_ids: Set[str] = set()
                disamb_lower = {dn.lower(): (dn, c) for dn, c in atk_disamb_map.items()}
                base_groups: Dict[str, List[Card]] = {}
                for _c in eligible:
                    base_groups.setdefault(_c.name.lower(), []).append(_c)
                # Expand "Shark x3" / "3x Shark" / "3 Sharks" syntax into 3 entries.
                expanded_attackers: List = []
                for _entry in attackers:
                    if isinstance(_entry, str):
                        _m = re.match(r'^(.+?)\s*[xX]\s*(\d+)\s*$', _entry.strip())
                        if not _m:
                            _m = re.match(r'^(\d+)\s*[xX]?\s+(.+)$', _entry.strip())
                            if _m:
                                _count, _nm = int(_m.group(1)), _m.group(2).strip().rstrip('s')
                            else:
                                expanded_attackers.append(_entry)
                                continue
                        else:
                            _nm, _count = _m.group(1).strip(), int(_m.group(2))
                        for _ in range(max(1, _count)):
                            expanded_attackers.append(_nm)
                    else:
                        expanded_attackers.append(_entry)
                attackers = expanded_attackers

                for name in attackers:
                    if isinstance(name, dict):
                        name = name.get("name") or name.get("creature") or name.get("card") or ""
                    if not isinstance(name, str):
                        continue
                    clean_name = re.sub(r'\s*\(\d+/\d+\)\s*$', '', name).strip()
                    name_lower = clean_name.lower()
                    chosen: Optional[Card] = None
                    if name_lower in disamb_lower:
                        _, cand = disamb_lower[name_lower]
                        if cand.id not in used_ids:
                            chosen = cand
                    if chosen is None and name_lower in base_groups:
                        for cand in base_groups[name_lower]:
                            if cand.id not in used_ids:
                                chosen = cand
                                break
                    if chosen is None:
                        # Fuzzy partial match ("Monastery Swift" → Swiftspear)
                        for ename_lower, group in base_groups.items():
                            if name_lower in ename_lower or ename_lower in name_lower:
                                for cand in group:
                                    if cand.id not in used_ids:
                                        chosen = cand
                                        break
                                if chosen is not None:
                                    break
                    if chosen is not None:
                        used_ids.add(chosen.id)
                        valid.append(chosen.name)

                # May 7 audit: AI repeatedly tried to attack with Birds of
                # Paradise (game 1501940665267912816 t13/18). 0-power
                # attackers deal no damage and either bounce off chump
                # blockers or just sit there tapped. Filter them post-hoc
                # — leave one in if it's the ONLY attacker (might trigger
                # "whenever a creature you control attacks" effects).
                filtered_valid: List[str] = []
                dropped_zero_power: List[str] = []
                for name in valid:
                    chosen_card = next(
                        (c for c in eligible if c.name == name and c.id in used_ids),
                        None,
                    )
                    if chosen_card is None:
                        # Couldn't re-identify — keep it; better to attack than drop silently.
                        filtered_valid.append(name)
                        continue
                    try:
                        eff_p = chosen_card.get_effective_power(game)
                    except Exception:
                        eff_p = int(chosen_card.power or 0)
                    if eff_p <= 0:
                        dropped_zero_power.append(name)
                        used_ids.discard(chosen_card.id)
                        continue
                    filtered_valid.append(name)
                for dropped in dropped_zero_power:
                    print(f"[ATTACK-VALIDATE] Filtered 0-power attacker: {dropped}")
                valid = filtered_valid

                print(f"{self.provider_tag} Attacking with: {valid}")
                return valid
            else:
                print(f"{self.provider_tag} Invalid attackers format: {type(attackers)}")
                return []

        except Exception as e:
            print(f"{self.provider_tag} Attackers decision error: {e}")
            self._check_circuit_breaker(e)
            return []

    async def decide_blocks(self, game: GameState, player_index: int, attackers: List[Card]) -> Dict[str, List[str]]:
        """Have Claude decide how to block attacking creatures."""
        if self._api_disabled:
            print(f"[COMBAT] decide_blocks: API disabled (circuit breaker), returning {{}}")
            return {}  # Circuit breaker tripped — no API calls
        player = game.players[player_index]
        blockers = player.untapped_creatures()

        if not blockers:
            # May 2 audit: previously this returned silently. Log so post-game
            # debugging can tell "Claude has no creatures and physically can't
            # block" apart from "Claude was asked and chose not to block."
            # When Toxic Deluge killed Sythis (Claude's only creature) on
            # turn 17 and she had no other creatures by turn 28, the silent
            # return looked like a block bug. It wasn't — there was nothing
            # to block with.
            total_attack = 0
            for c in attackers:
                try:
                    total_attack += c.get_effective_power(game)
                except Exception:
                    total_attack += int(c.power or 0)
            print(f"[COMBAT] {player.name} has no untapped creatures to block "
                  f"({len(attackers)} attackers totaling {total_attack} power; "
                  f"life {player.life}). Returning empty blocks.")
            return {}
        
        state_desc = self._describe_game_state(game, player_index)
        
        # Build disambiguation maps: when multiple creatures share a name (e.g. 6 Plant tokens),
        # append _1, _2 suffixes so the AI can distinguish them in its response
        from collections import Counter
        atk_counts = Counter(c.name for c in attackers)
        atk_name_map = {}  # disambiguated_name -> card object
        atk_idx = {}
        for c in attackers:
            if atk_counts[c.name] > 1:
                i = atk_idx.get(c.name, 1)
                atk_idx[c.name] = i + 1
                dname = f"{c.name}_{i}"
            else:
                dname = c.name
            atk_name_map[dname] = c

        blk_counts = Counter(c.name for c in blockers)
        blk_name_map = {}  # disambiguated_name -> card object
        blk_idx = {}
        for c in blockers:
            if blk_counts[c.name] > 1:
                i = blk_idx.get(c.name, 1)
                blk_idx[c.name] = i + 1
                dname = f"{c.name}_{i}"
            else:
                dname = c.name
            blk_name_map[dname] = c

        # Short, direct prompt — long prompts with game state cause Claude to write
        # reasoning essays instead of JSON. Keep it concise for reliable parsing.
        def _pt_pair(c):
            try:
                return f"{c.get_effective_power(game)}/{c.get_effective_toughness(game)}"
            except Exception:
                return f"{c.power}/{c.toughness}"
        # Include keyword/evasion markers on attackers so the AI sees Trample
        # and the "if unblocked you die" math is visible.
        def _atk_desc(dn):
            c = atk_name_map[dn]
            kws = []
            for kw in ('Trample', 'Flying', 'Menace', 'Deathtouch', 'Double Strike', 'First Strike', 'Lifelink'):
                try:
                    if c.has_keyword(kw, game=game):
                        kws.append(kw.lower())
                except Exception:
                    pass
            kw_str = f"[{','.join(kws)}]" if kws else ""
            return f"{dn}({_pt_pair(c)}){kw_str}"
        simple_atk = ", ".join(_atk_desc(dn) for dn in atk_name_map)
        simple_blk = ", ".join(f"{dn}({_pt_pair(blk_name_map[dn])})" for dn in blk_name_map)

        # Compute total unblocked damage if the AI blocks nothing, so it can
        # see when NOT blocking is lethal (bug #35).
        try:
            total_incoming = 0
            for dn, c in atk_name_map.items():
                try:
                    total_incoming += int(c.get_effective_power(game))
                except Exception:
                    total_incoming += int(c.power or 0)
            lethal_nudge = ""
            if total_incoming >= player.life and player.life > 0 and blockers:
                lethal_nudge = (
                    f"\n⚠️ IF YOU BLOCK NOTHING, you take {total_incoming} damage and die at "
                    f"{player.life} life. You MUST chump-block (throw at least one creature "
                    f"at a trample-or-non-trample attacker) to stay alive."
                )
        except Exception:
            lethal_nudge = ""

        # Apr 30 audit fix #26: explicit chump-blocking guidance for wide
        # token boards. Claude was chump-blocking with valuable Beasts while
        # leaving 1-power token attackers unblocked (Brawl game). Help it see
        # that "trade your weakest blocker against the strongest attacker"
        # and "let token attackers through unblocked is fine if they're weak"
        # are both legitimate strategies depending on board state.
        chump_guidance = (
            "BLOCKING GUIDANCE:\n"
            "- Chump-block the BIGGEST trampling/lethal attacker first.\n"
            "- For wide swarms (many small attackers), absorb damage from the SMALLEST attackers "
            "instead of trading away your big blockers — your 5/5 Beast is worth more than "
            "blocking a 1-power token, even if you'd survive the trade.\n"
            "- Tokens that die after combat are 'free' damage absorbers — use cheap tokens to "
            "block valuable attackers if you have the option.\n"
            "- Don't leave the LARGEST attackers unblocked unless you have lethal next turn.\n"
        )
        # May 14 audit (A8): wire strategist memo into block decisions. The
        # strategist often calls out which creature is worth saving and which
        # to throw at lethal attackers; without the memo, the actor blocks
        # value creatures against tokens.
        strategy_memo = getattr(game, '_strategy_memo', '') or self._strategy_memo
        memo_section = ""
        if strategy_memo:
            memo_section = (
                f"=== STRATEGIST MEMO (from earlier turn) ===\n{strategy_memo}\n"
                f"==========================================\n"
                f"The memo is a labeled 4-section briefing. For BLOCK decisions, "
                f"weight `Opp threats & answers:` (which attackers must be killed "
                f"and which can be chumped/ignored) and `Win condition:` (don't "
                f"trade your win-condition creature into a 1/1 token).\n"
            )

        prompt = (
            f"MTG blocking decision. Your life: {player.life}.\n"
            f"Attackers: [{simple_atk}]\n"
            f"Your blockers: [{simple_blk}]\n"
            f"{lethal_nudge}\n\n"
            f"{memo_section}"
            f"{chump_guidance}\n"
            f"RESPONSE FORMAT — JSON object with just blocks (no \"reasoning\" field — keep output minimal):\n"
            f"{{\"blocks\": {{\"Tarmogoyf\": [\"Snapcaster Mage\"], \"Goblin Guide\": []}}}}\n"
            f"Empty list [] = don't block that attacker. Output ONLY the JSON."
        )

        # Retry up to 3 times on failure
        for attempt in range(3):
            try:
                current_prompt = prompt

                # Run in thread pool to avoid blocking Discord's event loop
                # No prefill — Sonnet 4.6+ doesn't support ending with assistant message
                _blk_kwargs = dict(
                    model=self.model,
                    max_tokens=800,  # Bumped for <think> scratchpad
                    messages=[
                        {"role": "user", "content": current_prompt},
                    ],
                )
                if hasattr(self.client.messages, '_log_tag'):
                    _blk_kwargs['purpose'] = 'decide_blocks'
                response = await asyncio.to_thread(
                    self.client.messages.create,
                    **_blk_kwargs,
                )

                # Track usage
                self._track_usage(response)

                raw_text = response_text(response).strip()
                if not raw_text:
                    print(f"[COMBAT] Claude returned empty response (attempt {attempt + 1})")
                    continue

                # Strip <think> scratchpad
                text = self._strip_think_tags(raw_text, context="blocks")

                # Extract JSON from code fences if present
                if "```" in text:
                    fence_match = re.search(r'```(?:json)?\s*(.*?)\s*```', text, re.DOTALL)
                    text = fence_match.group(1) if fence_match else text
                # Try to find JSON object or array in the response even without fences
                if not text.startswith('{') and not text.startswith('['):
                    json_match = re.search(r'\{.*\}', text, re.DOTALL)
                    if json_match:
                        text = json_match.group(0)
                    else:
                        arr_match = re.search(r'\[.*\]', text, re.DOTALL)
                        if arr_match:
                            text = arr_match.group(0)
                        else:
                            print(f"[COMBAT] Claude response has no JSON (attempt {attempt + 1}): {text[:100]}")
                            continue
                elif text.startswith('['):
                    # Strip trailing } from outer object wrapping (e.g. ["Zombie"]})
                    text = text.rstrip('}').rstrip()

                # Sanitize common JSON issues
                text = re.sub(r',\s*\}', '}', text)  # trailing commas before }
                text = re.sub(r',\s*\]', ']', text)  # trailing commas before ]
                text = text.replace('&quot;', '"').replace('&#39;', "'")

                # Fix truncated JSON (missing closing brace)
                if text.startswith('{') and not text.endswith('}'):
                    # Remove trailing partial entry and close
                    text = re.sub(r',\s*"[^"]*$', '', text)
                    text = text.rstrip(', ') + '}'

                # May 14 audit: track whether the parse actually succeeded so we
                # can route a parse failure to the heuristic fallback INSTEAD of
                # silently returning {} blocks. Game 1 of the audit: strategist
                # explicitly said "I must block to survive" against 28 lethal
                # trample damage, JSON parse failed on a comma, engine returned
                # {} blocks, Claude went 18 → -10 with 7 untapped blockers idle.
                _parse_sentinel = object()
                raw_result = self._safe_json_loads(text, fallback=_parse_sentinel)
                _json_parse_failed = (raw_result is _parse_sentinel)
                if _json_parse_failed:
                    raw_result = {}
                # [FIX-6] Normalize list block formats to dict format.
                # AI sometimes returns [[blocker, attacker], ...] instead of {attacker: [blocker]}.
                if isinstance(raw_result, list):
                    if len(raw_result) == 0:
                        raw_result = {}
                    else:
                        converted = {}
                        for item in raw_result:
                            if isinstance(item, (list, tuple)) and len(item) >= 2:
                                # Determine which element is attacker vs blocker.
                                # Convention varies; try to match against known attackers.
                                # We'll add both orientations and let the resolver pick the valid one.
                                blocker_name, attacker_name = str(item[0]), str(item[1])
                                converted.setdefault(attacker_name, []).append(blocker_name)
                            elif isinstance(item, str):
                                # Flat list of blocker names with no attacker info — unusable
                                pass
                        if converted:
                            print(f"[COMBAT] Converted list blocks to dict: {converted}")
                            raw_result = converted
                        else:
                            print(f"[COMBAT] AI returned non-parseable list for blocks, treating as no blocks")
                            raw_result = {}
                # Log and strip reasoning key
                if isinstance(raw_result, dict) and 'reasoning' in raw_result:
                    print(f"{self.provider_tag} [REASONING] Blocks: {raw_result['reasoning'][:150]}")
                    del raw_result['reasoning']
                # Handle {"blocks": {...}} wrapper
                if isinstance(raw_result, dict):
                    for v in raw_result.values():
                        if isinstance(v, dict) and all(isinstance(vv, (list, str)) for vv in v.values()):
                            raw_result = v
                            break

                # Resolve attacker/blocker names through disambiguation maps → card IDs.
                # Returns {attacker_card_id: [blocker_card_ids]} for unambiguous registration.
                _resolve_card = _resolve_annotated_card_name

                result = {}
                used_blocker_ids = set()  # Prevent same blocker assigned twice
                for atk_name, blk_names in raw_result.items():
                    atk_card = _resolve_card(atk_name, atk_name_map)
                    if not atk_card:
                        print(f"[COMBAT] Could not resolve attacker '{atk_name}'")
                        continue

                    blocker_ids = []
                    blk_items = blk_names if isinstance(blk_names, list) else [blk_names]
                    for bn in blk_items:
                        if isinstance(bn, dict):
                            bn = bn.get("name") or bn.get("blocker") or bn.get("card") or ""
                        if not isinstance(bn, str) or not bn:
                            continue
                        blk_card = _resolve_card(bn, blk_name_map)
                        if blk_card and blk_card.id not in used_blocker_ids:
                            blocker_ids.append(blk_card.id)
                            used_blocker_ids.add(blk_card.id)

                    if blocker_ids:
                        result[atk_card.id] = blocker_ids

                # May 14 audit: if JSON parse failed AND we have blockers but
                # ended up with no blocks assigned AND life is in danger, fall
                # through to the heuristic block-pairing instead of returning
                # empty. Catastrophic empty-block bug in audit Game 1.
                def _atk_power(a):
                    try:
                        if hasattr(a, 'get_effective_power'):
                            return max(0, a.get_effective_power(game))
                        return max(0, int(a.power))
                    except (ValueError, TypeError, AttributeError):
                        return 0
                incoming_damage = sum(_atk_power(a) for a in (attackers or []))
                if (_json_parse_failed and not result
                        and blockers and attackers
                        and player.life <= incoming_damage):
                    print(f"[COMBAT] JSON parse failed and life is in lethal range "
                          f"({player.life} life vs {incoming_damage} incoming) — "
                          f"forcing heuristic block fallback instead of empty blocks")
                    raise RuntimeError("JSON parse failed under lethal threat — fall to heuristic")

                # Bug fix: double braces were escaping the dict comprehension, printing literal code
                summary = {k[:8]: v for k, v in list(result.items())[:5]}
                print(f"[COMBAT] {player.name} blocking decision (attempt {attempt + 1}): {summary}")
                return result

            except Exception as e:
                print(f"[COMBAT] {player.name} blocking decision error (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    await asyncio.sleep(1)  # Brief pause before retry

        # Fallback: pair all blockers with attackers by descending power
        print("[COMBAT] Claude blocking failed, using fallback strategy")
        if not blockers or not attackers:
            return {}
        try:
            def _safe_power(c):
                try:
                    return c.get_effective_power(game) if hasattr(c, 'get_effective_power') else int(c.power)
                except (ValueError, TypeError):
                    return 0
            def _safe_toughness(c):
                try:
                    return c.get_effective_toughness(game) if hasattr(c, 'get_effective_toughness') else int(c.toughness)
                except (ValueError, TypeError):
                    return 0
            sorted_attackers = sorted(attackers, key=_safe_power, reverse=True)
            sorted_blockers = sorted(blockers, key=_safe_toughness, reverse=True)
            blocks = {}
            for i, attacker in enumerate(sorted_attackers):
                if i < len(sorted_blockers):
                    blocks[attacker.id] = [sorted_blockers[i].id]
            print(f"[COMBAT] Fallback blocks: {len(blocks)} assignments")
            return blocks
        except Exception as e:
            print(f"[COMBAT] Fallback blocking also failed: {e}")
            return {}

    async def decide_mulligan(self, hand: List[Card], mulligans_taken: int) -> bool:
        """Decide whether to mulligan."""
        hand_desc = "\n".join([f"- {c.name} ({c.mana_cost})" for c in hand])
        
        prompt = f"""You are playing Magic: The Gathering and must decide whether to mulligan.

Your hand ({len(hand)} cards):
{hand_desc}

Mulligans already taken: {mulligans_taken}

Consider:
- Mana curve and land count
- Playable cards in early turns
- Win conditions or threats
- How many mulligans already taken

Respond with ONLY "keep" or "mulligan"."""

        try:
            # Run in thread pool to avoid blocking Discord's event loop
            _mull_kwargs = dict(
                model=self.model,
                max_tokens=50,
                messages=[{"role": "user", "content": prompt}],
            )
            # May 17 audit: tag for [CALL-BREAKDOWN] purpose attribution.
            if hasattr(self.client.messages, '_log_tag'):
                _mull_kwargs['purpose'] = 'decide_mulligan'
            response = await asyncio.to_thread(
                self.client.messages.create, **_mull_kwargs
            )

            # Track usage
            self._track_usage(response)

            text = response_text(response).strip().lower()
            return "mulligan" in text
            
        except Exception as e:
            print(f"Claude mulligan decision error: {e}")
            return False  # Keep by default
    
    def has_instant_speed_cards(self, player: Player) -> List[Card]:
        """Pre-filter: return instant/flash cards in hand. Empty list = auto-pass (no API call)."""
        instants = []
        for card in player.hand:
            if card.is_instant():
                instants.append(card)
            elif card.oracle_text and 'flash' in card.oracle_text.lower():
                instants.append(card)
        return instants

    def _can_meaningfully_respond(self, instants: List[Card], stack_card: Optional[Card]) -> bool:
        """Cheap pre-filter for decide_response: does the player have any
        instant in hand that could plausibly interact with the spell on the
        stack? Returns True if so (or if we can't tell), False if no
        instant has any interaction-shaped oracle text.

        Saves an API call per stack-priority window when the player's
        instants are all utility (cantrips, ramp, scry) and can't actually
        affect what's resolving. May 15 batch had 2371 of these calls with
        an 86% pass rate — even a 30% reduction here is hundreds of saved
        API calls per batch.

        May 23 audit (Hypothesis #2): veto rate observed at only 1% in the
        May 23 batch (12 of 1148 prompts). Reason: most decks have at least
        one counter/removal instant, so the "ANY interaction-shaped" check
        almost never gates. Add a complementary check for stack-spell EV:
        when the stack spell is low-value utility (basic ramp / mana rocks /
        cantrips), countering it is almost never correct regardless of
        what's in hand — skip the LLM call.
        """
        if stack_card is None:
            return True  # Combat priority window without stack spell — keep the API path.

        # May 23 audit: low-EV stack-spell filter. These don't impact the
        # board in ways worth a counter — countering Sol Ring trades 1-for-
        # 1 but Sol Ring's net mana advantage is only ~1, so it's roughly
        # mana-neutral while burning a card. Counters are valuable; don't
        # spend them on these.
        LOW_EV_STACK_SPELLS = {
            "sol ring", "arcane signet", "mind stone", "thought vessel",
            "talisman of dominance", "talisman of progress", "talisman of unity",
            "talisman of indulgence", "talisman of impulse", "talisman of curiosity",
            "talisman of conviction", "talisman of resilience", "talisman of creativity",
            "talisman of hierarchy",
            "fellwar stone", "prismatic lens", "coldsteel heart",
            "wayfarer's bauble", "mind stone", "fountain of ichor",
            # Basic dorks (these enter and tap-for-mana — no ETB worth countering)
            "llanowar elves", "elvish mystic", "fyndhorn elves", "arbor elf",
            "birds of paradise", "noble hierarch", "deathrite shaman",
            # Cantrips (replace themselves; counter is a 1-for-1 trade where
            # they get a new card for free).
            "ponder", "preordain", "brainstorm", "opt", "consider",
            # Land searches at sorcery speed (already resolved past instant)
            # — included for completeness in case they end up on stack via
            # a flash-grant.
        }
        stack_name_lower = (stack_card.name or '').lower()
        if stack_name_lower in LOW_EV_STACK_SPELLS:
            # June 10 audit (A4-F13): distinct log line — the generic
            # caller-side "no interaction-shaped instant in hand" message
            # read as a classifier bug when the hand DID hold Counterspell.
            # This branch is a deliberate value judgment, not a miss.
            print(f"[STACK-AI] Auto-pass: {stack_card.name} is low-EV to counter "
                  f"(deliberate skip, not a hand-classification miss)")
            return False  # Auto-pass — countering this is not worth it.
        # Classify the stack spell — is it a spell we'd want to counter or
        # an ETB-creature we'd want to destroy?
        stack_oracle = (stack_card.oracle_text or '').lower()
        stack_is_creature_or_pw = stack_card.is_creature() or stack_card.is_planeswalker()
        stack_targets_us = 'target' in stack_oracle  # rough heuristic — might target our stuff
        # Look at each instant in hand for response-shaped text.
        for card in instants:
            oracle = (card.oracle_text or '').lower()
            # Counterspell-shaped: countering ANY spell helps.
            if 'counter target' in oracle or 'counter the next' in oracle:
                return True
            # Removal/damage-shaped: usable on creatures/PWs entering or on stack.
            if any(p in oracle for p in (
                'destroy target creature', 'destroy target permanent',
                'exile target creature', 'exile target permanent',
                'destroy target planeswalker',
                'deal damage to any target', 'deal damage to target',
            )):
                if stack_is_creature_or_pw:
                    return True
            # Bounce: useful against ETB creatures and pump auras.
            if 'return target' in oracle and 'to its owner' in oracle:
                if stack_is_creature_or_pw:
                    return True
            # Protection / fog / shield: usable when something targets us.
            if any(p in oracle for p in (
                'prevent all damage', 'phases out', 'gain protection',
                'gains hexproof', 'gains indestructible',
            )):
                if stack_targets_us or stack_is_creature_or_pw:
                    return True
            # Combat trick — only relevant in combat. If no combat context, skip.
            if 'gets +' in oracle and 'until end of turn' in oracle:
                continue  # too narrow to call a meaningful response without combat
            # Free / pitch counters (Force of Will, Force of Negation, Solitude).
            # These are situationally hard to detect — preserve the LLM call.
            if 'without paying' in oracle or 'rather than pay' in oracle:
                return True
        return False

    async def decide_response(self, game: GameState, player_index: int,
                               stack_spell_name: str, stack_controller: str) -> Optional[Dict]:
        """Decide whether to respond to a spell on the stack with an instant/flash card.

        Returns None (pass) or {"type": "cast", "card": "Counterspell", "target": "stack_top"}.
        Only called when the player actually has instants/flash in hand (pre-filtered).
        """
        player = game.players[player_index]
        instants = self.has_instant_speed_cards(player)
        if not instants:
            return None  # No instants — auto-pass
        # June 11 audit: the engine's pre-filter counts AFFORDABLE instants,
        # but this list offered the LLM every instant in hand — it picked
        # unpayable ones 305 times in one batch (doomed cast + retry each
        # time). Filter here so the prompt only shows real options and the
        # downstream vetoes are affordability-aware.
        try:
            # July 20: alternate-cost aware — Force of Will is castable off
            # 1 life + a blue card even at zero mana (was dead in hand).
            _affordable = [c for c in instants
                           if player.can_pay_mana_cost(c.mana_cost, spending_card=c)[0]
                           or player.can_pay_printed_alternate_cost(c)]
            if len(_affordable) < len(instants):
                _dropped = [c.name for c in instants if c not in _affordable]
                print(f"[STACK-AI] {player.name}: filtered unaffordable instants from "
                      f"response options: {_dropped[:4]}")
            instants = _affordable
            # July 20 batch-3 audit: a {0} Pact is only a real option if its
            # upkeep cost is plausibly payable NEXT turn. Claude countered a
            # turn-3 Sylvan Library with Pact of Negation on ~3 mana sources
            # and auto-lost to the unpayable {3}{U}{U} at his upkeep
            # (game_1528942795019255889 — engine correct, decision suicidal).
            # Filter pacts when the controller's battlefield could not cover
            # the followup cost even fully untapped.
            _kept = []
            for c in instants:
                if not (getattr(c, 'cmc', None) == 0 and 'pact' in c.name.lower()):
                    _kept.append(c)
                    continue
                _pay_m = re.search(r'pay ((?:\{[^}]+\})+)', c.oracle_text or '', re.IGNORECASE)
                _needed = 0
                if _pay_m:
                    for _sym in re.findall(r'\{([^}]+)\}', _pay_m.group(1)):
                        _needed += int(_sym) if _sym.isdigit() else 1
                _potential = 0
                for _b in player.battlefield:
                    try:
                        _prod = player._get_mana_production(_b) or {}
                    except (ValueError, KeyError, AttributeError, TypeError):
                        _prod = {}
                    if _prod:
                        _potential += max(_prod.values())
                if _needed and _potential < _needed:
                    print(f"[STACK-AI] {player.name}: filtered {c.name} — pact upkeep "
                          f"cost needs {_needed} mana, only {_potential} producible on battlefield")
                else:
                    _kept.append(c)
            instants = _kept
        except (ValueError, KeyError, AttributeError, TypeError, IndexError) as _aff_err:
            print(f"[STACK-AI] affordability filter error (offering full list): {_aff_err}")
        if not instants:
            return None  # Nothing affordable — auto-pass

        # May 16 audit: cheap pre-filter for "I literally have nothing that
        # could respond meaningfully" cases. Saves an API call when hand is
        # all cantrips / ramp / utility. 86% of decide_response calls in the
        # May 15 batch ended in PASS — this catches a chunk before LLM.
        stack_top = game.stack[-1].card if game.stack else None
        if not self._can_meaningfully_respond(instants, stack_top):
            instant_names = [c.name for c in instants]
            print(f"[STACK-AI] {player.name} auto-passes — no interaction-shaped "
                  f"instant in hand ({instant_names[:3]})")
            return None

        instant_list = "\n".join(f"- {c.name} ({c.mana_cost}): {(c.oracle_text or '')[:100]}" for c in instants)
        available_mana = player.available_mana()

        # Build stack context — show all items so AI can see counter-counter opportunities
        stack_context = ""
        if game.stack:
            stack_lines = []
            for i, entry in enumerate(reversed(game.stack)):
                position = i + 1
                card_name = entry.card.name if hasattr(entry, 'card') and entry.card else "Unknown"
                ctrl = entry.controller_name if hasattr(entry, 'controller_name') else "?"
                target_str = ""
                if entry.target:
                    target_str = f" targeting {entry.target.name if hasattr(entry.target, 'name') else entry.target}"
                stack_lines.append(f"  {position}. {card_name} ({ctrl}){target_str}")
            stack_context = f"\nThe Stack (top = resolves first):\n" + "\n".join(stack_lines)
            # Top-of-stack summary: prefer the stack entry's card+controller directly
            # rather than re-parsing the formatted line (which mangles names containing
            # periods, e.g. "Return of the Wildspeaker" gets truncated at "Return of").
            top_entry = list(reversed(game.stack))[0] if game.stack else None
            if top_entry is not None:
                top_name = top_entry.card.name if hasattr(top_entry, 'card') and top_entry.card else "Unknown"
                top_ctrl = top_entry.controller_name if hasattr(top_entry, 'controller_name') else "?"
                top_tgt = ""
                if getattr(top_entry, 'target', None):
                    top_tgt = f" targeting {top_entry.target.name if hasattr(top_entry.target, 'name') else top_entry.target}"
                stack_context += f"\n\nYou may respond to the TOP of the stack ({top_name} ({top_ctrl}){top_tgt})."

        # Build combat context if in a combat priority window
        combat_context = ""
        if getattr(game, 'combat_priority_window', None):
            window = game.combat_priority_window
            if game.attackers:
                attacker_names = []
                for aid in game.attackers:
                    for p in game.players:
                        for c in p.battlefield:
                            if c.id == aid:
                                attacker_names.append(c.name)
                combat_context += f"\nCombat: {window}. Attackers: {', '.join(attacker_names) or 'none'}."
            if game.blockers:
                blocker_info = []
                for atk_id, blk_ids in game.blockers.items():
                    atk_name = atk_id
                    blk_names = []
                    for p in game.players:
                        for c in p.battlefield:
                            if c.id == atk_id:
                                atk_name = c.name
                            if c.id in blk_ids:
                                blk_names.append(c.name)
                    blocker_info.append(f"{atk_name} blocked by {', '.join(blk_names)}")
                combat_context += f"\nBlockers: {'; '.join(blocker_info)}."
            if window == "after_attackers":
                combat_context += "\nConsider: removal spells to kill an attacker before blocks are declared."
            elif window == "after_blockers":
                combat_context += "\nConsider: combat tricks (pump, protection) to save your creatures or deal extra damage."

        # Identify threat level of the top stack spell
        threat_note = ""
        if game.stack:
            top_entry = game.stack[-1]
            top_spell = top_entry.card if hasattr(top_entry, 'card') and top_entry.card else None
            if top_spell:
                top_oracle = (top_spell.oracle_text or '').lower()
                top_type = (top_spell.type_line or '').lower()
                top_cmc = getattr(top_spell, 'cmc', 0) or 0
                is_pw = 'planeswalker' in top_type
                is_wipe = any(w in top_oracle for w in ['destroy all', 'exile all', 'each creature gets', 'deals damage to each'])
                # Apr 30 audit fix #24: counterspell hand-hoarding was endemic.
                # Expand high-value list with cards the audit flagged as
                # game-deciding when uncountered (Avenger landfall, Sylvan
                # Awakening, Scute Swarm, Omnath, etc.). Also flag any spell
                # CMC ≥ 6 as high-value by default — big-mana threats
                # justify a counter unless the controller is mana-screwed.
                high_value_names = {
                    'teferi, time raveler', 'craterhoof behemoth', 'cyclonic rift',
                    'expropriate', 'rhystic study', 'smothering tithe', 'dockside extortionist',
                    'demonic tutor', 'vampiric tutor', 'time warp', 'extra turn',
                    # Game-deciding from Apr 30 audit
                    'avenger of zendikar', 'omnath, locus of creation', 'omnath, locus of rage',
                    'omnath, locus of mana', 'sylvan awakening', 'scute swarm', 'sun titan',
                    'panharmonicon', 'doubling season', 'parallel lives', 'mystic remora',
                    'sneak attack', 'eldrazi conscription', 'consecrated sphinx',
                    # Combo enablers
                    'thassa\'s oracle', 'jace, wielder of mysteries', 'laboratory maniac',
                    'food chain', 'dramatic reversal', 'isochron scepter',
                }
                is_named_threat = top_spell.name.lower() in high_value_names
                is_big_mana = top_cmc >= 6
                if is_pw or is_wipe or is_named_threat or is_big_mana:
                    why = []
                    if is_pw: why.append("planeswalker")
                    if is_wipe: why.append("board wipe")
                    if is_named_threat: why.append("known game-decider")
                    if is_big_mana: why.append(f"CMC {top_cmc}")
                    threat_note = (f"\n⚠️ HIGH-VALUE TARGET: {top_spell.name} ({', '.join(why)}). "
                                   f"Countering this prevents it from ever resolving — strongly prefer countering unless you have a better use for the mana THIS TURN.")

        # Combat window context
        if not game.stack and getattr(game, 'combat_priority_window', None):
            combat_header = f"You are in a COMBAT priority window ({game.combat_priority_window}). You may cast any instant-speed spell NOW. Your creatures may take damage this combat."
        elif game.stack:
            combat_header = f"OPPONENT ({stack_controller}) CAST: {stack_spell_name}{threat_note}\nIf you PASS, this resolves immediately and permanently."
        else:
            combat_header = f"Priority window — no spell on stack."

        # May 14 audit (A8): wire strategist memo into decide_response so the
        # AI has its earlier-turn strategic context when deciding whether to
        # counter / respond. Without this, the strategist's "save Counterspell
        # for Stoneforge / counter Ragavan / kill Wrenn" guidance was invisible
        # at exactly the priority window where it mattered most.
        strategy_memo = getattr(game, '_strategy_memo', '') or self._strategy_memo
        memo_section = ""
        if strategy_memo:
            memo_section = (
                f"\n=== STRATEGIST MEMO (from earlier turn — may be stale) ===\n"
                f"{strategy_memo}\n"
                f"========================================================\n"
                f"The memo is a labeled 4-section briefing. For INSTANT-SPEED "
                f"RESPONSE, the most relevant line is `Hold for opp turn:` — "
                f"if the memo named a specific spell or play type to counter/"
                f"respond to and that's what's on the stack right now, this is "
                f"the moment to use the named answer.\n"
            )

        prompt = (
            f"MTG instant-speed response window.\n"
            f"{combat_header}\n"
            f"{stack_context}{combat_context}\n"
            f"Your life: {player.life}. Opponent life: {game.players[1 - player_index].life}.\n"
            f"Mana available: {available_mana}\n"
            f"Instant-speed options:\n{instant_list}\n"
            f"{memo_section}\n"
            # Apr 30 audit fix #24: explicit guidance on when to counter.
            f"GUIDANCE: Counterspells are only useful in this exact window. "
            f"If the spell on the stack is a high-value target (planeswalker, board wipe, big creature with ETB) "
            f"and you can afford to counter, you SHOULD cast a counter unless you need that mana for a better play this turn. "
            f"Holding counters indefinitely is the worst outcome — they stay in hand the whole game and lose value. "
            f"If you have removal/protection that responds well here, use it.\n\n"
            f"Respond with \"pass\" or \"cast CardName\"."
        )
        try:
            _resp_kwargs = dict(
                model=self.model,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            if hasattr(self.client.messages, '_log_tag'):
                _resp_kwargs['purpose'] = 'decide_response'
            response = await asyncio.to_thread(
                self.client.messages.create,
                **_resp_kwargs,
            )
            self._track_usage(response)

            raw_text = response_text(response).strip()
            text = raw_text.lower()

            # Try parsing as JSON first — DeepSeek/Claude sometimes wraps the answer
            # in {"decision": "...", "card": "..."} / {"response": "cast X"} / {"action": "cast"} /
            # {"cast": "X"} / {"counter": "X"} etc.
            try:
                import json as _json
                _candidate = raw_text
                # Strip common code-fence wrappers
                if _candidate.startswith('```'):
                    _candidate = re.sub(r'^```[a-zA-Z]*\n?', '', _candidate)
                    _candidate = re.sub(r'\n?```$', '', _candidate)
                parsed = _json.loads(_candidate)
                if isinstance(parsed, dict):
                    # Look for any of these keys for the decision string
                    for key in ("decision", "response", "action", "answer", "choice"):
                        val = parsed.get(key)
                        if isinstance(val, str):
                            text = val.strip().lower()
                            break
                    # Explicit card field wins — "cast"/"counter"/"card"/"target"/"spell"
                    explicit_card = None
                    for ck in ("card", "target", "cast", "counter", "spell", "name"):
                        cv = parsed.get(ck)
                        if isinstance(cv, str) and cv.strip() and cv.strip().lower() not in ("pass", "none", "null", "stack_top", "opponent"):
                            explicit_card = cv.strip()
                            break
                    # Affirmative "yes" / "counter" / "counter it" / "respond" responses
                    # imply we want to cast SOMETHING. If no explicit card was named,
                    # try to match the decision text against the instants list, and if
                    # that fails, default to the cheapest counterspell/removal.
                    affirmative_markers = (
                        "yes", "counter", "counter it", "counter the", "respond",
                        "intervene", "stop", "fizzle",
                    )
                    is_affirmative = text in affirmative_markers or (
                        any(text.startswith(m + " ") for m in affirmative_markers)
                    )
                    if not explicit_card and is_affirmative and instants:
                        # Try name match inside the decision text first
                        for inst in instants:
                            if inst.name.lower() in text:
                                explicit_card = inst.name
                                break
                        # Otherwise pick the cheapest affordable instant that
                        # actually counters spells (since the AI said "counter").
                        if not explicit_card:
                            counter_instants = sorted(
                                (c for c in instants
                                 if 'counter' in (c.oracle_text or '').lower()
                                 and 'target' in (c.oracle_text or '').lower()
                                 and 'spell' in (c.oracle_text or '').lower()),
                                key=lambda x: (x.cmc or 99)
                            )
                            if counter_instants:
                                explicit_card = counter_instants[0].name
                    if explicit_card and text != "pass":
                        # Strip "targeting X" suffix if present
                        explicit_card = re.sub(r'\s+targeting\s+.*$', '', explicit_card, flags=re.IGNORECASE).strip()
                        # Resolve the card object up-front so both vetoes
                        # below can reference it. Must be assigned before
                        # any branch reads it (was the source of a silent
                        # UnboundLocalError that turned every response into
                        # a pass).
                        chosen_card_obj = None
                        for c in instants:
                            if c.name.lower() == explicit_card.lower():
                                chosen_card_obj = c
                                break
                        # Guard: if the stack is empty (combat priority window or
                        # no spell to counter), refuse to return a pure counter-
                        # spell. It would just fizzle and waste mana.
                        if not game.stack and chosen_card_obj:
                            co = (chosen_card_obj.oracle_text or '').lower()
                            is_pure_counter = (
                                'counter target' in co and 'spell' in co
                                and 'choose one' not in co
                                and 'choose two' not in co
                            )
                            if is_pure_counter:
                                print(f"[STACK-AI] Vetoed response '{explicit_card}' — counterspell with empty stack, passing instead")
                                return None
                        # Veto targeted removal at instant speed when no
                        # opposing creatures exist. Mirrors PLAN-VALIDATE
                        # logic for main-phase plans — without this, AI
                        # casts Path to Exile / Doom Blade in response to
                        # opponent's spell at empty board, wasting the card.
                        if chosen_card_obj:
                            co = (chosen_card_obj.oracle_text or '').lower()
                            opp_idx = 1 - player_index
                            opp_creatures = [
                                c for c in game.players[opp_idx].battlefield
                                if c.is_creature() and not getattr(c, '_phased_out', False)
                            ]
                            is_targeted_removal = (
                                ('destroy target' in co or 'exile target' in co)
                                and 'creature' in co
                                and 'permanent' not in co
                                and 'artifact' not in co
                                and 'enchantment' not in co
                                # June 11 audit: self-flicker (Ephemerate) is
                                # protection, not removal.
                                and not ('target creature you control' in co and 'return' in co)
                                and not chosen_card_obj.is_creature()
                            )
                            if is_targeted_removal and not opp_creatures:
                                print(f"[STACK-AI] Vetoed response '{explicit_card}' — targeted creature removal with no opposing creatures, passing instead")
                                return None
                        # June 11 audit: the model named a card outside the
                        # affordable-instants list 305 times in one batch;
                        # each pick burned a doomed cast attempt + retry.
                        # Veto up-front instead of failing downstream.
                        if chosen_card_obj is None:
                            print(f"[STACK-AI] Vetoed response '{explicit_card}' — not in affordable instants list, passing instead")
                            return None
                        return {"type": "cast", "card": explicit_card, "target": "stack_top"}
                    if text == "pass":
                        return None
            except (ValueError, ImportError):
                pass

            # Extract just the decision — strip any reasoning preamble
            for line in text.split('\n'):
                line = line.strip()
                # Strip leading list/quote markers and trailing punctuation
                line = re.sub(r'^[\-\*\d\.\)\>\"\'\s]+', '', line).rstrip('.,!?"\'')
                if line.startswith('cast ') or line == 'pass':
                    text = line
                    break
            print(f"[STACK-AI] Response decision: {text}")

            if text.startswith("cast "):
                card_name = text[5:].strip().rstrip('.,!?"\'')
                # Strip trailing "targeting ..." / "on ..." / "to ..." clauses
                card_name = re.sub(r'\s+(?:targeting|on|at|to|against)\s+.*$', '', card_name, flags=re.IGNORECASE).strip()
                if card_name:
                    # Mirror the JSON-path guard: don't return a counterspell
                    # when the stack is empty (prevents Dovin's Veto-at-combat
                    # misplays, Apr 2026 audit).
                    if not game.stack:
                        for c in instants:
                            if c.name.lower() == card_name.lower():
                                co = (c.oracle_text or '').lower()
                                if ('counter target' in co and 'spell' in co
                                        and 'choose one' not in co
                                        and 'choose two' not in co):
                                    print(f"[STACK-AI] Vetoed response '{card_name}' — counterspell with empty stack, passing instead")
                                    return None
                                break
                    # June 11 audit: same affordability veto as the JSON path.
                    if not any(c.name.lower() == card_name.lower() for c in instants):
                        print(f"[STACK-AI] Vetoed response '{card_name}' — not in affordable instants list, passing instead")
                        return None
                    return {"type": "cast", "card": card_name, "target": "stack_top"}

            # Last-resort fallback: the response may name an instant from hand without
            # a "cast" prefix ("Counterspell.", "- Counterspell"). Match against the
            # instant list so we don't silently pass when the AI clearly chose a card.
            #
            # May 25 audit (F13): the bare substring fallback misfired in 9 events
            # across 4 games — the model returned "pass" + chain-of-thought prose
            # explaining WHY ("swords to plowshares is removal for opponent's
            # creatures, using it on your own attackers would be counterproductive"),
            # and the substring match found the card name in the reasoning and
            # cast it. The spell then fizzled with no valid target, wasting mana.
            # Before substring-matching, check for explicit pass/no-action intent.
            # If present, short-circuit to pass — the model has decided not to cast,
            # the card name in the prose is just reasoning context.
            raw_lower = text.strip().rstrip('.,!?"\'').lower()
            pass_intent_markers = (
                'pass',
                'no response',
                'no action',
                'skip',
                "won't cast",
                'will not cast',
                "wouldn't cast",
                'would not cast',
                'counterproductive',
                'not worth',
                'hold this',
                'hold for later',
                'save for',
                'better to hold',
            )
            if any(m in raw_lower for m in pass_intent_markers):
                print(f"[STACK-AI] Pass-intent detected in response prose — not substring-matching card name")
                return None
            if raw_lower and raw_lower != "pass":
                for inst in instants:
                    if inst.name.lower() in raw_lower:
                        print(f"[STACK-AI] Name-match fallback: '{text}' → {inst.name}")
                        return {"type": "cast", "card": inst.name, "target": "stack_top"}
            return None  # pass
        except Exception as e:
            print(f"[STACK-AI] decide_response error: {e}")
            return None  # pass on error

    def _describe_game_state(self, game: GameState, player_index: int) -> str:
        """Describe game state from a player's perspective."""
        player = game.players[player_index]
        opponent = game.players[1 - player_index]
        
        # Count creatures for strategic decisions
        my_creatures = [c for c in player.battlefield if c.is_creature()]
        opp_creatures = [c for c in opponent.battlefield if c.is_creature()]
        
        lines = [
            f"GAME STATE (Turn {game.turn_number}):",
            f"",
            f"YOU ({player.name}):",
            f"  Life: {player.life}, Poison: {player.poison}",
            f"  Library: {len(player.library)} cards",
            f"  Hand: {len(player.hand)} cards",
            f"  Creatures you control: {len(my_creatures)}",
        ]
        
        if player.battlefield:
            # Bug #35: Show tapped, P/T, summoning sick, and counters for AI visibility
            bf_parts = []
            for c in player.battlefield:
                desc = c.name
                if c.is_creature():
                    p = c.get_effective_power(game)
                    t = c.get_effective_toughness(game)
                    desc += f" {p}/{t}"
                if c.tapped:
                    desc += "(T)"
                if c.summoning_sick and c.is_creature() and not c.has_haste():
                    desc += "(sick)"
                counters = {k: v for k, v in c.counters.items() if v > 0}
                if counters:
                    counter_str = ", ".join(f"{v} {k}" for k, v in counters.items())
                    desc += f"[{counter_str}]"
                bf_parts.append(desc)
            lines.append(f"  Battlefield: {', '.join(bf_parts)}")

        if player.graveyard:
            gy = [c.name for c in player.graveyard[-5:]]
            lines.append(f"  Graveyard (recent): {', '.join(gy)}")

        # Command zone (Commander format)
        if player.command_zone:
            cmd_parts = []
            for c in player.command_zone:
                tax = c.times_cast_from_command_zone * 2
                tax_str = f" (tax: {{{tax}}})" if tax > 0 else ""
                cmd_parts.append(f"{c.name} ({c.mana_cost}){tax_str}")
            lines.append(f"  COMMAND ZONE: {', '.join(cmd_parts)}")

        # Companion zone
        if player.companion_zone:
            comp_parts = [f"{c.name} ({c.mana_cost}) [pay {{3}} to move to hand]" for c in player.companion_zone]
            lines.append(f"  COMPANION: {', '.join(comp_parts)}")

        lines.extend([
            f"",
            f"OPPONENT ({opponent.name}):",
            f"  Life: {opponent.life}, Poison: {opponent.poison}",
            f"  Hand: {len(opponent.hand)} cards",
            f"  Creatures they control: {len(opp_creatures)}",
        ])
        
        if opponent.battlefield:
            bf_parts = []
            for c in opponent.battlefield:
                desc = c.name
                if c.is_creature():
                    p = c.get_effective_power(game)
                    t = c.get_effective_toughness(game)
                    desc += f" {p}/{t}"
                if c.tapped:
                    desc += "(T)"
                counters = {k: v for k, v in c.counters.items() if v > 0}
                if counters:
                    counter_str = ", ".join(f"{v} {k}" for k, v in counters.items())
                    desc += f"[{counter_str}]"
                bf_parts.append(desc)
            lines.append(f"  Battlefield: {', '.join(bf_parts)}")

        # Show pending unresolved effects the AI should handle
        if game.pending_resolves:
            lines.append("")
            lines.append("⚠️ PENDING EFFECTS (use resolve action to handle!):")
            for pr in game.pending_resolves:
                lines.append(f"  - {pr}")

        # Show planeswalkers with abilities for visibility
        my_pws = [c for c in player.battlefield if c.is_planeswalker()]
        if my_pws:
            lines.append("")
            lines.append("YOUR PLANESWALKERS (use activate action!):")
            for pw in my_pws:
                loyalty = getattr(pw, 'loyalty_counters', 0)
                lines.append(f"  - {pw.name} (loyalty: {loyalty})")

        return "\n".join(lines)

    # July 30, 2026: _get_graveyard_castable moved to
    # mtg/legal_actions.py (graveyard_castable_entries) — part of the
    # single legal-actions provider; both former callers were the two
    # castable builders, which now consume castable_labels().

    def _describe_hand(self, player: Player) -> str:
        """Describe player's hand."""
        if not player.hand:
            return "YOUR HAND: (empty)"

        lines = ["YOUR HAND:"]
        for card in player.hand:
            desc = f"  - {card.name} {card.mana_cost} - {card.type_line}"
            if card.adventure_name:
                desc += f" [Adventure: {card.adventure_name} {card.adventure_cost} — {card.adventure_text}]"
            lines.append(desc)

        return "\n".join(lines)
