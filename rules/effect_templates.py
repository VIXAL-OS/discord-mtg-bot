"""
ETB & Trigger Effect Template Library (Tier 1.5)
==================================================

Data-driven effect resolution that sits between:
- Tier 1: Hardcoded card-specific handlers (fast, reliable, limited)
- Tier 2: SpellResolver regex patterns (covers ~40% of effects)  
- Tier 3: Claude API resolve_effect (covers everything, costs tokens)

This module provides:
1. Card-name-indexed lookup for common ETB/trigger effects
2. Oracle text pattern matching for effect families
3. Action generators that produce the same JSON format as resolve_effect

The action format matches _execute_action_on_state in mtg_game.py:
  {"action": "deal_damage", "amount": N, "target_player": "name"}
  {"action": "draw_cards", "player": "name", "amount": N}
  {"action": "gain_life", "player": "name", "amount": N}
  {"action": "lose_life", "player": "name", "amount": N}
  {"action": "destroy", "card": "Card Name"}
  {"action": "move_card", "card": "X", "from_zone": "Z", "to_zone": "Z", "player": "name"}
  {"action": "create_token", "player": "name", "name": "N", "power": P, "toughness": T, "types": "...", "count": N}
  {"action": "add_counters", "card": "X", "counter_type": "+1/+1", "amount": N}
  {"action": "tap", "card": "X"}
  {"action": "untap", "card": "X"}
  {"action": "add_mana", "player": "name", "color": "C", "amount": N}
  {"action": "discard", "player": "name", "card": "random"}
  {"action": "no_action", "reason": "why"}

Inspired by XMage's effect class taxonomy (DamageTargetEffect, DrawCardEffect, etc.)
but implemented as pure Python pattern matching — no Java required.
"""

import json
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Callable
from dataclasses import dataclass, field

# Optional targeting validation for build_game_context target filtering
try:
    from rules.targeting_helpers import _validate_target_for_action as _tgt_validate
    _HAS_TARGET_VALIDATION = True
except ImportError:
    _HAS_TARGET_VALIDATION = False


@dataclass
class EffectTemplate:
    """A template for resolving an effect into game state actions."""
    name: str
    description: str
    # Function that generates actions given (controller_name, opponent_name, card, entering_creature_or_None)
    action_generator: Callable
    # Whether this template needs a target choice (if True, auto-targets opponent/opponent's best creature)
    needs_target: bool = False
    # Whether this effect is mandatory
    mandatory: bool = True


# =============================================================================
# JSON-backed templates (refactor #2, second half — July 20, 2026)
# =============================================================================
# The FIXED half of the name-keyed library lives in data/card_templates.json:
# entries whose action lists are constant apart from $controller/$opponent
# substitution. Adding a simple card = adding a JSON entry (the OSS
# contribution path). Generator-style templates — anything that reads the
# game context, branches, or computes — stays in Python below, as do the
# table-driven families (signets, fetchlands, counterspell variants) whose
# loops are already data.
# data/card_templates.json is validated two ways in CI: the schema check in
# _load_json_templates (raises → pytest fails on any import) and the Scryfall
# card-name validator (tools/validate_card_names.py reads the same registries
# the JSON loads into).

# Path anchored to the repo root (rules/ → repo root → data/).
CARD_TEMPLATES_JSON = Path(__file__).resolve().parent.parent / "data" / "card_templates.json"


def _substitute_placeholders(obj, ctrl: str, opp: str):
    """Deep-substitute $controller/$opponent in every string of a JSON action
    tree. Rebuilds containers, so each generated action list is a fresh
    structure — the action interpreter enriches actions in place and must
    never mutate the shared JSON master copy."""
    if isinstance(obj, str):
        return obj.replace("$controller", ctrl).replace("$opponent", opp)
    if isinstance(obj, list):
        return [_substitute_placeholders(x, ctrl, opp) for x in obj]
    if isinstance(obj, dict):
        return {k: _substitute_placeholders(v, ctrl, opp) for k, v in obj.items()}
    return obj


def _make_json_action_generator(actions: List[Dict]) -> Callable:
    """Wrap a JSON action list in the (ctrl, opp, ctx) generator interface."""
    def _generate(ctrl, opp, ctx):
        return _substitute_placeholders(actions, ctrl, opp)
    return _generate


# =============================================================================
# Token Registry — single source of truth for token stats
# =============================================================================

@dataclass(frozen=True)
class TokenDefinition:
    """Immutable definition for a token type. Used by templates and pattern generators."""
    name: str
    power: int
    toughness: int
    types: str
    keywords: Tuple[str, ...] = ()
    colors: Tuple[str, ...] = ()


# Registry key convention: "{name}_{power}_{toughness}" for creatures,
# "{name}" for non-creature artifacts. Append keywords if ambiguous.
TOKEN_REGISTRY: Dict[str, TokenDefinition] = {
    # --- Creature tokens ---
    "zombie_2_2":       TokenDefinition("Zombie", 2, 2, "Creature — Zombie", colors=("B",)),
    "squirrel_1_1":     TokenDefinition("Squirrel", 1, 1, "Creature — Squirrel", colors=("G",)),
    "goblin_1_1":       TokenDefinition("Goblin", 1, 1, "Creature — Goblin", colors=("R",)),
    "soldier_1_1":      TokenDefinition("Soldier", 1, 1, "Creature — Soldier", colors=("W",)),
    "cat_soldier_1_1":  TokenDefinition("Cat Soldier", 1, 1, "Creature — Cat Soldier", colors=("W",)),
    "insect_1_1":       TokenDefinition("Insect", 1, 1, "Creature — Insect", keywords=("Flying", "Deathtouch"), colors=("G",)),
    "insect_1_1_plain": TokenDefinition("Insect", 1, 1, "Creature — Insect", colors=("G",)),
    "thrull_1_1":       TokenDefinition("Thrull", 1, 1, "Creature — Thrull", colors=("B",)),
    "spirit_1_1_fly":   TokenDefinition("Spirit", 1, 1, "Creature — Spirit", keywords=("Flying",), colors=("W",)),
    "faerie_rogue_1_1": TokenDefinition("Faerie Rogue", 1, 1, "Creature — Faerie Rogue", keywords=("Flying",), colors=("B",)),
    "bird_2_2":         TokenDefinition("Bird", 2, 2, "Creature — Bird", colors=("U",)),
    "beast_3_3":        TokenDefinition("Beast", 3, 3, "Creature — Beast", colors=("G",)),
    "plant_0_1":        TokenDefinition("Plant", 0, 1, "Creature — Plant", colors=("G",)),
    "angel_4_4":        TokenDefinition("Angel", 4, 4, "Creature — Angel", keywords=("Flying",), colors=("W",)),
    "dragon_5_5":       TokenDefinition("Dragon", 5, 5, "Creature — Dragon", keywords=("Flying",), colors=("R",)),
    "snake_1_1_dt":     TokenDefinition("Snake", 1, 1, "Creature — Snake", keywords=("Deathtouch",), colors=("B",)),
    "elemental_5_5":    TokenDefinition("Elemental", 5, 5, "Creature — Elemental"),
    # --- Artifact creature tokens ---
    "thopter_1_1":      TokenDefinition("Thopter", 1, 1, "Artifact Creature — Thopter", keywords=("Flying",)),
    "servo_1_1":        TokenDefinition("Servo", 1, 1, "Artifact Creature — Servo"),
    "myr_1_1":          TokenDefinition("Myr", 1, 1, "Artifact Creature — Myr"),
    # --- Non-creature artifact tokens ---
    "treasure":         TokenDefinition("Treasure", 0, 0, "Artifact — Treasure"),
    "clue":             TokenDefinition("Clue", 0, 0, "Artifact — Clue"),
    "food":             TokenDefinition("Food", 0, 0, "Artifact — Food"),
    "blood":            TokenDefinition("Blood", 0, 0, "Artifact — Blood"),
}


def make_token_action(player: str, token_key: str, count: int = 1) -> Dict:
    """Create a token action dict from registry. Single source of truth for token stats."""
    tok = TOKEN_REGISTRY[token_key]
    action = {
        "action": "create_token", "player": player, "name": tok.name,
        "power": tok.power, "toughness": tok.toughness,
        "types": tok.types, "count": count,
    }
    # Forward registry keywords + colors so flying/deathtouch on Hornet Queen's
    # Insects (and similar) actually stick on the produced tokens. Without
    # these, Hornet Queen tokens were 1/1 vanilla — strategically very wrong.
    if tok.keywords:
        action["keywords"] = ",".join(tok.keywords)
    if tok.colors:
        action["colors"] = ",".join(tok.colors)
    return action


def _find_player_by_name(game, name: Optional[str]):
    """Find a player object on `game` whose name matches `name` (case-insensitive).
    Returns None if game is None or no match. Used by templates that need to
    check the OPPONENT's state (mana, hand, life) for optional-cost decisions."""
    if game is None or not name:
        return None
    target = name.lower()
    for p in getattr(game, 'players', []) or []:
        if getattr(p, 'name', '').lower() == target:
            return p
    return None


_RESIDUAL_EFFECT_VERBS = (
    'draw', 'discard', 'lose ', 'gain ', 'deals ', 'damage', 'destroy',
    'exile', 'sacrifice', 'return', 'create', 'counter target', 'search',
    'mill', 'reveals their hand', 'put a +1/+1',
)


def has_residual_clause_beyond_library_look(oracle_text: str) -> bool:
    """Does this text do something BESIDES look at / reorder the library?

    The scry, surveil and library-reorder shortcuts exist because library order
    isn't modeled, so those effects can't change visible state and a Tier-3 call
    would return nothing. But they are single-clause patterns with no exclusion
    list, and a match RESOLVES the spell — which blocked Tier 2 and Tier 3 from
    ever seeing the rest of the card. Read the Bones ("Scry 2, then draw two
    cards. You lose 2 life.") lost the draw AND the life; Notion Rain matched on
    its own surveil REMINDER TEXT and lost "draw two cards" plus 2 damage.

    Reminder text is stripped first: the scry and surveil reminders talk about
    graveyards and putting cards back, which would otherwise read as residue.
    """
    if not oracle_text:
        return False
    without_reminders = re.sub(r'\([^)]*\)', ' ', oracle_text.lower())
    return any(verb in without_reminders for verb in _RESIDUAL_EFFECT_VERBS)


def strip_activated_ability_lines(text: str) -> str:
    """Drop activated-ability lines ("<cost>: <effect>", CR 602.1) from
    oracle text before the GENERIC pattern pass.

    July 21 batch audit (R3-5, Glen Elendra Archmage): her ACTIVATED
    "{U}, Sacrifice this creature: Counter target noncreature spell."
    matched the generic counter-spell pattern on every ETB resolution
    (cast, persist return, flicker) and fired a free counter with no cost
    paid — harmless only because the stack happened to be empty. A colon
    preceded by cost-shaped text (mana/tap symbols, sacrifice, pay,
    discard, exile) or a bare loyalty number marks an activated ability;
    triggered abilities ("When/Whenever/At ...") have no such prefix.
    Name-keyed templates are unaffected — they never see this filter.
    """
    kept = []
    for line in (text or '').split('\n'):
        head = line.split(':', 1)[0] if ':' in line else ''
        head = head.strip()
        if head and len(head) <= 80 and (
                re.search(r'\{[^}]+\}|\btap\b|sacrifice|\bpay\b|discard|exile', head)
                or re.fullmatch(r'[+\-−]?\d+', head)):
            continue
        kept.append(line)
    return '\n'.join(kept)


def word_to_num(word: str) -> int:
    """Convert English number words to ints ('one' → 1, 'two' → 2, etc.).

    Also handles digit strings and 'a'/'an'. Shared between EffectTemplateLibrary
    and XMageActionTranslator to avoid duplication.
    """
    mapping = {
        'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4,
        'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    }
    try:
        return int(word)
    except (ValueError, TypeError):
        return mapping.get(word.lower(), 1)


class EffectTemplateLibrary:
    """
    Resolves effects by matching against known patterns.
    
    Usage:
        lib = EffectTemplateLibrary()
        
        # Try to resolve an ETB effect
        actions, explanation = lib.resolve_etb(
            card_name="Mulldrifter",
            oracle_text="When Mulldrifter enters, draw two cards.",
            controller="Player1",
            opponent="Claude",
            game_context={...}
        )
        
        if actions is not None:
            # Execute actions via _execute_action_on_state
            for action in actions:
                msg = engine._execute_action_on_state(game, action)
        else:
            # Not in library — fall through to tier 3 (Claude API)
            pass
    """
    
    def __init__(self):
        self._card_templates: Dict[str, EffectTemplate] = {}
        self._pattern_templates: List[Tuple[str, EffectTemplate]] = []
        self._attack_templates: Dict[str, EffectTemplate] = {}
        self._upkeep_templates: Dict[str, EffectTemplate] = {}
        # Dies/LTB templates need their own registry so cards with BOTH an ETB
        # AND a dies trigger (Solemn Simulacrum, Mulldrifter, etc.) don't have
        # one registration silently overwriting the other when keyed by name.
        self._dies_templates: Dict[str, EffectTemplate] = {}
        self._build_library()
        self._load_json_templates()

    def tier_for_card(self, card_name: str, oracle_text: str = "") -> str:
        """Classify what tier of the engine will resolve a card's effects.

        This is a pure lookup — does NOT execute the action generator, has no
        side effects, doesn't need a real game context. Used by deck-load
        coverage reports (mtg/coverage.py) to tell the user what they're in
        for before a game starts.

        Returns one of:
            "template"  — exact card-name match in this library (Tier 1.5,
                          most reliable, instant)
            "pattern"   — oracle text matches a regex pattern family (Tier
                          1.5, generic but reliable)
            "tier3"     — neither matches; will use Claude API at runtime
                          (Tier 3, slower, costs tokens). NOTE: this also
                          covers "trivial" cards with no triggerable text
                          (vanilla creatures, basic lands) — they don't
                          actually need resolution but are reported as tier3
                          by this lookup.

        Limitation: doesn't detect Tier 1 hardcoded handlers in mtg/spells.py
        and mtg/triggers.py (~15 specific cards like Terror of the Peaks,
        Warstorm Surge, Panharmonicon). Those may be reported as "tier3"
        even though they have a fast hardcoded path. For deck-coverage
        purposes this is fine — the count overstates Tier 3 fallback risk
        by at most ~15 cards.
        """
        if not card_name:
            return "tier3"
        card_key = card_name.lower().strip()
        if card_key in self._card_templates:
            return "template"
        # May 17 audit: also check dies-templates + attack-templates so cards
        # registered only on those paths (Protean Hulk, Sram via attack, etc.)
        # don't report as tier3 in coverage reports.
        if card_key in self._dies_templates:
            return "template"
        if card_key in self._attack_templates:
            return "template"
        if card_key in self._upkeep_templates:
            return "template"
        if oracle_text:
            oracle_lower = oracle_text.lower()
            for pattern, _template in self._pattern_templates:
                if re.search(pattern, oracle_lower):
                    return "pattern"
        return "tier3"

    def resolve_etb(
        self,
        card_name: str,
        oracle_text: str,
        controller: str,
        opponent: str,
        game_context: Optional[Dict] = None,
        event_type: str = "etb",
    ) -> Tuple[Optional[List[Dict]], str]:
        """
        Try to resolve an ETB effect from the library.

        Returns:
            Tuple of (actions, explanation) if resolved, or (None, "") if not in library.
            actions is a list of dicts in the resolve_effect JSON action format.

        event_type: "etb" (default) does name-based lookup. For "ltb"/"dies" pass the
            event so we skip the name-keyed ETB template (which would otherwise re-fire
            an aura's enter-effect when it leaves — Animate Dead bug).
        """
        # 1. Try exact card name match first — only for ETB/spell context.
        # Name-keyed templates encode the *enter* effect; matching them on LTB/dies
        # makes Animate Dead, Spell Queller, etc. re-fire their ETB at the wrong time.
        # Apr 29 audit: also skip name-keyed lookup for "activated" event_type.
        # Otherwise, AI "activating" Thassa, Deep-Dwelling's {3}{U} tap ability
        # silently runs its end-step exile-and-return template — a duplicate
        # trigger fires when end step actually arrives.
        if not card_name:
            return None, ""
        card_key = card_name.lower().strip()
        # Scryfall stores transforming DFC names as "front // back". The
        # name-keyed registry intentionally uses the front face, while split
        # cards may have full-name templates. Fall back only for a confirmed
        # transforming source and only when the combined key is unregistered.
        _source_card = (game_context or {}).get('_source_card')
        if (card_key not in self._card_templates and ' // ' in card_key
                and getattr(_source_card, 'has_transform', False)):
            card_key = card_key.split(' // ', 1)[0].strip()
        # May 16 audit: the outer gate used to be `event_type == "etb"`, which
        # silently bypassed the name-keyed block for every non-ETB dispatcher
        # (cast_trigger, dies, attacks, upkeep, end_step, beginning_combat).
        # The inner whenever_event_types / scheduled_event_types relaxations
        # below were dead code. Symptom: Kambal / Blind Obedience / Bitterblossom
        # / Massacre Wurm dies / etc. fired their detection log but never
        # resolved their template — 40-80 cards affected across the test matrix.
        _NAME_KEYED_EVENT_TYPES = {
            "etb",
            "cast_trigger",
            "dies",
            "attacks",
            "upkeep",
            "end_step",
            "beginning_combat",
            # July 29 batch audit: the July 27 main-phase scan added
            # "main_phase" to scheduled_event_types below but NOT here — the
            # exact May 16 Bug-B shape (inner relaxation dead behind the outer
            # gate). A name-keyed main-phase template could never fire.
            "main_phase",
        }
        # Dies/LTB triggers prefer the dies-templates registry, which holds the
        # dies-trigger half of cards that also have an ETB template
        # (Solemn Simulacrum: ETB land-search + dies draw card). Without this
        # split, only ONE side of the trigger was discoverable.
        if event_type == "dies" and card_key in self._dies_templates:
            template = self._dies_templates[card_key]
            actions = template.action_generator(controller, opponent, game_context or {})
            return actions, template.description

        # May 25 audit (F25): for scheduled events, try a suffix-keyed lookup
        # FIRST so cards with both an ETB template AND a scheduled-trigger
        # template (Agent of Treachery has ETB-steal under "agent of treachery"
        # and end-step draw under "agent of treachery endstep") don't accidentally
        # re-fire their ETB ability every end step. The old dispatcher only
        # looked up the bare card_key, so "agent of treachery endstep" was
        # unreachable — and the May 15 `is_scheduled = False` bypass let the
        # ETB-steal template fire every turn, cascading control-theft from
        # Rick's Mind Stone → Painter's Servant → Honor of the Pure across
        # consecutive end steps in game_1508575038356455638.
        # Aug 2 batch-13: "ltb" added — the Spell Queller / Detention Sphere
        # release templates were DOUBLY unreachable: "ltb" was missing from
        # this tuple AND from _NAME_KEYED_EVENT_TYPES (the latter exclusion
        # is deliberate — a bare ETB name key must not fire on LTB), so the
        # suffix key is the only sanctioned vehicle. Queller's first-ever
        # real exile (batch 15332) escalated its LTB to Tier 3, which knew
        # nothing about the linked exile. The JSON key also carried an
        # underscore ("spell queller_ltb") where this lookup builds
        # space-separated keys — both halves fixed together.
        if event_type in ("upkeep", "end_step", "beginning_combat", "ltb"):
            suffix_key = f"{card_key} {event_type.replace('_', '')}"
            # Also try the "card_name endstep"-style key (no underscore) which
            # matches the registration convention used at line ~6138.
            if suffix_key in self._card_templates:
                template = self._card_templates[suffix_key]
                try:
                    ctx = dict(game_context or {})
                    ctx['_oracle'] = (oracle_text or '').lower()
                    actions = template.action_generator(controller, opponent, ctx)
                    return actions, template.description
                except Exception as e:
                    print(f"[TEMPLATE] Error executing scheduled template for {card_name}: {e}")

        if event_type in _NAME_KEYED_EVENT_TYPES and card_key in self._card_templates:
            template = self._card_templates[card_key]
            # May 13 audit: templates for "At end step / At beginning of upkeep /
            # At beginning of combat" triggers are keyed by card name but they
            # are NOT ETB abilities. Skip them in the ETB code path so a flicker
            # that returns the creature doesn't accidentally re-fire its
            # end-step ability. Thassa, Deep-Dwelling's end-step flicker was
            # recursing 8 levels deep (capped only by FLICKER-LOOP guard)
            # because every time Thassa returned from her own flicker, this
            # ETB lookup re-triggered her end-step ability.
            desc = (template.description or "")
            desc_lower = desc.lower().lstrip()
            # May 15 audit: the original guard skipped any description starting
            # with "whenever ", but that's over-broad — "Whenever a/another
            # creature enters" IS an ETB-context trigger that's meant to fire
            # from the creature-enters scan (Rampaging Ferocidon, Trostani
            # Selesnya's Voice, Soul Warden family, etc.). Only "whenever"
            # triggers WITHOUT "enters" in the first clause are non-ETB
            # (cast/attack/dies/draw/etc.).
            first_clause = re.split(r"[.,]", desc_lower, maxsplit=1)[0]
            scheduled_prefixes = (
                "at end step", "at end of", "at the end of",
                "at beginning of", "at the beginning of",
                "at the start of",
            )
            is_scheduled = any(first_clause.startswith(p) for p in scheduled_prefixes)
            is_whenever_non_etb = (
                first_clause.startswith("whenever ") and "enters" not in first_clause
            )
            # May 15 audit: when the dispatcher already knows this is a scheduled
            # trigger (resolve_upkeep_trigger / resolve_end_step_trigger /
            # resolve_beginning_combat_trigger), the scheduled_prefixes guard
            # should NOT apply — the whole point of the call is to fire that
            # exact scheduled template. Without this, Bitterblossom's name
            # template (lose 1 life AND create a 1/1 Faerie token) gets
            # rejected and only the partial pattern-match template fires.
            #
            # May 25 audit (F25): the May 15 bypass was too permissive. When
            # the event IS scheduled but the template ISN'T (i.e., the bare
            # card-name template is an ETB template), the bypass let it fire
            # on end-step events too. Agent of Treachery's ETB-steal template
            # fired every end step, cascading control-theft from Rick's Mind
            # Stone → Painter's Servant → Honor of the Pure (one per turn).
            # Allow the bypass ONLY when the template's description actually
            # starts with a scheduled prefix; otherwise skip and fall through
            # to pattern matching (which is regex-scoped to "at <step>" anyway).
            scheduled_event_types = {"upkeep", "end_step", "beginning_combat", "main_phase"}
            if event_type in scheduled_event_types:
                # Template-vs-event matching: allow the bare card-name template
                # to fire on a scheduled event ONLY if its description starts
                # with a scheduled prefix ("at end step / at beginning of /
                # ..."). ETB templates registered under bare card name (Agent
                # of Treachery's steal-permanent template) MUST NOT fire on a
                # scheduled event — that was the May 25 cascade-steal bug.
                if is_scheduled:
                    # Template IS scheduled (description matches prefix) →
                    # allow it to fire.
                    is_scheduled = False
                else:
                    # Template ISN'T scheduled (ETB- or cast-shaped) →
                    # force the skip below so we fall through to pattern
                    # matching, which is regex-scoped to "at <step>".
                    is_scheduled = True
            # Same logic for cast triggers (Rhystic Study, Kambal, etc.) and
            # dies triggers (Blood Artist, Grave Pact). When the dispatcher
            # already knows it's a "whenever ... casts/dies" event, the
            # is_whenever_non_etb guard would otherwise skip the template
            # and fall through to pattern matching, which routinely misses
            # nuanced descriptions like "noncreature spell".
            whenever_event_types = {"cast_trigger", "dies", "attacks"}
            if event_type in whenever_event_types:
                is_whenever_non_etb = False
                is_scheduled = False
            # May 23 audit (MAJOR #12): Species Specialist's ETB template fired
            # 5 times per board wipe (game_1507611991995580537:580). The
            # whenever_event_types relaxation above lets ANY template through
            # when event_type=="dies", even ETB-shaped ones. Add a positive
            # scoping check: when event_type=="dies", the template description
            # must reference death (dies/dying/death), otherwise skip it.
            # Same for attacks (must mention attack/attacks). cast_trigger is
            # broader so leave that one alone.
            if event_type == "dies":
                dies_words = ("dies", "dying", "death", "leaves the battlefield", "graveyard")
                if not any(w in desc_lower for w in dies_words):
                    is_whenever_non_etb = True  # force fall-through to pattern matching
            elif event_type == "attacks":
                if "attack" not in desc_lower:
                    is_whenever_non_etb = True
            if is_scheduled or is_whenever_non_etb:
                # Fall through to oracle-text pattern matching (which IS scoped
                # to ETB phrasing via the regex itself).
                pass
            else:
                try:
                    # Expose oracle_text to the generator so templates that need to
                    # distinguish activation-context vs ETB-context (Viscera Seer's
                    # "Sacrifice: Scry 1") can check ctx.get('_oracle').
                    ctx = dict(game_context or {})
                    ctx['_oracle'] = (oracle_text or '').lower()
                    actions = template.action_generator(controller, opponent, ctx)
                    return actions, template.description
                except Exception as e:
                    print(f"[TEMPLATE] Error executing template for {card_name}: {e}")
        
        # 2. Try oracle text pattern matching
        if oracle_text:
            # July 21 batch audit (R3-5): generic patterns must never match
            # ACTIVATED-ability text — Glen Elendra Archmage's "{U},
            # Sacrifice...: Counter target noncreature spell." fired a free
            # counter on every ETB resolution. Name-keyed templates above
            # already saw the full text.
            oracle_lower = strip_activated_ability_lines(oracle_text.lower())
            for pattern, template in self._pattern_templates:
                match = re.search(pattern, oracle_lower)
                if match:
                    try:
                        ctx = game_context or {}
                        ctx['_match'] = match
                        ctx['_oracle'] = oracle_lower
                        ctx['_event_type'] = event_type
                        actions = template.action_generator(controller, opponent, ctx)
                        if actions is not None:
                            # May 7 audit fix #4: prefer a dynamic description_fn
                            # if the template provides one (Scry needs the parsed N).
                            description = template.description
                            try:
                                desc_fn = getattr(template, 'description_fn', None)
                                if callable(desc_fn):
                                    description = desc_fn(ctx)
                            except Exception:
                                pass
                            return actions, description
                    except Exception as e:
                        print(f"[TEMPLATE] Error with pattern '{template.name}' for {card_name}: {e}")

        return None, ""
    
    def resolve_spell(
        self,
        card_name: str,
        oracle_text: str,
        controller: str,
        opponent: str,
        game_context: Optional[Dict] = None,
    ) -> Tuple[Optional[List[Dict]], str]:
        """
        Try to resolve an instant/sorcery spell from the library.

        Same mechanism as resolve_etb — card name lookup, then oracle pattern matching.
        Uses the same template pool (many patterns are zone-agnostic).
        """
        return self.resolve_etb(
            card_name=card_name,
            oracle_text=oracle_text,
            controller=controller,
            opponent=opponent,
            game_context=game_context,
        )

    def resolve_trigger(
        self,
        trigger_card_name: str,
        trigger_oracle: str,
        entering_creature_name: str,
        entering_creature_power: int,
        entering_creature_toughness: int,
        controller: str,
        opponent: str,
        game_context: Optional[Dict] = None,
    ) -> Tuple[Optional[List[Dict]], str]:
        """
        Try to resolve a creature-enters trigger from the library.
        
        For "whenever another creature enters..." type effects.
        """
        ctx = game_context or {}
        ctx['entering_name'] = entering_creature_name
        ctx['entering_power'] = entering_creature_power
        ctx['entering_toughness'] = entering_creature_toughness
        
        return self.resolve_etb(
            card_name=trigger_card_name,
            oracle_text=trigger_oracle,
            controller=controller,
            opponent=opponent,
            game_context=ctx,
        )
    
    def resolve_dies_trigger(
        self,
        trigger_card_name: str,
        trigger_oracle: str,
        dying_creature_name: str,
        dying_creature_power: int,
        dying_creature_toughness: int,
        controller: str,
        opponent: str,
        game_context: Optional[Dict] = None,
    ) -> Tuple[Optional[List[Dict]], str]:
        """
        Try to resolve a 'whenever a creature dies' or 'when THIS dies' trigger.
        """
        ctx = game_context or {}
        ctx['dying_name'] = dying_creature_name
        ctx['dying_power'] = dying_creature_power
        ctx['dying_toughness'] = dying_creature_toughness

        return self.resolve_etb(
            card_name=trigger_card_name,
            oracle_text=trigger_oracle,
            controller=controller,
            opponent=opponent,
            game_context=ctx,
            event_type="dies",
        )

    def resolve_attack_trigger(
        self,
        trigger_card_name: str,
        trigger_oracle: str,
        attacking_creature_name: str,
        attacking_creature_power: int,
        controller: str,
        opponent: str,
        game_context: Optional[Dict] = None,
    ) -> Tuple[Optional[List[Dict]], str]:
        """
        Try to resolve a 'whenever THIS attacks' or 'whenever a creature attacks' trigger.

        Checks attack-specific templates first (to avoid matching ETB templates for
        cards that have both ETB and attack triggers, like Kogla).
        """
        ctx = game_context or {}
        ctx['attacking_name'] = attacking_creature_name
        ctx['attacking_power'] = attacking_creature_power

        # 1. Check attack-specific named templates first
        if trigger_card_name:
            card_key = trigger_card_name.lower().strip()
            if card_key in self._attack_templates:
                template = self._attack_templates[card_key]
                try:
                    actions = template.action_generator(controller, opponent, ctx)
                    return actions, template.description
                except Exception as e:
                    print(f"[ATTACK-TEMPLATE] Error executing attack template for {trigger_card_name}: {e}")

        # 2. Fall through to general resolve_etb (pattern matching)
        return self.resolve_etb(
            card_name=trigger_card_name,
            oracle_text=trigger_oracle,
            controller=controller,
            opponent=opponent,
            game_context=ctx,
            event_type="attacks",
        )

    def resolve_upkeep_trigger(
        self,
        trigger_card_name: str,
        trigger_oracle: str,
        controller: str,
        opponent: str,
        game_context: Optional[Dict] = None,
    ) -> Tuple[Optional[List[Dict]], str]:
        """
        Try to resolve an 'at the beginning of your upkeep' trigger.
        """
        ctx = game_context or {}
        card_key = (trigger_card_name or "").lower().strip()
        if card_key in self._upkeep_templates:
            template = self._upkeep_templates[card_key]
            ctx['_event_type'] = 'upkeep'
            try:
                actions = template.action_generator(controller, opponent, ctx)
                return actions, template.description
            except Exception as exc:
                print(f"[UPKEEP-TEMPLATE] Error executing upkeep template "
                      f"for {trigger_card_name}: {exc}")
        return self.resolve_etb(
            card_name=trigger_card_name,
            oracle_text=trigger_oracle,
            controller=controller,
            opponent=opponent,
            game_context=ctx,
            event_type="upkeep",
        )

    def resolve_pw_ability(
        self,
        pw_name: str,
        ability_text: str,
        controller: str,
        opponent: str,
        game_context: Optional[Dict] = None,
    ) -> Tuple[Optional[List[Dict]], str]:
        """
        Try to resolve a planeswalker ability from templates.

        Checks the PW ability template table first, then falls through to
        oracle-text pattern matching (same as resolve_etb).

        Returns:
            Tuple of (actions, explanation) if resolved, or (None, "") if not.
        """
        # 1. Try PW ability template table (keyed on pw_name_lower + ability snippet)
        pw_key = pw_name.lower().strip()
        ability_lower = ability_text.lower().strip()

        for (key_pw, key_snippet), template in self._pw_ability_templates.items():
            if key_pw in pw_key and key_snippet in ability_lower:
                try:
                    ctx = game_context or {}
                    actions = template.action_generator(controller, opponent, ctx)
                    return actions, template.description
                except Exception as e:
                    print(f"[PW-TEMPLATE] Error executing PW template for {pw_name}: {e}")

        # 2. Fall through to oracle text pattern matching
        return self.resolve_etb(
            card_name=pw_name,
            oracle_text=ability_text,
            controller=controller,
            opponent=opponent,
            game_context=game_context,
        )

    # =========================================================================
    # Library Building
    # =========================================================================

    def _build_library(self):
        """Build the full template library."""
        self._pw_ability_templates: Dict[Tuple[str, str], EffectTemplate] = {}
        self._build_card_templates()
        self._build_pattern_templates()
        self._build_pw_ability_templates()
    
    def _build_card_templates(self):
        """Card-name-indexed templates for specific well-known cards.

        Organized as data tables + factory loops to reduce redundancy.
        Complex cards with game-state-dependent logic stay as individual templates.
        """

        # =================================================================
        # DATA-DRIVEN TABLES — simple cards that differ only in parameters
        # =================================================================
        # IMPORTANT: factory loops use default-arg capture (n=count) to avoid
        # late-binding lambda bugs.

        # --- ETB DRAW: {card_name: draw_amount} ---
        DRAW_ETBS = {
            "mulldrifter": 2,
            "elvish visionary": 1,
            "wall of omens": 1,
            "wall of blossoms": 1,
            # coiling oracle: has its own template below (reveal top, land→battlefield or draw)
            "baleful strix": 1,
            "ice-fang coatl": 1,
            "fblthp, the lost": 1,
            "rogue refiner": 1,        # Fixed: was registered under "messenger's speed"
        }
        for name, count in DRAW_ETBS.items():
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=f"Draw {count} card{'s' if count != 1 else ''}",
                action_generator=lambda ctrl, opp, ctx, n=count: [
                    {"action": "draw_cards", "player": ctrl, "amount": n}
                ]))

        # --- ETB DRAW + LIFE GAIN: {card_name: (draw, life_gain)} ---
        DRAW_LIFE_ETBS = {
            "inspiring overseer": (1, 1),
            "cloudblazer": (2, 2),
        }
        for name, (draw, life) in DRAW_LIFE_ETBS.items():
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=f"Gain {life} life and draw {draw} card{'s' if draw != 1 else ''}",
                action_generator=lambda ctrl, opp, ctx, d=draw, l=life: [
                    {"action": "gain_life", "player": ctrl, "amount": l},
                    {"action": "draw_cards", "player": ctrl, "amount": d}
                ]))

        # --- ETB LIFE GAIN: {card_name: life_amount} ---
        LIFE_GAIN_ETBS = {
            "thragtusk": 5,
            "loxodon hierarch": 4,
            "obstinate baloth": 4,
        }
        for name, amount in LIFE_GAIN_ETBS.items():
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=f"Gain {amount} life",
                action_generator=lambda ctrl, opp, ctx, n=amount: [
                    {"action": "gain_life", "player": ctrl, "amount": n}
                ]))

        # --- ETB DRAIN: {card_name: (opponent_loses, you_gain)} ---
        DRAIN_ETBS = {
            "siege rhino": (3, 3),
        }
        for name, (drain, gain) in DRAIN_ETBS.items():
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=f"Each opponent loses {drain} life, you gain {gain} life",
                action_generator=lambda ctrl, opp, ctx, d=drain, g=gain: [
                    {"action": "lose_life", "player": opp, "amount": d},
                    {"action": "gain_life", "player": ctrl, "amount": g}
                ]))

        # --- ETB TOKEN CREATION: {card_name: (registry_key, count)} ---
        # Uses TOKEN_REGISTRY for stats — single source of truth
        TOKEN_ETBS = {
            "grave titan":              ("zombie_2_2",   2),
            "deranged hermit":          ("squirrel_1_1", 4),
            "myr battlesphere":         ("myr_1_1",      4),
            "siege-gang commander":     ("goblin_1_1",   3),
            "hornet queen":             ("insect_1_1",   4),
            "whirler rogue":            ("thopter_1_1",  2),
            "weaponcraft enthusiast":   ("servo_1_1",    2),
        }
        for name, (tok_key, cnt) in TOKEN_ETBS.items():
            tok = TOKEN_REGISTRY[tok_key]
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=f"Create {cnt} {tok.power}/{tok.toughness} {tok.name} token{'s' if cnt != 1 else ''}",
                action_generator=lambda ctrl, opp, ctx, k=tok_key, c=cnt: [
                    make_token_action(ctrl, k, c)
                ]))

        # --- LAND SEARCH: (card_name, basic_only, enters_tapped, search_description) ---
        LAND_SEARCH_ETBS = [
            # (name, basic_only, enters_tapped, desc)
            ("wood elves", False, False, "a Forest (basic or non)"),
            ("farhaven elf", True, True, "a basic land (tapped)"),
            ("solemn simulacrum", True, True, "a basic land (tapped)"),
            ("sakura-tribe elder", True, True, "a basic land (tapped)"),
            ("cultivator colossus", False, False, "any number of lands"),
        ]
        for name, basic_only, enters_tapped, land_desc in LAND_SEARCH_ETBS:
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=f"Search library for {land_desc}, put onto battlefield",
                action_generator=lambda ctrl, opp, ctx, _bo=basic_only, _et=enters_tapped: [
                    {"action": "search_library_land", "player": ctrl,
                     "basic_only": _bo, "enters_tapped": _et},
                ]))

        # --- HAND DISRUPTION (simplified as random discard): {card_name: description} ---
        HAND_DISRUPTION_ETBS = {
            "tidehollow sculler":   "Exile target card from opponent's hand",
            "kitesail freebooter":  "Exile noncreature, nonland card from opponent's hand",
            "elite spellbinder":    "Exile card from opponent's hand (they can cast for 2 more)",
            "grief":                "Target opponent reveals hand, you choose a nonland card, exile it",
        }
        for name, desc in HAND_DISRUPTION_ETBS.items():
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=desc,
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "discard", "player": opp, "card": "random"}
                ]))

        # --- DESTROY OPPONENT'S CREATURE: list of card names ---
        DESTROY_CREATURE_ETBS = ["ravenous chupacabra", "nekrataal"]
        for name in DESTROY_CREATURE_ETBS:
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description="Destroy target creature an opponent controls",
                action_generator=lambda ctrl, opp, ctx: self._destroy_best_creature(ctrl, opp, ctx),
                needs_target=True,
            ))

        # --- Shriekmaw: destroy target nonartifact, nonblack creature ---
        self._add_card("shriekmaw", EffectTemplate(
            name="Shriekmaw",
            description="Destroy target nonartifact, nonblack creature an opponent controls",
            action_generator=lambda ctrl, opp, ctx: self._destroy_nonblack_nonartifact_creature(ctrl, opp, ctx),
            needs_target=True,
        ))

        # --- BOUNCE CREATURE: list of card names ---
        BOUNCE_CREATURE_ETBS = ["reflector mage", "man-o'-war"]
        for name in BOUNCE_CREATURE_ETBS:
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description="Return target creature to its owner's hand",
                action_generator=lambda ctrl, opp, ctx: self._bounce_best_creature(ctrl, opp, ctx),
                needs_target=True,
            ))

        # --- NO-ETB CARDS (keywords only / X-cost system): list ---
        NO_ETB_CARDS = [
            "sphinx of the steel wind",
            "walking ballista",     # X-cost system handles counters
            "hangarback walker",    # X-cost system handles counters
            "sakura-tribe elder",   # Activated sacrifice, not ETB
            # snapcaster mage handled below with grant_flashback template
            "subtlety",             # Hard to auto-resolve
            "endurance",            # Complex zone manipulation
        ]
        for name in NO_ETB_CARDS:
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description="No ETB action",
                action_generator=lambda ctrl, opp, ctx: [],
            ))

        # Blood Artist, Zulaport Cutthroat, and Bastion of Remembrance are
        # battlefield death WATCHERS, not own-ETB effects. The authoritative
        # dies scanner in mtg.triggers handles them. Registering those words
        # as name-keyed ETB templates caused a false drain as each permanent
        # entered, and duplicated later deaths through a second route.
        # Bastion's actual ETB is the Soldier token below.
        self._add_card("bastion of remembrance", EffectTemplate(
            name="Bastion of Remembrance",
            description="Create a 1/1 white Human Soldier creature token",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl,
                 "name": "Human Soldier", "power": 1, "toughness": 1,
                 "types": "Creature - Human Soldier", "count": 1},
            ],
        ))

        # Kokusho's drain is his OWN death trigger, so it belongs in the
        # dedicated dies registry.
        self._add_dies_card("kokusho, the evening star", EffectTemplate(
            name="Kokusho, the Evening Star",
            description="Kokusho dies: each opponent loses 5 life, you gain "
                        "5 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "lose_life", "player": opp, "amount": 5},
                {"action": "gain_life", "player": ctrl, "amount": 5},
            ]))

        # --- DIES FORCE SACRIFICE: each opponent sacrifices their weakest creature ---
        DIES_FORCE_SACRIFICE = {
            "grave pact": "Grave Pact",
            "dictate of erebos": "Dictate of Erebos",
            "butcher of malakir": "Butcher of Malakir",
        }
        for name, display_name in DIES_FORCE_SACRIFICE.items():
            self._add_card(name, EffectTemplate(
                name=display_name,
                description=f"Whenever a creature you control dies, each opponent sacrifices a creature",
                action_generator=lambda ctrl, opp, ctx, dn=display_name: self._force_sacrifice_creature(ctrl, opp, ctx),
            ))

        # --- ATTACK TOKEN CREATORS: {card_name: (registry_key, count)} ---
        ATTACK_TOKEN_CREATORS = {
            "hero of bladehold":       ("soldier_1_1",     2),
            "brimaz, king of oreskos": ("cat_soldier_1_1", 1),
        }
        for name, (tok_key, cnt) in ATTACK_TOKEN_CREATORS.items():
            tok = TOKEN_REGISTRY[tok_key]
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=f"Create {cnt} {tok.power}/{tok.toughness} {tok.name} token{'s' if cnt != 1 else ''} when attacking",
                action_generator=lambda ctrl, opp, ctx, k=tok_key, c=cnt: [
                    make_token_action(ctrl, k, c)
                ]))

        # =================================================================
        # COUNTERSPELL FAMILY — data-driven with variant handlers
        # =================================================================

        def _counter_action(ctrl, opp, ctx):
            return [{"action": "counter_spell", "player": ctrl, "target": "stack_top"}]

        def _counter_and_draw(ctrl, opp, ctx):
            # Arcane Denial: "Counter target spell. Its controller may draw up
            # to two cards at the beginning of the next turn's upkeep. You draw
            # a card at the beginning of the next turn's upkeep."
            # July 23 audit (#9): both draws are DELAYED triggered abilities
            # (CR 603.7) — the previous shape resolved them inline at counter
            # resolution, handing both players a turn's worth of cards early
            # (game_1529677587935396020). upkeep_of=None means "the next upkeep,
            # whoever's it is" (engine._process_delayed_triggers only gates when
            # upkeep_of is set), which is exactly "the next turn's upkeep";
            # Pact of Negation's caster-only variant is the gated counterpart.
            # If the counter fizzles (no target), no draws happen.
            target = ctx.get('stack_top_spell')
            if not target:
                return [{"action": "no_action", "reason": "Arcane Denial: no spell to counter (fizzled)"}]
            return [
                {"action": "counter_spell", "player": ctrl, "target": "stack_top"},
                {"action": "schedule_delayed_trigger",
                 "trigger_at": "upkeep", "turn_delay": 0, "upkeep_of": None,
                 "source": "Arcane Denial",
                 "actions": [
                     {"action": "draw_cards", "player": opp, "amount": 2},
                     {"action": "draw_cards", "player": ctrl, "amount": 1},
                 ]},
            ]

        def _counter_and_drain(ctrl, opp, ctx):
            # Get CMC of the countered spell from context. Look in stack_top_cmc (set by
            # the counterspell resolution path) or countered_cmc, default to 3 as a reasonable
            # fallback since we'd rather give some mana than none.
            cmc = ctx.get('countered_cmc', 0) or ctx.get('stack_top_cmc', 0) or 3
            return [
                {"action": "counter_spell", "player": ctrl, "target": "stack_top"},
                # June 11 audit: Mana Drain creates mana at the beginning of
                # its caster's next main phase, not during counter resolution.
                {"action": "schedule_delayed_trigger",
                 "trigger_at": "main_phase", "phase_of": ctrl,
                 "source": "Mana Drain", "turn_delay": 0,
                 "actions": [{"action": "add_mana", "player": ctrl,
                              "color": "C", "amount": cmc}]},
            ]

        def _counter_and_token(ctrl, opp, ctx):
            return [
                {"action": "counter_spell", "player": ctrl, "target": "stack_top"},
                make_token_action(opp, "bird_2_2", 1),
            ]

        # Plain counterspells — all use _counter_action
        COUNTERSPELLS = [
            'counterspell', 'cancel', 'dissolve', 'dissipate', 'void shatter',
            'neutralize', 'absorb', 'undermine',
            'scatter to the winds',
            'sinister sabotage', "didn't say please",
            'force of will', 'flusterstorm',
        ]
        for name in COUNTERSPELLS:
            self._add_card(name, EffectTemplate(
                name=name.title(), description="Counter target spell",
                action_generator=_counter_action,
            ))


        # Conditional counters — "unless its controller pays {N}". June 11
        # audit: these resolved as hard counters with no payment offered
        # (Mana Leak beat a caster with the {3} floating). Autoplay pays
        # automatically when able via the counter_unless_pays action.
        def _counter_unless(cost):
            def _gen(ctrl, opp, ctx):
                return [{"action": "counter_unless_pays", "player": ctrl,
                         "target": "stack_top", "cost": cost}]
            return _gen
        CONDITIONAL_COUNTERS = {
            'mana leak': '{3}',
            'daze': '{1}',
            'miscalculation': '{2}',
            'mana tithe': '{1}',
            'censor': '{1}',
        }
        for _cc_name, _cc_cost in CONDITIONAL_COUNTERS.items():
            self._add_card(_cc_name, EffectTemplate(
                name=_cc_name.title(),
                description=f"Counter target spell unless its controller pays {_cc_cost}",
                action_generator=_counter_unless(_cc_cost),
            ))
        # Spell Pierce: noncreature-only AND conditional
        def _spell_pierce(ctrl, opp, ctx):
            if ctx.get('stack_top_is_creature', False):
                return [{"action": "no_action", "reason": "Fizzle — target is a creature spell (Spell Pierce)"}]
            return [{"action": "counter_unless_pays", "player": ctrl,
                     "target": "stack_top", "cost": "{2}"}]
        self._add_card('spell pierce', EffectTemplate(
            name="Spell Pierce",
            description="Counter target noncreature spell unless its controller pays {2}",
            action_generator=_spell_pierce,
        ))

        # Noncreature-only counterspells — fizzle if target is a creature spell
        # Bug fix: Negate/Stubborn Denial were countering creature spells during cascade
        def _noncreature_counter(ctrl, opp, ctx):
            if ctx.get('stack_top_is_creature', False):
                return [{"action": "no_action", "reason": "Fizzle — target is a creature spell (noncreature counter)"}]
            return [{"action": "counter_spell", "player": ctrl, "target": "stack_top"}]

        NONCREATURE_COUNTERS = ['negate', 'stubborn denial', "dovin's veto"]
        for name in NONCREATURE_COUNTERS:
            self._add_card(name, EffectTemplate(
                name=name.title(), description="Counter target noncreature spell",
                action_generator=_noncreature_counter,
            ))

        # July 23 audit (#8): Force of Negation is a noncreature counter with a
        # zone-replacement rider — "exile it instead of putting it into its
        # owner's graveyard." countered_to="exile" routes the countered card to
        # exile (the cast pipeline's _countered_to dispatch), instead of the
        # default graveyard drop that let the countered Rhystic Study sit
        # recoverable (game_1529677587935396020).
        def _force_of_negation(ctrl, opp, ctx):
            if ctx.get('stack_top_is_creature', False):
                return [{"action": "no_action", "reason": "Fizzle — target is a creature spell (noncreature counter)"}]
            return [{"action": "counter_spell", "player": ctrl, "target": "stack_top",
                     "countered_to": "exile"}]
        self._add_card('force of negation', EffectTemplate(
            name="Force of Negation", description="Counter target noncreature spell; exile it instead of graveyard",
            action_generator=_force_of_negation,
        ))


        # Creature-only counterspells
        def _creature_counter(ctrl, opp, ctx):
            if ctx.get('stack_top_is_creature', False) or not ctx.get('stack_top_type_known', False):
                # If creature or unknown, allow the counter
                return [{"action": "counter_spell", "player": ctrl, "target": "stack_top"}]
            return [{"action": "no_action", "reason": "Fizzle — target is not a creature spell"}]

        self._add_card('essence scatter', EffectTemplate(
            name="Essence Scatter", description="Counter target creature spell",
            action_generator=_creature_counter,
        ))

        # Counterspells with bonus effects
        self._add_card("mana drain", EffectTemplate(
            name="Mana Drain", description="Counter target spell, add colorless mana equal to its CMC",
            action_generator=_counter_and_drain,
        ))
        self._add_card("arcane denial", EffectTemplate(
            name="Arcane Denial", description="Counter target spell, its controller draws 1",
            action_generator=_counter_and_draw,
        ))

        # Pact of Negation: counter + delayed trigger at the CASTER'S next
        # upkeep — pay {3}{U}{U} or lose the game.
        # June 11 audit: the old shape (turn_delay=1, no upkeep_of, unconditional
        # lose_the_game) fired on the OPPONENT'S upkeep one turn late, after the
        # caster had tapped out on their own turn, and never attempted payment.
        # That decided 6 of 139 games in the June 11 batch.
        def _pact_of_negation(ctrl, opp, ctx):
            return [
                {"action": "counter_spell", "player": ctrl, "target": "stack_top"},
                {"action": "schedule_delayed_trigger", "trigger_at": "upkeep",
                 "turn_delay": 0, "upkeep_of": ctrl,
                 "actions": [{"action": "pay_or_lose", "player": ctrl,
                              "cost": "{3}{U}{U}", "source": "Pact of Negation",
                              "reason": "failed to pay Pact of Negation cost"}],
                 "source": "Pact of Negation"},
            ]

        # Modal spells — default to best autoplay mode choices
        # (mystic confluence: single registration lives at
        # _mystic_confluence_modal — real mode evaluation. Aug 7 registry
        # dedup removed the flat draw-3 duplicate.)

        # Aug 7 queue item Q2b: Cryptic Command reads the AI's actual mode
        # choice (ctx['_modes'], the Kolaghan's Command pattern) instead of
        # the fixed counter+draw pair. The old `_cryptic_command` def here
        # was DEAD CODE (defined, never registered — the shadowed-duplicate
        # class); the live registration was a fixed JSON pair, now retired.
        def _gen_cryptic_command(ctrl, opp, ctx):
            """Cryptic Command — choose two of four modes.

            Modes:
              1 / "counter" — Counter target spell
              2 / "bounce"  — Return target permanent to its owner's hand
              3 / "tap"     — Tap all creatures your opponents control
              4 / "draw"    — Draw a card

            Default (no modes given): counter + draw, the historical pair.
            The G5-1 modal-fizzle discriminator already lets a chosen
            non-counter mode resolve when the counter mode fizzles (CR 700.2).
            """
            modes = ctx.get('_modes')
            normalized = []
            if modes:
                name_to_idx = {'counter': 1, 'bounce': 2, 'return': 2,
                               'tap': 3, 'draw': 4}
                for m in modes:
                    if isinstance(m, int) and 1 <= m <= 4:
                        normalized.append(m)
                    elif isinstance(m, str):
                        idx = name_to_idx.get(m.lower().strip())
                        if idx:
                            normalized.append(idx)
            if not normalized:
                normalized = [1, 4]
            normalized = normalized[:2]  # "Choose two" (CR 700.2)

            actions = []
            for m in normalized:
                if m == 1:
                    actions.append({"action": "counter_spell", "player": ctrl,
                                    "target": "stack_top"})
                elif m == 2:
                    # Best opponent creature first, else any opponent
                    # nonland permanent; no legal object -> mode dropped
                    # (the fallback below keeps the spell from blanking).
                    _bounce = ctx.get('best_opponent_creature')
                    if not _bounce:
                        # Aug 9 adversarial review (B-2): filter like the
                        # primary key this falls back from.
                        _ct = ctx.get('_can_target')
                        _opp_pl = ctx.get('_opponent_player')
                        for c in (ctx.get('opponent_battlefield') or []):
                            _tl = (getattr(c, 'type_line', '') or '').lower()
                            if 'land' in _tl:
                                continue
                            if getattr(c, '_phased_out', False):
                                continue
                            if _ct is not None and not _ct(c, _opp_pl):
                                continue
                            _bounce = getattr(c, 'name', None)
                            break
                    if _bounce:
                        actions.append({"action": "move_card", "card": _bounce,
                                        "from_zone": "battlefield",
                                        "to_zone": "hand", "player": opp})
                elif m == 3:
                    actions.append({"action": "tap", "scope": "all_creatures",
                                    "target_player": opp,
                                    "types": "creatures"})
                elif m == 4:
                    actions.append({"action": "draw_cards", "player": ctrl,
                                    "amount": 1})
            if not actions:
                actions = [{"action": "draw_cards", "player": ctrl,
                            "amount": 1}]
            return actions

        self._add_card("cryptic command", EffectTemplate(
            name="Cryptic Command",
            description=("Choose two: counter / bounce / tap opponents' "
                         "creatures / draw — honors the AI's mode choice"),
            action_generator=_gen_cryptic_command,
        ))


        # =================================================================
        # COUNTER ABILITY FAMILY (Stifle effects)
        # =================================================================

        def _counter_ability_action(ctrl, opp, ctx):
            return [{"action": "counter_ability", "player": ctrl, "target": "stack_top_ability"}]

        COUNTER_ABILITY_CARDS = [
            "stifle", "trickbind", "tale's end", "voidslime", "disallow",
        ]
        for name in COUNTER_ABILITY_CARDS:
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description="Counter target triggered or activated ability",
                action_generator=_counter_ability_action,
            ))

        # =================================================================
        # INDIVIDUAL COMPLEX TEMPLATES — game-state-dependent logic
        # =================================================================

        # --- Gray Merchant (devotion-based) ---
        self._add_card("gray merchant of asphodel", EffectTemplate(
            name="Gray Merchant of Asphodel",
            description="Each opponent loses X life (X = devotion to black), you gain that much",
            action_generator=lambda ctrl, opp, ctx: self._gary_drain(ctrl, opp, ctx),
        ))

        # --- Avenger of Zendikar (land-count-based) ---
        self._add_card("avenger of zendikar", EffectTemplate(
            name="Avenger of Zendikar",
            description="Create 0/1 Plant tokens equal to lands you control",
            action_generator=lambda ctrl, opp, ctx: self._avenger_tokens(ctrl, opp, ctx),
        ))

        # --- Knight of Autumn (modal) ---
        self._add_card("knight of autumn", EffectTemplate(
            name="Knight of Autumn",
            description="Choose: destroy artifact/enchantment, +1/+1 counters, or gain 4 life",
            action_generator=lambda ctrl, opp, ctx: self._knight_of_autumn(ctrl, opp, ctx),
        ))

        # --- Removal with targeting logic ---
        self._add_card("acidic slime", EffectTemplate(
            name="Acidic Slime",
            description="Destroy target artifact, enchantment, or land",
            action_generator=lambda ctrl, opp, ctx: self._destroy_best_noncreature(ctrl, opp, ctx),
            needs_target=True,
        ))

        self._add_card("reclamation sage", EffectTemplate(
            name="Reclamation Sage",
            description="Destroy target artifact or enchantment",
            action_generator=lambda ctrl, opp, ctx: self._destroy_best_artifact_enchantment(ctrl, opp, ctx),
            needs_target=True,
        ))

        # Krosan Grip — split second prevents responses but the effect is simple destroy.
        # Split second is a property of the spell (can't be responded to), not of the effect.
        # The engine doesn't model stack interaction granularly enough to enforce split second,
        # but we at least resolve the effect correctly via template so it doesn't fizzle.
        for _kgrip_name in [
            "krosan grip",         # Split Second, destroy target artifact or enchantment
            "naturalize",          # Destroy target artifact or enchantment
            "return to nature",    # Destroy target artifact, enchantment, or put a +1/+1 counter
            "nature's claim",      # Destroy target artifact or enchantment; owner gains 4 life
            "disenchant",          # Destroy target artifact or enchantment
            "forsake the worldly", # Destroy target artifact or enchantment, then draw a card
            "fragmentize",         # Destroy target artifact or enchantment with CMC 4 or less
            "unravel the aether",  # Put target artifact or enchantment on bottom of owner's library
        ]:
            self._add_card(_kgrip_name, EffectTemplate(
                name=_kgrip_name.title(),
                description="Destroy target artifact or enchantment",
                action_generator=lambda ctrl, opp, ctx: self._destroy_best_artifact_enchantment(ctrl, opp, ctx),
                needs_target=True,
            ))

        # Nature's Claim has a mandatory rider: the destroyed permanent's
        # controller gains 4 life. Keep the target owner captured in context.
        self._add_card("nature's claim", EffectTemplate(
            name="Nature's Claim",
            description="Destroy target artifact or enchantment; its controller gains 4 life",
            action_generator=self._gen_natures_claim,
            needs_target=True,
        ))

        self._add_card("flametongue kavu", EffectTemplate(
            name="Flametongue Kavu",
            description="Deal 4 damage to target creature",
            action_generator=lambda ctrl, opp, ctx: self._damage_best_creature(ctrl, opp, ctx, 4),
            needs_target=True,
        ))

        # (inferno titan + sun titan: Aug 7 registry dedup — their single
        # registrations live further down: inferno titan's creature-first
        # version, sun titan's _gen_sun_titan with the CR 110.1
        # permanent-card filter. The _reanimate_small version here had no
        # MV-3 or permanent filter at all.)

        self._add_card("frost titan", EffectTemplate(
            name="Frost Titan",
            description="On ETB or attack, tap target permanent; it doesn't untap next untap step",
            action_generator=lambda ctrl, opp, ctx: self._tap_best_permanent(ctrl, opp, ctx),
            needs_target=True,
        ))


        self._add_card("solitude", EffectTemplate(
            name="Solitude",
            description="Exile target creature, its controller gains life equal to its power",
            action_generator=lambda ctrl, opp, ctx: self._exile_creature_with_life(ctrl, opp, ctx),
            needs_target=True,
        ))

        self._add_card("fury", EffectTemplate(
            name="Fury",
            description="Deal 4 damage divided among any number of target creatures/planeswalkers",
            action_generator=lambda ctrl, opp, ctx: self._damage_best_creature(ctrl, opp, ctx, 4),
            needs_target=True,
        ))

        self._add_card("skyclave apparition", EffectTemplate(
            name="Skyclave Apparition",
            description="Exile target nonland, nontoken permanent with MV 4 or less",
            action_generator=lambda ctrl, opp, ctx: self._exile_best_small_permanent(ctrl, opp, ctx, max_mv=4),
            needs_target=True,
        ))

        # (eternal witness: single registration lives further down — Aug 7
        # registry dedup; both read the same aliased ctx key, the kept one
        # has the cleaner no-op fallback.)

        self._add_card("meteor golem", EffectTemplate(
            name="Meteor Golem",
            description="Destroy target nonland permanent",
            action_generator=lambda ctrl, opp, ctx: self._destroy_best_nonland(ctrl, opp, ctx),
            needs_target=True,
        ))


        self._add_card("agent of treachery", EffectTemplate(
            name="Agent of Treachery",
            description="Gain control of target permanent",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "steal_permanent", "player": ctrl, "from_player": opp,
                 "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland', 'target'),
                 "source": "Agent of Treachery"},
            ],
            needs_target=True,
        ))

        self._add_card("the abyss upkeep", EffectTemplate(
            name="The Abyss",
            description="At the beginning of each player's upkeep, that player destroys a nonartifact creature they control",
            action_generator=self._gen_the_abyss_upkeep,
        ))

        # Etali, Primal Storm — attack trigger template is registered below (line ~1251)
        # with a proper etali_trigger action that actually exiles and casts cards


        self._add_card("gonti, lord of luxury", EffectTemplate(
            name="Gonti, Lord of Luxury",
            description="Look at top 4 of opponent's library, exile one face down, rest on bottom",
            action_generator=lambda ctrl, opp, ctx: self._gonti_etb(ctrl, opp, ctx),
        ))

        # July 24 batch-6 audit (reviewer D1, CRITICAL): Puppeteer Clique had
        # NO template, so Tier 2's generic EXILE regex matched its "exile it"
        # clause and exiled the CASTER'S OWN best creature immediately —
        # Woodfall Primus vanished the moment Clique entered
        # (game_1529985418743910420; the reanimation clause was dropped and
        # both the zone and controller restrictions of CR 601.2c violated).
        self._add_card("puppeteer clique", EffectTemplate(
            name="Puppeteer Clique",
            description=("Return target creature card from an opponent's "
                         "graveyard to the battlefield under your control "
                         "with haste; exile it at your next end step"),
            action_generator=lambda ctrl, opp, ctx: self._puppeteer_clique_etb(
                ctrl, opp, ctx),
        ))
        self._add_card("capricious hellraiser", EffectTemplate(
            name="Capricious Hellraiser",
            description=("Exile three random graveyard cards, choose a "
                         "noncreature nonland card among them, copy and cast it"),
            action_generator=self._gen_capricious_hellraiser_etb,
        ))

        # --- Spell templates (instants/sorceries) ---
        self._add_card("rishkar's expertise", EffectTemplate(
            name="Rishkar's Expertise",
            description="Draw cards equal to greatest power, free cast MV 5 or less",
            action_generator=self._gen_rishkars_expertise,
        ))

        self._add_card("soul's majesty", EffectTemplate(
            name="Soul's Majesty",
            description="Draw cards equal to target creature's power",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": max(1, ctx.get('greatest_power', 0))}
            ],
        ))

        self._add_card("return of the wildspeaker", EffectTemplate(
            name="Return of the Wildspeaker",
            description="Draw cards equal to greatest power (or +3/+3 to non-Humans)",
            action_generator=self._gen_return_of_wildspeaker,
        ))

        self._add_card("shamanic revelation", EffectTemplate(
            name="Shamanic Revelation",
            description="Draw a card for each creature, ferocious: gain 4 life per creature",
            action_generator=self._gen_shamanic_revelation,
        ))

        # --- Apr 28 audit: top template-backlog cards (silent "X resolves") ---

        # Diabolic Intent's sacrifice is paid during CR 601.2h in mtg.spells;
        # resolution performs only the search.
        self._add_card("diabolic intent", EffectTemplate(
            name="Diabolic Intent",
            description="Search your library for a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "count": 1,
                 "reason": "Diabolic Intent: tutor any card to hand"},
            ],
        ))
        self._add_card("primal command", EffectTemplate(
            name="Primal Command",
            description="Choose two: gain 7; put a noncreature permanent on top; recycle a graveyard; tutor a creature",
            action_generator=self._gen_primal_command,
            needs_target=True,
        ))


        # Abrupt Decay: destroy target nonland permanent with mana value 3 or less.
        # June 10 deep-dive (B10b): honor the named target when LEGAL, and
        # DECLINE when the AI names an illegal one (MV 4 Abyss) instead of
        # silently retargeting — the substitute kill (a token) fed an illegal
        # game-ending Massacre Wurm misfire in the audit game (CR 601.2c).
        def _gen_abrupt_decay(ctrl, opp, ctx):
            explicit = (ctx.get('explicit_target_name') or '').strip()
            opp_player = ctx.get('_opponent_player')
            if explicit and opp_player is not None:
                for c in opp_player.battlefield:
                    if (c.name or '').lower() == explicit.lower():
                        if c.is_land() or (getattr(c, 'cmc', 0) or 0) > 3:
                            return [{"action": "no_action",
                                     "reason": f"Abrupt Decay: {c.name} is not a legal target "
                                               f"(needs nonland permanent with mana value 3 or less)"}]
                        return [{"action": "destroy", "card": c.name,
                                 "target_controller": opp,
                                 "reason": "Abrupt Decay (CMC ≤ 3, uncounterable)"}]
            if ctx.get('best_opponent_nonland_le3'):
                return [{"action": "destroy", "card": ctx['best_opponent_nonland_le3'],
                         "target_controller": opp,
                         "reason": "Abrupt Decay (CMC ≤ 3, uncounterable)"}]
            return [{"action": "no_action", "reason": "Abrupt Decay: no legal target (CMC ≤ 3)"}]

        self._add_card("abrupt decay", EffectTemplate(
            name="Abrupt Decay",
            description="Destroy target nonland permanent with mana value 3 or less",
            action_generator=_gen_abrupt_decay,
        ))


        # Wheel of Fortune / Reforge the Soul / Wheel of Misfortune-likes:
        # each player discards their hand and draws 7
        wheel_action = lambda ctrl, opp, ctx: [
            {"action": "discard", "player": ctrl, "card": "all"},
            {"action": "discard", "player": opp, "card": "all"},
            {"action": "draw_cards", "player": ctrl, "amount": 7},
            {"action": "draw_cards", "player": opp, "amount": 7},
        ]
        for _wheel in ("wheel of fortune", "reforge the soul", "time reversal",
                       "echo of eons", "winds of change"):
            self._add_card(_wheel, EffectTemplate(
                name=_wheel.title(),
                description="Each player discards their hand and draws 7",
                action_generator=wheel_action,
            ))

        # Wheel of Misfortune (Aug 1 batch-12 reviewer, escape game): Tier 2
        # half-captured the card — the caster discarded their hand with NO
        # draw (actively harmful) and the number-choice damage clause mapped
        # to the power-based damage branch (silent no-op). Deterministic
        # secret-number model per the Mana Crypt / d20 hash convention.
        self._add_card("wheel of misfortune", EffectTemplate(
            name="Wheel of Misfortune",
            description=("Secret numbers: caster picks high (takes that "
                         "damage, wheels); opponent picks low (keeps hand)"),
            action_generator=self._gen_wheel_of_misfortune,
        ))


        # Unbreakable Formation: creatures you control gain indestructible until EOT
        self._add_card("unbreakable formation", EffectTemplate(
            name="Unbreakable Formation",
            description="Creatures you control gain indestructible until EOT",
            action_generator=self._gen_unbreakable_formation,
        ))


        # Green Sun's Zenith: search for green creature with CMC <= X, put onto battlefield, shuffle Zenith into library
        self._add_card("green sun's zenith", EffectTemplate(
            name="Green Sun's Zenith",
            description="Search for green creature with mana value <= X, put onto battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "count": 1,
                 "card_type": "creature",
                 "max_mv": ctx.get('x_value', 0),
                 "destination": "battlefield",
                 "reason": "Green Sun's Zenith: tutor green creature with MV ≤ X to battlefield"},
            ],
        ))


        # =================================================================
        # ADVENTURE-HALF TEMPLATES (Throne of Eldraine, Wilds of Eldraine).
        # Adventure cards are looked up under the adventure_name (the
        # sorcery/instant half), not the creature name. The cast path
        # invokes the template library on adventure_name. See B14 in the
        # Apr 28 audit — these were leaking "Complex effect:" messages
        # without actually firing the effect.
        # =================================================================

        # Oaken Boon (Tuinvale Treefolk — sorcery): two +1/+1 counters on a creature
        self._add_card("oaken boon", EffectTemplate(
            name="Oaken Boon",
            description="Put two +1/+1 counters on target creature",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "add_counters",
                  "card": ctx.get('best_own_creature') or ctx.get('best_etb_creature') or '',
                  "counter_type": "+1/+1", "amount": 2}]
                if (ctx.get('best_own_creature') or ctx.get('best_etb_creature'))
                else [{"action": "no_action", "reason": "Oaken Boon: no creature target"}]
            ),
        ))
        # Petty Theft (Brazen Borrower — instant): bounce target nonland opp permanent
        self._add_card("petty theft", EffectTemplate(
            name="Petty Theft",
            description="Return target nonland permanent an opponent controls to its owner's hand",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "move_card", "card": ctx['best_opponent_nonland'],
                  "from_zone": "battlefield", "to_zone": "hand",
                  "player": opp,
                  "reason": "Petty Theft: bounce nonland permanent"}]
                if ctx.get('best_opponent_nonland')
                else [{"action": "no_action", "reason": "Petty Theft: no nonland permanent target"}]
            ),
        ))
        # Gift of the Fae (Faerie Guidemother — instant): +1/+1 + flying EOT (target)
        # Approximation: pump the controller's best creature; flying is granted via temp keyword
        self._add_card("gift of the fae", EffectTemplate(
            name="Gift of the Fae",
            description="Target creature gets +1/+1 and gains flying until end of turn",
            action_generator=lambda ctrl, opp, ctx: (
                [
                    {"action": "pump_all_creatures", "player": ctrl,
                     "power": 1, "toughness": 1,
                     "source": "Gift of the Fae"},
                    {"action": "grant_keywords", "player": ctrl,
                     "target": "all_own_creatures",
                     "keywords": ["Flying"]},
                ]
                if ctx.get('controller_creature_count', 0) > 0
                else [{"action": "no_action", "reason": "Gift of the Fae: no creature to pump"}]
            ),
        ))
        # Usher to Safety (Shepherd of the Flock — instant): return your creature to hand
        self._add_card("usher to safety", EffectTemplate(
            name="Usher to Safety",
            description="Return target creature you control to its owner's hand",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "bounce_own_permanent", "player": ctrl,
                  "card": ctx.get('best_own_flickerable') or ctx.get('best_own_creature') or ''}]
                if (ctx.get('best_own_flickerable') or ctx.get('best_own_creature'))
                else [{"action": "no_action", "reason": "Usher to Safety: no own creature to bounce"}]
            ),
        ))
        # Chop Down (Giant Killer — instant): destroy creature with power 4+
        self._add_card("chop down", EffectTemplate(
            name="Chop Down",
            description="Destroy target creature with power 4 or greater",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "destroy", "card": ctx['best_opponent_creature'],
                  "target_controller": opp,
                  "reason": "Chop Down: destroy power 4+ creature"}]
                if (ctx.get('best_opponent_creature')
                    and int(ctx.get('best_opponent_creature_power', 0) or 0) >= 4)
                else [{"action": "no_action",
                       "reason": "Chop Down: no opponent creature with power 4+"}]
            ),
        ))
        # Dizzying Swoop (Ardenvale Tactician — instant): tap up to 2 creatures
        # Tap two opponent creatures (best two)
        def _gen_dizzying_swoop(ctrl, opp, ctx):
            opp_creatures = ctx.get('opponent_creatures_by_power', [])
            actions = []
            for name in opp_creatures[:2]:
                actions.append({"action": "tap", "card": name})
            if not actions:
                return [{"action": "no_action", "reason": "Dizzying Swoop: no creatures to tap"}]
            return actions
        self._add_card("dizzying swoop", EffectTemplate(
            name="Dizzying Swoop",
            description="Tap up to two target creatures",
            action_generator=_gen_dizzying_swoop,
        ))
        # Shield's Might (Garenbrig Carver — instant): target creature +2/+2 EOT
        # Approximation: pump-all on the controller's creatures
        self._add_card("shield's might", EffectTemplate(
            name="Shield's Might",
            description="Target creature gets +2/+2 until end of turn",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "pump_all_creatures", "player": ctrl,
                  "power": 2, "toughness": 2,
                  "source": "Shield's Might"}]
                if ctx.get('controller_creature_count', 0) > 0
                else [{"action": "no_action", "reason": "Shield's Might: no creature to pump"}]
            ),
        ))
        # Bring Back (Oakhame Ranger — instant): two creatures +1/+1 EOT
        self._add_card("bring back", EffectTemplate(
            name="Bring Back",
            description="Up to two target creatures each get +1/+1 until end of turn",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "pump_all_creatures", "player": ctrl,
                  "power": 1, "toughness": 1,
                  "source": "Bring Back"}]
                if ctx.get('controller_creature_count', 0) > 0
                else [{"action": "no_action", "reason": "Bring Back: no creatures"}]
            ),
        ))
        # Bring to Life (Animating Faerie — sorcery): noncreature artifact → 5/4 creature EOT
        # May 17 audit: animate_land was extended to accept required_type=artifact
        # via the unified "animate_permanent" action, so this template can
        # actually fire now (was no-op-deferred-to-Tier-3 before).
        self._add_card("bring to life", EffectTemplate(
            name="Bring to Life",
            description="Target noncreature artifact you control becomes a 5/4 Artifact creature until EOT",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "animate_permanent", "player": ctrl, "scope": "target",
                 "required_type": "artifact",
                 "power": 5, "toughness": 4,
                 "card": ctx.get('target_card', '')},
            ],
        ))

        # Ensoul Artifact (aura, noncreature artifact → 5/5 creature permanently
        # while enchanted). Setting `permanent_until_leaves=True` so EOT
        # cleanup doesn't revert it.
        self._add_card("ensoul artifact", EffectTemplate(
            name="Ensoul Artifact",
            description="Enchanted noncreature artifact is a 5/5 creature",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "animate_permanent", "player": ctrl, "scope": "target",
                 "required_type": "artifact",
                 "power": 5, "toughness": 5,
                 "permanent_until_leaves": True,
                 "card": ctx.get('target_card', '')},
            ],
        ))

        # Pernicious Deed: {X}, Sacrifice this enchantment: Destroy each
        # artifact, creature, and enchantment with mana value X or less.
        # The activation path handles the sacrifice cost; the template runs
        # on the EFFECT text, so it just needs to wipe permanents <= X.
        self._add_card("pernicious deed", EffectTemplate(
            name="Pernicious Deed",
            description="Destroy each artifact, creature, and enchantment with mana value X or less",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy_creatures_by_cmc",
                 "max_cmc": int(ctx.get('x_value', 0) or 0)},
                {"action": "destroy_all_by_type", "type": "artifacts",
                 "max_cmc": int(ctx.get('x_value', 0) or 0)},
                {"action": "destroy_all_by_type", "type": "enchantments",
                 "max_cmc": int(ctx.get('x_value', 0) or 0)},
            ],
        ))

        # --- X-COST TOKEN SPELLS ---
        # These need explicit templates because generic oracle patterns can mis-match
        # (e.g., "draw X" regex catching the X before the token-creation text).

        self._add_card("decree of justice", EffectTemplate(
            name="Decree of Justice",
            description="Create X 4/4 white Angel creature tokens with flying",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Angel",
                 "power": 4, "toughness": 4, "types": "Creature — Angel",
                 "count": max(1, ctx.get('x_value', 1))}
            ],
        ))

        self._add_card("secure the wastes", EffectTemplate(
            name="Secure the Wastes",
            description="Create X 1/1 white Warrior creature tokens",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Warrior",
                 "power": 1, "toughness": 1, "types": "Creature — Warrior",
                 "count": max(1, ctx.get('x_value', 1))}
            ],
        ))

        self._add_card("entreat the angels", EffectTemplate(
            name="Entreat the Angels",
            description="Create X 4/4 white Angel creature tokens with flying",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Angel",
                 "power": 4, "toughness": 4, "types": "Creature — Angel",
                 "count": max(1, ctx.get('x_value', 1))}
            ],
        ))

        self._add_card("finale of glory", EffectTemplate(
            name="Finale of Glory",
            description="Create X 2/2 Soldiers; if X>=10 also create X 4/4 Angels",
            action_generator=self._gen_finale_of_glory,
        ))

        self._add_card("mystic sanctuary", EffectTemplate(
            name="Mystic Sanctuary",
            description="Put target instant or sorcery from graveyard on top of library",
            action_generator=self._gen_mystic_sanctuary,
        ))

        # Thassa's Oracle: registered once below (near line ~1175) with win condition check

        # --- Searing Blood: deal 2 to creature, if it dies this turn deal 3 to controller ---
        # Register the contingent damage as an exact-object death watcher;
        # prediction cannot account for prevention, indestructible, or SBAs.
        def _searing_blood_gen(ctrl, opp, ctx):
            # Aug 2 batch-14 audit (R-L2): "target creature" is UNRESTRICTED —
            # the caster's own creatures are legal targets (CR 601.2c), so
            # reporting "no legal target" while the caster had two Monastery
            # Swiftspears out (game_1533407568360112128) was wrong, and it
            # burned the card and {R}{R} for nothing. A DECLARED target is
            # honored wherever it lives; the auto-pick deliberately stays
            # opponent-only, because blind-targeting your own creature with
            # this card is self-harm (2 damage to it, then 3 to YOU when it
            # dies) — declining is the strategically correct default, and
            # the plan-validate hold is what should stop the cast upstream.
            target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')
            if not target:
                return [{"action": "no_action",
                         "reason": "Searing Blood: requires a creature target — no legal target"}]
            # A declared own-creature target flips the second clause's victim
            # to the caster, so resolve the controller from the board rather
            # than assuming the opponent.
            _tgt_ctrl = opp
            _game = ctx.get('_game')
            _ctrl_player = ctx.get('_controller_player')
            if _game is not None and _ctrl_player is not None:
                from mtg.helpers import names_match
                if any(names_match(c.name, target)
                       for c in getattr(_ctrl_player, 'battlefield', []) or []):
                    _tgt_ctrl = ctrl
            opp = _tgt_ctrl
            return [
                {"action": "deal_damage", "amount": 2, "target_card": target,
                 "target_controller": opp, "source": "Searing Blood"},
                {"action": "schedule_death_trigger", "watch_target": target,
                 "watch_target_id": ctx.get('explicit_target_id', ''),
                 "source": "Searing Blood",
                 "on_death_actions": [{"action": "deal_damage", "amount": 3,
                                       "target_player": opp,
                                       "source": "Searing Blood"}]},
            ]

        self._add_card("searing blood", EffectTemplate(
            name="Searing Blood",
            description="Deal 2 damage to target creature. If it dies this turn, deal 3 to its controller.",
            action_generator=_searing_blood_gen,
        ))

        # --- Flicker spells (exile and return immediately) ---
        # Single-target flickers. Prefer the AI's chosen target; fall back to
        # the best ETB creature only when the AI didn't specify one. (CR 601.2c
        # — the targets chosen at cast time stick; auto-selection should only
        # fire when no target was provided.)
        for _fname in ["ephemerate", "cloudshift", "momentary blink", "flicker of fate",
                        "essence flux"]:
            self._add_card(_fname, EffectTemplate(
                name=_fname.title(),
                description="Exile target creature you control, then return it to the battlefield",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "flicker", "player": ctrl,
                     "target": ctx.get('explicit_target_name') or ctx.get('best_own_etb_creature', '')},
                ],
            ))
        # Ghostly Flicker: TWO targets (creature, artifact, or land)
        self._add_card("ghostly flicker", EffectTemplate(
            name="Ghostly Flicker",
            description="Exile two target artifacts, creatures, or lands you control, return them",
            action_generator=self._gen_ghostly_flicker,
        ))

        # --- Conjurer's Closet / Teleportation Circle (end-step flicker) ---
        self._add_card("conjurer's closet", EffectTemplate(
            name="Conjurer's Closet",
            description="At end step, exile target creature you control, return it",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "flicker", "player": ctrl, "target": ctx.get('best_own_etb_creature', ''),
                 "source": "Conjurer's Closet"},
            ],
        ))
        self._add_card("teleportation circle", EffectTemplate(
            name="Teleportation Circle",
            description="At end step, exile target artifact/creature you control that entered this turn, return it",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "flicker", "player": ctrl, "target": ctx.get('best_own_etb_creature', ''),
                 "source": "Teleportation Circle"},
            ],
        ))

        # --- End-step flicker creatures ---
        self._add_card("soulherder", EffectTemplate(
            name="Soulherder",
            description="At end step, exile target creature you control, return it to battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "flicker", "player": ctrl, "target": ctx.get('best_own_etb_creature', ''),
                 "source": "Soulherder"},
            ],
        ))
        # July 31 batch-10: suffix-keyed twin so end-step dispatch finds the
        # template FIRST via the F25 suffix lookup, immune to any future
        # bare-key overwrite (a second bare "soulherder" registration at
        # ~line 2955 shadowed the one above for months — its non-scheduled
        # description dodged the F25 prefix guard and every end step
        # escalated to Tier 3, 17 drains in batch 15324).
        self._add_card("soulherder endstep", EffectTemplate(
            name="Soulherder",
            description="At end step, exile target creature you control, return it to battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "flicker", "player": ctrl, "target": ctx.get('best_own_etb_creature', ''),
                 "source": "Soulherder"},
            ],
        ))
        self._add_card("thassa, deep-dwelling", EffectTemplate(
            name="Thassa, Deep-Dwelling",
            description="At end step, exile target creature you control, return it to battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "flicker", "player": ctrl, "target": ctx.get('best_own_etb_creature', ''),
                 "source": "Thassa, Deep-Dwelling"},
            ],
        ))

        # --- Sun Titan: return permanent MV 3 or less from graveyard ---
        self._add_card("sun titan", EffectTemplate(
            name="Sun Titan",
            description="Return target permanent with MV 3 or less from graveyard to battlefield",
            action_generator=self._gen_sun_titan,
        ))
        self._add_attack_card("sun titan", EffectTemplate(
            name="Sun Titan",
            description="Whenever Sun Titan attacks, return a permanent with MV 3 or less from graveyard",
            action_generator=self._gen_sun_titan,
        ))

        # --- Land Tax: search for up to 3 basic lands ---
        # Two skip conditions: (1) opponent doesn't control more lands; (2) we
        # already have enough basic lands in hand to be flooded — searching for
        # more is wasted activation and floods the discard step. If the
        # controller already holds 5+ basic lands in hand, skip silently.
        # (land tax: single registration lives at _land_tax_gen — the June 10
        # audited version that reads real player objects and the real
        # library. Aug 7 registry dedup removed the ctx-count duplicate.)

        # --- Removal spells that give controller a token ---
        self._add_card("reality shift", EffectTemplate(
            name="Reality Shift",
            description="Exile target creature, its controller manifests top card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_creature') or '',
                 "from_zone": "battlefield", "to_zone": "exile", "player": ctx.get('explicit_target_owner') or opp},
                {"action": "manifest_top", "player": ctx.get('explicit_target_owner') or opp,
                 "count": 1},
            ] if (ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')) else [
                {"action": "no_action", "reason": "No creature to target with Reality Shift"}
            ],
            needs_target=True,
        ))
        self._add_card("rapid hybridization", EffectTemplate(
            name="Rapid Hybridization",
            description="Destroy target creature. Its controller creates a 3/3 Frog Lizard",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_creature', 'target')},
                {"action": "create_token", "player": ctx.get('explicit_target_owner') or opp, "name": "Frog Lizard",
                 "power": 3, "toughness": 3, "types": "Creature - Frog Lizard", "count": 1},
            ],
            needs_target=True,
        ))
        self._add_card("pongify", EffectTemplate(
            name="Pongify",
            description="Destroy target creature. Its controller creates a 3/3 Ape",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_creature', 'target')},
                {"action": "create_token", "player": ctx.get('explicit_target_owner') or opp, "name": "Ape",
                 "power": 3, "toughness": 3, "types": "Creature - Ape", "count": 1},
            ],
            needs_target=True,
        ))
        # June 10 deep-dive: lands ARE legal "destroy target permanent"
        # targets — on a land-only board the old nonland-only selector
        # destroyed NOTHING while the unconditional token rider still handed
        # the opponent a free 3/3. Fall back to the any-permanent ctx key.
        self._add_card("beast within", EffectTemplate(
            name="Beast Within",
            description="Destroy target permanent. Its controller creates a 3/3 Beast",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": (ctx.get('explicit_target_name')
                                               or ctx.get('best_opponent_nonland')
                                               or ctx.get('best_opponent_any_permanent', 'target'))},
                {"action": "create_token", "player": ctx.get('explicit_target_owner') or opp, "name": "Beast",
                 "power": 3, "toughness": 3, "types": "Creature - Beast", "count": 1},
            ],
            needs_target=True,
        ))
        self._add_card("generous gift", EffectTemplate(
            name="Generous Gift",
            description="Destroy target permanent. Its controller creates a 3/3 Elephant",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": (ctx.get('explicit_target_name')
                                               or ctx.get('best_opponent_nonland')
                                               or ctx.get('best_opponent_any_permanent', 'target'))},
                {"action": "create_token", "player": ctx.get('explicit_target_owner') or opp, "name": "Elephant",
                 "power": 3, "toughness": 3, "types": "Creature - Elephant", "count": 1},
            ],
            needs_target=True,
        ))

        # --- Fatal Push: destroy creature MV ≤ 2 (≤ 4 with revolt) ---
        self._add_card("fatal push", EffectTemplate(
            name="Fatal Push",
            description="Destroy target creature if it has mana value 2 or less (4 or less with revolt)",
            action_generator=self._gen_fatal_push,
            needs_target=True,
        ))


        # --- Archmage's Charm: modal (counter, draw 2, steal MV≤1) ---
        self._add_card("archmage's charm", EffectTemplate(
            name="Archmage's Charm",
            description="Counter target spell, OR draw two cards, OR gain control of target nonland permanent with MV ≤ 1",
            action_generator=self._gen_archmages_charm,
        ))

        def _gen_prismatic_ending(ctrl, opp, ctx):
            """Converge (CR 702.100a): exile a nonland permanent whose mana
            value is <= the number of COLORS of mana spent to cast this.

            The bound is colors SPENT, not the printed {X}: Prismatic Ending
            is {X}{W}, and paying {2}{W} off two Plains and an Island spends
            two colors, not three.
            """
            bound = int(ctx.get('colors_spent') or 0)

            # Aug 9 audit (B-2): the old guard was `if mv is not None and
            # mv > bound` — a None mv skipped the bound entirely, and mv
            # was ALWAYS None on the auto-pick path (explicit_target_mv is
            # only set by the explicit-target branches, and the fallback
            # read the then-producer-less opponent_battlefield key). An
            # MV-3 Liliana was exiled with colors_spent=2 — structurally
            # impossible for the two-color caster (CR 702.100a). Now: gate
            # the PICK, never exile on unknown mv, and DECLINE an
            # over-bound declared target (the Abrupt Decay precedent — no
            # silent retargeting).
            def _mv_of(name_lc):
                # Aug 9 adversarial review: scan BOTH battlefields — an
                # own-side declared target is legal for "target nonland
                # permanent" and used to decline as "unknown mana value".
                for _p in (list(ctx.get('opponent_battlefield') or [])
                           + list(ctx.get('controller_battlefield') or [])):
                    _n = _p.get('name') if isinstance(_p, dict) else getattr(_p, 'name', '')
                    if _n and _n.lower() == name_lc:
                        return (_p.get('cmc') if isinstance(_p, dict)
                                else getattr(_p, 'cmc', None))
                return None

            explicit = ctx.get('explicit_target_name') or ''
            if explicit:
                mv = ctx.get('explicit_target_mv')
                if mv is None:
                    mv = _mv_of(explicit.lower())
                if mv is None or int(mv or 0) > bound:
                    return [{"action": "no_action",
                             "reason": (f"Prismatic Ending exiles mana value "
                                        f"{bound} or less ({bound} color(s) "
                                        f"of mana spent) — {explicit} "
                                        f"{'has unknown mana value' if mv is None else f'is mana value {int(mv or 0)}'}")}]
                return [{"action": "move_card", "card": explicit,
                         "from_zone": "battlefield", "to_zone": "exile",
                         "player": ctx.get('explicit_target_owner') or opp}]

            # Auto-pick: highest-value LEGAL candidate (nonland, known mv,
            # mv <= bound) from the opponent's battlefield.
            # Aug 9 adversarial review (B-2): the raw loop lost the
            # _can_target legality filter its best_opponent_* predecessors
            # had — a hexproof or phased-out creature was picked and the
            # action layer then blocked it (cost paid, effect lost, plus a
            # misleading shield message where the pre-fix code declined
            # cleanly). Filter like the keys this loop falls back from.
            _ct = ctx.get('_can_target')
            _opp_pl = ctx.get('_opponent_player')
            best_name, best_mv = None, -1
            for _p in (ctx.get('opponent_battlefield') or []):
                _n = _p.get('name') if isinstance(_p, dict) else getattr(_p, 'name', '')
                _tl = (_p.get('type_line') if isinstance(_p, dict)
                       else getattr(_p, 'type_line', '')) or ''
                _mv = (_p.get('cmc') if isinstance(_p, dict)
                       else getattr(_p, 'cmc', None))
                _is_land = (_p.is_land() if hasattr(_p, 'is_land')
                            else 'land' in _tl.lower())
                if (not _n or _is_land or _mv is None
                        or int(_mv or 0) > bound):
                    continue
                if getattr(_p, '_phased_out', False):
                    continue
                if _ct is not None and not _ct(_p, _opp_pl):
                    continue
                if int(_mv or 0) > best_mv:
                    best_name, best_mv = _n, int(_mv or 0)
            if not best_name:
                return [{"action": "no_action",
                         "reason": (f"Prismatic Ending: no nonland permanent "
                                    f"with mana value {bound} or less to "
                                    f"exile")}]
            return [{"action": "move_card", "card": best_name,
                     "from_zone": "battlefield", "to_zone": "exile",
                     "player": opp}]

        # --- Prismatic Ending: exile nonland permanent with MV <= colors spent ---
        # Aug 3: the converge CONDITION was ignored entirely — the template
        # exiled any nonland permanent whatever its mana value, so a
        # one-color Prismatic Ending answered a 6-drop. CR 702.100a: the
        # bound is the number of COLORS of mana actually spent, which
        # ctx['colors_spent'] now carries.
        self._add_card("prismatic ending", EffectTemplate(
            name="Prismatic Ending",
            description="Exile target nonland permanent with mana value X or less",
            action_generator=_gen_prismatic_ending,
            needs_target=True,
        ))

        # --- Detention Sphere: exile target nonland permanent + all others with same name ---
        self._add_card("detention sphere", EffectTemplate(
            name="Detention Sphere",
            description="Exile target nonland permanent an opponent controls and all others with same name",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card",
                 "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland', ''),
                 "from_zone": "battlefield", "to_zone": "exile",
                 "player": ctx.get('explicit_target_owner') or opp},
            ] if (ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland')) else [
                {"action": "no_action", "reason": "No valid nonland permanent to exile with Detention Sphere"}
            ],
            needs_target=True,
        ))

        # --- Shard Volley: its land sacrifice is paid while casting ---
        # Aug 7 registry dedup: this key was registered TWICE and the later,
        # target-blind always-face version silently won (the deep-dive's
        # dead-code example). One registration now, on the Volcanic Geyser
        # any-target convention: declared creature → target_card; declared
        # player → target_player; else face. The sacrifice-a-land additional
        # cost is paid at CAST time (Aug 5 fix), not here.
        self._add_card("shard volley", EffectTemplate(
            name="Shard Volley",
            description="Sacrifice a land. Shard Volley deals 3 damage to any target.",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "deal_damage", "amount": 3,
                  "target_card": ctx.get('explicit_target_name'),
                  "target_controller": ctx.get('explicit_target_owner') or opp,
                  "source": "Shard Volley"}]
                if (ctx.get('explicit_target_name')
                    and ctx.get('explicit_target_is_creature', False))
                else [{"action": "deal_damage", "amount": 3,
                       "target_player": ctx.get('explicit_target_player') or opp,
                       "source": "Shard Volley"}]
            ),
        ))

        # --- Shark Typhoon (enchantment): whenever you cast noncreature spell, create X/X Shark ---
        # Distinguish two trigger sources by oracle text:
        #   * Battlefield trigger ("Whenever you cast a noncreature spell"): use spell_mv
        #     (the cast spell's mana value).
        #   * Cycle trigger ("When you cycle Shark Typhoon"): use _cycle_x (the X paid to
        #     cycle, set by the cycle action in mtg/actions.py).
        # Both trigger paragraphs invoke this template; the action_generator picks the
        # correct X by looking at which context key is set.
        self._add_card("shark typhoon", EffectTemplate(
            name="Shark Typhoon",
            description="Whenever you cast a noncreature spell or cycle this card, create an X/X blue Shark creature token with flying",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Shark",
                 "power": ctx.get('_cycle_x') if ctx.get('_cycle_x') is not None else ctx.get('spell_mv', 1),
                 "toughness": ctx.get('_cycle_x') if ctx.get('_cycle_x') is not None else ctx.get('spell_mv', 1),
                 "types": "Creature — Shark", "count": 1, "keywords": ["Flying"]},
            ],
        ))


        # --- Swords to Plowshares: exile creature, controller gains life equal to its power ---
        # July 21 batch audit (R3-4): the generic "exile target X" pattern
        # only ever applies the FIRST clause (single-pattern-per-resolution),
        # so Anguished Unmaking's "You lose 3 life." was silently dropped —
        # a clean 3-point hole in an otherwise fully-reconciled life ledger
        # (game_1529168824723570750). Name-keyed template carries both.
        self._add_card("anguished unmaking", EffectTemplate(
            name="Anguished Unmaking",
            description="Exile target nonland permanent. You lose 3 life.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card",
                 "card": (ctx.get('explicit_target_name')
                          or ctx.get('best_opponent_nonland')
                          or ctx.get('best_opponent_creature') or ''),
                 "from_zone": "battlefield", "to_zone": "exile",
                 "player": ctx.get('explicit_target_owner') or opp},
                {"action": "lose_life", "player": ctrl, "amount": 3},
            ] if (ctx.get('explicit_target_name')
                  or ctx.get('best_opponent_nonland')
                  or ctx.get('best_opponent_creature')) else [
                {"action": "no_action",
                 "reason": "No nonland permanent to target"}
            ],
            needs_target=True,
        ))

        self._add_card("swords to plowshares", EffectTemplate(
            name="Swords to Plowshares",
            description="Exile target creature. Its controller gains life equal to its power.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_creature') or '',
                 "from_zone": "battlefield", "to_zone": "exile",
                 "player": ctx.get('explicit_target_owner') or opp},
                {"action": "gain_life", "player": ctx.get('explicit_target_owner') or opp,
                 "amount": resolve_target_power(
                     ctx, ctx.get('explicit_target_name') or ctx.get('best_opponent_creature'))},
            ] if (ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')) else [
                {"action": "no_action", "reason": "No creature to target with Swords to Plowshares"}
            ],
            needs_target=True,
        ))

        # --- Path to Exile: exile creature, controller may search for basic land ---
        self._add_card("path to exile", EffectTemplate(
            name="Path to Exile",
            description="Exile target creature. Its controller may search for a basic land tapped.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_creature') or '',
                 "from_zone": "battlefield", "to_zone": "exile",
                 "player": ctx.get('explicit_target_owner') or opp},
                {"action": "search_library_land", "player": ctx.get('explicit_target_owner') or opp,
                 "basic_only": True, "enters_tapped": True},
            ] if (ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')) else [
                {"action": "no_action", "reason": "No creature to target with Path to Exile"}
            ],
            needs_target=True,
        ))

        # --- Ram Through: target creature you control deals damage equal to its power ---
        self._add_card("ram through", EffectTemplate(
            name="Ram Through",
            description="Target creature you control deals damage equal to its power to target creature you don't control.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage",
                 "amount": ctx.get('greatest_power', 3),
                 "target_card": ctx.get('explicit_target_name') or ctx.get('best_opponent_creature', 'target'),
                 "target_controller": ctx.get('explicit_target_owner') or opp},
            ],
            needs_target=True,
        ))


        # --- Dream Stalker: return a permanent you control to hand on ETB ---
        self._add_card("dream stalker", EffectTemplate(
            name="Dream Stalker",
            description="When Dream Stalker enters, return a permanent you control to its owner's hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "bounce_own_permanent", "player": ctrl,
                 "exclude": ctx.get('_source_card_name', '')},
            ],
        ))

        # --- Board wipes: destroy/exile/tuck all creatures ---
        # May 20 audit: removed "toxic deluge" — it's NOT destroy-all-creatures.
        # See dedicated Toxic Deluge entry below for the -X/-X pump_all_creatures
        # template (which kills indestructibles via 0-toughness SBA, persists
        # for end of turn, and costs X life).
        # July 20: "decree of pain" removed from this table — its draw clause
        # ("Draw a card for each creature destroyed this way") was silently
        # dropped by the generic wipe (2 cards lost, game_1526071467035459665).
        # It now lives in data/card_templates.json with draw_per_destroyed.
        for wipe_name in ["wrath of god", "day of judgment", "damnation",
                          "blasphemous act", "black sun's zenith",
                          "fumigate", "cleansing nova"]:
            self._add_card(wipe_name, EffectTemplate(
                name=wipe_name.title(),
                description="Destroy all creatures",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "destroy_all_creatures"},
                ],
            ))

        # --- Toxic Deluge: pay X life, all creatures get -X/-X until end of
        # turn (CR-correct: not "destroy", which would miss indestructibles).
        # X defaults to 4 in autoplay (kills most commander creatures, costs
        # 4 life — usually affordable). game_1506623303765463040:500 had this
        # in the generic destroy-all list before the May 20 audit caught it.
        # June 10 audit (V23): three stacked defects fixed — (1) the pump
        # action had no "player" key → find_player("") → silent no-op (the
        # -X/-X never applied while the life was still paid); (2) the effect
        # hits BOTH sides → player="all" (handler support added June 10);
        # (3) X was hardcoded 4 — now reads the cast's chosen X from ctx.
        self._add_card("toxic deluge", EffectTemplate(
            name="Toxic Deluge",
            description="Pay X life. Each creature gets -X/-X until end of turn",
            action_generator=lambda ctrl, opp, ctx: (lambda _x: [
                {"action": "lose_life", "player": ctrl, "amount": _x},
                {"action": "pump_all_creatures", "player": "all",
                 "power": -_x, "toughness": -_x,
                 "duration": "end_of_turn", "source": "Toxic Deluge"},
            ])(max(1, int(ctx.get('x_value') or 4))),
        ))

        # --- Dread Return: single-target reanimate from graveyard. The
        # "Flashback — Sacrifice three creatures" is an ALTERNATE CAST COST
        # for flashback, not part of the main spell effect. Previously the AI
        # confused the two and Tier 3 resolved it as "return 3 creatures with
        # no sacrifice", producing a 3-for-0 (game_1506623303765463040:867).
        # June 10 audit (V7): "from YOUR graveyard" — the cross-player
        # best_graveyard_creature key let Claude's Dread Return take Rick's
        # best creature. Now uses the own-graveyard key + the handler-side
        # own_graveyard restriction.
        self._add_card("dread return", EffectTemplate(
            name="Dread Return",
            description="Return target creature card from your graveyard to the battlefield",
            # July 21 batch audit (R3-2): honor the declared cast-time target
            # (CR 601.2c/608.2b) — the heuristic-only pick reanimated
            # Sakura-Tribe Elder when the AI declared Blood Artist
            # (game_1529168824723570750). The reanimate handler's
            # own_graveyard restriction still gates an illegal declared name.
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "reanimate", "player": ctrl,
                 "card": (ctx.get('explicit_target_name')
                          or ctx.get('best_own_graveyard_creature', '')),
                 "own_graveyard": True,
                 "reason": "Dread Return single-target reanimate"},
            ] if (ctx.get('explicit_target_name')
                  or ctx.get('best_own_graveyard_creature')) else [
                {"action": "no_action", "reason": "No creature cards in your graveyard"}
            ],
        ))

        # --- Goblin Rabblemaster (Aug 10 audit) ---
        # The JSON entry this replaces was wrong in three ways at once and
        # the stale comment here endorsed all three. Printed text, three
        # SEPARATE abilities:
        #   "Other Goblin creatures you control attack each combat if able.
        #    At the beginning of combat on your turn, create a 1/1 red Goblin
        #      creature token with haste.
        #    Whenever this creature attacks, IT gets +1/+0 until end of turn
        #      for each other attacking Goblin."
        # The old attack entry (a) minted a tapped, attacking token the ATTACK
        # trigger does not create — a phantom third attacker every combat —
        # (b) pumped every OTHER Goblin instead of the source, and (c) used
        # +1/+1 instead of +1/+0 and did not scale. game_1536023731808509984.
        # The token half belongs to beginning-of-combat and is registered
        # under the sanctioned suffix key so it also stops taking a Tier-3
        # call per combat and arrives in time to attack with its haste.
        def _gen_rabblemaster_attack(ctrl, opp, ctx):
            """+1/+0 for each OTHER attacking Goblin, on the source only."""
            if ctx.get('damage_dealt'):
                return []
            name = ctx.get('attacking_name') or "Goblin Rabblemaster"
            game_obj = ctx.get('_game')
            battlefield = ctx.get('controller_battlefield', []) or []
            # game.attackers is the authoritative per-combat list; a stale
            # `.attacking` flag fired Battalion with two attackers in
            # game_1532756674203619470, so prefer the list and keep the flag
            # only as the no-_game fallback.
            attacker_ids = (set(getattr(game_obj, 'attackers', None) or [])
                            if game_obj is not None else None)

            def _attacking(creature):
                if attacker_ids is not None:
                    return getattr(creature, 'id', None) in attacker_ids
                return getattr(creature, 'attacking', False)

            others = 0
            for creature in battlefield:
                if getattr(creature, 'name', '') == name:
                    continue
                if 'goblin' not in (getattr(creature, 'type_line', '') or '').lower():
                    continue
                if _attacking(creature):
                    others += 1
            if others <= 0:
                return [{"action": "no_action",
                         "reason": "Goblin Rabblemaster: no other attacking Goblins"}]
            # `include_name` / `include_id` are the keys the handler actually
            # reads — a plausible-looking invented key (the first draft said
            # `name_filter`) is silently ignored, which here would have pumped
            # the WHOLE team instead of the source. Always execute new
            # vocabulary against real state before shipping it.
            action = {"action": "pump_all_creatures", "player": ctrl,
                      "include_name": name, "power": others, "toughness": 0,
                      "source": "Goblin Rabblemaster"}
            attacking_obj = ctx.get('_attacking_creature')
            if attacking_obj is not None and getattr(attacking_obj, 'id', None):
                action["include_id"] = attacking_obj.id
            return [action]

        self._add_attack_card("goblin rabblemaster", EffectTemplate(
            name="Goblin Rabblemaster (attack)",
            description="Gets +1/+0 until end of turn for each other attacking Goblin",
            action_generator=_gen_rabblemaster_attack,
        ))

        # Suffix key, never the bare name: _NAME_KEYED_EVENT_TYPES covers
        # both `beginning_combat` and `etb`, so a bare key would also mint a
        # Goblin on Rabblemaster's own ETB — the re-fire class the suffix
        # convention exists to prevent.
        self._add_card("goblin rabblemaster beginningcombat", EffectTemplate(
            name="Goblin Rabblemaster (beginning of combat)",
            description="Create a 1/1 red Goblin creature token with haste",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Goblin",
                 "power": 1, "toughness": 1, "types": "Creature — Goblin",
                 "count": 1, "colors": ["R"], "keywords": ["Haste"]},
            ],
        ))

        # June 10 audit (V31f): Drakuseth — the generic attack pattern only
        # captured the FIRST number of "deals 4 damage to any target and 3
        # damage to each of up to two other targets" (4 of up to 10) and
        # threaded no source (all 15 "(unknown source)" lines in the batch).
        # Targeting heuristic: 4 → biggest opponent creature, 3 → second
        # creature, 3 → opponent's face; face-first when no creatures (one 4 —
        # the "other targets" must be DIFFERENT objects per CR 601.2c).
        def _gen_drakuseth(ctrl, opp, ctx):
            src = "Drakuseth, Maw of Flames"
            creatures = sorted(ctx.get('_opponent_creatures') or [],
                               key=lambda c: c.get('power', 0), reverse=True)
            acts = []
            if creatures:
                acts.append({"action": "deal_damage", "amount": 4,
                             "target_card": creatures[0]['name'],
                             "target_controller": opp, "source": src})
                if len(creatures) > 1:
                    acts.append({"action": "deal_damage", "amount": 3,
                                 "target_card": creatures[1]['name'],
                                 "target_controller": opp, "source": src})
                acts.append({"action": "deal_damage", "amount": 3,
                             "target_player": opp, "source": src})
            else:
                acts.append({"action": "deal_damage", "amount": 4,
                             "target_player": opp, "source": src})
            return acts

        self._add_attack_card("drakuseth, maw of flames", EffectTemplate(
            name="Drakuseth, Maw of Flames (attack)",
            description="Deals 4 damage to any target and 3 damage to each of up to two other targets",
            action_generator=_gen_drakuseth,
        ))

        # Aug 8 batch audit (#4): Kogla's attack trigger was a JSON entry
        # whose action named the sentinel "BEST_ARTIFACT_OR_ENCHANTMENT" —
        # resolved NOWHERE (only $controller/$opponent substitute), so the
        # destroy silently no-opped on every attack while [ATTACK-TEMPLATE]
        # printed "Resolved" (two live games: Golgari/Dimir Signet survived
        # every Kogla attack). A computed choice can't live in constant
        # JSON — the migrator's rule. The printed clause is verbatim
        # Frenzied Trapbreaker's ("destroy target artifact or enchantment
        # defending player controls"), so it shares that generator; the
        # JSON entry is deleted, and the loader's Python/JSON collision
        # check guarantees it stays deleted.
        self._add_attack_card("kogla, the titan ape", EffectTemplate(
            name="Kogla, the Titan Ape (attack)",
            description="Destroy the defending player's best artifact or enchantment (fizzles when none, CR 603.3c)",
            action_generator=self._gen_frenzied_trapbreaker,
        ))

        # June 10 deep-dive: Karlach, Fury of Avernus — "Whenever you attack"
        # was unreachable behind the bare-"attacks" pre-filter in
        # mtg/triggers.py (now relaxed). Model the untap + first-strike
        # grant; the ADDITIONAL combat phase isn't modeled — breadcrumb in
        # console keeps that honest instead of letting Tier 3 hallucinate it.
        def _gen_karlach_attack(ctrl, opp, ctx):
            # Aug 1 deferred slate: the additional combat phase is granted
            # now (additional_combat action → the Moraug consumption loop).
            # Aug 2 batch-13: Karlach's printed intervening-if ("if it's the
            # first combat phase of the turn", CR 603.4) — the whole trigger
            # declines in an extra combat. Without the gate she re-granted
            # untap + first strike + another phase on every attack, masked
            # only by the consumption loop's tail discard.
            g = ctx.get('_game')
            if g is not None and getattr(g, '_in_extra_combat', False):
                return [{"action": "no_action",
                         "reason": "Karlach: not the first combat phase of "
                                   "the turn (intervening if, CR 603.4)"}]
            acts = [{"action": "grant_keywords", "player": ctrl,
                     "target": "all_own_creatures", "keywords": ["first strike"]},
                    {"action": "additional_combat",
                     "source": "Karlach, Fury of Avernus"}]
            if g is not None:
                for _atk_id in list(getattr(g, 'attackers', []) or []):
                    _res = g.find_card_global(_atk_id)
                    if _res:
                        acts.append({"action": "untap", "card": _res[0].name})
            return acts

        self._add_attack_card("karlach, fury of avernus", EffectTemplate(
            name="Karlach, Fury of Avernus (attack)",
            description="Whenever you attack: untap attacking creatures, they gain first strike, and there is an additional combat phase",
            action_generator=_gen_karlach_attack,
        ))


        # June 10 round 3: Teachings of the Kirin — chapter-dispatching
        # name-keyed template. The saga resolver passes ONLY the chapter
        # text as oracle (now with real game_context — see mtg/spells.py),
        # and the name-key path injects it as ctx['_oracle'], so one
        # generator can dispatch per chapter. Pre-fix, chapter I went
        # through a generic mill pattern (one-pattern-wins) and the Spirit
        # token sentence was silently dropped. Chapter III (the transform)
        # is intercepted upstream by looks_like_transform_chapter and never
        # reaches the library; the branch here is defensive.
        def _gen_teachings_of_the_kirin(ctrl, opp, ctx):
            text = (ctx.get('_oracle') or '')
            acts = []
            if 'mill' in text:
                acts.append({"action": "mill", "player": ctrl, "amount": 3})
            if 'spirit creature token' in text:
                acts.append({"action": "create_token", "player": ctrl,
                             "name": "Spirit", "power": 1, "toughness": 1,
                             "types": "Creature — Spirit", "count": 1})
            if acts:
                return acts  # Chapter I: mill three + Spirit
            if '+1/+1 counter on target creature' in text:
                # Chapter II — biggest own creature.
                best, best_p = None, -1
                for info in (ctx.get('_controller_creatures') or []):
                    if isinstance(info, dict) and info.get('power', 0) > best_p:
                        best, best_p = info.get('name'), info.get('power', 0)
                if best:
                    return [{"action": "add_counters", "card": best,
                             "counter_type": "+1/+1", "amount": 1}]
                return [{"action": "no_action",
                         "reason": "Teachings of the Kirin II: no creature to put the counter on"}]
            if 'exile this saga' in text:
                return [{"action": "no_action",
                         "reason": "transform chapter — handled by the SAGA_COMPLETE engine path"}]
            return [{"action": "no_action",
                     "reason": "Teachings of the Kirin: unrecognized chapter text"}]

        self._add_card("teachings of the kirin", EffectTemplate(
            name="Teachings of the Kirin",
            description="Saga chapters: I mill three + create a 1/1 Spirit; II +1/+1 counter on target creature you control; III transform (engine)",
            action_generator=_gen_teachings_of_the_kirin,
        ))

        # June 10 deep-dive (B5c): Kirin-Touched Orochi's attack trigger is
        # reflexive — the Spirit token REQUIRES exiling a creature card from
        # a graveyard ("When you do, create…"). The generic pattern was
        # minting free Spirits with no exile, 4× in one game.
        def _gen_kirin_orochi_attack(ctrl, opp, ctx):
            ctrl_player = ctx.get('_controller_player')
            opp_player = ctx.get('_opponent_player')
            victim = None
            victim_owner = None
            for pl in (opp_player, ctrl_player):
                if pl is None:
                    continue
                for c in pl.graveyard:
                    if 'creature' in ((getattr(c, 'type_line', '') or '').lower()):
                        victim, victim_owner = c, pl
                        break
                if victim is not None:
                    break
            if victim is None:
                return [{"action": "no_action",
                         "reason": "Kirin-Touched Orochi: no creature card in any graveyard to exile — no Spirit (reflexive cost)"}]
            return [
                {"action": "move_card", "card": victim.name, "from_zone": "graveyard",
                 "to_zone": "exile", "player": victim_owner.name,
                 "reason": "exiled by Kirin-Touched Orochi"},
                {"action": "create_token", "player": ctrl, "name": "Spirit",
                 "power": 1, "toughness": 1, "types": "Creature — Spirit", "count": 1},
            ]

        self._add_attack_card("kirin-touched orochi", EffectTemplate(
            name="Kirin-Touched Orochi (attack)",
            description="Whenever this creature attacks, you may exile a creature card from a graveyard; if you do, create a 1/1 colorless Spirit creature token",
            action_generator=_gen_kirin_orochi_attack,
        ))


        # Austere Command: choose two — destroy artifacts; destroy creatures
        # CMC ≤3; destroy creatures CMC ≥4; destroy enchantments. Pick the two
        # modes that wipe the most opposing permanents while preserving our own
        # high-value pieces. If neither side has a clear advantage, pick the
        # combo that hits the most total opposing permanents.
        self._add_card("austere command", EffectTemplate(
            name="Austere Command",
            description="Choose two — destroy artifacts; destroy creatures with mana value 3 or less; destroy creatures with mana value 4 or greater; destroy enchantments",
            action_generator=lambda ctrl, opp, ctx: _austere_command_modes(ctrl, opp, ctx),
        ))
        self._add_card("cyclonic rift", EffectTemplate(
            name="Cyclonic Rift",
            description="Return nonland permanent(s) to owner's hand; overloaded: return all",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "bounce_all_opponents", "player": ctrl},
            ] if ctx.get('mana_paid_total', 0) >= 7 or ctx.get('overloaded', False) else [
                {"action": "move_card",
                 "card": ctx.get('best_opponent_nonland', ctx.get('best_opponent_creature', '')),
                 "from_zone": "battlefield", "to_zone": "hand",
                 "player": opp},
            ],
        ))

        # --- Bounce spells (instant/sorcery) ---
        # Into the Roil / Blink of an Eye: bounce + draw if kicked
        self._add_card("into the roil", EffectTemplate(
            name="Into the Roil",
            description="Return target nonland permanent to its owner's hand. If kicked, draw a card.",
            action_generator=self._gen_bounce_opponent_permanent,
        ))
        self._add_card("blink of an eye", EffectTemplate(
            name="Blink of an Eye",
            description="Return target nonland permanent to its owner's hand. If kicked, draw a card.",
            action_generator=self._gen_bounce_opponent_permanent,
        ))
        # Unsummon / Vapor Snag: bounce target creature
        self._add_card("unsummon", EffectTemplate(
            name="Unsummon",
            description="Return target creature to its owner's hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": ctx.get('best_opponent_creature', ''),
                 "from_zone": "battlefield", "to_zone": "hand", "player": opp}
            ] if ctx.get('best_opponent_creature') else [
                {"action": "no_action", "reason": "No creature to bounce"}
            ],
        ))
        # July 21 batch audit (R1-1): Snap + Vapor Snag were the two bounce
        # templates skipping the explicit_target_name override — Snap bounced
        # Korvold when the AI declared Birds of Paradise (CR 601.2c/608.2b,
        # game_1529154418816057364). Same or-chain every other template uses.
        self._add_card("vapor snag", EffectTemplate(
            name="Vapor Snag",
            description="Return target creature to its owner's hand, its controller loses 1 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card",
                 "card": (ctx.get('explicit_target_name')
                          or ctx.get('best_opponent_creature', '')),
                 "from_zone": "battlefield", "to_zone": "hand", "player": opp},
                {"action": "lose_life", "player": opp, "amount": 1},
            ] if (ctx.get('explicit_target_name')
                  or ctx.get('best_opponent_creature')) else [
                {"action": "no_action", "reason": "No creature to bounce"}
            ],
        ))
        # Snap: bounce + untap 2 lands
        self._add_card("snap", EffectTemplate(
            name="Snap",
            description="Return target creature to its owner's hand, untap up to two lands",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card",
                 "card": (ctx.get('explicit_target_name')
                          or ctx.get('best_opponent_creature', '')),
                 "from_zone": "battlefield", "to_zone": "hand", "player": opp},
                {"action": "untap_lands", "player": ctrl, "count": 2},
            ] if (ctx.get('explicit_target_name')
                  or ctx.get('best_opponent_creature')) else [
                {"action": "no_action", "reason": "No creature to bounce"}
            ],
        ))
        # Chain of Vapor: bounce target nonland permanent
        self._add_card("chain of vapor", EffectTemplate(
            name="Chain of Vapor",
            description="Return target nonland permanent to its owner's hand",
            action_generator=self._gen_bounce_opponent_permanent,
        ))


        # (Gonti already registered at line ~754 with proper _gonti_etb handler)

        # --- Dies triggers with special logic ---
        # Thragtusk dies → Beast token (ETB is life gain, handled above)
        self._add_card("thragtusk", EffectTemplate(
            name="Thragtusk",
            description="When Thragtusk dies, create a 3/3 green Beast creature token",
            action_generator=lambda ctrl, opp, ctx: [make_token_action(ctrl, "beast_3_3", 1)],
        ))

        # May 17 audit: templates for cards flagged as the top Tier-3 escalations
        # in the May 16 batch — these collectively account for ~12 of ~110
        # escalations/batch.

        # Protean Hulk: when it dies, search library for any number of
        # creatures with total mana value 6 or less, put them onto the
        # battlefield. Approximation: tutor a single high-impact creature
        # (no exact-total-MV constraint — that's too combinatorial to model
        # quickly; in practice the AI was banking on a single fat creature).
        def _protean_hulk_gen(ctrl, opp, ctx):
            return [{
                "action": "search_library",
                "player": ctrl,
                "card_type": "Creature",
                "max_mv": 6,
                "to_zone": "battlefield",
                "count": 1,
                "reason": "Protean Hulk: tutor creature(s) totaling MV<=6",
            }]

        # Pattern of Rebirth: "Enchanted creature has 'When this creature
        # dies, search your library for a creature card, put that card onto
        # the battlefield...'". Modeled as a dies-trigger from the enchanted
        # creature. The trigger source ARRIVES via the death event of the
        # creature wearing this aura — but our dies templates are keyed by
        # the dying creature's NAME. Cleaner: register Pattern of Rebirth as
        # the source card itself; when it goes to graveyard via its enchanted
        # creature dying, fire the tutor. (Adventure path: the LTB scan on
        # the aura itself fires when its enchantee dies; that's the natural
        # hook for this template.)
        def _pattern_of_rebirth_gen(ctrl, opp, ctx):
            return [{
                "action": "search_library",
                "player": ctrl,
                "card_type": "Creature",
                "to_zone": "battlefield",
                "count": 1,
                "reason": "Pattern of Rebirth: enchanted creature died, tutor any creature",
            }]

        # Sidisi, Undead Vizier: "Exploit (When this creature enters, you
        # may sacrifice a creature). When you exploit a creature, you may
        # search your library for a card, put it into your hand."
        # The exploit-then-tutor combo. Heuristic: always exploit if there's
        # a creature to sacrifice (the value is high). Already has a handler
        # at line ~6855 (_gen_sidisi_exploit) — verify it's registered.
        # (Existing handler; not duplicating.)

        # Mystic Snake: "Flash. When this creature enters, counter target
        # spell." Counterspell-on-ETB.
        def _mystic_snake_gen(ctrl, opp, ctx):
            game = ctx.get('_game')
            stack = getattr(game, 'stack', None) or []
            if not stack:
                return [{"action": "no_action",
                         "reason": "Mystic Snake: stack empty, nothing to counter"}]
            return [{"action": "counter_spell", "player": ctrl, "target": "stack_top"}]
        self._add_card("mystic snake", EffectTemplate(
            name="Mystic Snake",
            description="When Mystic Snake enters, counter target spell",
            action_generator=_mystic_snake_gen,
        ))

        # Bloodghast: "Landfall — Whenever a land enters under your control,
        # you may return Bloodghast from your graveyard to the battlefield."
        # This is a landfall trigger that reads from graveyard. The trigger
        # fires when the controller plays a land, so we model it as a
        # landfall handler that returns Bloodghast.
        def _bloodghast_landfall_gen(ctrl, opp, ctx):
            game = ctx.get('_game')
            player = _find_player_by_name(game, ctrl)
            if player is None:
                return [{"action": "no_action", "reason": "Bloodghast: no controller in ctx"}]
            for c in player.graveyard:
                if c.name.lower() == "bloodghast":
                    return [{
                        "action": "move_card",
                        "card": "Bloodghast",
                        "from_zone": "graveyard",
                        "to_zone": "battlefield",
                        "player": ctrl,
                    }]
            return [{"action": "no_action",
                     "reason": "Bloodghast: not in graveyard to return"}]
        self._add_card("bloodghast", EffectTemplate(
            name="Bloodghast",
            description="Landfall — return Bloodghast from graveyard to battlefield",
            action_generator=_bloodghast_landfall_gen,
        ))


        # June 10 deep-dive (CRITICAL — Tier 3 fabricated a mana payment):
        # Leyline Tyrant's death trigger ("you may pay any amount of {R}…")
        # resolved via Tier 3, which invented a 5-mana payment with ZERO
        # untapped sources and an empty pool. Free template: count actual
        # available red, tap it, deal that much; decline at 0.
        def _gen_leyline_tyrant_dies(ctrl, opp, ctx):
            ctrl_player = ctx.get('_controller_player')
            if ctrl_player is None:
                return [{"action": "no_action",
                         "reason": "Leyline Tyrant: controller object unavailable"}]
            pool = getattr(ctrl_player, 'mana_pool', {}) or {}
            red = pool.get('R', 0)
            red_sources = []
            for c in ctrl_player.battlefield:
                if getattr(c, 'tapped', False):
                    continue
                try:
                    prod = ctrl_player._get_mana_production(c)
                except (AttributeError, TypeError):
                    prod = {}
                if (prod or {}).get('R', 0) > 0:
                    red_sources.append((c, prod.get('R', 0)))
            red += sum(v for _c, v in red_sources)
            if red <= 0:
                return [{"action": "no_action",
                         "reason": "Leyline Tyrant: no red mana available — optional payment declined"}]
            for c, _v in red_sources:
                c.tapped = True
            return [{"action": "deal_damage", "amount": red, "target_player": opp,
                     "source": "Leyline Tyrant"}]

        self._add_dies_card("leyline tyrant", EffectTemplate(
            name="Leyline Tyrant (dies)",
            description="When this creature dies, you may pay any amount of red mana; it deals that much damage to any target",
            action_generator=_gen_leyline_tyrant_dies,
        ))


        # June 10 audit: Land Tax was the batch's #1 Tier-3 money pit (53
        # escalations, 9 in one game — one per upkeep). Free upkeep template:
        # check the land-count condition, then move up to 3 basics to hand.
        def _land_tax_gen(ctrl, opp, ctx):
            ctrl_player = ctx.get('_controller_player')
            opp_player = ctx.get('_opponent_player')
            if ctrl_player is None or opp_player is None:
                return [{"action": "no_action",
                         "reason": "Land Tax: player objects unavailable"}]
            my_lands = sum(1 for c in ctrl_player.battlefield if c.is_land())
            opp_lands = sum(1 for c in opp_player.battlefield if c.is_land())
            if my_lands >= opp_lands:
                return [{"action": "no_action",
                         "reason": f"Land Tax: not fewer lands ({my_lands} vs {opp_lands})"}]
            basics = []
            for c in ctrl_player.library:
                tl = (getattr(c, 'type_line', '') or '').lower()
                if 'basic' in tl and 'land' in tl:
                    basics.append(c.name)
                    if len(basics) >= 3:
                        break
            if not basics:
                return [{"action": "no_action",
                         "reason": "Land Tax: no basic lands left in library"}]
            return [{"action": "move_card", "card": n, "from_zone": "library",
                     "to_zone": "hand", "player": ctrl,
                     "reason": "Land Tax fetches a basic land"} for n in basics]

        self._add_card("land tax", EffectTemplate(
            name="Land Tax",
            description="At the beginning of your upkeep, if an opponent controls more lands than you, search your library for up to three basic land cards and put them into your hand",
            action_generator=_land_tax_gen,
        ))

        # June 10 deep-dive (CRITICAL): Marit Lage's Slumber — dedicated
        # condition-checked template. The generic upkeep-token pattern was
        # creating an unconditional vanilla 20/20 "Black Avatar" every upkeep
        # (no 10-snow gate, no sacrifice, no flying/indestructible/legendary)
        # and won game …069662904551 with three of them.
        def _gen_marit_lage_slumber(ctrl, opp, ctx):
            ctrl_player = ctx.get('_controller_player')
            if ctrl_player is None:
                return [{"action": "no_action",
                         "reason": "Marit Lage's Slumber: controller object unavailable"}]
            snow_count = sum(
                1 for c in ctrl_player.battlefield
                if 'snow' in ((getattr(c, 'type_line', '') or '').lower()))
            if snow_count < 10:
                return [{"action": "no_action",
                         "reason": f"Marit Lage's Slumber: {snow_count} of 10 snow permanents — trigger doesn't fire (intervening if, CR 603.4)"}]
            return [
                {"action": "move_card", "card": "Marit Lage's Slumber",
                 "from_zone": "battlefield", "to_zone": "graveyard", "player": ctrl,
                 "reason": "sacrificed (Marit Lage awakens)"},
                {"action": "create_token", "player": ctrl, "name": "Marit Lage",
                 "power": 20, "toughness": 20,
                 "types": "Legendary Creature — Avatar", "count": 1,
                 "keywords": ["Flying", "Indestructible"]},
            ]

        self._add_card("marit lage's slumber", EffectTemplate(
            name="Marit Lage's Slumber",
            description="At the beginning of your upkeep, if you control ten or more snow permanents, sacrifice this enchantment and create Marit Lage, a legendary 20/20 black Avatar creature token with flying and indestructible",
            action_generator=_gen_marit_lage_slumber,
        ))
        # Supported intervening-if upkeep triggers that previously fell into
        # the generic token guard or Tier 3. Keep them event-scoped: Hellkite
        # also has a combat-damage trigger and must not run this check on ETB.
        def _dragonmaster_outcast_upkeep(ctrl, opp, ctx):
            player = ctx.get('_controller_player')
            land_count = sum(1 for card in getattr(player, 'battlefield', [])
                             if card.is_land()) if player is not None else 0
            if land_count < 6:
                return [{"action": "no_action",
                         "reason": f"Dragonmaster Outcast: {land_count} of 6 lands"}]
            return [make_token_action(ctrl, "dragon_5_5", 1)]

        self._add_upkeep_card("dragonmaster outcast", EffectTemplate(
            name="Dragonmaster Outcast",
            description="At upkeep with six or more lands, create a 5/5 red Dragon token with flying",
            action_generator=_dragonmaster_outcast_upkeep,
        ))

        def _damia_upkeep(ctrl, opp, ctx):
            player = ctx.get('_controller_player')
            hand_size = len(getattr(player, 'hand', [])) if player is not None else 0
            if hand_size >= 7:
                return [{"action": "no_action",
                         "reason": f"Damia: hand already has {hand_size} cards"}]
            return [{"action": "draw_cards", "player": ctrl,
                     "amount": 7 - hand_size}]

        self._add_upkeep_card("damia, sage of stone", EffectTemplate(
            name="Damia, Sage of Stone",
            description="At upkeep with fewer than seven cards, draw the difference",
            action_generator=_damia_upkeep,
        ))

        def _hellkite_tyrant_upkeep(ctrl, opp, ctx):
            player = ctx.get('_controller_player')
            artifact_count = sum(
                1 for card in getattr(player, 'battlefield', [])
                if card.is_artifact()) if player is not None else 0
            if artifact_count < 20:
                return [{"action": "no_action",
                         "reason": f"Hellkite Tyrant: {artifact_count} of 20 artifacts"}]
            return [{"action": "win_game", "player": ctrl,
                     "reason": "controls twenty or more artifacts at upkeep"}]

        self._add_upkeep_card("hellkite tyrant", EffectTemplate(
            name="Hellkite Tyrant",
            description="At upkeep with twenty or more artifacts, win the game",
            action_generator=_hellkite_tyrant_upkeep,
        ))

        # Mana Vault — "At the beginning of your upkeep, if Mana Vault is
        # tapped, you may pay {4}. If you don't, Mana Vault deals 1 damage
        # to you." Same "may pay" pattern as Extort/Smothering Tithe: check
        # affordability heuristically and either pay (no damage) or skip
        # (1 self-damage). The damage path was previously always firing
        # because the trigger fell through to Tier 3 which wasn't great
        # at modeling optional costs.
        def _mana_vault_upkeep_gen(ctrl, opp, ctx):
            ctrl_player = ctx.get('_controller_player')
            game = ctx.get('_game')
            if ctrl_player is None:
                return [{"action": "no_action",
                         "reason": "Mana Vault: no controller in ctx"}]
            # Find the Mana Vault on battlefield; if it's untapped, the
            # trigger doesn't actually fire (CR: "if Mana Vault is tapped").
            mana_vault = None
            for perm in ctrl_player.battlefield:
                if (perm.name or '').lower() == "mana vault":
                    mana_vault = perm
                    break
            if mana_vault is None or not getattr(mana_vault, 'tapped', False):
                return [{"action": "no_action",
                         "reason": "Mana Vault: not on battlefield or untapped (trigger doesn't fire)"}]
            # Count untapped mana from OTHER permanents (Mana Vault itself is
            # tapped, so can't tap for its own upkeep — and even if you
            # untapped it via a trick, the payment can't use Mana Vault's
            # produced mana per CR 117.5).
            untapped_mana = 0
            for perm in ctrl_player.battlefield:
                if perm is mana_vault or getattr(perm, 'tapped', False):
                    continue
                try:
                    prod = ctrl_player._get_mana_production(perm) or {}
                except Exception:
                    prod = {}
                untapped_mana += sum(int(v or 0) for v in prod.values())
            # Heuristic: pay if controller has 5+ untapped mana (4 for the
            # cost + 1 spare for interaction). Skip if not enough OR if
            # life > 5 and the controller would benefit from preserving
            # mana for spells. Conservative: always pay if can afford.
            if untapped_mana >= 4:
                # "Pay" — tap permanents totalling 4 generic. We don't
                # model the actual tap (tier 1.5 doesn't have great hooks
                # for cost payment), but we DO signal that the damage
                # doesn't fire. This is the most common autoplay path.
                return [{"action": "no_action",
                         "reason": f"Mana Vault: paid {{4}} ({untapped_mana} untapped available)"}]
            # Can't afford — take 1 damage.
            return [
                {"action": "lose_life", "player": ctrl, "amount": 1,
                 "source": "Mana Vault upkeep"},
            ]
        self._add_card("mana vault", EffectTemplate(
            name="Mana Vault",
            description="At your upkeep, if tapped, you may pay {4}. If you don't, Mana Vault deals 1 damage to you",
            action_generator=_mana_vault_upkeep_gen,
        ))

        # Pestilence — "At the beginning of the end step, sacrifice
        # Pestilence unless it dealt damage to a creature or player this
        # turn." May-pay-cost variant: not "you may pay" but a similar
        # optional path (if-not-then sacrifice). Skipping for now since
        # the damage-tracking required is heavier than the value.

        # Mana Crypt — "At the beginning of your upkeep, flip a coin.
        # Lose the flip, Mana Crypt deals 3 damage to you." This is NOT
        # a may-pay effect (no choice) but it's in the same family of
        # upkeep self-damage cards. Random 50/50 — heuristic: take damage
        # half the time (deterministic-randomness based on turn number to
        # keep games reproducible).
        def _mana_crypt_upkeep_gen(ctrl, opp, ctx):
            game = ctx.get('_game')
            turn = int(getattr(game, 'turn_number', 0) or 0)
            ctrl_player = ctx.get('_controller_player')
            ctrl_name = (ctrl_player.name if ctrl_player else ctrl) or ""
            # Hash turn + controller name for a stable per-game coin flip.
            flip = (turn * 17 + sum(ord(c) for c in ctrl_name)) % 2
            if flip == 0:
                return [{"action": "no_action",
                         "reason": "Mana Crypt: coin flip won (no damage)"}]
            return [{"action": "lose_life", "player": ctrl, "amount": 3,
                     "source": "Mana Crypt upkeep"}]
        self._add_card("mana crypt", EffectTemplate(
            name="Mana Crypt",
            description="At your upkeep, flip a coin. If lost, Mana Crypt deals 3 damage to you",
            action_generator=_mana_crypt_upkeep_gen,
        ))

        # Smothering Tithe — "Whenever an opponent draws a card, that player
        # may pay {2}. If they don't, you create a Treasure token." May 17
        # audit: previously always created the Treasure, treating "may pay" as
        # "never pays". Now check opponent's available mana — they pay if they
        # can afford {2} and aren't tapped out (heuristic: keep 1 mana free
        # for instants on opponent's turn).
        def _smothering_tithe_gen(ctrl, opp, ctx):
            opp_player = _find_player_by_name(ctx.get('_game'), opp)
            if opp_player is None:
                return [make_token_action(ctrl, "treasure", 1)]
            untapped_mana = 0
            for perm in opp_player.battlefield:
                if not getattr(perm, 'tapped', False):
                    try:
                        prod = opp_player._get_mana_production(perm) or {}
                    except Exception:
                        prod = {}
                    untapped_mana += sum(int(v or 0) for v in prod.values())
            # Opponent pays if they have 2+ untapped mana with 1 to spare for
            # interaction (3+ total). Otherwise treasure mints.
            if untapped_mana >= 3:
                return [{"action": "no_action",
                         "reason": "Smothering Tithe: opponent paid {2} (untapped >= 3)"}]
            return [make_token_action(ctrl, "treasure", 1)]

        self._add_card("smothering tithe", EffectTemplate(
            name="Smothering Tithe",
            description="Whenever an opponent draws, they may pay {2}. If they don't, you create a Treasure token",
            action_generator=_smothering_tithe_gen,
        ))

        # Sphinx's Tutelage — "Whenever you draw a card except the first one
        # each turn, target opponent puts the top two cards of their library
        # into their graveyard. If they're both nonland and share a color,
        # repeat this process." Opponent has no "may pay" choice here — but
        # the engine wasn't modeling mill correctly. (Actually Sphinx's
        # Tutelage isn't a "may pay" effect — it's automatic mill. Including
        # here because the audit flagged it as a class, but the right fix is
        # the mill action — flag for future template work.)
        # (No template change yet — keeping mill in Tier 3 fallback.)


        # --- Creature-enters triggers (for other permanents) ---
        # Trostani: whenever a creature enters, gain life equal to its toughness
        # Aug 2 (corners-of-corners): Everflowing Chalice enters with a
        # charge counter per multikicker payment — the production side has
        # read charge counters since forever (models._get_mana_production);
        # the PAYMENT -> counters half never existed, so it entered dead.
        # --- Aug 2, 2026: ability-word CONDITION cards (CR 207.2c) ---
        # Each used its weak half forever — no delirium/morbid predicate
        # existed, so Tragic Slip was a -1/-1 "removal" spell and Unholy Heat
        # never dealt 6.
        # Consumers for the predicates added alongside them — without these
        # the predicates were built and never called, which is its own bug
        # shape (the July-21 `game._rules_engine` class: machinery wired to
        # nothing). Each of these probed as resolving NOTHING at any event.
        self._add_card("reaper from the abyss endstep", EffectTemplate(
            name="Reaper from the Abyss",
            description=("At end step, if a creature died this turn, destroy "
                         "target non-Demon creature"),
            action_generator=self._gen_reaper_from_the_abyss,
        ))
        self._add_card("puresteel paladin", EffectTemplate(
            name="Puresteel Paladin",
            description="Whenever an Equipment you control enters, draw a card",
            action_generator=self._gen_puresteel_paladin_etb,
        ))
        self._add_card("polukranos, world eater", EffectTemplate(
            name="Polukranos, World Eater",
            description=("When it becomes monstrous, deal X damage divided "
                         "among opposing creatures; each deals its power back"),
            action_generator=self._gen_polukranos_monstrosity,
        ))
        self._add_card("tragic slip", EffectTemplate(
            name="Tragic Slip",
            description="Target creature gets -1/-1, or -13/-13 with morbid",
            action_generator=self._gen_tragic_slip,
        ))
        self._add_card("unholy heat", EffectTemplate(
            name="Unholy Heat",
            description="Deal 2 damage, or 6 with delirium",
            action_generator=self._gen_unholy_heat,
        ))
        self._add_card("traverse the ulvenwald", EffectTemplate(
            name="Traverse the Ulvenwald",
            description=("Search for a basic land, or any creature/land with "
                         "delirium"),
            action_generator=self._gen_traverse_the_ulvenwald,
        ))
        # --- Aug 2 batch-14 Tier-3 shrink (the batch's top escalations) ---
        # Scheduled keys carry a scheduled-prefix description so the F25
        # guard lets them fire on their own event; the end-step one uses the
        # suffix-key convention so a future bare-name registration can't
        # silently overwrite it (the July-31 Soulherder lesson).
        self._add_card("sire of insanity endstep", EffectTemplate(
            name="Sire of Insanity",
            description="At end step, each player discards their hand",
            action_generator=self._gen_sire_of_insanity,
        ))
        self._add_card("song of the worldsoul", EffectTemplate(
            name="Song of the Worldsoul",
            description="Whenever you cast a spell, populate",
            action_generator=self._gen_song_of_the_worldsoul,
        ))
        self._add_dies_card("glissa, the traitor", EffectTemplate(
            name="Glissa, the Traitor",
            description=("When a creature an opponent controls dies, return "
                         "target artifact card from your graveyard to hand"),
            action_generator=self._gen_glissa_the_traitor,
        ))
        self._add_card("the ozolith beginningcombat", EffectTemplate(
            name="The Ozolith",
            description=("At beginning of combat, move all counters from The "
                         "Ozolith onto target creature"),
            action_generator=self._gen_the_ozolith_combat,
        ))
        self._add_card("arclight phoenix beginningcombat", EffectTemplate(
            name="Arclight Phoenix",
            description=("At beginning of combat, if you've cast three or "
                         "more instant and sorcery spells this turn, return "
                         "this card from your graveyard to the battlefield"),
            action_generator=self._gen_arclight_phoenix,
        ))
        # Aug 2 batch-14 audit: without this name key, the generic ETB-draw
        # pattern matched across "target opponent may have you draw three
        # cards" and resolved every Gearhulk as an unconditional draw-3,
        # silently discarding the opponent's choice and the mill+burn branch.
        self._add_card("combustible gearhulk", EffectTemplate(
            name="Combustible Gearhulk",
            description=("Target opponent may have you draw three cards; if "
                         "they decline, mill three and deal damage equal to "
                         "their total mana value"),
            action_generator=self._gen_combustible_gearhulk,
        ))
        self._add_card("everflowing chalice", EffectTemplate(
            name="Everflowing Chalice",
            description=("Enters with a charge counter for each time it was "
                         "kicked; taps for {C} per charge counter"),
            action_generator=self._gen_everflowing_chalice,
        ))
        # Aug 2 (corners-of-corners): Chandra, Spark Hunter's beginning-of-
        # combat vehicle animate — the other half of the batch-13 PW-token
        # fix (her Vehicle token is a real non-creature artifact now; this
        # trigger is what lets it attack). Suffix key per the scheduled-
        # lookup convention; "up to one target" resolves none-chosen when no
        # Vehicle is out (CR 601.2c).
        self._add_card("chandra, spark hunter beginningcombat", EffectTemplate(
            name="Chandra, Spark Hunter (combat)",
            description=("Beginning of combat: up to one target Vehicle you "
                         "control becomes an artifact creature with haste "
                         "until end of turn"),
            action_generator=self._gen_chandra_spark_hunter_combat,
        ))
        # Aug 2 batch-13: Life from the Loam was a zero-action Tier-3
        # escalation (no template anywhere).
        self._add_card("life from the loam", EffectTemplate(
            name="Life from the Loam",
            description=("Return up to three land cards from your graveyard "
                         "to your hand"),
            action_generator=self._gen_life_from_the_loam,
        ))
        # Aug 2 batch-13: moved from card_templates.json — JSON can't branch
        # on ctx['entwined'], and the flat entry resolved the entwined result
        # unconditionally (both modes at base cost).
        self._add_card("tooth and nail", EffectTemplate(
            name="Tooth and Nail",
            description=("Tooth and Nail: one mode (search to hand, or put "
                         "from hand onto battlefield); both modes with "
                         "entwine paid"),
            action_generator=self._gen_tooth_and_nail,
        ))
        self._add_card("trostani, selesnya's voice", EffectTemplate(
            name="Trostani, Selesnya's Voice",
            description="Whenever a creature enters, gain life equal to its toughness",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "gain_life", "player": ctrl,
                 "amount": ctx.get('entering_creature_toughness', 2)}
            ],
        ))

        # --- Thassa's Oracle: ETB win condition ---
        # Bug fix: was returning no_action, never checking devotion vs library size
        self._add_card("thassa's oracle", EffectTemplate(
            name="Thassa's Oracle",
            description="When Thassa's Oracle enters, check devotion to blue vs library size — may win",
            action_generator=self._gen_thassas_oracle,
        ))

        # --- Genesis Wave: reveal top X, put permanents with CMC ≤ X onto battlefield ---
        # Bug fix: was falling to Tier 3 → always returning no_action. 5 casts, 0 resolved.
        self._add_card("genesis wave", EffectTemplate(
            name="Genesis Wave",
            description="Reveal top X cards, put permanents with CMC ≤ X onto battlefield, rest to graveyard",
            action_generator=self._gen_genesis_wave,
        ))

        # --- Growing Rites of Itlimoc: look at top 4, put creature in hand ---
        # Bug fix: fell to Tier 3 → no_action → judge. 8 wasted API calls.
        self._add_card("growing rites of itlimoc", EffectTemplate(
            name="Growing Rites of Itlimoc",
            description="Look at top 4 cards, put a creature into your hand, rest on bottom",
            action_generator=self._gen_growing_rites,
        ))
        self._add_card("growing rites of itlimoc endstep", EffectTemplate(
            name="Growing Rites of Itlimoc",
            description="At the beginning of your end step, transform if you control four or more creatures",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "transform_permanent", "player": ctrl,
                  "card": "Growing Rites of Itlimoc"}]
                if sum(1 for c in ctx.get('controller_battlefield', [])
                       if c.is_creature() and not getattr(c, '_phased_out', False)) >= 4
                else [{"action": "no_action",
                       "reason": "Growing Rites: fewer than four creatures"}]
            ),
        ))

        # ===========================================================
        # NEW TEMPLATES — March 22 log audit
        # ===========================================================

        # June 10 deep-dive: the old Twinflame Tyrant template here was a
        # March-22 HALLUCINATION ("Deal 5 damage to any target" — matching
        # nothing printed on the card). The real card is a static damage
        # doubler, now registered in rules/replacement.py. A normal cast
        # would have dealt 5 phantom damage; deleted.


        self._add_card("oath of teferi", EffectTemplate(
            name="Oath of Teferi",
            description="Exile another permanent you control, return it immediately",
            action_generator=self._gen_oath_of_teferi,
        ))

        self._add_card("cavalier of dawn", EffectTemplate(
            name="Cavalier of Dawn",
            description="Destroy target nonland permanent, its controller creates 3/3 Golem",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": ctx.get('best_opponent_nonland', ''),
                 "target_controller": opp},
                {"action": "create_token", "player": opp, "name": "Golem",
                 "power": 3, "toughness": 3, "types": "Artifact Creature — Golem", "count": 1}
            ] if ctx.get('best_opponent_nonland') else [
                {"action": "no_action", "reason": "No nonland permanent to destroy"}
            ],
        ))

        self._add_card("spark double", EffectTemplate(
            name="Spark Double",
            description="Enter as copy of creature you control with +1/+1 counter",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "become_copy", "player": ctrl,
                 "target": ctx.get('best_own_creature', ''),
                 "modifications": [{"action": "add_counters", "counter_type": "+1/+1", "amount": 1}]}
            ] if ctx.get('best_own_creature') else [
                {"action": "no_action", "reason": "No creature to copy"}
            ],
        ))


        # --- Fog / damage prevention spells ---
        for fog_name in ["fog", "moment's peace", "constant mists", "tangle",
                         "arachnogenesis", "comeuppance", "dawn charm",
                         "holy day", "ethereal haze", "darkness"]:
            self._add_card(fog_name, EffectTemplate(
                name=fog_name.title(),
                description="Prevent all combat damage this turn",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "prevent_combat_damage", "scope": "all"},
                ],
            ))


        # (reanimate: single registration lives at _reanimate_gen — the one
        # that honors the declared target and charges the real mana value.
        # Aug 7 registry dedup removed the _gen_reanimate duplicate, which
        # the Aug 5 verification had already identified as dead code.)

        # Animate Dead: reanimate from any graveyard (no life loss, aura attaches)
        # Template avoids Tier 3 which incorrectly fires LTB trigger during ETB
        self._add_card("animate dead", EffectTemplate(
            name="Animate Dead",
            description="Return target creature from a graveyard to battlefield under your control",
            # CR 608.2b: honor the declared target. Without the
            # explicit_target_name term this always grabbed the highest-CMC
            # creature in ANY graveyard, so a declared Birds of Paradise became
            # the opponent's Sun Titan. "Any graveyard" IS the right scope here
            # (the card reads "Enchant creature card in a graveyard"), so only
            # the dropped target was wrong — no own_graveyard flag.
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "reanimate", "player": ctrl,
                 "card": (ctx.get('explicit_target_name')
                          or ctx.get('best_graveyard_creature', '')),
                 "reason": "Animate Dead returns creature from graveyard"},
            ] if (ctx.get('explicit_target_name')
                  or ctx.get('best_graveyard_creature')) else [
                {"action": "no_action", "reason": "No creature cards in any graveyard"}
            ],
        ))

        # Sidisi, Undead Vizier: Exploit — sacrifice a creature, search for any card
        self._add_card("sidisi, undead vizier", EffectTemplate(
            name="Sidisi, Undead Vizier",
            description="Exploit: sacrifice a creature, then search library for a card",
            action_generator=lambda ctrl, opp, ctx: self._gen_sidisi_exploit(ctrl, opp, ctx),
        ))


        # --- Commit // Memory (Commit half — fizzles politely if no target) ---
        # Aftermath card; only the Commit half is castable from hand. The
        # Memory half is hardcoded as cast-from-graveyard-only, which the
        # current engine doesn't model — so we just handle the Commit side.
        # CR text: "Put target spell or nonland permanent into its owner's
        # library second from the top." Engine doesn't model second-from-top
        # exactly, so we approximate as bounce-to-library.
        def _gen_commit(ctrl, opp, ctx):
            # June 11 audit: Commit has two modes — target SPELL or target
            # nonland permanent. The spell mode was missing entirely, so
            # "Commit resolves" printed while the targeted spell resolved
            # anyway (Greater Good survived and drew 6). Prefer the spell
            # mode when there's an opposing spell on the stack.
            if ctx.get('stack_top_spell'):
                return [{"action": "counter_spell", "player": ctrl,
                         "target": "stack_top", "countered_to": "library"}]
            target = ctx.get('best_opponent_threat') or ctx.get('best_opponent_creature')
            if not target:
                return [{"action": "no_action",
                         "reason": "Commit: no opposing threat to bounce"}]
            return [{
                "action": "move_card",
                "card": target,
                "from_zone": "battlefield",
                "to_zone": "library",
                "player": opp,
                "position": "top",  # approximation of "second from top"
            }]
        self._add_card("commit // memory", EffectTemplate(
            name="Commit // Memory",
            description="Commit: bounce target nonland permanent to library (second from top)",
            action_generator=_gen_commit,
        ))
        # Some printings are aliased to just "Commit" in deck files
        self._add_card("commit", EffectTemplate(
            name="Commit",
            description="Bounce target nonland permanent to library (second from top)",
            action_generator=_gen_commit,
        ))

        # --- Curse of the Swine ---
        # June 11 audit: with no template, this charged X=7 for one chosen
        # target, exiled nothing, and printed a Boar for the WRONG player
        # (which then never existed). "Exile X target creatures. For each
        # creature exiled this way, its controller creates a 2/2 green Boar."
        def _curse_of_swine(ctrl, opp, ctx):
            x = max(1, int(ctx.get('x_value') or 1))
            victims = sorted(ctx.get('_opponent_creatures') or [],
                             key=lambda c: -(c.get('power') or 0))[:x]
            if not victims:
                return [{"action": "no_action",
                         "reason": "Curse of the Swine: no creatures to exile"}]
            actions = [{"action": "move_card", "card": v['name'],
                        "from_zone": "battlefield", "to_zone": "exile",
                        "player": opp}
                       for v in victims]
            actions.append({"action": "create_token", "player": opp,
                            "name": "Boar", "power": "2", "toughness": "2",
                            "types": "Creature Token — Boar", "colors": "G",
                            "count": len(victims)})
            return actions
        self._add_card("curse of the swine", EffectTemplate(
            name="Curse of the Swine",
            description="Exile X target creatures; their controller gets that many 2/2 Boars",
            action_generator=_curse_of_swine,
        ))

        # --- Cruel Ultimatum ---
        # June 11 audit: with no template this fell to Tier 3, which resolved
        # it as caster-gains-5/caster-loses-5 and nothing else (a 7-mana
        # no-op with the life loss on the wrong player). Full text: "Target
        # opponent sacrifices a creature, discards three cards, then loses 5
        # life. You return a creature card from your graveyard to your hand,
        # draw three cards, then gain 5 life."
        def _cruel_ultimatum(ctrl, opp, ctx):
            actions = [
                {"action": "sacrifice_permanent", "player": opp,
                 "type_filter": "creature",
                 "reason": "Cruel Ultimatum: target opponent sacrifices a creature"},
                {"action": "discard", "player": opp, "card": "worst"},
                {"action": "discard", "player": opp, "card": "worst"},
                {"action": "discard", "player": opp, "card": "worst"},
                {"action": "lose_life", "player": opp, "amount": 5,
                 "source": "Cruel Ultimatum"},
            ]
            gy = ctx.get('controller_graveyard') or []
            best_creature = None
            for c in gy:
                try:
                    if c.is_creature() and (best_creature is None
                                            or (c.cmc or 0) > (best_creature.cmc or 0)):
                        best_creature = c
                except AttributeError:
                    continue
            if best_creature is not None:
                actions.append({"action": "move_card", "card": best_creature.name,
                                "from_zone": "graveyard", "to_zone": "hand",
                                "player": ctrl})
            actions.append({"action": "draw_cards", "player": ctrl, "amount": 3})
            actions.append({"action": "gain_life", "player": ctrl, "amount": 5,
                            "source": "Cruel Ultimatum"})
            return actions
        self._add_card("cruel ultimatum", EffectTemplate(
            name="Cruel Ultimatum",
            description="Opponent sacrifices a creature, discards 3, loses 5; you return a creature to hand, draw 3, gain 5",
            action_generator=_cruel_ultimatum,
        ))

        # --- Inferno Titan / Bogardan Hellkite: divided damage ETB ---
        # (inferno titan: second duplicate removed here too — Aug 7 registry
        # dedup; the surviving registration is the creature-first version.)

        # --- Altar of Dementia: sacrifice creature, mill equal to power ---
        self._add_card("altar of dementia", EffectTemplate(
            name="Altar of Dementia",
            description="Sacrifice a creature: target player mills cards equal to sacrificed creature's power",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "mill", "player": opp,
                 "amount": ctx.get('greatest_power', 3)},
            ],
        ))

        # --- Eternal Witness: return card from graveyard to hand ---
        self._add_card("eternal witness", EffectTemplate(
            name="Eternal Witness",
            description="Return target card from graveyard to hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": ctx.get('best_graveyard_card', ''),
                 "from_zone": "graveyard", "to_zone": "hand", "player": ctrl},
            ] if ctx.get('best_graveyard_card') else [
                {"action": "no_action", "reason": "No cards in graveyard to return"}
            ],
        ))


        self._add_attack_card("robber of the rich", EffectTemplate(
            name="Robber of the Rich",
            description="Whenever Robber attacks, conditionally exile the defending player's top card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": "top_of_library", "from_zone": "library",
                 "to_zone": "exile", "player": opp,
                 "reason": "Robber of the Rich: exile top card"}
            ] if len(ctx.get('opponent_hand', [])) > len(ctx.get('controller_hand', [])) else [
                {"action": "no_action", "reason": "Robber of the Rich: defending player does not have more cards in hand"}
            ],
        ))

        # --- END STEP TRIGGERS ---
        # (July 31 batch-10: the SECOND bare "soulherder" registration that
        # lived here was DELETED — _add_card is a plain dict assignment, so it
        # silently overwrote the guard-compliant template above (line ~1752)
        # with a description ("End step: ...") that dodges the F25
        # scheduled-prefix guard. Result: end-step dispatch skipped the
        # template entirely and escalated to Tier 3 on every end step (17
        # drains in batch 15324). Same duplicate-key-overwrite class as the
        # July 30 Ancient Bronze Dragon note below and July 28's dead
        # _gen_reanimate. The surviving registration is the suffix-keyed
        # "soulherder endstep" one added alongside the ~1752 site.)


        # (July 30, 2026: the March-27 "ancient bronze dragon" ETB
        # registration was DELETED — the card's only trigger is COMBAT
        # DAMAGE, and the old generator pumped the whole team ±d20 until end
        # of turn via random.randint. The real trigger lives in the attack
        # registry now; same hallucinated-template class as Twinflame Tyrant.)

        # ===========================================================
        # NEW TEMPLATES — March 27 Tier 3 gap closure
        # ===========================================================


        # --- Grisly Salvage: reveal top 5, creature/land to hand, rest to graveyard ---
        self._add_card("grisly salvage", EffectTemplate(
            name="Grisly Salvage",
            description="Reveal top 5 cards, put a creature or land into hand, rest into graveyard",
            action_generator=self._gen_grisly_salvage,
        ))

        # --- Massacre Wurm: ETB shrinks, dies-trigger drains ---
        # Two distinct abilities, dispatched by oracle context:
        #  ETB: "When Massacre Wurm enters, creatures your opponents control
        #       get -2/-2 until end of turn"
        #  Dies trigger: "Whenever a creature an opponent controls dies, that
        #               player loses 2 life"
        self._add_card("massacre wurm", EffectTemplate(
            name="Massacre Wurm",
            description="ETB: opponents' creatures -2/-2 EOT. Dies-trigger: opp loses 2 life when their creature dies",
            action_generator=lambda ctrl, opp, ctx: (
                # Dies-trigger context (called from _check_dies_triggers_sync via
                # the template path, which passes the trigger sentence as oracle).
                [{"action": "lose_life", "player": opp, "amount": 2}]
                if ('whenever a creature an opponent controls dies' in (ctx.get('_oracle') or '').lower()
                    or 'that player loses 2 life' in (ctx.get('_oracle') or '').lower())
                else
                # ETB pump (default — also fires when oracle contains "enters" or no specific snippet)
                [{"action": "pump_all_creatures", "player": opp,
                  "power": -2, "toughness": -2, "filter": "opponents",
                  "controller": ctrl}]
            ),
        ))


        # NOTE: Light Up the Stage is registered later in this file with a
        # working draw-2 approximation (line ~5778). The earlier "exile
        # top_of_library" version that lived here referenced a card name the
        # move_card action doesn't recognize, so it silently produced no state
        # change. Removed during Apr 29 audit — the later registration is the
        # canonical one.


        def _shared_animosity_gen(ctrl, opp, ctx):
            attacker = ctx.get('_attacking_creature')
            game = ctx.get('_game')
            battlefield = ctx.get('controller_battlefield', []) or []
            if attacker is None:
                return [{"action": "no_action", "reason": "Shared Animosity: attacking creature unavailable"}]
            attacker_types = set(attacker.get_creature_types())
            amount = sum(
                1 for creature in battlefield
                if creature.id != attacker.id
                and getattr(creature, 'attacking', False)
                and attacker_types.intersection(creature.get_creature_types())
            )
            return [{
                "action": "pump_all_creatures", "player": ctrl,
                "card_id": attacker.id, "power": amount, "toughness": 0,
                "source": "Shared Animosity",
            }]

        self._add_attack_card("shared animosity", EffectTemplate(
            name="Shared Animosity",
            description="Whenever a creature you control attacks, it gets +1/+0 for each other attacking creature sharing a type",
            action_generator=_shared_animosity_gen,
        ))

    def _gen_grisly_salvage(self, ctrl, opp, ctx) -> List[Dict]:
        """Grisly Salvage: reveal top 5 cards, put a creature or land card
        into your hand, rest into your graveyard.

        For autoplay, we look at the top 5 cards from the library context and
        pick the best creature or land. The rest go to graveyard.
        """
        library = ctx.get('controller_library', [])
        if not library:
            return [{"action": "no_action", "reason": "Grisly Salvage: library is empty"}]

        look_count = min(5, len(library))
        found_card = None
        found_idx = -1

        # Pick the best creature, then best land from top 5
        for i in range(look_count):
            card = library[i]
            type_line = (card.type_line or '').lower() if hasattr(card, 'type_line') else ''
            if 'creature' in type_line:
                found_card = card.name if hasattr(card, 'name') else str(card)
                found_idx = i
                break  # Prefer creatures
        if not found_card:
            for i in range(look_count):
                card = library[i]
                type_line = (card.type_line or '').lower() if hasattr(card, 'type_line') else ''
                if 'land' in type_line:
                    found_card = card.name if hasattr(card, 'name') else str(card)
                    found_idx = i
                    break

        actions = []
        for i in range(look_count):
            card = library[i]
            card_name = card.name if hasattr(card, 'name') else str(card)
            if i == found_idx:
                actions.append({"action": "move_card", "card": card_name,
                                "from_zone": "library", "to_zone": "hand", "player": ctrl})
            else:
                actions.append({"action": "move_card", "card": card_name,
                                "from_zone": "library", "to_zone": "graveyard", "player": ctrl})

        if not actions:
            return [{"action": "no_action", "reason": "Grisly Salvage: no cards to reveal"}]
        return actions

    def _gen_genesis_wave(self, ctrl, opp, ctx) -> List[Dict]:
        """Genesis Wave: reveal top X cards. Put all permanent cards with CMC ≤ X
        onto the battlefield. Put the rest into the graveyard.

        For autoplay, we simulate by moving top X cards: permanents go to battlefield,
        non-permanents go to graveyard. Since we can't literally reveal, we use move_card
        actions for each card.
        """
        import random
        x_value = ctx.get('x_value', 0)
        if x_value <= 0:
            # Try to get X from other context keys
            x_value = ctx.get('X', 0) or ctx.get('x', 0)
        if x_value <= 0:
            return [{"action": "no_action", "reason": "Genesis Wave: X=0, no cards revealed"}]

        library = ctx.get('controller_library', [])
        if not library:
            return [{"action": "no_action", "reason": "Genesis Wave: library is empty"}]

        actions = []
        cards_to_reveal = min(x_value, len(library))
        permanent_types = {'creature', 'artifact', 'enchantment', 'land', 'planeswalker'}

        for i in range(cards_to_reveal):
            if i >= len(library):
                break
            card = library[i]
            card_name = card.name if hasattr(card, 'name') else str(card)
            type_line = (card.type_line or '').lower() if hasattr(card, 'type_line') else ''
            card_cmc = getattr(card, 'cmc', 0) or 0

            is_permanent = any(t in type_line for t in permanent_types)

            if is_permanent and card_cmc <= x_value:
                # Permanent with CMC ≤ X → battlefield
                actions.append({"action": "move_card", "card": card_name,
                                "from_zone": "library", "to_zone": "battlefield",
                                "player": ctrl})
            else:
                # Non-permanent or CMC > X → graveyard
                actions.append({"action": "move_card", "card": card_name,
                                "from_zone": "library", "to_zone": "graveyard",
                                "player": ctrl})

        if not actions:
            return [{"action": "no_action", "reason": "Genesis Wave: no cards in library to reveal"}]
        return actions

    def _gen_growing_rites(self, ctrl, opp, ctx) -> List[Dict]:
        """Growing Rites of Itlimoc: look at top 4 cards, put a creature into
        your hand, rest on bottom in random order.

        For autoplay, we find the first creature in the top 4 and move it to hand.
        The rest go to the bottom of the library.
        """
        library = ctx.get('controller_library', [])
        if not library:
            return [{"action": "no_action", "reason": "Growing Rites: library is empty"}]
        return [{"action": "select_from_top", "player": ctrl, "amount": 4,
                 "card_type": "creature", "rest_to": "library_bottom"}]

    def _gen_rishkars_expertise(self, ctrl, opp, ctx) -> List[Dict]:
        """Rishkar's Expertise: draw = greatest power, then free cast MV ≤ 5."""
        power = ctx.get('greatest_power', 0)
        actions = []
        if power > 0:
            actions.append({"action": "draw_cards", "player": ctrl, "amount": power})
        # The free cast is flagged via turn_effects in resolve_special_effects;
        # template just handles the draw part since free-cast is a game mechanic
        return actions if actions else [{"action": "no_action", "reason": "No creatures on battlefield"}]

    def _gen_return_of_wildspeaker(self, ctrl, opp, ctx) -> List[Dict]:
        """Return of the Wildspeaker: modal — draw cards equal to greatest power
        among non-Human creatures, OR non-Human creatures get +3/+3 and trample.

        Draw mode is almost always better. Only pick pump if we have many
        creatures and the greatest power is low (combat lethal scenario).
        """
        power = ctx.get('greatest_power', 4)
        creature_count = ctx.get('creature_count', 0)

        # Pump mode: only if we have 4+ creatures and low power (combat lethal)
        if creature_count >= 4 and power <= 3:
            return [{"action": "pump_all_creatures", "player": ctrl,
                     "power": 3, "toughness": 3, "keywords": ["Trample"],
                     "exclude_types": ["Human"]}]

        # Draw mode: default and almost always correct
        if power > 0:
            return [{"action": "draw_cards", "player": ctrl, "amount": power}]
        return [{"action": "no_action", "reason": "No non-Human creatures for either mode"}]

    def _gen_mystic_sanctuary(self, ctrl, opp, ctx) -> List[Dict]:
        """Mystic Sanctuary: put target instant or sorcery from graveyard on top of library.

        Only triggers when entering untapped (3+ Islands). We find the best
        instant/sorcery in the graveyard by CMC.
        """
        graveyard = ctx.get('controller_graveyard', [])
        # Find best instant/sorcery by CMC
        best = None
        best_cmc = -1
        for card_info in graveyard:
            if isinstance(card_info, dict):
                types = card_info.get('types', '').lower()
                if 'instant' in types or 'sorcery' in types:
                    cmc = card_info.get('cmc', 0)
                    if cmc > best_cmc:
                        best = card_info.get('name')
                        best_cmc = cmc
            elif hasattr(card_info, 'type_line'):
                types = (card_info.type_line or '').lower()
                if 'instant' in types or 'sorcery' in types:
                    cmc = getattr(card_info, 'cmc', 0) or 0
                    if cmc > best_cmc:
                        best = card_info.name
                        best_cmc = cmc

        if best:
            return [{"action": "move_card", "card": best, "from_zone": "graveyard",
                     "to_zone": "library", "position": "top", "player": ctrl}]
        return [{"action": "no_action",
                 "reason": "No instant or sorcery in graveyard for Mystic Sanctuary"}]

    def _gen_thassas_oracle(self, ctrl, opp, ctx) -> List[Dict]:
        """Thassa's Oracle: ETB — if devotion to blue >= library size, you win.

        Bug fix: was only announcing the effect, never checking the win condition.
        Now checks devotion_to_blue vs library_size and returns win_game if met.
        Also does the scry-like look at top X cards as a secondary effect.
        """
        devotion = ctx.get('devotion_to_blue', 2)  # Default 2 (UU from Oracle itself)
        library_size = ctx.get('library_size', 99)  # Default high so we don't false-win

        if devotion >= library_size:
            # WIN CONDITION MET
            return [{"action": "win_game", "player": ctrl,
                     "reason": f"Thassa's Oracle: devotion to blue ({devotion}) >= "
                               f"library size ({library_size})"}]

        # Win condition not met — still do the look-at-top-X scry effect
        if devotion > 0:
            return [{"action": "no_action",
                     "reason": f"Thassa's Oracle: devotion to blue = {devotion}, "
                               f"library = {library_size} cards — no win. "
                               f"Look at top {devotion}, put up to 1 on top, rest on bottom"}]
        return [{"action": "no_action",
                 "reason": "Thassa's Oracle: devotion to blue is 0, no cards to look at"}]

    def _build_pattern_templates(self):
        """
        Oracle text patterns that match families of cards.

        These are tried after card-name lookup fails.
        Patterns should be specific enough to avoid false positives.
        """
        
        # "When [this] enters, draw [N] cards?"
        self._add_pattern(
            r"when .+? enters.*?draw (\w+) cards?",
            EffectTemplate(
                name="ETB Draw",
                description="Draw cards on ETB",
                action_generator=self._gen_draw_from_match,
            )
        )

        # Thrasios, Triton Hero's {4} activation (Aug 1 batch-12, reviewer,
        # partner game): the name-keyed template was UNREACHABLE — the
        # activation dispatch passes event_type="activated", which
        # _NAME_KEYED_EVENT_TYPES deliberately excludes (Apr 29, the Thassa
        # double-fire fix) — so every activation escalated to Tier 3, which
        # resolved only the scry and dropped the land/draw payoff. A PATTERN
        # runs on activation (the Feldon precedent, July 28). The scry-1
        # ahead of the reveal is deliberately skipped (the generator cannot
        # know the scry decision the executor would make; reveal-top is the
        # dominant clause).
        self._add_pattern(
            r"reveal the top card of your library\W+if it'?s a land card, "
            r"put it onto the battlefield tapped\W+otherwise,? draw a card",
            EffectTemplate(
                name="Thrasios-style reveal",
                description="Reveal top: land onto battlefield tapped, else draw",
                action_generator=self._gen_reveal_top_land_or_draw,
            )
        )

        # Proliferate (Atraxa end-step, Flux Channeler cast-trigger, Evolution
        # Sage landfall, Tezzeret's Gambit). May 26 audit: proliferate had NO
        # lower-tier handler, so it escalated to Tier 3 — which hallucinated a
        # +1/-1 life swing for Atraxa's proliferate (proliferate never touches
        # life). Match only when "proliferate" is the whole effect clause
        # (clause-final, before reminder text or end-of-string) so bundled
        # effects ("draw a card, then proliferate") fall through rather than
        # dropping their other half. _gen_proliferate additionally guards
        # against firing on a creature's ETB scan when the proliferate is
        # really a scheduled (end-step/upkeep) trigger.
        self._add_pattern(
            r"(?:^|[,.:—]\s*|then\s+)proliferate\b\.?\s*(?:\(|$)",
            EffectTemplate(
                name="Proliferate",
                description="Proliferate (add a counter of each existing kind)",
                action_generator=self._gen_proliferate,
            )
        )

        # July 20 audit (CRITICAL): populate had no lower-tier handler — Song
        # of the Worldsoul escalated to Tier 3, which FABRICATED a Human token
        # on a zero-token board (CR 701.34a: no token → populate does
        # nothing); the phantom token blocked and fed an Athreos misfire
        # (game_1526071401499328634). Deterministic populate action; same
        # event guard shape as proliferate (don't fire on a permanent's own
        # ETB scan when the populate belongs to a whenever/scheduled clause).
        def _gen_populate(ctrl, opp, ctx):
            if ctx.get('_event_type', 'etb') == 'etb':
                m = ctx.get('_match')
                oracle = ctx.get('_oracle', '') or ''
                head = oracle[:m.start()] if m else oracle
                if 'enters' not in head[-60:].lower():
                    return None
            return [{"action": "populate", "player": ctrl}]

        self._add_pattern(
            r"(?:^|[,.:—]\s*|then\s+)populate\b\.?\s*(?:\(|$)",
            EffectTemplate(
                name="Populate",
                description="Populate (copy a creature token you control; nothing if you control none)",
                action_generator=_gen_populate,
            )
        )

        # July 20 audit (CRITICAL — Athreos, God of Passage): "Whenever
        # another creature YOU OWN dies, return it to your hand unless target
        # opponent pays 3 life." Tier 3 resolved this with NO ownership gate
        # — it fired on the OPPONENT's dying creatures all game, and once
        # even drained Athreos's own controller 3 life
        # (game_1526071401499328634, game-deciding). Ownership comes from
        # build_game_context's dying_owned_by_controller; None (unknown)
        # does not fire. Opponent-pays heuristic mirrors the Rhystic/extort
        # style: pay 3 while life is comfortable, otherwise let it return.
        def names_match_local(a, b):
            return (a or "").strip().lower() == b.strip().lower()

        def _gen_athreos_dies(ctrl, opp, ctx):
            dying_name = ctx.get('dying_name', '')
            opp_player = ctx.get('_opponent_player')
            if names_match_local(dying_name, "Athreos, God of Passage"):
                return [{"action": "no_action",
                         "reason": "Athreos: 'another creature' — his own death doesn't trigger"}]
            owned = ctx.get('dying_owned_by_controller')
            if owned is not True:
                return [{"action": "no_action",
                         "reason": f"Athreos: {dying_name or 'the creature'} isn't owned by Athreos's controller — no trigger"}]
            if opp_player is not None and opp_player.life >= 8:
                return [{"action": "lose_life", "player": opp,
                         "amount": 3, "source": "Athreos, God of Passage"}]
            return [{"action": "move_card", "card": dying_name,
                     "from_zone": "graveyard", "to_zone": "hand",
                     "player": ctrl}]

        self._add_dies_card("athreos, god of passage", EffectTemplate(
            name="Athreos, God of Passage",
            description="Whenever another creature you own dies, return it to your hand unless target opponent pays 3 life",
            action_generator=_gen_athreos_dies,
        ))

        # July 20 audit (CRITICAL — Faith Unbroken): the generic exile
        # pattern gave it NO return tracking, so the aura's forced-detach
        # (SBA) re-ran the ETB exile on a SECOND unrelated creature instead
        # of returning the first — one cast permanently ate two of the
        # opponent's creatures (game_1526071467035459665). Register through
        # the banisher (Oblivion Ring) shape: exile + track_exiled_by; the
        # LTB return is handled by _check_ltb_triggers_sync.
        def _gen_faith_unbroken(ctrl, opp, ctx):
            target = (ctx.get('explicit_target_name')
                      or ctx.get('best_opponent_creature'))
            if not target:
                return [{"action": "no_action",
                         "reason": "Faith Unbroken: no opponent creature to exile"}]
            return [
                {"action": "move_card", "card": target,
                 "from_zone": "battlefield", "to_zone": "exile", "player": opp},
                {"action": "track_exiled_by", "source": "Faith Unbroken",
                 "card": target, "owner": opp},
            ]

        self._add_card("faith unbroken", EffectTemplate(
            name="Faith Unbroken",
            description="Exile target creature an opponent controls until Faith Unbroken leaves the battlefield; enchanted creature gets +2/+2",
            action_generator=_gen_faith_unbroken,
            needs_target=True,
        ))

        # July 20 audit (CRITICAL — Worldslayer): the card's entire defining
        # ability had no handler — three combat hits while equipped, zero
        # wipes (game_1526071467035459665). The combat-damage-to-player
        # dispatcher (mtg/combat.py) now scans the attacker's EQUIPMENT and
        # resolves attack-templates keyed on the equipment's name.
        self._add_attack_card("worldslayer", EffectTemplate(
            name="Worldslayer",
            description="Whenever equipped creature deals combat damage to a player, destroy all permanents other than Worldslayer",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy_all_permanents",
                 "except_card": "Worldslayer"}],
        ))
        
        # "When [this] enters, scry N"
        self._add_pattern(
            r"when .+? enters.*?scry (\d+)",
            EffectTemplate(
                name="ETB Scry",
                description="Scry on ETB",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "scry", "player": ctrl, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )

        # "When [this] enters, surveil N" — Watcher in the Mist, Doom Whisperer
        # ETB, Notion Rain ETB, etc. (May 13 audit: was missing, ~3 cards
        # fell through to ETB-UNHANDLED → manual `!resolve` prompts.)
        self._add_pattern(
            r"when .+? enters.*?surveil (\d+)",
            EffectTemplate(
                name="ETB Surveil",
                description="Surveil on ETB",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "surveil", "player": ctrl, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )

        # Cast-trigger surveil (Dragon's Rage Channeler family). The ETB-only
        # pattern above cannot match this trigger paragraph, so it previously
        # escalated to Tier 3 and was deliberately shortcut to a no-op.
        self._add_pattern(
            r"whenever you cast a noncreature spell,?\s*surveil (\d+)",
            EffectTemplate(
                name="Cast-trigger Surveil",
                description="Surveil after casting a noncreature spell",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "surveil", "player": ctrl,
                     "amount": int(ctx['_match'].group(1))}
                ],
            )
        )

        # Hallowed Haunting's token uses a characteristic-defining ability;
        # preserve */* and the oracle text so effective P/T updates with the
        # controller's live Spirit count.
        self._add_pattern(
            r"whenever you cast an enchantment spell,?\s*create a white spirit cleric creature token",
            EffectTemplate(
                name="Hallowed Haunting cast trigger",
                description="Create a dynamic */* white Spirit Cleric token",
                action_generator=lambda ctrl, opp, ctx: [{
                    "action": "create_token", "player": ctrl,
                    "name": "Spirit Cleric", "power": "*", "toughness": "*",
                    "types": "Creature — Spirit Cleric", "colors": "W", "count": 1,
                    "oracle_text": "This token's power and toughness are each equal to the number of Spirits you control.",
                    "source": "Hallowed Haunting",
                }],
            )
        )

        # Saga chapter text is passed independently to the template library.
        # Keep this scoped to the whole chapter clause so later Fall of the
        # Thran chapters cannot re-run the wipe.
        self._add_pattern(
            r"^destroy all lands\.?$",
            EffectTemplate(
                name="Destroy all lands",
                description="Destroy all lands",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "destroy_all_by_type", "type": "lands"}
                ],
            )
        )

        # "Whenever <this> or another <type> permanent you control enters,
        # scry N" — Marit Lage's Slumber, similar saga/snow scry payoffs.
        # The trigger fires per snow-permanent ETB; we approximate as a
        # single scry-N when the slumber itself enters and rely on the
        # engine's per-ETB sweep to fire it on subsequent triggers.
        self._add_pattern(
            r"whenever .+? or another .+? you control enters.*?scry (\d+)",
            EffectTemplate(
                name="ETB Scry (Permanent Family Trigger)",
                description="Scry on permanent-enter trigger",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "scry", "player": ctrl, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )

        # "When [this] enters, you gain [N] life"
        self._add_pattern(
            r"when .+? enters.*?you gain (\d+) life",
            EffectTemplate(
                name="ETB Gain Life",
                description="Gain life on ETB",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "gain_life", "player": ctrl, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )
        
        # "When [this] enters, each opponent loses [N] life"
        self._add_pattern(
            r"when .+? enters.*?each opponent loses (\d+) life",
            EffectTemplate(
                name="ETB Drain Opponents",
                description="Drain opponents on ETB",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "lose_life", "player": opp, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )
        
        # "When [this] enters, [each opponent loses N / you gain N]" (drain)
        self._add_pattern(
            r"when .+? enters.*?each opponent loses (\d+) life.*?you gain .*?life",
            EffectTemplate(
                name="ETB Drain + Gain",
                description="Drain opponents and gain life",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "lose_life", "player": opp, "amount": int(ctx['_match'].group(1))},
                    {"action": "gain_life", "player": ctrl, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )
        
        # "When [this] enters, create [N] [P]/[T] [type] tokens"
        self._add_pattern(
            r"when .+? enters.*?create (\w+) (\d+)/(\d+) (\w[\w\s]*?) (?:creature |artifact )?tokens?",
            EffectTemplate(
                name="ETB Create Tokens",
                description="Create tokens on ETB",
                action_generator=self._gen_tokens_from_match,
            )
        )
        
        # "When [this] enters, deal [N] damage to any target / target creature"
        self._add_pattern(
            r"when .+? enters.*?deals? (\d+) damage to (any target|target [\w\s]+)",
            EffectTemplate(
                name="ETB Deal Damage",
                description="Deal damage on ETB",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "deal_damage", "amount": int(ctx['_match'].group(1)), "target_player": opp}
                ]
            )
        )
        
        # "When [this] enters, destroy target [artifact/enchantment/creature/permanent]"
        self._add_pattern(
            r"when .+? enters.*?destroy target ([\w\s]+?)(?:\.|,|$)",
            EffectTemplate(
                name="ETB Destroy Target",
                description="Destroy target on ETB",
                action_generator=self._gen_destroy_from_match,
                needs_target=True,
            )
        )
        
        # "When [this] enters, exile target [thing]"
        self._add_pattern(
            r"when .+? enters.*?exile target ([\w\s]+?)(?:\.|,|$)",
            EffectTemplate(
                name="ETB Exile Target",
                description="Exile target on ETB",
                action_generator=self._gen_exile_from_match,
                needs_target=True,
            )
        )
        
        # "When [this] enters, return target [thing] to [its owner's hand]"
        self._add_pattern(
            r"when .+? enters.*?return target ([\w\s]+?) to (?:its|their) owner",
            EffectTemplate(
                name="ETB Bounce",
                description="Bounce target on ETB",
                action_generator=self._gen_bounce_from_match,
                needs_target=True,
            )
        )
        
        # "When [this] enters, target player/opponent discards [N] cards?"
        self._add_pattern(
            r"when .+? enters.*?(?:target (?:player|opponent)|each opponent) discards? (\w+) cards?",
            EffectTemplate(
                name="ETB Discard",
                description="Force discard on ETB",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "discard", "player": opp, "card": "random"}
                    for _ in range(self._word_to_num(ctx['_match'].group(1)))
                ]
            )
        )
        
        # "When [this] enters, put [N] +1/+1 counters on [target creature / it / ~]"
        self._add_pattern(
            r"when .+? enters.*?put (\w+) \+1/\+1 counters? on",
            EffectTemplate(
                name="ETB +1/+1 Counters",
                description="Add +1/+1 counters on ETB",
                action_generator=self._gen_counters_from_match,
            )
        )
        
        # "When [this] enters, search your library for a [thing] card, put into your hand"
        # Catches restricted tutors: Recruiter, Stoneforge, Trinket Mage, etc.
        # Must come BEFORE the generic search pattern to match first.
        self._add_pattern(
            r"when .+? enters.*?search your library for .+? card.*?put .+? into your hand",
            EffectTemplate(
                name="ETB Search to Hand",
                description="Search library for a card and put into hand",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "search_library", "player": ctrl, "to_zone": "hand"}
                ]
            )
        )

        # "When [this] enters, search your library for a [basic land / thing]"
        self._add_pattern(
            r"when .+? enters.*?search your library for (?:a|an|up to \w+) ([\w\s]+?)(?:,|\.|and)",
            EffectTemplate(
                name="ETB Search Library",
                description="Search library on ETB",
                action_generator=lambda ctrl, opp, ctx: [
                    # Best-effort filter: pull the first noun-ish word from the
                    # captured phrase as the type filter (e.g. "creature card" →
                    # "creature", "basic land" → "basic land"). To-zone defaults
                    # to hand; specific patterns (Gravebreaker Lamia goes to
                    # battlefield, Final Parting splits hand/graveyard) need
                    # named-card templates.
                    {"action": "search_library", "player": ctrl,
                     "filter_type": ctx['_match'].group(1).strip().lower().split(' card')[0],
                     "to_zone": "hand", "count": 1, "shuffle": True},
                ]
            )
        )
        
        # "Whenever another creature [you control] enters, draw a card"
        # July 29 batch audit: the `.*?` between "creature" and "enters"
        # silently swallowed Mentor of the Meek's "with power 2 or less"
        # restriction — a 5-power Heliod drew a card. The generator now
        # honors power bounds embedded in the trigger condition. The
        # optional "you may pay {1}" is still not charged (no payment
        # vocabulary at this tier) — noted, the power gate is the
        # material fix.
        def _gen_creature_enters_draw(ctrl, opp, ctx):
            oracle = ctx.get('_oracle') or ''
            m = re.search(r'with power (\d+) or (less|greater)', oracle)
            if m:
                bound = int(m.group(1))
                ep = ctx.get('entering_power')
                if ep is None:
                    return []  # power unknown — decline rather than misfire
                if m.group(2) == 'less' and int(ep) > bound:
                    return []  # handled no-op: condition not met
                if m.group(2) == 'greater' and int(ep) < bound:
                    return []
            return [{"action": "draw_cards", "player": ctrl, "amount": 1}]
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?enters.*?draw a card",
            EffectTemplate(
                name="Creature-enters Draw",
                description="Draw a card when a creature enters",
                action_generator=_gen_creature_enters_draw,
            )
        )
        
        # "Whenever another creature enters, you gain [N] life"
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?enters.*?you gain (\d+) life",
            EffectTemplate(
                name="Creature-enters Life Gain",
                description="Gain life when a creature enters",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "gain_life", "player": ctrl, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )
        
        # BOLSTER (CR 701.28) — registered BEFORE the generic counter pattern
        # below, which it would otherwise lose to (first match wins).
        #
        # Aug 3, 2026. Bolster N is "choose a creature with the LEAST TOUGHNESS
        # among creatures you control and put N +1/+1 counters on it", and that
        # sentence is printed only as REMINDER text. The generic pattern below
        # matched the reminder's "put a +1/+1 counter on it" and read "it" as
        # the SOURCE, so Anafenza, Kin-Tree Spirit grew herself every time
        # instead of the smallest creature — the wrong creature, every trigger.
        # (She also never fired at all until the same day's fix to the
        # creature-enters DETECTION gate, which enumerated phrasings and had no
        # entry for "whenever another NONTOKEN creature you control enters".)
        def _gen_bolster(ctrl, opp, ctx):
            n = int(ctx['_match'].group(1))
            creatures = ctx.get('_controller_creatures') or []
            if not creatures:
                return [{"action": "no_action",
                         "reason": "bolster: you control no creatures"}]
            # Least toughness; name breaks ties so the choice is deterministic.
            chosen = min(creatures,
                         key=lambda c: (c.get('toughness', 0) or 0,
                                        c.get('name', '')))
            return [{"action": "add_counters", "card": chosen['name'],
                     "counter_type": "+1/+1", "amount": n}]

        self._add_pattern(
            r"whenever (?:a|another)\b[^.]*?\bcreature\b[^.]*?\benters\b"
            r"[^.]*?\bbolster (\d+)",
            EffectTemplate(
                name="Bolster on creature-enters",
                description="Bolster N — counters on the least-toughness creature",
                action_generator=_gen_bolster,
            )
        )

        # "Whenever another creature enters, put a +1/+1 counter on it/itself"
        # The "on it" form means the counter goes on the source permanent (e.g.,
        # Forgotten Ancient-style upkeep growers).
        # NEGATIVE LOOKAHEAD: skip if the text says "on each creature" —
        # that's the Cathars'-Crusade pattern (distribute to ALL controller
        # creatures) and has its own card-specific template above. Without
        # this lookahead, Cathars' Crusade fell through here and counters
        # accumulated on the enchantment itself.
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?enters(?!.*each creature).*?put (?:a|\d+) \+1/\+1 counter",
            EffectTemplate(
                name="Creature-enters Counter",
                description="Add +1/+1 counter when a creature enters",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "add_counters", "card": ctx.get('_trigger_source', 'self'),
                     "counter_type": "+1/+1", "amount": 1}
                ]
            )
        )

        # BACKUP (CR 702.165) needs NO pattern of its own: its reminder text
        # is "When this creature enters, put a +1/+1 counter on target
        # creature", which the "ETB +1/+1 Counters" pattern above already
        # matches, placing the counter on the source — a legal choice, since
        # the source is itself a legal target. A dedicated pattern registered
        # after that one could never win (first match wins) and would be dead
        # code. The unmodeled half is the rider that grants the listed
        # abilities when the target is ANOTHER creature.
        #
        # Aug 3, 2026: backup was on the missing-mechanics backlog as
        # "unassessed" only because the earlier probe used the creature-enters
        # WATCHER dispatch, which a self-ETB never reaches.

        # Generic tutor — "search your library for a [type] card, put it into
        # your hand/onto the battlefield". May 17 audit: tutor effects were
        # the single biggest Tier 3 escalation category (~37 of ~110/batch).
        # This pattern catches the bulk; card-specific templates win first.
        # May 25 audit (F9): library-manipulation tutors (Mystical Tutor,
        # Vampiric Tutor, Worldly Tutor, Imperial Seal) put the card on top
        # of library instead of into hand. The May 17 regex didn't include
        # this variant, escalating ~11 of 15 May 25 AUTOPLAY-JUDGE events.
        # Extended to capture "on top of (your|its owner's) library".
        def _generic_tutor_gen(ctrl, opp, ctx):
            m = ctx.get('_match')
            if m is None:
                return [{"action": "no_action", "reason": "Tutor pattern: no match"}]
            type_word = (m.group(1) or "").strip().lower()
            # The zone is whichever of groups 2-4 matched (regex alternation).
            zone_word = ""
            for grp_idx in (2, 3, 4):
                try:
                    g = m.group(grp_idx)
                    if g:
                        zone_word = g.strip().lower()
                        break
                except (IndexError, re.error):
                    continue
            if not zone_word:
                zone_word = "hand"
            # Normalize zone words to action's `to_zone` field.
            if "battlefield" in zone_word:
                to_zone = "battlefield"
            elif "graveyard" in zone_word:
                to_zone = "graveyard"
            elif "top" in zone_word and "library" in zone_word:
                # Mystical Tutor / Vampiric Tutor / Worldly Tutor / Imperial
                # Seal-shape: the search action handler supports `library_top`
                # natively (inserts at index 0 so the card is the next draw).
                to_zone = "library_top"
            else:
                to_zone = "hand"
            # Type word "card" with no qualifier means any card — pass empty
            # string so the action handler matches everything.
            if type_word == "card":
                type_filter = ""
            else:
                # Strip trailing " card"
                type_filter = type_word.replace(" card", "").strip().title()
            return [{
                "action": "search_library",
                "player": ctrl,
                "card_type": type_filter,
                "to_zone": to_zone,
                "count": 1,
            }]

        self._add_pattern(
            (
                r"search your library for (?:a|an)\s+([\w\- ]+? card)[^.]*?"
                # Branch alternations: (2) into hand / onto battlefield / into graveyard,
                # (3) on top of your|its owner's library,
                # (4) reveal-it variant → into your hand.
                r"(?:put it (into your hand|onto the battlefield|into your graveyard)"
                r"|put it (on top of (?:your|its owner's) library)"
                r"|reveal it[^.]*?put it (into your hand))"
            ),
            EffectTemplate(
                name="Generic Tutor",
                description="Search library for a card and put it into the specified zone",
                action_generator=_generic_tutor_gen,
            )
        )

        # "Counter target spell" — generic, counters anything on stack
        self._add_pattern(
            r"counter target spell(?!\s)",
            EffectTemplate(
                name="Counter Target Spell",
                description="Counter a target spell on the stack",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "counter_spell", "player": ctrl, "target": "stack_top"}
                ]
            )
        )

        # "Counter target noncreature spell" — fizzles if target is a creature
        # Bug fix: this pattern was wrongly countering creature spells during cascade
        self._add_pattern(
            r"counter target noncreature spell",
            EffectTemplate(
                name="Counter Target Noncreature Spell",
                description="Counter a target noncreature spell on the stack",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action", "reason": "Fizzle — target is a creature spell"}
                ] if ctx.get('stack_top_is_creature', False) else [
                    {"action": "counter_spell", "player": ctrl, "target": "stack_top"}
                ]
            )
        )

        # "Counter target creature spell" / "counter target instant or sorcery" / etc.
        self._add_pattern(
            r"counter target (?:creature spell|instant or sorcery spell|artifact spell|enchantment spell)",
            EffectTemplate(
                name="Counter Target Typed Spell",
                description="Counter a target spell of a specific type on the stack",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "counter_spell", "player": ctrl, "target": "stack_top"}
                ]
            )
        )

        # =================================================================
        # STIFLE / COUNTER ABILITY PATTERNS
        # =================================================================

        # "Counter target triggered ability" / "counter target activated or triggered ability"
        self._add_pattern(
            r"counter target (?:triggered|activated).*?ability",
            EffectTemplate(
                name="Counter Target Ability",
                description="Counter a triggered or activated ability on the stack",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "counter_ability", "player": ctrl, "target": "stack_top_ability"}
                ]
            )
        )

        # =================================================================
        # DIES TRIGGER PATTERNS
        # =================================================================

        # "Whenever a/another creature [you control] dies, each opponent loses N life"
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?dies.*?each opponent loses (\d+) life",
            EffectTemplate(
                name="Dies Drain Opponents",
                description="Each opponent loses life when a creature dies",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "lose_life", "player": opp, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )

        # "Whenever a/another creature dies, each opponent loses N life and you gain N life"
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?dies.*?each opponent loses (\d+) life.*?you gain .*?life",
            EffectTemplate(
                name="Dies Drain + Gain",
                description="Drain opponents and gain life when a creature dies",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "lose_life", "player": opp, "amount": int(ctx['_match'].group(1))},
                    {"action": "gain_life", "player": ctrl, "amount": int(ctx['_match'].group(1))},
                ]
            )
        )

        # July 23 audit (#12/#13): combined "draw + lose life" / "gain life +
        # draw" dies triggers. These MUST precede the single-clause patterns
        # below (first registered wins), or Dark Prophecy ("you draw a card and
        # you lose 1 life") matches Dies-Draw and silently drops the life loss,
        # and Moldervine Reclamation ("you gain 1 life and draw a card") matches
        # Dies-Gain-Life and silently drops the draw
        # (game_1529677634588377108: ~12 Dark Prophecy + 3 Moldervine triggers,
        # each half-resolved).
        # "Whenever a creature you control dies, you draw a card and you lose N life" (Dark Prophecy)
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?dies.*?draw a card and you lose (\d+) life",
            EffectTemplate(
                name="Dies Draw + Lose Life",
                description="Draw a card and lose life when a creature dies",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl, "amount": 1},
                    {"action": "lose_life", "player": ctrl, "amount": int(ctx['_match'].group(1))},
                ]
            )
        )

        # "Whenever a creature you control dies, you gain N life and draw a card" (Moldervine Reclamation)
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?dies.*?gain (\d+) life and draw a card",
            EffectTemplate(
                name="Dies Gain Life + Draw",
                description="Gain life and draw a card when a creature dies",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "gain_life", "player": ctrl, "amount": int(ctx['_match'].group(1))},
                    {"action": "draw_cards", "player": ctrl, "amount": 1},
                ]
            )
        )

        # "Whenever a/another creature dies, you gain N life"
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?dies.*?you gain (\d+) life",
            EffectTemplate(
                name="Dies Gain Life",
                description="Gain life when a creature dies",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "gain_life", "player": ctrl, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )

        # "Whenever a/another creature dies, draw a card"
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?dies.*?draw a card",
            EffectTemplate(
                name="Dies Draw",
                description="Draw a card when a creature dies",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl, "amount": 1}
                ]
            )
        )

        # "When [this] dies, create [N] [P]/[T] tokens"
        self._add_pattern(
            r"when .+? dies.*?create (\w+) (\d+)/(\d+) (\w[\w\s]*?) (?:creature |artifact )?tokens?",
            EffectTemplate(
                name="Dies Create Tokens",
                description="Create tokens when this creature dies",
                action_generator=self._gen_tokens_from_match,
            )
        )

        # "When [this] dies, draw [N] cards?"
        self._add_pattern(
            r"when .+? dies.*?draw (\w+) cards?",
            EffectTemplate(
                name="Dies Draw Cards",
                description="Draw cards when this dies",
                action_generator=self._gen_draw_from_match,
            )
        )

        # Undying reminder text: "When this creature dies, if it had no +1/+1 counters on it,
        # return it to the battlefield with a +1/+1 counter on it."
        # The SBA engine handles the actual undying mechanic; this pattern marks the trigger text
        # as handled so it doesn't generate spurious !resolve prompts on the second death
        # (when undying can't trigger because the creature already has a +1/+1 counter).
        self._add_pattern(
            r"when this creature dies.*if it had no \+1/\+1 counters",
            EffectTemplate(
                name="Undying (reminder text)",
                description="Undying is handled by the SBA engine",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action", "reason": "Undying: handled by SBA engine — creature already resolved"}
                ],
            )
        )

        # "Whenever a creature you control dies, each other player sacrifices a creature"
        self._add_pattern(
            r"whenever a creature you control dies.*?each (?:other player|opponent) sacrifices a creature",
            EffectTemplate(
                name="Dies Force Sacrifice",
                description="Each opponent sacrifices a creature when your creature dies",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action", "reason": f"{opp} must sacrifice a creature (use !fix if needed)"}
                ]
            )
        )

        # =================================================================
        # EDICT PATTERNS — "each player sacrifices a creature"
        # =================================================================

        # "each player sacrifices a creature" — generic edict effect
        self._add_pattern(
            r"each player sacrifices a creature",
            EffectTemplate(
                name="Edict Sacrifice",
                description="Each player sacrifices a creature",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "edict_sacrifice", "types": "creature"}
                ]
            )
        )

        # "each opponent sacrifices a creature" — one-sided edict
        self._add_pattern(
            r"each opponent sacrifices a creature",
            EffectTemplate(
                name="Edict Opponents Only",
                description="Each opponent sacrifices a creature",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "edict_sacrifice", "types": "creature", "opponents_only": True}
                ]
            )
        )

        # =================================================================
        # ATTACK TRIGGER PATTERNS
        # =================================================================

        # "Whenever [this] attacks, deals? [N] damage"
        self._add_pattern(
            r"whenever .+? attacks.*?deals? (\d+) damage",
            EffectTemplate(
                name="Attack Deal Damage",
                description="Deal damage when attacking",
                # June 10 audit (V31f): thread the source so the damage line
                # isn't "(unknown source)" — 15 such lines in the June batch,
                # 9 of them Drakuseth (who also has a dedicated template now;
                # this generic pattern only captures the FIRST number).
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "deal_damage", "amount": int(ctx['_match'].group(1)),
                     "target_player": opp,
                     "source": ctx.get('_source_card_name', 'attack trigger')}
                ]
            )
        )

        # "Whenever [this] attacks, draw a card"
        self._add_pattern(
            r"whenever .+? attacks.*?draw a card",
            EffectTemplate(
                name="Attack Draw",
                description="Draw a card when attacking",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl, "amount": 1}
                ]
            )
        )

        # "Whenever [this] attacks, create [N] [P]/[T] tokens"
        self._add_pattern(
            r"whenever .+? attacks.*?create (\w+) (\d+)/(\d+) (\w[\w\s]*?) (?:creature |artifact )?tokens?",
            EffectTemplate(
                name="Attack Create Tokens",
                description="Create tokens when attacking",
                action_generator=self._gen_tokens_from_match,
            )
        )

        # =================================================================
        # LANDFALL TRIGGER PATTERNS
        # =================================================================

        # "Whenever a land enters the battlefield under your control, create [N] [P]/[T] [type] token"
        # Catches Scute Swarm, Zendikar's Roil, Omnath variants, Field of the Dead, etc.
        self._add_pattern(
            r"whenever a land enters the battlefield under your control.*?create (\w+) (\d+)/(\d+) (\w[\w\s]*?) (?:creature |artifact )?tokens?",
            EffectTemplate(
                name="Landfall Create Tokens",
                description="Landfall: create tokens when a land enters",
                action_generator=self._gen_tokens_from_match,
            )
        )

        # =================================================================
        # UPKEEP TRIGGER PATTERNS
        # =================================================================

        # "At the beginning of your upkeep, draw a card"
        self._add_pattern(
            r"at the beginning of your upkeep.*?draw a card",
            EffectTemplate(
                name="Upkeep Draw",
                description="Draw a card at the beginning of your upkeep",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl, "amount": 1}
                ]
            )
        )

        # "At the beginning of your upkeep, lose [N] life"
        self._add_pattern(
            r"at the beginning of your upkeep.*?lose (\d+) life",
            EffectTemplate(
                name="Upkeep Lose Life",
                description="Lose life at the beginning of your upkeep",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "lose_life", "player": ctrl, "amount": int(ctx['_match'].group(1))}
                ]
            )
        )

        # "At the beginning of your upkeep, create [a/N] [type] token"
        self._add_pattern(
            r"at the beginning of your upkeep.*?create (?:a|(\w+)) .*?tokens?",
            EffectTemplate(
                name="Upkeep Create Token",
                description="Create a token at the beginning of your upkeep",
                action_generator=self._gen_upkeep_token_from_match,
            )
        )

        # Werewolf day/night transform pattern — "at the beginning of each upkeep … transform"
        # We track spells_cast_prev_turn per player. Transforms fire only when the
        # condition is met; otherwise silent no-op (no Discord noise — the previous
        # implementation spammed 78+ "use !fix transform" lines per game).
        self._add_pattern(
            r"at the beginning of each upkeep.*transform",
            EffectTemplate(
                name="Werewolf Transform Check",
                description="Day/night upkeep transform check (uses spells_cast_prev_turn)",
                action_generator=lambda ctrl, opp, ctx: [],
            )
        )

        # =================================================================
        # END STEP TRIGGER PATTERNS
        # =================================================================

        # "At the beginning of [the/your/the next] end step, sacrifice [this/it]"
        self._add_pattern(
            r"at the beginning of .*?end step.*?sacrifice",
            EffectTemplate(
                name="End Step Sacrifice",
                description="Sacrifice at end of turn",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action", "reason": f"End step: sacrifice {ctx.get('_source_card_name', 'this permanent')} (use !fix if needed)"}
                ]
            )
        )

        # =====================================================================
        # Spell-Specific Patterns (no ETB prefix — for instants/sorceries)
        # =====================================================================

        # "Exile target [nonland permanent / creature / artifact / etc.]"
        self._add_pattern(
            r"^exile target ([\w\s]+?)(?:\.|,|$)",
            EffectTemplate(
                name="Spell Exile Target",
                description="Exile target permanent (spell)",
                action_generator=self._gen_spell_exile_target,
                needs_target=True,
            )
        )

        # "Destroy target [nonland permanent / creature / artifact / etc.]"
        self._add_pattern(
            r"^destroy target ([\w\s]+?)(?:\.|,|\s+it)",
            EffectTemplate(
                name="Spell Destroy Target",
                description="Destroy target permanent (spell)",
                action_generator=self._gen_spell_destroy_target,
                needs_target=True,
            )
        )

        # Board wipe patterns: "destroy all creatures" / "all creatures get -X/-X"
        self._add_pattern(
            r"^destroy all creatures",
            EffectTemplate(
                name="Board Wipe Destroy",
                description="Destroy all creatures",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "destroy_all_creatures"},
                ]
            )
        )
        self._add_pattern(
            r"put all creatures on the bottom of their owners.? librar",
            EffectTemplate(
                name="Board Wipe Tuck",
                description="Put all creatures on bottom of owners' libraries",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "tuck_all_creatures"},
                ]
            )
        )

        # "mill N cards" / "put the top N cards of your library into your graveyard"
        self._add_pattern(
            r"(?:mill|puts? the top) (\w+) cards?",
            EffectTemplate(
                name="Mill",
                description="Mill cards from library to graveyard",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "mill", "player": ctrl, "amount": word_to_num(ctx['_match'].group(1))}
                ]
            )
        )

        # "exile target creature/permanent...return it/that...to the battlefield"
        self._add_pattern(
            r"exile target (?:creature|permanent).*?return (?:it|that)",
            EffectTemplate(
                name="Flicker (pattern)",
                description="Exile and return a target creature/permanent",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "flicker", "player": ctrl, "target": ctx.get('best_own_etb_creature', '')},
                ],
            )
        )

        # ====================================================================
        # Tier 3 Gap Closure: Mutate, Magecraft, Amass, Ninjutsu (Batch 9)
        # ====================================================================

        # Mutate: "when this creature mutates, destroy target [type]"
        self._add_pattern(
            r"(?:when(?:ever)? this creature mutates|whenever .*? mutates).*?destroy target ([\w\s]+)",
            EffectTemplate(
                name="Mutate Destroy",
                description="When this creature mutates, destroy target permanent",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "destroy", "card": ctx.get('best_opponent_creature', 'target')}
                ]
            )
        )

        # Mutate: "when this creature mutates, draw N cards"
        self._add_pattern(
            r"(?:when(?:ever)? this creature mutates|whenever .*? mutates).*?draw (?:a|(\w+)) cards?",
            EffectTemplate(
                name="Mutate Draw",
                description="When this creature mutates, draw cards",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl,
                     "amount": word_to_num(ctx['_match'].group(1)) if ctx['_match'].group(1) else 1}
                ]
            )
        )

        # Mutate: generic "when this creature mutates" + deal damage
        self._add_pattern(
            r"(?:when(?:ever)? this creature mutates|whenever .*? mutates).*?deals? (\d+) damage",
            EffectTemplate(
                name="Mutate Damage",
                description="When this creature mutates, deal damage",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "deal_damage", "amount": int(ctx['_match'].group(1)),
                     "target_player": opp}
                ]
            )
        )

        # Magecraft: "whenever you cast or copy an instant or sorcery spell, draw"
        self._add_pattern(
            r"whenever you cast or copy an instant or sorcery spell.*?draw (?:a|(\w+)) cards?",
            EffectTemplate(
                name="Magecraft Draw",
                description="Magecraft: draw card(s) when casting instant/sorcery",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl,
                     "amount": word_to_num(ctx['_match'].group(1)) if ctx['_match'].group(1) else 1}
                ]
            )
        )

        # Magecraft: "whenever you cast or copy an instant or sorcery spell, [creature] gets +1/+1"
        self._add_pattern(
            r"whenever you cast or copy an instant or sorcery spell.*?gets? \+(\d+)/\+(\d+)",
            EffectTemplate(
                name="Magecraft Pump",
                description="Magecraft: pump creature when casting instant/sorcery",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action",
                     "reason": f"Magecraft pump: +{ctx['_match'].group(1)}/+{ctx['_match'].group(2)} until end of turn (tracked via temp modifiers)"}
                ]
            )
        )

        # Amass: grow an Army you already control; create one only when none
        # exists (CR 701.44). The old template created a second Army on every
        # resolution and then put all counters on the first same-name token.
        def _gen_amass_amount(ctrl, ctx, amount):
            battlefield = ctx.get('controller_battlefield', []) or []
            army = next((
                card for card in battlefield
                if card.is_creature(ctx.get('_game'))
                and 'army' in (card.type_line or '').lower().split()
            ), None)
            if army is not None:
                return [{"action": "add_counters", "card": army.name,
                         "counter_type": "+1/+1", "amount": amount}]
            oracle = (ctx.get('_oracle') or '').lower()
            tribe = "Orc" if "amass orcs" in oracle else "Zombie"
            token_name = f"{tribe} Army"
            return [
                {"action": "create_token", "player": ctrl, "name": token_name,
                 "power": 0, "toughness": 0,
                 "types": f"Creature - {tribe} Army", "count": 1},
                {"action": "add_counters", "card": token_name,
                 "counter_type": "+1/+1", "amount": amount},
            ]

        def _gen_amass(ctrl, opp, ctx):
            return _gen_amass_amount(ctrl, ctx, int(ctx['_match'].group(1)))

        # Dreadhorde Invasion's upkeep paragraph contains both effects. The
        # generic lose-life regex otherwise wins first and silently drops Amass.
        self._add_upkeep_card("dreadhorde invasion", EffectTemplate(
            name="Dreadhorde Invasion",
            description="Lose 1 life, then amass Zombies 1",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "lose_life", "player": ctrl, "amount": 1},
                *_gen_amass_amount(ctrl, ctx, 1),
            ],
        ))

        self._add_pattern(
            r"amass (?:\w+ )?(\d+)",
            EffectTemplate(
                name="Amass",
                description="Amass: create or grow Army token with +1/+1 counters",
                action_generator=_gen_amass,
            )
        )

        # Ninjutsu: "ninjutsu {cost}"
        # Returns unblocked attacker to hand, puts ninja on battlefield attacking
        self._add_pattern(
            r"ninjutsu \{",
            EffectTemplate(
                name="Ninjutsu",
                description="Return unblocked attacker to hand, put this creature onto battlefield attacking",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "move_card", "card": "unblocked_attacker", "from_zone": "battlefield",
                     "to_zone": "hand", "player": ctrl},
                    {"action": "move_card", "card": ctx.get('card_name', 'Ninja'), "from_zone": "hand",
                     "to_zone": "battlefield", "player": ctrl}
                ]
            )
        )

        # ====================================================================
        # NEW ORACLE TEXT PATTERNS — Batch added for autoplay coverage
        # ====================================================================

        # Bounce-self ETB: "When X enters, return a permanent/creature you control to"
        # Catches Dream Stalker variants, Kor Skyfisher, Invasive Species, etc.
        # Returns self by default since it's the safest auto-resolution
        # June 11 audit: the old single-template dispatch let "return a
        # CREATURE you control" cards (Whitemane Lion) bounce a land, and the
        # unconditional self-exclusion was wrong for "a" (vs "another")
        # wordings — Whitemane Lion may legally return itself.
        def _etb_bounce_own(ctrl, opp, ctx):
            m = ctx.get('_match')
            article = (m.group(1) if m else 'a').lower()
            perm_word = (m.group(2) if m else 'permanent').lower()
            action = {"action": "bounce_own_permanent", "player": ctrl}
            if article == 'another':
                action["exclude"] = ctx.get('_source_card_name', '')
            if perm_word == 'creature':
                action["type_filter"] = "creature"
            return [action]
        self._add_pattern(
            r"when .+? enters.*?return (a|another) (permanent|creature) you control to",
            EffectTemplate(
                name="ETB Bounce Own Permanent",
                description="Return a permanent you control to its owner's hand",
                action_generator=_etb_bounce_own,
            )
        )

        # Scry pattern: "scry N" — covers any card with scry
        # Bug fix: was returning no_action, causing 26 Tier 3 escalations + judge double-calls.
        # Now simulates scry for autoplay: 50% chance bottom the top card, 50% keep.
        # Since there's no "scry" action type, we either move top card to bottom or keep it.
        # May 7 audit fix #4: stash the parsed N on the context so the description
        # template can fill it in (was leaking literal "Scry N" to Discord).
        def _scry_action(ctrl, opp, ctx):
            # Refuse the whole card when scry is only its first clause. Read the
            # Bones is "Scry 2, then draw two cards. You lose 2 life." — this
            # pattern matched the scry, resolved the spell, and the draw and the
            # life loss were never seen by any tier. Returning None keeps the
            # cascade going instead of claiming a card we only half-understand.
            # Temple lands ("enters tapped. When ~ enters, scry 1") and Viscera
            # Seer (the activated path passes just "Scry 1") have no residue and
            # still resolve here.
            if has_residual_clause_beyond_library_look(ctx.get('_oracle', '')):
                return None
            import random
            n = word_to_num(ctx['_match'].group(1))
            ctx['_scry_n'] = n  # consumed by description override below
            if n <= 0:
                return [{"action": "no_action", "reason": "Scry 0: nothing to do"}]
            # Autoplay heuristic: for each scry card, 50% chance to bottom it
            # This is a rough approximation — real scry depends on what's on top
            actions = []
            bottomed = 0
            for _ in range(n):
                if random.random() < 0.5:
                    actions.append({"action": "move_card", "card": "top_of_library",
                                    "from_zone": "library_top", "to_zone": "library_bottom",
                                    "player": ctrl})
                    bottomed += 1
            if not actions:
                # Kept everything on top — that's a valid scry choice
                return [{"action": "no_action",
                         "reason": f"Scry {n}: kept all cards on top"}]
            return actions

        # May 7 audit fix #4: description with literal N was leaking to Discord
        # ("activates Viscera Seer: Scry N (look at top cards, reorder/bottom)").
        # Use a description_fn that fills in the parsed N from ctx.
        _scry_template = EffectTemplate(
            name="Scry",
            description="Scry (look at top cards, reorder/bottom)",
            action_generator=_scry_action,
        )
        # Attach a callable that lib code can use; description string above is a fallback.
        try:
            _scry_template.description_fn = lambda ctx: (
                f"Scry {ctx.get('_scry_n', 1)} (look at top cards, reorder/bottom)"
            )
        except Exception:
            pass
        self._add_pattern(
            r"scry (\d+|one|two|three|four|five)",
            _scry_template,
        )

        # "When X enters, scry N" — ETB scry (Temple lands, etc.)
        # Separate from the generic scry pattern above because this matches
        # the ETB context specifically (e.g. "When Temple of Mystery enters, scry 1")
        self._add_pattern(
            r"when .+? enters.*?scry (\d+|one|two|three|four|five)",
            EffectTemplate(
                name="ETB Scry",
                description="Scry N on ETB (Temple lands and similar)",
                action_generator=_scry_action,
            )
        )

        # "Creatures your opponents control get -N/-N until end of turn"
        # Catches Massacre Wurm ETB, Elesh Norn (partially), Crippling Fear, etc.
        self._add_pattern(
            r"creatures (?:your opponents?|you don't) control get -(\d+)/-(\d+)",
            EffectTemplate(
                name="Opponents Creatures -N/-N",
                description="Opponents' creatures get -N/-N until end of turn",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "pump_all_creatures", "player": opp,
                     "power": -int(ctx['_match'].group(1)),
                     "toughness": -int(ctx['_match'].group(2)),
                     "filter": "opponents", "controller": ctrl}
                ]
            )
        )

        # Fight self-ETB: "When X enters, it fights target creature"
        # Covers Kogla variants, Territorial Allosaurus, Affectionate Indrik, etc.
        # Approximated as mutual damage since no "fight" action type exists.
        self._add_pattern(
            r"when .+? enters.*?(?:it )?fights? (?:up to )?(?:one )?(?:target )?creature",
            EffectTemplate(
                name="ETB Fight",
                description="Fight target creature when entering the battlefield",
                action_generator=lambda ctrl, opp, ctx: self._fight_from_pattern(ctrl, opp, ctx),
                needs_target=True,
            )
        )

        # --- Generic bounce patterns (ETB and spell) ---
        # "return target nonland permanent to its owner's hand"
        self._add_pattern(
            r"return target (?:nonland )?(?:creature|permanent) (?:an opponent controls )?to (?:its|their) owner'?s? hand",
            EffectTemplate(
                name="Bounce Spell/ETB",
                description="Return target permanent to hand",
                action_generator=self._gen_bounce_from_match,
                needs_target=True,
            )
        )

        # --- Clone / Copy token patterns ---
        # GRAVEYARD-sourced copies must be registered BEFORE the battlefield
        # pattern below. Feldon of the Third Path copies "target creature CARD
        # in your graveyard"; the battlefield pattern used to match that text
        # and copy a LIVE creature instead (July 27 fanout — observed with no
        # creature in any graveyard at all).
        self._add_pattern(
            r"create a token that(?:'s| is) a copy of (?:target )?creature card in (your|a|an opponent's) graveyard",
            EffectTemplate(
                name="Graveyard Copy Token",
                description="Create a token copy of a creature card in a graveyard",
                action_generator=self._gen_graveyard_copy_token,
                needs_target=True,
            )
        )

        # "create a token that's a copy of [target]" — covers Thousand-Faced Shadow,
        # Helm of the Host, Rite of Replication, Mimic Vat, etc.
        # The `(?!\s+card\b)` guard keeps this BATTLEFIELD pattern off cards in
        # other zones: MTG templating says "permanent" for a battlefield object
        # and "card" for one in a graveyard / exile / hand, so "creature card"
        # is never something this handler can see.
        self._add_pattern(
            r"create a token that(?:'s| is) a copy of (?:another )?(?:target )?(?:attacking )?(creature|permanent)(?!\s+card\b)",
            EffectTemplate(
                name="Copy Token",
                description="Create a token copy of a creature",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "create_copy_token", "player": ctrl,
                     "target": "best_creature",
                     "filter": "attacking" if "attacking" in (ctx.get('_oracle', '')) else "own",
                     "count": 1}
                ]
            )
        )


        # --- Clone creatures: enter as a copy of another creature ---
        # These modify themselves, NOT create tokens. Uses 'become_copy' action.
        self._add_card("clone", EffectTemplate(
            name="Clone",
            description="Enter as a copy of any creature on the battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "become_copy", "source": ctx.get('_source_card_name', 'Clone'),
                 "target": "best_creature", "filter": "any", "player": ctrl},
            ],
        ))
        # (spark double: single registration lives at the become_copy block
        # above — the shape the handler actually reads (`target` = a NAME,
        # `modifications` list). Aug 7 registry dedup removed the duplicate
        # here, which passed an unresolvable "best_creature" sentinel and an
        # `extra_counters` key nothing reads — the silent-no-op vocabulary
        # class. NOTE: Clone / Clever Impersonator below still carry that
        # sentinel shape at their only registrations; clones resolve via the
        # cast-path Tier-1 branch in practice, so those templates are
        # near-dead — flagged, not drive-by-changed.)
        self._add_card("clever impersonator", EffectTemplate(
            name="Clever Impersonator",
            description="Enter as a copy of any nonland permanent",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "become_copy", "source": ctx.get('_source_card_name', 'Clever Impersonator'),
                 "target": "best_nonland", "filter": "any", "player": ctrl},
            ],
        ))
        self._add_card("phyrexian metamorph", EffectTemplate(
            name="Phyrexian Metamorph",
            description="Enter as a copy of any artifact or creature on the battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "become_copy", "source": ctx.get('_source_card_name', 'Phyrexian Metamorph'),
                 "target": "best_creature", "filter": "any", "player": ctrl},
            ],
        ))
        self._add_card("sakashima of a thousand faces", EffectTemplate(
            name="Sakashima of a Thousand Faces",
            description="Enter as a copy of another creature you control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "become_copy", "source": ctx.get('_source_card_name', 'Sakashima'),
                 "target": "best_creature", "filter": "own", "player": ctrl},
            ],
        ))

        # Pattern: "enters the battlefield as a copy of" — catches generic clones
        self._add_pattern(
            r"enters (?:the battlefield )?as a copy of",
            EffectTemplate(
                name="Clone ETB Copy",
                description="Enter as a copy of a creature",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "become_copy", "source": ctx.get('_source_card_name', 'Clone'),
                     "target": "best_creature", "filter": "any", "player": ctrl},
                ],
            )
        )

        # --- Generic fetch/ramp pattern: "search your library for a [type] card,
        # put it onto the battlefield" ---
        # Catches any fetchland or ramp effect not already handled by card templates.
        # Determines enters-tapped from oracle text ("tapped" without "untapped").
        # Picks the first mentioned land type, or basic land as fallback.
        self._add_pattern(
            r"search your library for (?:a |an? )?(basic )?(?:land|plains|island|swamp|mountain|forest)\b.*?(?:put (?:it|that card) onto the battlefield)",
            EffectTemplate(
                name="Search Library for Land",
                description="Search library for a land, put onto battlefield",
                action_generator=self._gen_fetch_from_pattern,
            )
        )

        # Molten Duplication — Aug 7 batch audit (A-2): no template existed,
        # and the Tier-3 judge dropped the MANDATORY "Sacrifice it at the
        # beginning of the next end step" clause — the token copy survived
        # 2+ extra turns and attacked again (game_1535051230815064206). Same
        # shape as the Feldon graveyard-copy generator, battlefield zone.
        def _gen_molten_duplication(ctrl, opp, ctx):
            target = (ctx.get('explicit_target_name')
                      or ctx.get('controller_best_creature')
                      or ctx.get('best_own_creature'))
            if not target:
                # "artifact or creature you control" — fall back to any own
                # artifact when the board has no creatures.
                for perm in (ctx.get('controller_battlefield') or []):
                    tl = (perm.get('type_line') if isinstance(perm, dict) else getattr(perm, 'type_line', '')) or ''
                    nm = perm.get('name') if isinstance(perm, dict) else getattr(perm, 'name', None)
                    if nm and 'artifact' in tl.lower():
                        target = nm
                        break
            if not target:
                return [{"action": "no_action",
                         "reason": "no artifact or creature you control to copy"}]
            return [
                {"action": "create_copy_token", "player": ctrl,
                 "target": target, "count": 1,
                 "extra_types": ["Artifact"], "keywords": ["Haste"]},
                {"action": "schedule_delayed_trigger", "trigger_at": "end_step",
                 "turn_delay": 0, "source": "Molten Duplication",
                 "actions": [{
                     "action": "sacrifice_permanent", "player": ctrl,
                     "preferred_card": target, "only_preferred": True,
                     "reason": "Molten Duplication: sacrifice the copy at the next end step",
                 }]},
            ]
        self._add_card("molten duplication", EffectTemplate(
            name="Molten Duplication",
            description="Copy an artifact/creature you control as an artifact token with haste; sacrifice it at the next end step",
            action_generator=_gen_molten_duplication,
        ))

        # Rite of Replication — create 5 copies if kicked, 1 otherwise
        # Kicker detection: base cost {2}{U}{U} (4 mana), kicked adds {5} (total 9+)
        self._add_card("rite of replication", EffectTemplate(
            name="Rite of Replication",
            description="Create a token copy of target creature (5 if kicked)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_copy_token", "player": ctrl,
                 "target": "best_creature", "filter": "any",
                 # Aug 1: ctx['kicked'] is the STAMPED truth when present
                 # (kicker payment is modeled now); the mana-paid guess
                 # survives only for card-less ctx shapes.
                 "count": 5 if (ctx['kicked'] if 'kicked' in ctx
                                else (ctx.get('mana_paid_total', 0) or 0) >= 9) else 1}
            ],
        ))

        # Gatekeeper of Malakir — kicked ETB ("if it was kicked, target player
        # sacrifices a creature"). Aug 1 2026: kicker payment IS modeled now
        # (_compute_alt_costs stamps card._kicked, surfaced as ctx['kicked'])
        # — the stamped truth decides when present; the old mana_paid >= 3
        # guess survives only for card-less ctx shapes (it reads commander
        # tax / cost increases as "kicked", which is why it can't stay
        # primary).
        self._add_card("gatekeeper of malakir", EffectTemplate(
            name="Gatekeeper of Malakir",
            description="When this creature enters, if it was kicked, target player sacrifices a creature",
            action_generator=lambda ctrl, opp, ctx: (
                self._force_sacrifice_creature(ctrl, opp, ctx)
                if (ctx['kicked'] if 'kicked' in ctx
                    else (ctx.get('mana_paid_total', 0) or 0) >= 3)
                else [{"action": "no_action",
                       "reason": "Gatekeeper of Malakir: not kicked (intervening if, CR 603.4)"}]
            ),
        ))

        # =================================================================
        # NEW TEMPLATES — Batch added for autoplay coverage
        # =================================================================

        # --- Timeless Witness: same as Eternal Witness (graveyard to hand) ---
        self._add_card("timeless witness", EffectTemplate(
            name="Timeless Witness",
            description="Return target card from graveyard to hand",
            action_generator=lambda ctrl, opp, ctx: self._return_best_from_graveyard(ctrl, opp, ctx),
        ))


        # --- Lose-N-life, draw-N-cards family (Night's Whisper, Sign in Blood,
        # Ambition's Cost, Read the Bones-without-scry, Promise of Power, etc.)
        # May 20 audit: previously silently partial-resolved — life paid, no
        # cards drawn — because no template covered "lose N life, draw N cards".
        for _card_name, _amt in (
            ("night's whisper", 2),
            ("sign in blood", 2),
            ("ambition's cost", 3),
        ):
            self._add_card(_card_name, EffectTemplate(
                name=_card_name.title(),
                description=f"Lose {_amt} life and draw {_amt} cards",
                action_generator=lambda ctrl, opp, ctx, _a=_amt: [
                    {"action": "lose_life", "player": ctrl, "amount": _a,
                     "_source_card_name": ctx.get('_source_card_name', '')},
                    {"action": "draw_cards", "player": ctrl, "amount": _a},
                ],
            ))


        # --- Call to the Netherworld: return target black creature from graveyard to hand ---
        self._add_card("call to the netherworld", EffectTemplate(
            name="Call to the Netherworld",
            description="Return target black creature card from your graveyard to your hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card",
                 "card": ctx.get('best_own_graveyard_card', 'target'),
                 "from_zone": "graveyard", "to_zone": "hand", "player": ctrl},
            ],
        ))


        # --- Fleshbag Marauder / Merciless Executioner: each player sacrifices a creature ---
        # No "sacrifice_creature" action, so we use a no_action hint for the
        # controller sacrifice (they choose) and destroy the opponent's weakest
        for _edict_name in ["fleshbag marauder", "merciless executioner"]:
            self._add_card(_edict_name, EffectTemplate(
                name=_edict_name.title(),
                description="Each player sacrifices a creature",
                action_generator=lambda ctrl, opp, ctx: self._edict_effect(ctrl, opp, ctx),
            ))

        # Aug 5 confirmation-batch card tail. These all reached the pending
        # trigger drain in real games; keeping them in the shared library
        # lets ordinary and sync-queued paths use the same deterministic
        # actions.
        def _greatest_live_power(ctrl, ctx):
            controller = ctx.get('_controller_player')
            game = ctx.get('_game')
            if controller is None:
                return int(ctx.get('greatest_power', 0) or 0)
            powers = []
            for creature in controller.battlefield:
                if not creature.is_creature(game):
                    continue
                try:
                    powers.append(creature.get_effective_power(game))
                except (AttributeError, TypeError, ValueError):
                    powers.append(int(creature.power or 0))
            return max(powers, default=0)

        self._add_attack_card("pathbreaker ibex", EffectTemplate(
            name="Pathbreaker Ibex attack",
            description=("Whenever Pathbreaker Ibex attacks, creatures you "
                         "control gain trample and +X/+X until end of turn"),
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "pump_all_creatures", "player": ctrl,
                 "power": _greatest_live_power(ctrl, ctx),
                 "toughness": _greatest_live_power(ctrl, ctx),
                 "keywords": ["Trample"], "source": "Pathbreaker Ibex"},
            ],
        ))

        self._add_attack_card("kessig naturalist", EffectTemplate(
            name="Kessig Naturalist attack",
            description=("Whenever Kessig Naturalist attacks, add green mana "
                         "that does not empty until end of turn"),
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "add_mana", "player": ctrl, "color": "G",
                 "amount": 1, "retains_through_turn": True},
            ],
        ))

        def _ardenn_attach(ctrl, opp, ctx):
            controller = ctx.get('_controller_player')
            game = ctx.get('_game')
            if controller is None:
                return []
            creatures = [c for c in controller.battlefield
                         if c.is_creature(game)]
            if not creatures:
                return []
            def _power(creature):
                try:
                    return creature.get_effective_power(game)
                except (AttributeError, TypeError, ValueError):
                    return int(creature.power or 0)
            target = max(creatures, key=_power)
            attachments = [
                card for card in controller.battlefield
                if 'equipment' in (card.type_line or '').lower()
                or (
                    'aura' in (card.type_line or '').lower()
                    and any(shape in (card.oracle_text or '').lower()
                            for shape in ('enchant creature',
                                          'enchant permanent'))
                )
            ]
            return [
                {"action": "attach", "player": ctrl,
                 "attachment": card.name, "target": target.name}
                for card in attachments
            ]

        self._add_card("ardenn, intrepid archaeologist", EffectTemplate(
            name="Ardenn beginning combat",
            description=("At beginning of combat, attach Auras and Equipment "
                         "you control to the best creature you control"),
            action_generator=_ardenn_attach,
        ))

        self._add_card("wandermare", EffectTemplate(
            name="Wandermare Adventure cast trigger",
            description=("Whenever you cast a creature spell with Adventure, "
                         "put a +1/+1 counter on Wandermare"),
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "add_counters", "card": "Wandermare",
                 "counter_type": "+1/+1", "amount": 1},
            ],
        ))

        self._add_card("gadwick, the wizened", EffectTemplate(
            name="Gadwick blue-spell cast trigger",
            description=("Whenever you cast a blue spell, tap target nonland "
                         "permanent an opponent controls"),
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "tap", "card": ctx['best_opponent_nonland']}]
                if ctx.get('best_opponent_nonland') else []),
        ))

        def _omarthis_manifest(ctrl, opp, ctx):
            source = ctx.get('_source_card')
            count = sum(int(v or 0) for v in
                        (getattr(source, 'counters', {}) or {}).values())
            if count <= 0:
                return []
            return [{"action": "manifest_top", "player": ctrl,
                     "count": count, "source": "Omarthis, Ghostfire Initiate"}]

        self._add_dies_card("omarthis, ghostfire initiate", EffectTemplate(
            name="Omarthis dies",
            description=("When Omarthis dies, manifest cards equal to the "
                         "number of counters on it"),
            action_generator=_omarthis_manifest,
        ))

        # --- Rashmi, Eternities Crafter: reveal top card, cast free or put into hand ---
        self._add_card("rashmi, eternities crafter", EffectTemplate(
            name="Rashmi, Eternities Crafter",
            description="Reveal top card of library; if nonland with lesser MV, may cast free; else put into hand",
            action_generator=self._gen_rashmi_cast_trigger,
        ))

        # --- Thrasios, Triton Hero: {4}: Scry 1, reveal top; land → battlefield tapped, else draw ---
        self._add_card("thrasios, triton hero", EffectTemplate(
            name="Thrasios, Triton Hero",
            description="Scry 1, then reveal top: land enters tapped, otherwise draw a card",
            action_generator=self._gen_thrasios_activation,
        ))

        # --- Viscera Seer: "Sacrifice a creature: Scry 1."
        # The sacrifice cost is handled by the activated-ability path (auto-
        # picks weakest / token). The EFFECT here is just the scry. Registering
        # by card name so the activated-ability template fallback at line ~19593
        # of mtg_game.py resolves it cleanly instead of escalating to Tier 3.
        # Guard: only fire when we're in the ACTIVATION context (oracle_text
        # passed by the activation path is just "Scry 1."); skip when called
        # from the ETB scanner (full oracle contains "sacrifice a creature").
        def _viscera_seer_gen(ctrl, opp, ctx):
            ot = (ctx.get('_oracle') or '').lower()
            if 'sacrifice a creature' in ot:
                return None  # ETB context — Viscera Seer has no ETB effect
            return [{"action": "scry", "player": ctrl, "amount": 1}]
        self._add_card("viscera seer", EffectTemplate(
            name="Viscera Seer",
            description="Scry 1 (the sacrifice cost is processed separately)",
            action_generator=_viscera_seer_gen,
        ))

        # --- Inferno Titan: deal 3 damage divided on ETB and attack ---
        self._add_card("inferno titan", EffectTemplate(
            name="Inferno Titan",
            description="Deal 3 damage divided among targets (ETB/attack trigger)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 3,
                 "target_card": ctx.get('best_opponent_creature', ''),
                 "target_controller": opp, "source": "Inferno Titan"},
            ] if ctx.get('best_opponent_creature') else [
                {"action": "deal_damage", "amount": 3, "target_player": opp,
                 "source": "Inferno Titan"},
            ],
        ))

        # --- Read the Bones / Notion Rain: library look + draw + cost ---
        # Both were victims of single-clause library-look patterns claiming the
        # whole spell. The residual guard now stops that, but without a named
        # template they would only fall through to the NEXT partial pattern
        # (Read the Bones was resolving to just "lose 2 life"), so pin the full
        # effect here where every clause is visible at once.
        self._add_card("read the bones", EffectTemplate(
            name="Read the Bones",
            description="Scry 2, then draw two cards. You lose 2 life.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "scry", "player": ctrl, "amount": 2},
                {"action": "draw_cards", "player": ctrl, "amount": 2},
                {"action": "lose_life", "player": ctrl, "amount": 2},
            ],
        ))
        self._add_card("notion rain", EffectTemplate(
            name="Notion Rain",
            description="Surveil 2, then draw two cards. Notion Rain deals 2 damage to you.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "surveil", "player": ctrl, "amount": 2},
                {"action": "draw_cards", "player": ctrl, "amount": 2},
                {"action": "deal_damage", "amount": 2, "target_player": ctrl,
                 "source": "Notion Rain"},
            ],
        ))

        # --- Chulane: cast-trigger draw + free land drop ---
        self._add_card("chulane, teller of tales", EffectTemplate(
            name="Chulane, Teller of Tales",
            # Deliberately NOT phrased "Whenever you cast...": the cast-trigger
            # scan looks templates up through resolve_spell, which runs the ETB
            # name-key gate, and that gate skips any description whose first
            # clause starts with "whenever" without "enters" in it. A template
            # worded like the card would be silently unreachable from the only
            # site that wants it.
            description=("Chulane cast trigger: draw a card, then put a land card "
                         "from your hand onto the battlefield"),
            action_generator=self._gen_chulane_cast_trigger,
        ))

        # --- Felidar Retreat: modal landfall (2/2 white Cat Beast OR counters) ---
        self._add_card("felidar retreat", EffectTemplate(
            name="Felidar Retreat",
            description="Landfall: create a 2/2 white Cat Beast, or +1/+1 counters and vigilance",
            action_generator=self._gen_felidar_retreat,
        ))

        # --- Leonin Vanguard: conditional self-pump at beginning of combat ---
        self._add_card("leonin vanguard", EffectTemplate(
            name="Leonin Vanguard",
            description=("At the beginning of combat, if you control three or more "
                         "creatures, Leonin Vanguard gets +1/+1 and you gain 1 life"),
            action_generator=self._gen_leonin_vanguard,
        ))

        # --- Kroxa, Titan of Death's Hunger: sacrifice unless escaped ---
        self._add_card("kroxa, titan of death's hunger", EffectTemplate(
            name="Kroxa, Titan of Death's Hunger",
            description=("Each opponent discards a card, then each opponent who didn't "
                         "discard a nonland card loses 3 life; sacrifice Kroxa unless escaped"),
            action_generator=self._gen_kroxa_etb,
        ))
        # --- Ragavan, Nimble Pilferer: combat damage to a player ---
        # July 29 batch audit: the combat-damage dispatcher queued Ragavan to
        # Tier 3 on every connect, and mtg/judge.py's combat-shaped-resolve
        # guard (CR 510.1) refused the drain every time — one of Modern's
        # most impactful 1-drops produced zero value across a 55-turn game.
        # The Treasure + the opponent's exiled top card are modeled; the
        # "you may cast that card this turn" rider is not (noted in the
        # exile_top_of_library handler).
        self._add_attack_card("ragavan, nimble pilferer", EffectTemplate(
            name="Ragavan, Nimble Pilferer",
            description=("Ragavan deals combat damage to a player: create a "
                         "Treasure token and exile the top card of that "
                         "player's library"),
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Treasure",
                 "power": 0, "toughness": 0, "types": "Artifact — Treasure",
                 "count": 1},
                {"action": "exile_top_of_library", "player": opp, "count": 1},
            ],
        ))

        # --- Tymna the Weaver: postcombat main phase draw engine ---
        # July 29 batch audit: the July 27 main-phase trigger scan was wired
        # and threaded ctx['_opponents_dealt_combat_damage'], but no template
        # consumed it — every trigger queued to Tier 3, which refused with a
        # hallucinated reason ("combat actions can't resolve at sorcery
        # speed"), so Tymna's card-advantage engine STILL never happened.
        # The description must start with a scheduled prefix so the
        # scheduled-event gate lets the bare-name key fire on main_phase
        # (and blocks it on ETB, Bitterblossom-style).
        self._add_card("tymna the weaver", EffectTemplate(
            name="Tymna the Weaver",
            description=("At the beginning of each of your postcombat main phases, "
                         "pay X life to draw X cards (X = opponents dealt combat "
                         "damage this turn)"),
            action_generator=self._gen_tymna_main_phase,
        ))

        # "Whenever Kroxa enters OR ATTACKS" — the attack half fired nowhere
        # before July 28 2026, so a Kroxa that stuck around was half a card.
        self._add_attack_card("kroxa, titan of death's hunger", EffectTemplate(
            name="Kroxa, Titan of Death's Hunger (attack)",
            description=("Kroxa attacks: each opponent discards a card, then each opponent "
                         "who didn't discard a nonland card loses 3 life"),
            action_generator=self._gen_kroxa_attack,
        ))

        # --- July 30 batch-9 audit: combat-damage trigger templates ---
        # All 60 [DRAIN-COMBAT_DAMAGE] drains in batch 15322 were refused by
        # judge.py's combat-shaped-resolve guard (CR 510.1, by design) — the
        # July 28 queue is an audit trail, but NO queued combat-damage
        # trigger can ever resolve via Tier 3. The Ragavan fix (July 29)
        # already proved the template path; these cover the batch's ranked
        # refusal list. Every generator gates on ctx['damage_dealt'] so a
        # declare-time attack scan can never misfire them, and returns []
        # (handled no-op) when its condition/choice declines. Watcher-shaped
        # variants ("whenever a creature you control deals...") still only
        # fire on the SOURCE's own connect — battlefield-wide watchers are
        # pub/sub slice 5 (COMBAT_DAMAGE_DEALT) territory.
        self._add_attack_card("hellkite tyrant", EffectTemplate(
            name="Hellkite Tyrant",
            description=("Hellkite Tyrant deals combat damage to a player: "
                         "gain control of all artifacts that player controls"),
            action_generator=self._gen_hellkite_tyrant,
        ))
        self._add_attack_card("quartzwood crasher", EffectTemplate(
            name="Quartzwood Crasher",
            description=("Trample creatures dealt combat damage to a player: "
                         "create an X/X Dinosaur Beast with trample (X = that damage)"),
            action_generator=self._gen_quartzwood_crasher,
        ))
        # (July 31 slice 5b: the "ohran frostfang" AND "tovolar, dire
        # overlord" own-connect registrations that lived here were DELETED.
        # The battlefield-watcher loop in mtg/combat.py now handles the whole
        # "whenever a [qualifier] creature you control deals combat damage to
        # a player" family generically — Ohran's template was a strict
        # DUPLICATE of the old name-gated loop (4 draws for 3 attackers,
        # game_1532415549039050783), and Tovolar's own-connect-only template
        # both under-covered (other wolves' connects drew nothing) and would
        # now double-fire against the generalized watcher.)
        self._add_attack_card("neheb, dreadhorde champion", EffectTemplate(
            name="Neheb, Dreadhorde Champion",
            description=("Neheb deals combat damage: discard excess lands, "
                         "draw that many, add that much {R}"),
            action_generator=self._gen_neheb_dreadhorde,
        ))
        self._add_attack_card("glissa sunslayer", EffectTemplate(
            name="Glissa Sunslayer",
            description=("Glissa Sunslayer deals combat damage to a player: "
                         "destroy an enchantment, or draw a card and lose 1 life"),
            action_generator=self._gen_glissa_sunslayer,
        ))
        # --- Aug 1 batch-12 audit: the batch-15327 refused-trigger tail ---
        # Both queued 6x to [COMBAT-TRIGGER-UNHANDLED] and can never resolve
        # via Tier 3 (judge.py's combat-shape guard). Same damage_dealt gate
        # as the rest of the family.
        self._add_attack_card("stromkirk occultist", EffectTemplate(
            name="Stromkirk Occultist",
            description=("Stromkirk Occultist deals combat damage to a "
                         "player: exile the top card of your library, "
                         "playable this turn"),
            action_generator=self._gen_stromkirk_occultist,
        ))
        def _gen_mycoloth_etb(ctrl, opp, ctx):
            """Devour 2 (CR 702.81): "As this creature enters, you may
            sacrifice any number of creatures. It enters with twice that many
            +1/+1 counters on it."

            Devour did not exist, so Mycoloth entered with ZERO counters — and
            his whole payoff is "create a Saproling for EACH +1/+1 counter",
            which a generic token pattern was resolving as a flat ONE token
            forever. He was very nearly a dead card in the deck built around
            him.

            v1 choice, deliberately conservative: devour only TOKENS. Eating
            real cards for counters is a genuine cost and a strategic call
            this engine has no model for, but feeding spare tokens to Mycoloth
            IS the card's line — and he converts each one into a permanent
            Saproling engine, so it is close to pure profit.
            """
            fodder = [c for c in (ctx.get('controller_battlefield') or [])
                      if getattr(c, 'is_token', False)
                      and c is not ctx.get('_source_card')
                      and 'creature' in (getattr(c, 'type_line', '') or '').lower()]
            if not fodder:
                return [{"action": "no_action",
                         "reason": "Devour 2 — no expendable tokens to sacrifice"}]
            actions = []
            for token in fodder:
                actions.append({"action": "sacrifice_permanent", "player": ctrl,
                                "preferred_card": token.name,
                                "only_preferred": True,
                                "source": "Mycoloth (devour 2)",
                                "reason": "devoured"})
            actions.append({"action": "add_counters", "card": "self",
                            "counter_type": "+1/+1",
                            "amount": 2 * len(fodder)})
            return actions

        def _gen_mycoloth_upkeep(ctrl, opp, ctx):
            """"At the beginning of your upkeep, create a 1/1 green Saproling
            creature token for each +1/+1 counter on Mycoloth." A generic
            token pattern was making exactly ONE regardless."""
            source = ctx.get('_source_card')
            counters = 0
            if source is not None:
                try:
                    counters = int((getattr(source, 'counters', {}) or {})
                                   .get('+1/+1', 0) or 0)
                except (TypeError, ValueError, AttributeError):
                    counters = 0
            if counters <= 0:
                return [{"action": "no_action",
                         "reason": "Mycoloth has no +1/+1 counters — no Saprolings"}]
            return [{"action": "create_token", "player": ctrl,
                     "name": "Saproling", "power": 1, "toughness": 1,
                     "types": "Creature — Saproling", "colors": ["G"],
                     "count": counters}]

        self._add_card("mycoloth", EffectTemplate(
            name="Mycoloth",
            description="Devour 2 — sacrifice spare tokens for twice that many +1/+1 counters",
            action_generator=_gen_mycoloth_etb,
        ))
        self._add_card("mycoloth upkeep", EffectTemplate(
            name="Mycoloth",
            description="At the beginning of your upkeep, create a Saproling for each +1/+1 counter",
            action_generator=_gen_mycoloth_upkeep,
        ))

        def _gen_werewolf_pack_leader(ctrl, opp, ctx):
            """Pack tactics (CR 207.2c ability word): "Whenever this creature
            attacks, IF you attacked with creatures with total power 6 or
            greater this combat, draw a card."

            The condition was ignored — a generic "whenever this attacks,
            draw a card" pattern matched and drew EVERY combat, which is free
            card advantage the card does not have. Same shape as the wave-2
            ability-word conditions (delirium, morbid, metalcraft), and the
            reason ability words need a consumer rather than a pattern.
            """
            total = 0
            for perm in (ctx.get('controller_battlefield') or []):
                if not getattr(perm, 'attacking', False):
                    continue
                try:
                    total += int(getattr(perm, 'power', 0) or 0)
                except (TypeError, ValueError):
                    pass
            if total < 6:
                return [{"action": "no_action",
                         "reason": (f"Pack tactics not met — attacking total "
                                    f"power {total}, needs 6")}]
            return [{"action": "draw_cards", "player": ctrl, "amount": 1}]

        self._add_attack_card("werewolf pack leader", EffectTemplate(
            name="Werewolf Pack Leader",
            description=("Pack tactics — draw a card if you attacked with "
                         "creatures of total power 6 or greater"),
            action_generator=_gen_werewolf_pack_leader,
        ))

        self._add_attack_card("drana, liberator of malakir", EffectTemplate(
            name="Drana, Liberator of Malakir",
            description=("Drana deals combat damage to a player: put a +1/+1 "
                         "counter on each attacking creature you control"),
            action_generator=self._gen_drana_liberator,
        ))
        self._add_attack_card("ancient bronze dragon", EffectTemplate(
            name="Ancient Bronze Dragon",
            description=("Ancient Bronze Dragon deals combat damage to a "
                         "player: roll a d20, put that many +1/+1 counters on "
                         "up to two target creatures"),
            action_generator=self._gen_ancient_bronze_dragon,
        ))
        self._add_attack_card("flaxen intruder", EffectTemplate(
            name="Flaxen Intruder",
            description=("Flaxen Intruder deals combat damage to a player: "
                         "may sacrifice it to destroy an artifact or enchantment"),
            action_generator=self._gen_flaxen_intruder,
        ))
        # --- July 31 batch-11: the batch-15325 refused-trigger tail ---
        self._add_attack_card("port razer", EffectTemplate(
            name="Port Razer",
            description=("Port Razer deals combat damage to a player: untap "
                         "each creature you control, and an additional combat "
                         "phase follows this one"),
            action_generator=self._gen_port_razer,
        ))
        self._add_attack_card("frenzied trapbreaker", EffectTemplate(
            name="Frenzied Trapbreaker",
            description=("Frenzied Trapbreaker attacks: destroy target "
                         "artifact or enchantment defending player controls"),
            action_generator=self._gen_frenzied_trapbreaker,
        ))
        # Aug 2 batch-13: replaces the JSON no_action "use !fix" placeholder
        # (the pre-slate breadcrumb class) — the additional_combat action
        # exists now, so Aurelia's whole ability is modelable.
        self._add_attack_card("aurelia, the warleader", EffectTemplate(
            name="Aurelia, the Warleader",
            description=("Aurelia attacks for the first time each turn: untap "
                         "all creatures you control, and an additional combat "
                         "phase follows this one"),
            action_generator=self._gen_aurelia_warleader,
        ))
        self._add_attack_card("boros elite", EffectTemplate(
            name="Boros Elite",
            description=("Battalion: attacks with two other creatures — "
                         "Boros Elite gets +2/+2 until end of turn"),
            action_generator=self._gen_boros_elite_battalion,
        ))

        # --- July 31 batch-10 audit: the batch-15324 refused-trigger tail ---
        # Same design as the block above: declare-time generators gate on NOT
        # ctx['damage_dealt'], combat-damage generators on ctx['damage_dealt'],
        # so the two dispatch paths sharing this registry can never double-fire
        # a template.
        self._add_attack_card("predator ooze", EffectTemplate(
            name="Predator Ooze",
            description=("Predator Ooze attacks: put a +1/+1 counter on it "
                         "(its dealt-damage-dies counter trigger is separate "
                         "and unmodeled here)"),
            action_generator=self._gen_attack_self_counter,
        ))
        self._add_attack_card("bloodmad vampire", EffectTemplate(
            name="Bloodmad Vampire",
            description=("Bloodmad Vampire deals combat damage to a player: "
                         "put a +1/+1 counter on it"),
            action_generator=self._gen_combat_damage_self_counter,
        ))
        self._add_attack_card("underworld sentinel", EffectTemplate(
            name="Underworld Sentinel",
            description=("Underworld Sentinel attacks: exile a creature card "
                         "from your graveyard (linked — returned to the "
                         "battlefield by its dies trigger)"),
            action_generator=self._gen_underworld_sentinel_attack,
        ))
        self._add_dies_card("underworld sentinel", EffectTemplate(
            name="Underworld Sentinel",
            description=("Underworld Sentinel dies: put all cards exiled "
                         "with it onto the battlefield"),
            action_generator=self._gen_underworld_sentinel_dies,
        ))
        self._add_attack_card("yidris, maelstrom wielder", EffectTemplate(
            name="Yidris, Maelstrom Wielder",
            description=("Yidris deals combat damage to a player: spells cast "
                         "from your hand this turn gain cascade"),
            action_generator=self._gen_yidris_cascade_grant,
        ))

        # --- Felidar Guardian: blink ANOTHER permanent you control (not self) ---
        self._add_card("felidar guardian", EffectTemplate(
            name="Felidar Guardian",
            description="Exile another target permanent you control, then return it to the battlefield",
            action_generator=self._gen_felidar_guardian,
        ))

        # --- Worldgorger Dragon: exile all other permanents you control (LTB returns them) ---
        self._add_card("worldgorger dragon", EffectTemplate(
            name="Worldgorger Dragon",
            description="Exile all other permanents you control. When this leaves, return them.",
            action_generator=self._gen_worldgorger_dragon,
        ))

        # --- Oblivion Ring / Banishing Light / Detention Sphere: exile target nonland permanent (LTB returns it) ---
        for _oring_name in ["oblivion ring", "banishing light", "fiend hunter", "deputy of detention", "detention sphere"]:
            self._add_card(_oring_name, EffectTemplate(
                name=_oring_name.title(),
                description="Exile target nonland permanent until this leaves the battlefield",
                action_generator=self._gen_oblivion_ring,
            ))

        # --- Eerie Interlude: exile creatures, return at end step ---
        self._add_card("eerie interlude", EffectTemplate(
            name="Eerie Interlude",
            description="Exile any number of target creatures you control, return at next end step",
            action_generator=self._gen_eerie_interlude,
        ))

        # --- Coiling Oracle: reveal top card, if land put it on battlefield, else draw ---
        self._add_card("coiling oracle", EffectTemplate(
            name="Coiling Oracle",
            description="Reveal top card: if land, put onto battlefield; otherwise put into hand",
            action_generator=self._gen_coiling_oracle,
        ))

        # --- Restoration Angel: blink ANOTHER non-Angel creature you control ---
        self._add_card("restoration angel", EffectTemplate(
            name="Restoration Angel",
            description="Exile another target non-Angel creature you control, return it to the battlefield",
            action_generator=self._gen_restoration_angel,
        ))


        # --- Hullbreaker Horror: "Whenever you cast another spell, choose one
        # that hasn't been chosen — return target creature/artifact/enchantment/
        # planeswalker to its owner's hand." Fires on cast triggers; if opponent
        # has no valid targets, the trigger fizzles silently.
        def _hullbreaker_trigger(ctrl, opp, ctx):
            opp_perms_info = ctx.get('_opponent_nonland_permanents', []) or []
            # Pick the highest-power or highest-MV opponent permanent.
            best = None
            best_score = -1
            for info in opp_perms_info:
                if isinstance(info, dict):
                    name = info.get('name')
                    score = (info.get('power', 0) or 0) + (info.get('cmc', 0) or 0)
                else:
                    name = str(info)
                    score = 0
                if not name:
                    continue
                if score > best_score:
                    best_score = score
                    best = name
            if not best:
                best = ctx.get('best_opponent_nonland') or ctx.get('best_opponent_creature')
            if not best:
                return []  # Silent fizzle — no legal targets
            return [
                {"action": "move_card", "card": best,
                 "from_zone": "battlefield", "to_zone": "hand", "player": opp},
            ]
        self._add_card("hullbreaker horror", EffectTemplate(
            name="Hullbreaker Horror",
            description="On cast trigger: bounce best opponent permanent (silent if none)",
            action_generator=_hullbreaker_trigger,
        ))


        # --- Craterhoof Behemoth: pump all creatures +X/+X where X = creature count ---
        # Game-context-dependent: needs creature count. Uses grant_keywords for trample
        # and a no_action hint since the pump is a continuous effect tracked differently.
        self._add_card("craterhoof behemoth", EffectTemplate(
            name="Craterhoof Behemoth",
            description="Creatures you control gain trample and get +X/+X (X = creature count)",
            action_generator=lambda ctrl, opp, ctx: self._craterhoof_pump(ctrl, opp, ctx),
        ))

        # --- Kogla, the Titan Ape: fights target creature opponent controls ---
        # No "fight" action exists; approximate as dealing damage equal to
        # Kogla's power (7) to opponent's best creature
        self._add_card("kogla, the titan ape", EffectTemplate(
            name="Kogla, the Titan Ape",
            description="Kogla fights target creature you don't control",
            action_generator=lambda ctrl, opp, ctx: self._fight_best_creature(ctrl, opp, ctx, 7),
            needs_target=True,
        ))


        # =================================================================
        # FETCHLANDS — sacrifice to search library for a land
        # =================================================================
        # These need templates because Tier 3 (Claude API) hallucinates
        # "enters tapped" for fetchlands that should enter UNTAPPED.
        # The move_card action puts the land onto the battlefield; the
        # tapped state is controlled by _activate_permanent in mtg_game.py,
        # but these templates ensure correct behavior when resolved via
        # the template library path (autoplay, !judge, etc.).

        # --- Onslaught / Khans fetches (pay 1 life, untapped) ---
        # Data: {card_name: (land_type_to_fetch, description)}
        ALLIED_FETCHES = {
            "wooded foothills":   "Mountain",
            "flooded strand":     "Plains",
            "polluted delta":     "Island",
            "bloodstained mire":  "Swamp",
            "windswept heath":    "Forest",
        }
        ENEMY_FETCHES = {
            "scalding tarn":      "Island",
            "verdant catacombs":  "Swamp",
            "misty rainforest":   "Forest",
            "marsh flats":        "Plains",
            "arid mesa":          "Mountain",
        }
        for name, land_type in {**ALLIED_FETCHES, **ENEMY_FETCHES}.items():
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=f"Pay 1 life, sacrifice: search for a {land_type} (untapped)",
                action_generator=lambda ctrl, opp, ctx, lt=land_type: [
                    {"action": "lose_life", "player": ctrl, "amount": 1},
                    {"action": "search_library_land", "player": ctrl,
                     "land_type": lt, "enters_tapped": False},
                ],
            ))


        # --- Evolving Wilds / Terramorphic Expanse (basic land, tapped, no life) ---
        for _ew_name in ["evolving wilds", "terramorphic expanse"]:
            self._add_card(_ew_name, EffectTemplate(
                name=_ew_name.title(),
                description="Sacrifice: search for a basic land (tapped)",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "search_library_land", "player": ctrl,
                     "basic_only": True, "enters_tapped": True},
                ],
            ))

        # --- Fabled Passage (basic land, untapped if 4+ lands, else tapped) ---
        self._add_card("fabled passage", EffectTemplate(
            name="Fabled Passage",
            description="Sacrifice: search for a basic land (untapped if 4+ lands)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library_land", "player": ctrl,
                 "basic_only": True,
                 "enters_tapped": ctx.get('controller_land_count', 3) < 4},
            ],
        ))

        # =================================================================
        # SAKURA-TRIBE ELDER — sacrifice: search for basic land (tapped)
        # =================================================================
        self._add_card("sakura-tribe elder", EffectTemplate(
            name="Sakura-Tribe Elder",
            description="Sacrifice: search for a basic land (tapped)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library_land", "player": ctrl,
                 "basic_only": True, "enters_tapped": True},
            ],
        ))

        # =================================================================
        # PHYREXIAN PROCESSOR — ETB: pay life to set token size
        # =================================================================
        # Can't prompt for a choice in autoplay, so default to a reasonable
        # amount: min(life - 5, 10) — enough for decent tokens but not suicidal.
        self._add_card("phyrexian processor", EffectTemplate(
            name="Phyrexian Processor",
            description="Pay life as Phyrexian Processor enters (sets token P/T)",
            action_generator=lambda ctrl, opp, ctx: self._phyrexian_processor_etb(ctrl, opp, ctx),
        ))

        # --- Victimize: sacrifice a creature, reanimate 2 from your graveyard ---
        self._add_card("victimize", EffectTemplate(
            name="Victimize",
            description="Sacrifice a creature, return two creature cards from your graveyard to the battlefield",
            action_generator=lambda ctrl, opp, ctx: self._gen_victimize(ctrl, opp, ctx),
        ))

        # --- Searing Blaze: deal damage based on landfall ---
        self._add_card("searing blaze", EffectTemplate(
            name="Searing Blaze",
            description="Deal 1 (or 3 with landfall) to target creature and its controller",
            action_generator=lambda ctrl, opp, ctx: self._gen_searing_blaze(ctrl, opp, ctx),
        ))

        # --- Rift Bolt: deal 3 damage to any target (prevents Tier 3 spurious exile) ---
        self._add_card("rift bolt", EffectTemplate(
            name="Rift Bolt",
            description="Deal 3 damage to any target",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 3,
                 "target_card": ctx.get('best_opponent_creature', ''),
                 "target_controller": opp}
            ] if ctx.get('best_opponent_creature') and ctx.get('explicit_target_is_creature', False) else [
                {"action": "deal_damage", "amount": 3, "target_player": opp},
            ],
        ))

        # --- Volcanic Geyser: deal X damage to any target ---
        # July 23 audit (#11): this X-damage spell had no template and escalated
        # to Tier 3, which DROPPED the AI's declared creature target and hit the
        # opponent's face instead (game_1529677634588377108: cast with
        # target=Savra x=2, resolved as 2 to Rick's face, Savra survived). Honor
        # the declared creature target (CR 608.2b) with the chosen X; fall back
        # to face only when the target isn't a creature.
        self._add_card("volcanic geyser", EffectTemplate(
            name="Volcanic Geyser",
            description="Deal X damage to any target",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "deal_damage",
                  "amount": max(0, int(ctx.get('x_value', 0) or 0)),
                  "target_card": ctx.get('explicit_target_name') or ctx.get('best_opponent_creature', ''),
                  "target_controller": opp}]
                if ((ctx.get('explicit_target_name') or ctx.get('best_opponent_creature'))
                    and ctx.get('explicit_target_is_creature', False))
                else [{"action": "deal_damage",
                       "amount": max(0, int(ctx.get('x_value', 0) or 0)),
                       "target_player": opp}]
            ),
        ))

        # --- Insult // Injury (front half): turn-scoped damage doubler ---
        # July 23 audit (#15): Tier 3 correctly declined this as "a continuous
        # effect for the turn; no immediate state change", so the doubling never
        # happened and that turn's combat damage landed at base values. The
        # action registers a real, self-expiring replacement effect (see
        # mtg/actions.py register_turn_damage_doubler) so it stacks with
        # Gisela / Furnace under CR 616.1 ordering.
        # Both clauses are modeled: the action also sets the turn-scoped
        # "damage can't be prevented" flag (helpers.damage_prevention_disabled,
        # honored at every prevention gate) so Fog / Teferi's Protection /
        # Glacial Chasm can't blank the doubled damage — which matters here,
        # since the replacement_chain and replacement_fog decks both run Fog
        # effects alongside the doublers.
        for _insult_key in ("insult // injury", "insult"):
            self._add_card(_insult_key, EffectTemplate(
                name="Insult // Injury",
                description="Damage from your sources is doubled this turn",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "register_turn_damage_doubler", "player": ctrl,
                     "source": "Insult // Injury"}
                ],
            ))

        # --- Shard Volley: sacrifice was already paid as an additional cost ---
        # (shard volley: single registration lives at the burn-spell block —
        # Aug 7 registry dedup removed the target-blind duplicate here.)

        # --- Aura Shards: destroy target artifact/enchantment when creature enters ---
        self._add_card("aura shards", EffectTemplate(
            name="Aura Shards",
            description="Destroy target artifact or enchantment when a creature enters",
            action_generator=lambda ctrl, opp, ctx: self._gen_aura_shards(ctrl, opp, ctx),
        ))


        # --- Martial Coup: create X tokens, wipe if X >= 5 ---
        self._add_card("martial coup", EffectTemplate(
            name="Martial Coup",
            description="Create X 1/1 Soldiers; if X >= 5, destroy all other creatures",
            action_generator=lambda ctrl, opp, ctx: self._gen_martial_coup(ctrl, opp, ctx),
        ))

        # --- Inquisition of Kozilek ---
        self._add_card("inquisition of kozilek", EffectTemplate(
            name="Inquisition of Kozilek",
            description="Target opponent reveals hand, you choose a nonland card with MV 3 or less, they discard it",
            action_generator=lambda ctrl, opp, ctx: self._gen_inquisition(ctrl, opp, ctx),
        ))


        # =================================================================
        # RESTRICTED TUTORS — search library for specific card types to hand
        # =================================================================


        # =================================================================
        # LANDFALL / ATTACK / MISC TRIGGERS — Named cards
        # =================================================================

        # Strangleroot Geist (and other undying creatures): self-death trigger
        # The undying mechanic is handled by the SBA engine in process_state_based_actions().
        # This template prevents a spurious !resolve prompt when the creature dies the second
        # time (with +1/+1 counter from undying), at which point the trigger text is still
        # present in oracle but undying cannot apply.
        for _undying_card in [
            "strangleroot geist", "young wolf", "butcher ghoul",
            "gravecrawler",  # (Gravecrawler has a different mechanic but similar self-death text)
            "mikaeus, the unhallowed",  # Gives undying to non-humans
        ]:
            self._add_card(_undying_card, EffectTemplate(
                name=_undying_card.title(),
                description="Undying/self-death trigger handled by SBA engine",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action", "reason": "Undying is handled mechanically by the SBA engine"}
                ],
            ))


        # Finale of Devastation: {X}{G}{G} — creatures you control get +X/+X and
        # gain trample; if X >= 10, search your library for a creature and put it
        # onto the battlefield. May 30 audit: no template existed, so it escalated
        # to Tier 3 (which returned no actions → the spell did nothing, bare
        # "resolves." shown). Both halves are handled here at Tier 1.5.
        self._add_card("finale of devastation", EffectTemplate(
            name="Finale of Devastation",
            description="Creatures you control get +X/+X and trample; if X>=10, tutor a creature into play",
            action_generator=self._gen_finale_of_devastation,
        ))

        # Voice of Resurgence: "whenever an opponent casts a spell during your turn OR when VoR dies"
        # → create a green and white Elemental token with P/T = # of creatures controller controls.
        # Used by both the opp-cast trigger path (resolve_spell) and the dies trigger path.
        def _gen_voice_of_resurgence(ctrl, opp, ctx):
            # Token enters as (N+1)/(N+1) where N = current creature count,
            # because the token counts itself.
            count = ctx.get('controller_creature_count', 0) + 1
            return [
                {"action": "create_token", "player": ctrl, "name": "Elemental",
                 "power": count, "toughness": count,
                 "types": "Creature Token — Elemental", "count": 1}
            ]

        self._add_card("voice of resurgence", EffectTemplate(
            name="Voice of Resurgence",
            description="Create a green and white Elemental token with P/T = creatures you control",
            action_generator=_gen_voice_of_resurgence,
        ))
        self._add_dies_card("voice of resurgence", EffectTemplate(
            name="Voice of Resurgence dies",
            description=("When Voice of Resurgence dies, create an Elemental "
                         "token with P/T equal to creatures you control"),
            action_generator=_gen_voice_of_resurgence,
        ))



        # Goblin Guide: attack trigger — defending player reveals top card,
        # if it's a land, they put it into their hand
        self._add_attack_card("goblin guide", EffectTemplate(
            name="Goblin Guide",
            description="Whenever Goblin Guide attacks, defending player reveals top card; if land, put it into hand",
            action_generator=self._gen_goblin_guide_attack,
        ))

        # Venser, Shaper Savant: ETB — return target spell or permanent to owner's hand
        self._add_card("venser, shaper savant", EffectTemplate(
            name="Venser, Shaper Savant",
            description="Return target spell or permanent to its owner's hand",
            action_generator=lambda ctrl, opp, ctx: self._bounce_best_creature(ctrl, opp, ctx),
            needs_target=True,
        ))

        # Blue Sun's Zenith: target player draws X cards, shuffle back into library
        # Default X=3 if no context. Template targets controller (not opponent).
        self._add_card("blue sun's zenith", EffectTemplate(
            name="Blue Sun's Zenith",
            description="Draw X cards (default X=3), shuffle Blue Sun's Zenith into library",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards",
                 "player": ctx.get('explicit_target_player') or ctx.get('explicit_target_name') or ctrl,  # B-2 (Aug 7): player key first
                 "amount": max(1, ctx.get('x_value', 3))},
            ],
        ))


        # Temur Sabertooth: activated ability — {1}{G}, return another creature
        # you control to hand: target creature you control gains indestructible.
        # Triggers via the activated-ability path at _activate_permanent:22106
        # (Temur has no self-ETB, so this only fires on activation).
        self._add_card("temur sabertooth", EffectTemplate(
            name="Temur Sabertooth",
            description="Bounce lowest-MV creature, grant indestructible to best creature",
            action_generator=self._gen_temur_sabertooth,
        ))


        # --- Apr 29 audit additions: gap-filling tutors ---


        # --- Increasing Devotion: create 5 (or 10 from graveyard) 1/1 Human tokens ---
        self._add_card("increasing devotion", EffectTemplate(
            name="Increasing Devotion",
            description="Create 5 (or 10 if from graveyard) 1/1 Human tokens",
            action_generator=lambda ctrl, opp, ctx: self._gen_increasing_devotion(ctrl, opp, ctx),
        ))

        # Endrek Sahr, Master Breeder: cast trigger creates X 1/1 Thrull tokens
        # (X = cast creature's MV). NOT an ETB — the cast trigger is handled elsewhere.
        # Register as no-ETB so templates don't fall through to Tier 3. Its
        # seven-Thrull state trigger is handled by the PERMANENT_ENTERED subscriber.
        self._add_card("endrek sahr, master breeder", EffectTemplate(
            name="Endrek Sahr, Master Breeder",
            description="No ETB action (cast trigger and seven-Thrull state trigger are handled mechanically)",
            action_generator=lambda ctrl, opp, ctx: [],
        ))


        # =================================================================
        # CONDITIONAL CREATURE-ENTERS TRIGGERS
        # =================================================================

        # Authority of the Consuls: "Whenever a creature enters under an
        # opponent's control, you gain 1 life."
        # Conditional on entering creature NOT being controlled by Authority's owner.
        # (The "creatures your opponents control enter tapped" replacement is
        # handled separately by the replacement engine.)
        self._add_card("authority of the consuls", EffectTemplate(
            name="Authority of the Consuls",
            description="When an opponent's creature enters, you gain 1 life",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "gain_life", "player": ctrl, "amount": 1}]
                if ctx.get('entering_controller_name', opp) != ctrl
                else [{"action": "no_action",
                       "reason": "Authority: entering creature not controlled by opponent"}]
            ),
        ))

        # Guardian Project: "Whenever a nontoken creature enters under your
        # control, if it doesn't have the same name as another creature you
        # control or a creature card in your graveyard, draw a card."
        def _guardian_project_gen(ctrl, opp, ctx):
            entering_ctrl = ctx.get('entering_controller_name', ctrl)
            if entering_ctrl != ctrl:
                return [{"action": "no_action",
                         "reason": "Guardian Project: not your creature"}]
            if ctx.get('entering_is_token', False):
                return [{"action": "no_action",
                         "reason": "Guardian Project: tokens don't trigger"}]
            entering_name = (ctx.get('entering_name') or '').lower()
            if not entering_name:
                return [{"action": "no_action", "reason": "Guardian Project: unknown creature name"}]
            # Check other creatures you control (excluding the entering creature itself)
            for c in ctx.get('controller_battlefield', []) or []:
                try:
                    if not hasattr(c, 'is_creature') or not c.is_creature():
                        continue
                    if c.name.lower() == entering_name and id(c) != ctx.get('_entering_id'):
                        return [{"action": "no_action",
                                 "reason": f"Guardian Project: another '{c.name}' already on battlefield"}]
                except Exception:
                    continue
            # Check graveyard for creature cards with same name
            for c in ctx.get('controller_graveyard', []) or []:
                try:
                    type_line = (getattr(c, 'type_line', '') or '').lower()
                    if 'creature' in type_line and c.name.lower() == entering_name:
                        return [{"action": "no_action",
                                 "reason": f"Guardian Project: '{c.name}' in graveyard"}]
                except Exception:
                    continue
            return [{"action": "draw_cards", "player": ctrl, "amount": 1}]
        self._add_card("guardian project", EffectTemplate(
            name="Guardian Project",
            description="Draw a card when a unique nontoken creature enters under your control",
            action_generator=_guardian_project_gen,
        ))


        # Scry lands (Temple of Mystery, Temple of Triumph, etc.):
        # "When X enters, scry 1." Tapped land with scry ETB.
        for temple in (
            "temple of abandon", "temple of deceit", "temple of enlightenment",
            "temple of epiphany", "temple of malady", "temple of malice",
            "temple of mystery", "temple of plenty", "temple of silence",
            "temple of triumph",
        ):
            self._add_card(temple, EffectTemplate(
                name=temple.title(),
                description="Scry 1 on ETB",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "scry", "player": ctrl, "amount": 1}
                ],
            ))


        # =================================================================
        # EDICT EFFECTS — Plaguecrafter and similar
        # =================================================================


    def _build_pw_ability_templates(self):
        """
        Planeswalker ability templates keyed on (pw_name_snippet, ability_text_snippet).

        These are checked by resolve_pw_ability() before falling through to
        generic oracle text pattern matching.
        """

        # Chandra, Torch of Defiance +1: "Exile the top card of your library.
        # You may cast that card. If you don't, Chandra deals 2 damage to each opponent."
        self._pw_ability_templates[("chandra, torch of defiance", "exile the top card")] = EffectTemplate(
            name="Chandra ToD +1",
            description="Exile top card; play it this turn or deal 2 damage to each opponent",
            action_generator=self._chandra_tod_plus1,
        )

        # Chandra, Flamecaller 0: "Create two 3/1 red Elemental creature tokens
        # with haste. Exile them at the beginning of the next end step."
        self._pw_ability_templates[("chandra, flamecaller", "3/1 red elemental")] = EffectTemplate(
            name="Chandra Flamecaller 0",
            description="Create two 3/1 red Elemental creature tokens with haste",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Elemental",
                 "power": 3, "toughness": 1,
                 "types": "Creature — Elemental",
                 "keywords": ["Haste"], "count": 2},
            ],
        )
        # Chandra, Flameshaper +2: "Add {R}{R}{R}. Exile the top three cards
        # of your library. Choose one. You may play that card this turn."
        # Simplified: add RRR + draw 1 (approximates "exile 3, play one of them").
        self._pw_ability_templates[("chandra, flameshaper", "exile the top three cards")] = EffectTemplate(
            name="Chandra Flameshaper +2",
            description=("Add {R}{R}{R} and exile the top card; you may "
                         "play it (top-3 selection not modelled)"),
            # Same C5 treatment as Vivien's -2 below — identical shape, found
            # in the same grep. Exiles face UP (this one is not a face-down
            # ability) and marks the card playable rather than drawing it.
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "add_mana", "player": ctrl, "color": "R", "amount": 3},
                {"action": "exile_top_of_library", "player": ctrl, "count": 1,
                 "playable": True},
            ],
        )

        # Chandra, Flamecaller -X: "Chandra, Flamecaller deals X damage to each creature your opponents control."
        self._pw_ability_templates[("chandra, flamecaller", "damage to each creature")] = EffectTemplate(
            name="Chandra Flamecaller -X",
            description="Deal X damage to each creature opponents control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "damage_all_creatures", "amount": max(1, ctx.get('x_value', 3)),
                 "controller": opp},
            ],
        )

        # Confirmation-batch planeswalker tail: these repeated abilities must
        # never depend on Tier 3's duplicate-suppression cache.
        self._pw_ability_templates[(
            "garruk wildspeaker", "untap two target lands")] = EffectTemplate(
            name="Garruk Wildspeaker +1",
            description="Untap up to two target lands",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "untap_lands", "player": ctrl, "count": 2},
            ],
        )
        self._pw_ability_templates[(
            "garruk wildspeaker", "get +3/+3 and gain trample")] = EffectTemplate(
            name="Garruk Wildspeaker -4",
            description="Creatures you control get +3/+3 and trample",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "pump_all_creatures", "player": ctrl,
                 "power": 3, "toughness": 3, "keywords": ["Trample"],
                 "source": "Garruk Wildspeaker"},
            ],
        )

        self._pw_ability_templates[(
            "liliana, dreadhorde general",
            "each player sacrifices two creatures")] = EffectTemplate(
            name="Liliana Dreadhorde General -4",
            description="Each player sacrifices two creatures",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "sacrifice_permanent", "player": ctrl,
                 "type_filter": "creature", "source": "Liliana -4"},
                {"action": "sacrifice_permanent", "player": ctrl,
                 "type_filter": "creature", "source": "Liliana -4"},
                {"action": "sacrifice_permanent", "player": opp,
                 "type_filter": "creature", "source": "Liliana -4"},
                {"action": "sacrifice_permanent", "player": opp,
                 "type_filter": "creature", "source": "Liliana -4"},
            ],
        )

        def _jaya_discard_draw(ctrl, opp, ctx):
            hand = list(ctx.get('controller_hand') or [])
            count = min(3, len(hand))
            if count <= 0:
                return []
            chosen = sorted(
                hand, key=lambda c: (not c.is_land(), int(c.cmc or 0)))[:count]
            actions = [
                {"action": "discard", "player": ctrl, "card": card.name}
                for card in chosen
            ]
            actions.append(
                {"action": "draw_cards", "player": ctrl, "amount": count})
            return actions

        self._pw_ability_templates[(
            "jaya ballard", "discard up to three cards")] = EffectTemplate(
            name="Jaya Ballard +1 loot",
            description="Discard up to three cards, then draw that many",
            action_generator=_jaya_discard_draw,
        )

        def _nahiri_put_equipment(ctrl, opp, ctx):
            candidates = [
                (card, "hand") for card in (ctx.get('controller_hand') or [])
                if 'equipment' in (card.type_line or '').lower()
            ]
            candidates.extend(
                (card, "graveyard")
                for card in (ctx.get('controller_graveyard') or [])
                if 'equipment' in (card.type_line or '').lower())
            if not candidates:
                return []
            card, zone = max(
                candidates, key=lambda pair: int(pair[0].cmc or 0))
            return [{"action": "move_card", "card": card.name,
                     "from_zone": zone, "to_zone": "battlefield",
                     "player": ctrl}]

        self._pw_ability_templates[(
            "nahiri, the lithomancer",
            "equipment card from your hand or graveyard")] = EffectTemplate(
            name="Nahiri Lithomancer -2",
            description="Put an Equipment from hand or graveyard onto battlefield",
            action_generator=_nahiri_put_equipment,
        )

        # Garruk, Primal Hunter +1: "Create a 3/3 green Beast creature token."
        self._pw_ability_templates[("garruk", "3/3 green beast")] = EffectTemplate(
            name="Garruk +1 Beast",
            description="Create a 3/3 green Beast creature token",
            action_generator=lambda ctrl, opp, ctx: [
                make_token_action(ctrl, "beast_3_3", 1)
            ],
        )

        # Daretti, Scrap Savant +2: "Discard up to two cards, then draw that many cards."
        self._pw_ability_templates[("daretti", "discard up to two")] = EffectTemplate(
            name="Daretti +2 Loot",
            description="Discard up to 2 cards, then draw that many",
            action_generator=self._daretti_loot,
        )

        # Daretti, Scrap Savant -2: "Sacrifice an artifact. Return target artifact card from your graveyard to the battlefield."
        self._pw_ability_templates[("daretti", "sacrifice an artifact")] = EffectTemplate(
            name="Daretti -2 Weld",
            description="Sacrifice an artifact, return an artifact from graveyard to battlefield",
            action_generator=self._daretti_weld,
        )

        # Elspeth, Sun's Champion +1: "Create three 1/1 white Soldier creature tokens."
        self._pw_ability_templates[("elspeth, sun's champion", "three 1/1")] = EffectTemplate(
            name="Elspeth SC +1 Soldiers",
            description="Create three 1/1 white Soldier tokens",
            action_generator=lambda ctrl, opp, ctx: [
                make_token_action(ctrl, "soldier_1_1", 3)
            ],
        )
        # Also match shortened oracle text
        self._pw_ability_templates[("elspeth, sun's champion", "1/1 white soldier")] = EffectTemplate(
            name="Elspeth SC +1 Soldiers",
            description="Create three 1/1 white Soldier tokens",
            action_generator=lambda ctrl, opp, ctx: [
                make_token_action(ctrl, "soldier_1_1", 3)
            ],
        )
        self._pw_ability_templates[("elspeth, sun's champion", "power 4 or greater")] = EffectTemplate(
            name="Elspeth SC -3 selective wipe",
            description="Destroy all creatures with power 4 or greater",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy_by_power", "min_power": 4}
            ],
        )

        # Garruk, Primal Hunter -3: "Draw cards equal to greatest power among creatures you control."
        # July 31 batch-10 reviewer: the old inline lambda floored the draw at
        # max(..., 1), drawing a card off an EMPTY board (0 creatures → the
        # card says draw 0). The correct generator _gen_garruk_minus3 had
        # existed since March as DEAD CODE (never referenced) — the
        # live-wrong/dead-right split the audit history keeps finding. Wired.
        self._pw_ability_templates[("garruk", "draw cards equal to the greatest power")] = EffectTemplate(
            name="Garruk -3 Draw",
            description="Draw cards equal to greatest power among creatures you control",
            action_generator=self._gen_garruk_minus3,
        )

        # Liliana, Dreadhorde General +1: "Create a 2/2 black Zombie creature token."
        self._pw_ability_templates[("liliana", "2/2 black zombie")] = EffectTemplate(
            name="Liliana +1 Zombie",
            description="Create a 2/2 black Zombie creature token",
            action_generator=lambda ctrl, opp, ctx: [
                make_token_action(ctrl, "zombie_2_2", 1)
            ],
        )

        # Nissa, Who Shakes the World +1: land becomes 3/3 (approximated as token)
        self._pw_ability_templates[("nissa", "put three +1/+1 counters on")] = EffectTemplate(
            name="Nissa +1 Animate Land",
            description="Animate a land (put three +1/+1 counters on it, becomes a creature)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Nissa animates a land (it becomes a 3/3 Elemental with vigilance and haste)"}
            ],
        )

        # Liliana of the Veil +1: "Each player discards a card."
        self._pw_ability_templates[("liliana of the veil", "each player discards")] = EffectTemplate(
            name="Liliana of the Veil +1",
            description="Each player discards a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "discard", "player": ctrl, "card": "worst"},
                {"action": "discard", "player": opp, "card": "worst"},
            ],
        )
        # Also match shortened text
        self._pw_ability_templates[("liliana of the veil", "discards a card")] = EffectTemplate(
            name="Liliana of the Veil +1",
            description="Each player discards a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "discard", "player": ctrl, "card": "worst"},
                {"action": "discard", "player": opp, "card": "worst"},
            ],
        )

        # Liliana of the Veil -2: "Target player sacrifices a creature."
        self._pw_ability_templates[("liliana of the veil", "sacrifices a creature")] = EffectTemplate(
            name="Liliana of the Veil -2",
            description="Target player sacrifices a creature",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "edict_sacrifice", "player": opp, "types": "creature"},
            ],
        )

        # Aminatou, the Fateshifter +1: draw one, then put one from hand on top.
        self._pw_ability_templates[("aminatou", "draw a card, then put a card")] = EffectTemplate(
            name="Aminatou +1",
            description="Draw a card, then put a card from hand on top of your library",
            action_generator=lambda ctrl, opp, ctx: self._gen_aminatou_plus1(ctrl, opp, ctx),
        )
        self._pw_ability_templates[("aminatou", "from your hand on top of your library")] = EffectTemplate(
            name="Aminatou +1",
            description="Draw a card, then put a card from hand on top of your library",
            action_generator=lambda ctrl, opp, ctx: self._gen_aminatou_plus1(ctrl, opp, ctx),
        )

        # Aminatou -1: "Exile another target permanent you own, then return it to the
        # battlefield under your control."
        # May 7 audit fix: the flicker action handler reads action["target"], not
        # action["card"] — the previous "card" key was silently ignored, so the
        # flicker always fell back to the auto-target heuristic. Also: this
        # ability requires a permanent *you own*, so refuse to fire (no_action)
        # when the controller has no legal own-permanent target, rather than
        # producing a meaningless "no creature to flicker" line after charging
        # loyalty. PlaneswalkerManager.activate() detects the no_action /
        # empty-action pattern and refunds the loyalty cost (Bug 1, May 7).
        # July 23 audit (#1): honor the AI-declared target FIRST (matches every
        # other targeted template, e.g. the Momentary Blink family at line 1646)
        # — the previous generator ignored explicit_target_name entirely and
        # always substituted its heuristic pick, so an explicitly-targeted
        # activation flickered the wrong permanent
        # (game_1529666152597426327: AI declared Animate Dead, engine flickered
        # Rick's reanimated Korvold). require_own tells the flicker handler to
        # enforce Aminatou's "permanent you own" restriction (CR 208) so a
        # controlled-but-not-owned permanent can't be flickered and kept.
        self._pw_ability_templates[("aminatou", "exile another target permanent")] = EffectTemplate(
            name="Aminatou -1 Flicker",
            description="Exile another permanent you own, then return it to the battlefield",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "flicker", "player": ctrl,
                  "target": (ctx.get('explicit_target_name')
                             or ctx.get('best_own_etb_creature')
                             or ctx.get('best_own_creature') or ''),
                  "require_own": True}]
                if (ctx.get('explicit_target_name') or ctx.get('best_own_etb_creature') or ctx.get('best_own_creature'))
                else [{"action": "no_action",
                       "reason": "Aminatou's -1 has no legal target (no other permanent you own)"}]
            ),
        )

        # May 7 audit (Bug 1): Vivien, Champion of the Wilds. She has two
        # loyalty abilities in our deck inventory:
        #
        # +1: "Until your next turn, up to one target creature gains
        #      vigilance and reach." — beneficial pump-grant on your creature.
        #      We approximate "until your next turn" with end_of_turn since
        #      that's the closest duration we model.
        # -2: "Look at the top three cards of your library. Exile one face
        #      down and put the rest on the bottom of your library in any
        #      order. For as long as it remains exiled, you may cast it if
        #      it's a creature spell." — close enough to "draw 1 creature
        #      from top 3" for autoplay purposes. We approximate as +1 card
        #      draw, which is a strict downgrade from the real ability
        #      (no creature filter) but produces a real state change so the
        #      loyalty isn't wasted.
        self._pw_ability_templates[("vivien, champion of the wilds", "vigilance and reach")] = EffectTemplate(
            name="Vivien Champion +1 Vigilance+Reach",
            description="Until end of turn, target creature you control gains vigilance and reach",
            action_generator=lambda ctrl, opp, ctx: (
                # Aug 10 audit: honour the target the engine already chose.
                # `best_own_creature` is controller_creatures[0], i.e.
                # battlefield INSERTION order, so the grant landed on the
                # oldest creature forever while [PW-TARGET] one line above
                # announced a different one (game 1536000998537953280 — the
                # two console lines contradicted each other). The Aminatou
                # template two dozen lines up already reads
                # explicit_target_name first; this is that unadopted fix.
                [{"action": "grant_keywords", "player": ctrl,
                  "target_card": (ctx.get('explicit_target_name')
                                  or ctx.get('best_own_creature', '')),
                  "keywords": ["Vigilance", "Reach"],
                  "duration": "end_of_turn",
                  "source": "Vivien, Champion of the Wilds"}]
                if (ctx.get('explicit_target_name')
                    or ctx.get('best_own_creature')) else
                [{"action": "no_action",
                  "reason": "Vivien's +1 has no legal target (no creature you control)"}]
            ),
        )
        self._pw_ability_templates[("vivien, champion of the wilds", "look at the top three")] = EffectTemplate(
            name="Vivien Champion -2 Exile Top",
            description=("Exile the top card face down; you may cast it "
                         "(top-3 selection not modelled)"),
            # Aug 10 deferred (C5): this used to resolve as a bare DRAW while
            # the activation header rendered the full printed text, so a
            # player read "exile one face down, castable if a creature" and
            # then watched an unrestricted card go to hand. The impulse
            # vocabulary now exists (exile_top_of_library + playable), so the
            # real mechanic is modelled instead of approximated: the card
            # goes to exile FACE DOWN (name withheld) and is castable.
            # Unmodelled and stated rather than hidden: the top-3 look and
            # the creature-only cast restriction.
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "exile_top_of_library", "player": ctrl, "count": 1,
                 "playable": True, "face_down": True},
            ],
        )

        # Vivien, Monsters' Advocate +1: "Create a 3/3 green Beast creature
        # token. Put your choice of a vigilance counter, a reach counter, or
        # a trample counter on it." Auto-picks trample (best beater keyword).
        self._pw_ability_templates[("vivien, monsters' advocate", "3/3 green beast")] = EffectTemplate(
            name="Vivien MA +1 Beast Token",
            description="Create a 3/3 green Beast creature token with trample",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Beast",
                 "power": 3, "toughness": 3, "types": "Creature — Beast",
                 "keywords": ["Trample"], "count": 1},
            ],
        )
        # Vivien, Monsters' Advocate -2: "When you next cast a creature spell
        # this turn, search your library for a creature card with lesser mana
        # value, put it onto the battlefield, then shuffle." We can't predict
        # the next-cast trigger; approximate as a generic tutor for any small
        # creature. Won't fire reflexively but at least it surfaces *some*
        # state change so the loyalty cost isn't wasted.
        self._pw_ability_templates[("vivien, monsters' advocate", "when you next cast")] = EffectTemplate(
            name="Vivien MA -2 Reflexive Tutor",
            description="When you next cast a creature, search library for a smaller creature (approximated as immediate tutor)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "filter_type": "creature",
                 "to_zone": "battlefield", "max_cmc": 3, "count": 1, "shuffle": True},
            ],
        )

        # Teferi, Time Raveler +1 is handled by PlaneswalkerManager's inline
        # sorcery_flash turn-effect path. Do not key it on "draw a card": that
        # text belongs to the -3 and caused the bounce rider to be skipped.

        # Teferi, Time Raveler -3: "Return up to one target artifact, creature, or
        # enchantment to its owner's hand. Draw a card."
        self._pw_ability_templates[("teferi, time raveler", "return up to one")] = EffectTemplate(
            name="Teferi T3 -3",
            description="Bounce a nonland permanent, then draw a card",
            action_generator=self._gen_teferi_time_raveler_minus3,
        )
        # Also match "return up to one target"
        self._pw_ability_templates[("teferi, time raveler", "return up to one target")] = EffectTemplate(
            name="Teferi T3 -3",
            description="Bounce a nonland permanent, then draw a card",
            action_generator=self._gen_teferi_time_raveler_minus3,
        )

        self._pw_ability_templates[("calix, destiny's hand", "look at the top four")] = EffectTemplate(
            name="Calix +1",
            description="Look at the top four; put an enchantment into hand and the rest on bottom",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "select_from_top", "player": ctrl, "amount": 4,
                 "card_type": "enchantment", "rest_to": "library_bottom"},
            ],
        )
        self._pw_ability_templates[("calix, destiny's hand", "exile target creature or enchantment")] = EffectTemplate(
            name="Calix -3",
            description="Exile an opposing creature or enchantment until your target enchantment leaves",
            action_generator=self._gen_calix_minus3,
        )

        # Teferi, Hero of Dominaria +1: "Draw a card. At the beginning of the next
        # end step, untap up to two lands."
        self._pw_ability_templates[("teferi, hero of dominaria", "draw a card")] = EffectTemplate(
            name="Teferi Hero +1",
            description="Draw a card, then untap two lands at end step",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
                {"action": "untap_lands", "player": ctrl, "count": 2},
            ],
        )

        # Jace, the Mind Sculptor 0: "Draw three cards, then put two cards from
        # your hand on top of your library in any order."
        self._pw_ability_templates[("jace, the mind sculptor", "draw three cards")] = EffectTemplate(
            name="Jace TMS 0 (Brainstorm)",
            description="Draw 3, put 2 back on top",
            action_generator=lambda ctrl, opp, ctx: self._gen_jace_brainstorm(ctrl, opp, ctx),
        )

        # May 20 audit (Bug B): Jace TMS [+2] "Look at the top card of target
        # player's library. You may put that card on the bottom of that
        # player's library." Without this template the Tier 3 dedup at
        # rules/planeswalker.py refunded every activation after the first
        # (5+ refunds/game in the May 20 batch's stagnation game).
        self._pw_ability_templates[("jace, the mind sculptor", "top card of target player")] = EffectTemplate(
            name="Jace TMS +2 (fateseal)",
            description="Look at top of target opponent's library; may bottom",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "fateseal", "target_player": opp, "amount": 1},
            ],
        )

        # Jace Beleren +2: "Each player draws a card."
        self._pw_ability_templates[("jace beleren", "each player draws")] = EffectTemplate(
            name="Jace Beleren +2",
            description="Each player draws a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
                {"action": "draw_cards", "player": opp, "amount": 1},
            ],
        )
        # Jace, Architect of Thought +1: "Until your next turn, whenever a creature
        # an opponent controls attacks, it gets -1/-0 until end of turn." Treat
        # the upside as a silent static effect for now — no immediate state change.
        self._pw_ability_templates[("jace, architect", "gets -1/-0")] = EffectTemplate(
            name="Jace AoT +1 (Minus Attack Debuff)",
            description="Silent tracker; attackers get -1/-0 until next turn",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Jace, Architect of Thought: attackers -1/-0 until your next turn (approximated)"},
            ],
        )

        # Wrenn and Six +1: "Return target land card from your graveyard to your hand."
        self._pw_ability_templates[("wrenn and six", "return target land")] = EffectTemplate(
            name="Wrenn and Six +1",
            description="Return a land card from graveyard to hand",
            action_generator=lambda ctrl, opp, ctx: self._gen_wrenn_plus1(ctrl, opp, ctx),
        )
        # Also match "land card from your graveyard"
        self._pw_ability_templates[("wrenn and six", "land card from your graveyard")] = EffectTemplate(
            name="Wrenn and Six +1",
            description="Return a land card from graveyard to hand",
            action_generator=lambda ctrl, opp, ctx: self._gen_wrenn_plus1(ctrl, opp, ctx),
        )

        # Ashiok, Dream Render -1: "Each opponent searches their library for nothing,
        # then shuffles. Exile the top four cards of target player's library."
        # Post-errata oracle: "Target player mills four cards. Then exile each
        # opponent's graveyard."
        self._pw_ability_templates[("ashiok", "exile the top four")] = EffectTemplate(
            name="Ashiok -1 Mill",
            description="Mill 4 cards from target player, then exile each opponent's graveyard",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "mill", "player": opp, "amount": 4},
                {"action": "exile_graveyard", "player": opp},
            ],
        )
        # Also match post-errata oracle text ("mills four cards")
        self._pw_ability_templates[("ashiok", "mills four")] = EffectTemplate(
            name="Ashiok -1 Mill",
            description="Mill 4 cards from target player, then exile each opponent's graveyard",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "mill", "player": opp, "amount": 4},
                {"action": "exile_graveyard", "player": opp},
            ],
        )

        # =================================================================
        # APRIL 2026 AUDIT — Templates for cards found missing in 100-game batch
        # =================================================================

        # --- Meren of Clan Nel Toth end-step trigger ---
        # "At the beginning of your end step, choose target creature card in your graveyard.
        #  If that card's power <= experience counters, return it to battlefield. Otherwise, to hand."
        # Simplified: reanimate best small creature from graveyard (approximate experience counter check)
        self._add_card("meren of clan nel toth", EffectTemplate(
            name="Meren of Clan Nel Toth",
            description="At end step, return a creature from graveyard to battlefield or hand",
            action_generator=lambda ctrl, opp, ctx: self._gen_meren_end_step(ctrl, opp, ctx),
        ))


        # --- Assassin's Trophy: destroy nonland permanent, controller searches for basic land ---
        # Aug 7 batch audit (B-1, CRITICAL): the old template hardcoded a
        # "nonland" restriction the printed card does not have ("Destroy
        # target PERMANENT an opponent controls") — cast against a lands-only
        # board it burned {B}{G} for a "No nonland permanent to target" no-op
        # (game_1535065702061318155). It also forced the ramp land in TAPPED
        # (that's Path to Exile's clause, not Trophy's) and treated the "may
        # search" as mandatory (kept: declining a free basic is almost never
        # right, documented approximation).
        def _gen_assassins_trophy(ctrl, opp, ctx):
            target = ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland')
            if not target:
                # Real card text: any permanent — fall back to an opponent
                # LAND when the board is lands-only.
                # Aug 9 adversarial review (B-2): filter like the primary
                # keys — an untargetable/phased pick made the destroy fail
                # at the action layer while the UNLINKED search still ran,
                # granting a free ramp land (the CO-4 linkage class). An
                # illegal-only board declines cleanly again.
                _ct = ctx.get('_can_target')
                _opp_pl = ctx.get('_opponent_player')
                for perm in (ctx.get('opponent_battlefield') or []):
                    nm = perm.get('name') if isinstance(perm, dict) else getattr(perm, 'name', None)
                    if not nm:
                        continue
                    if getattr(perm, '_phased_out', False):
                        continue
                    if _ct is not None and not _ct(perm, _opp_pl):
                        continue
                    target = nm
                    break
            if not target:
                return [{"action": "no_action", "reason": "Opponent controls no permanents"}]
            return [
                {"action": "destroy", "card": target},
                {"action": "search_library_land", "player": ctx.get('explicit_target_owner') or opp,
                 "basic_only": True, "enters_tapped": False},
            ]
        self._add_card("assassin's trophy", EffectTemplate(
            name="Assassin's Trophy",
            description="Destroy target permanent an opponent controls. Its controller may search for a basic land.",
            action_generator=_gen_assassins_trophy,
            needs_target=True,
        ))

        # --- Aura Shards: duplicate registration removed (working version is at line ~3142) ---

        # --- Archmage's Charm: modal spell (counter / draw 2 / steal MV<=1) ---
        # Default to draw-two when no stack target exists; counter when stack has spell
        # May 14 audit: filter self from stack_top so an empty-stack cast doesn't
        # end up "countering itself".
        # (archmage's charm: single registration lives at the charm block
        # using _gen_archmages_charm — Aug 7 registry dedup removed the
        # duplicate lambda here, which ignored the declared target.)

        # --- Mystic Confluence: modal spell (choose three, may repeat) ---
        # Modes: counter spell / draw card / bounce creature
        # Evaluate which modes have legal targets and pick 3 best instances.
        def _mystic_confluence_modal(ctrl, opp, ctx):
            actions: List[Dict] = []
            slots_remaining = 3

            # Mode 1: counter target spell (if a counterable spell is on the stack)
            stack_top = ctx.get('stack_top_spell')
            if stack_top and slots_remaining > 0:
                actions.append({
                    "action": "counter_spell",
                    "target": stack_top,
                    "controller": ctrl,
                })
                slots_remaining -= 1

            # Mode 2: bounce the best opponent creatures (up to 2, most valuable first)
            opp_creatures_info = ctx.get('_opponent_creatures', []) or []
            sorted_creatures = sorted(
                opp_creatures_info,
                key=lambda c: c.get('power', 0) if isinstance(c, dict) else 0,
                reverse=True,
            )
            bounced_names = set()
            for info in sorted_creatures:
                if slots_remaining <= 0:
                    break
                name = info.get('name') if isinstance(info, dict) else None
                if not name or name in bounced_names:
                    continue
                actions.append({
                    "action": "move_card",
                    "card": name,
                    "from_zone": "battlefield",
                    "to_zone": "hand",
                    "player": opp,
                })
                bounced_names.add(name)
                slots_remaining -= 1
                # Cap bounces at 2 so draws get at least one slot in mixed boards
                if len(bounced_names) >= 2:
                    break

            # Mode 3: fill the remainder with draws (always legal)
            if slots_remaining > 0:
                actions.append({
                    "action": "draw_cards",
                    "player": ctrl,
                    "amount": slots_remaining,
                })

            return actions

        self._add_card("mystic confluence", EffectTemplate(
            name="Mystic Confluence",
            description="Choose three (may repeat): counter target spell / draw a card / return target creature to hand",
            action_generator=_mystic_confluence_modal,
        ))

        # --- "deals N damage to each player" pattern ---
        self._add_pattern(
            r"deals? (\d+) damage to each player",
            EffectTemplate(
                name="Damage to each player",
                description="Deal damage to each player",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "deal_damage", "amount": int(ctx['_match'].group(1)), "target_player": ctrl},
                    {"action": "deal_damage", "amount": int(ctx['_match'].group(1)), "target_player": opp},
                ],
            )
        )

        # --- "deals N damage to each opponent" pattern ---
        self._add_pattern(
            r"deals? (\d+) damage to each opponent",
            EffectTemplate(
                name="Damage to each opponent",
                description="Deal damage to each opponent",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "deal_damage", "amount": int(ctx['_match'].group(1)), "target_player": opp},
                ],
            )
        )

        # --- "you lose N life" / "its controller loses N life" pattern ---
        self._add_pattern(
            r"you lose (\d+) life",
            EffectTemplate(
                name="You lose life",
                description="Controller loses life",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "lose_life", "player": ctrl, "amount": int(ctx['_match'].group(1))},
                ],
            )
        )

        # =================================================================
        # APR 6 AUDIT — Missing pattern families
        # =================================================================

        # "When ~ enters, search your library for a [type] card" (tutor-on-ETB)
        self._add_pattern(
            r"when .+? enters.*?search your library for (?:a |an )?([\w\s]+?) card",
            EffectTemplate(
                name="ETB tutor",
                description="Search library for a card when this enters",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "search_library", "player": ctrl, "count": 1,
                     "card_type": ctx['_match'].group(1).strip(),
                     "reason": f"ETB tutor: search for {ctx['_match'].group(1).strip()} card"},
                ],
            )
        )

        # "When ~ enters, return target [card/creature/instant/sorcery] from your graveyard to your hand"
        self._add_pattern(
            r"when .+? enters.*?return (?:target )?([\w\s]+?) (?:card )?from your graveyard to your hand",
            EffectTemplate(
                name="ETB graveyard recursion",
                description="Return a card from graveyard to hand when this enters",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "move_card", "card": ctx.get('best_card_in_gy', ''),
                     "from_zone": "graveyard", "to_zone": "hand", "player": ctrl},
                ],
            )
        )

        # "When ~ enters, each opponent discards a card"
        self._add_pattern(
            r"when .+? enters.*?each opponent discards (\w+) cards?",
            EffectTemplate(
                name="ETB group discard",
                description="Each opponent discards when this enters",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "discard", "player": opp, "card": "random",
                     "count": self._word_to_num(ctx['_match'].group(1))},
                ],
            )
        )

        # "When ~ enters, target opponent discards a card"
        self._add_pattern(
            r"when .+? enters.*?target (?:opponent|player) discards (\w+) cards?",
            EffectTemplate(
                name="ETB targeted discard",
                description="Target opponent discards when this enters",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "discard", "player": opp, "card": "random",
                     "count": self._word_to_num(ctx['_match'].group(1))},
                ],
            )
        )

        # "When ~ leaves the battlefield, return [exiled cards/it/them]"
        self._add_pattern(
            r"when .+? leaves the battlefield.*?return",
            EffectTemplate(
                name="LTB return exiled",
                description="When this leaves, return exiled cards",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action",
                     "reason": "LTB: exiled cards return (tracked by exile association)"},
                ],
            )
        )

        # "When ~ leaves the battlefield, [effect]" — generic LTB catch
        self._add_pattern(
            r"when .+? leaves the battlefield",
            EffectTemplate(
                name="Generic LTB trigger",
                description="Leaves-the-battlefield trigger",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action",
                     "reason": "LTB trigger (generic — specific cards should have named templates)"},
                ],
            )
        )

        # "Whenever ~ attacks, [draw/deal/create/gain]" — generic attack trigger catch
        self._add_pattern(
            r"whenever .+? attacks.*?draw (\w+) cards?",
            EffectTemplate(
                name="Attack draw trigger",
                description="Draw cards when this attacks",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl,
                     "amount": self._word_to_num(ctx['_match'].group(1))},
                ],
            )
        )

        # "At the beginning of your upkeep, [draw/lose life/create]" — generic upkeep catch
        self._add_pattern(
            r"at the beginning of your upkeep.*?draw (\w+) cards?",
            EffectTemplate(
                name="Upkeep draw trigger",
                description="Draw cards at upkeep",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl,
                     "amount": self._word_to_num(ctx['_match'].group(1))},
                ],
            )
        )

        self._add_pattern(
            r"at the beginning of your upkeep.*?lose (\d+) life",
            EffectTemplate(
                name="Upkeep lose life",
                description="Lose life at upkeep",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "lose_life", "player": ctrl, "amount": int(ctx['_match'].group(1))},
                ],
            )
        )

        self._add_pattern(
            r"at the beginning of your upkeep.*?create (\w+) (\d+)/(\d+)",
            EffectTemplate(
                name="Upkeep create token",
                description="Create token at upkeep",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "create_token", "player": ctrl,
                     "name": "Token", "power": int(ctx['_match'].group(2)),
                     "toughness": int(ctx['_match'].group(3)),
                     "types": "Creature", "count": self._word_to_num(ctx['_match'].group(1))},
                ],
            )
        )

        # "At the beginning of your end step, [draw/create/gain]" — generic end-step catch
        self._add_pattern(
            r"at the beginning of (?:your |the )?end step.*?draw (\w+) cards?",
            EffectTemplate(
                name="End step draw trigger",
                description="Draw cards at end step",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl,
                     "amount": self._word_to_num(ctx['_match'].group(1))},
                ],
            )
        )

        self._add_pattern(
            r"at the beginning of (?:your |the )?end step.*?create (\w+) (\d+)/(\d+)",
            EffectTemplate(
                name="End step create token",
                description="Create token at end step",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "create_token", "player": ctrl,
                     "name": "Token", "power": int(ctx['_match'].group(2)),
                     "toughness": int(ctx['_match'].group(3)),
                     "types": "Creature", "count": self._word_to_num(ctx['_match'].group(1))},
                ],
            )
        )

        self._add_pattern(
            r"at the beginning of (?:your |the )?end step.*?gain (\d+) life",
            EffectTemplate(
                name="End step gain life",
                description="Gain life at end step",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "gain_life", "player": ctrl, "amount": int(ctx['_match'].group(1))},
                ],
            )
        )

        # "Whenever you cast a <type> spell, create an X/X <color?> <subtype> creature
        #  token with <abilities>, where X is that spell's mana value."
        # Generic family — catches Shark Typhoon-style cast triggers beyond the
        # hardcoded Shark Typhoon template. Controller pays X so we use the
        # triggering spell's MV from ctx['spell_mv']/['cast_spell_mv'].
        def _gen_cast_xx_token(ctrl, opp, ctx):
            m = ctx['_match']
            subtype = (m.group(1) or 'Token').strip().title()
            x = ctx.get('cast_spell_mv', ctx.get('spell_mv', 0)) or 0
            if x <= 0:
                return [{"action": "no_action",
                         "reason": f"X/X {subtype} token: triggering spell MV unknown"}]
            return [{"action": "create_token", "player": ctrl, "name": subtype,
                     "power": x, "toughness": x,
                     "types": f"Creature — {subtype}", "count": 1}]
        self._add_pattern(
            r"create an x/x(?:[^,]*?)?\s+([\w\s]+?)\s+creature token(?:[^,]*)?,\s*where x is that spell'?s mana value",
            EffectTemplate(
                name="Cast trigger X/X token",
                description="Create X/X token sized by triggering spell's MV",
                action_generator=_gen_cast_xx_token,
            )
        )

        # =================================================================
        # APRIL 2026 AUDIT — Templates for !judge/!resolve prompt cards
        # These cards were generating manual prompts in autoplay because
        # no template existed. Adding them eliminates the prompts.
        # =================================================================


        # --- Temple of Mystery / Abandon / etc.: scry 1 ---
        for temple_name in ["temple of mystery", "temple of abandon", "temple of deceit",
                            "temple of enlightenment", "temple of epiphany", "temple of malady",
                            "temple of malice", "temple of plenty", "temple of silence",
                            "temple of triumph"]:
            self._add_card(temple_name, EffectTemplate(
                name=f"Temple Scry",
                description="Scry 1 when this land enters",
                action_generator=lambda ctrl, opp, ctx: self._scry_n(ctrl, 1),
            ))


        # --- Vineglimmer Snarl / check lands: reveal or enter tapped ---
        for check_land in ["vineglimmer snarl", "foreboding ruins", "choked estuary",
                           "fortified village", "port town", "game trail"]:
            self._add_card(check_land, EffectTemplate(
                name="Check land",
                description="May reveal matching land card; if not, enters tapped",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action",
                     "reason": "Check land ETB resolved (enters tapped if no matching land revealed)"},
                ],
            ))

        # =================================================================
        # ADDITIONAL MISSING TEMPLATES — from autoplay audit
        # =================================================================

        # --- Chainer, Nightmare Adept: nontoken creature enters (not from hand) → haste ---
        self._add_card("chainer, nightmare adept", EffectTemplate(
            name="Chainer, Nightmare Adept",
            description="Nontoken creature entering (not from hand) gains haste until your next turn",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "grant_keywords",
                 "card": ctx.get('entering_creature', ctx.get('entering_name', 'target')),
                 "keywords": ["Haste"]},
            ],
        ))


        # --- Cathar's Crusade: whenever a creature enters, put a +1/+1 counter
        # on EACH creature you control (not on the enchantment itself) ---
        # May 13 audit: was N name-targeted add_counters actions, but when
        # Avenger of Zendikar made 7 same-named Plant tokens (or Trostani
        # Discordant made 32 Soldier tokens) all counters collapsed onto
        # the first match. Use the bulk add_counters target instead so each
        # creature is hit by identity.
        def _cathars_crusade_gen(ctrl, opp, ctx):
            creatures = ctx.get('_controller_creatures', [])
            if not creatures:
                return [{"action": "no_action",
                         "reason": "Cathar's Crusade: no creatures to put counters on"}]
            return [{
                "action": "add_counters",
                "player": ctrl,
                "target": "all_own_creatures",
                "counter_type": "+1/+1",
                "amount": 1,
                "source": "Cathar's Crusade",
            }]

        # NOTE: card name is "Cathars' Crusade" (possessive plural, apostrophe
        # AFTER the s). The card-key lookup is .lower().strip(), so register
        # both spellings — autoplay logs have shown both in the wild.
        self._add_card("cathars' crusade", EffectTemplate(
            name="Cathars' Crusade",
            description="Whenever a creature enters under your control, put a +1/+1 counter on each creature you control",
            action_generator=_cathars_crusade_gen,
        ))
        self._add_card("cathar's crusade", EffectTemplate(
            name="Cathars' Crusade",
            description="Whenever a creature enters under your control, put a +1/+1 counter on each creature you control",
            action_generator=_cathars_crusade_gen,
        ))

        # Extraction Specialist — ETB returns a low-CMC creature from graveyard
        # to the battlefield (can't attack/block this turn). Flagged as the
        # only [ETB-UNHANDLED] in the May 16 batch.
        def _extraction_specialist_gen(ctrl, opp, ctx):
            gy = ctx.get('controller_graveyard', []) or []
            candidates = []
            for c in gy:
                if 'creature' in (getattr(c, 'type_line', '') or '').lower():
                    cmc = getattr(c, 'cmc', 0) or 0
                    if cmc <= 2:
                        candidates.append((cmc, c.name))
            if not candidates:
                return [{"action": "no_action",
                         "reason": "Extraction Specialist: no MV-2 creature in graveyard"}]
            # Prefer highest CMC (more value)
            candidates.sort(key=lambda x: -x[0])
            return [{
                "action": "move_card",
                "card": candidates[0][1],
                "from_zone": "graveyard",
                "to_zone": "battlefield",
                "player": ctrl,
            }]

        self._add_card("extraction specialist", EffectTemplate(
            name="Extraction Specialist",
            description="When Extraction Specialist enters, return target creature card with mana value 2 or less from your graveyard to the battlefield",
            action_generator=_extraction_specialist_gen,
        ))


        # --- Nightshade Assassin: ETB reveal X black cards to -X/-X ---
        # "When this creature enters, you may reveal X black cards in your
        # hand. If you do, target creature gets -X/-X until end of turn."
        # X = how many black cards in the controller's hand. Apply as a
        # -X/-X pump on the best (highest power) opponent creature. The
        # template doesn't actually move cards; the reveal is hidden info,
        # but the strategic effect (kill / shrink the threat) is captured.
        def _nightshade_assassin_gen(ctrl, opp, ctx):
            hand = ctx.get('controller_hand', []) or []
            black_count = 0
            for c in hand:
                if not c:
                    continue
                colors = getattr(c, 'colors', []) or getattr(c, 'color_identity', []) or []
                if 'B' in colors or (c.mana_cost and 'B' in c.mana_cost.upper()):
                    black_count += 1
            target = ctx.get('best_opponent_creature')
            if not target or black_count <= 0:
                return [{"action": "no_action",
                         "reason": "Nightshade Assassin: no black cards to reveal or no target"}]
            # We model the "-X/-X until end of turn" as N permanent -1/-1
            # counters on the target. Stronger than the printed effect (real
            # card wears off end-of-turn; counters persist) but the strategic
            # use case is "kill the threat" which both flavors accomplish.
            return [{
                "action": "add_counters",
                "card": target,
                "counter_type": "-1/-1",
                "amount": black_count,
                "source": "Nightshade Assassin",
            }]

        self._add_card("nightshade assassin", EffectTemplate(
            name="Nightshade Assassin",
            description="Reveal X black cards from hand: target creature gets -X/-X",
            action_generator=_nightshade_assassin_gen,
        ))


        # --- Luminarch Aspirant: at the beginning of combat on your turn,
        # put a +1/+1 counter on target creature you control ---
        def _luminarch_aspirant_gen(ctrl, opp, ctx):
            # Pick the best own creature to put the counter on
            creatures = ctx.get('_controller_creatures', [])
            if not creatures:
                return [{"action": "no_action",
                         "reason": "Luminarch Aspirant: no creatures to target"}]
            # Pick the creature with highest power (best target for aggro)
            best = max(creatures, key=lambda c: c.get('power', 0))
            return [{"action": "add_counters", "card": best['name'],
                     "counter_type": "+1/+1", "amount": 1}]

        self._add_card("luminarch aspirant", EffectTemplate(
            name="Luminarch Aspirant",
            description="At the beginning of combat on your turn, put a +1/+1 counter on target creature you control",
            action_generator=_luminarch_aspirant_gen,
        ))

        # --- Ophiomancer: at the beginning of each upkeep, if you control no Snakes,
        # create a 1/1 black Snake creature token with deathtouch ---
        def _ophiomancer_gen(ctrl, opp, ctx):
            battlefield = ctx.get('controller_battlefield', []) or []
            has_snake = any(
                permanent.is_creature(game=ctx.get('_game'))
                and 'snake' in {t.lower() for t in permanent.get_creature_types()}
                for permanent in battlefield
            )
            if has_snake:
                return [{"action": "no_action",
                         "reason": "Ophiomancer: controller already controls a Snake"}]
            return [make_token_action(ctrl, "snake_1_1_dt", 1)]

        self._add_card("ophiomancer", EffectTemplate(
            name="Ophiomancer",
            description="At the beginning of each upkeep, if you control no Snakes, create a 1/1 black Snake token with deathtouch",
            action_generator=_ophiomancer_gen,
        ))


        # =====================================================================
        # APR 6 AUDIT FIXES — New templates to eliminate Tier 3 API calls
        # =====================================================================


        # Fix 9: Merciless Eviction — exile all of a chosen type
        self._add_card("merciless eviction", EffectTemplate(
            name="Merciless Eviction",
            description="Choose one: exile all artifacts, creatures, enchantments, or planeswalkers",
            action_generator=lambda ctrl, opp, ctx: self._gen_merciless_eviction(ctrl, opp, ctx),
        ))

        # Fix 11: Agent of Treachery end-step (draw 3 if you control 3+ stolen permanents)
        self._add_card("agent of treachery endstep", EffectTemplate(
            name="Agent of Treachery",
            description="At end step, if you control 3+ permanents you don't own, draw 3 cards",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 3},
            ] if ctx.get('controller_stolen_count', 0) >= 3 else [
                {"action": "no_action", "reason": "Agent of Treachery: control fewer than 3 stolen permanents"},
            ],
        ))

        # Fix 13: Rampaging Ferocidon — creature enters → 1 damage to controller
        self._add_card("rampaging ferocidon", EffectTemplate(
            name="Rampaging Ferocidon",
            description="Whenever another creature enters, Rampaging Ferocidon deals 1 damage to that creature's controller",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 1,
                 "target_player": ctx.get('entering_creature_controller', opp)},
            ],
        ))

        # Fix 14: Blind Obedience / Extort — extort is an OPTIONAL "you may pay
        # {W/B}" trigger (CR 702.99a). The previous template fired the drain
        # unconditionally, treating "may" as "always". Now we check the
        # controller has at least one untapped W or B source AND deduct one
        # mana from the pool / treat one source as tapped. If neither is
        # available, the trigger fires but does nothing (per "If you do…").
        def _extort_gen(ctrl, opp, ctx):
            ctrl_player = ctx.get('_controller_player')
            if ctrl_player is None:
                return [{"action": "no_action", "reason": "Extort: no controller in ctx"}]
            can_pay, _ = ctrl_player.can_pay_mana_cost('{W/B}')
            if can_pay and ctrl_player.tap_sources_for_cost('{W/B}'):
                return [{"action": "extort_drain", "player": ctrl,
                         "opponent": opp, "amount": 1, "source": "Extort"}]
            return [{"action": "no_action",
                     "reason": "Extort: no untapped W or B source to pay optional cost"}]
            # Legacy source-by-source payment code remains below unreachable;
            # all live payment now uses the color-aware mana engine above.
            # Check mana pool first (already-floating mana)
            pool = getattr(ctrl_player, 'mana_pool', {}) or {}
            pool_wb = pool.get('W', 0) + pool.get('B', 0)
            if pool_wb >= 1:
                # Deduct from pool — prefer B then W (cheaper opportunity cost
                # in Orzhov decks since W mana is usually needed for spells).
                if pool.get('B', 0) >= 1:
                    pool['B'] -= 1
                else:
                    pool['W'] -= 1
                return [
                    {"action": "lose_life", "player": opp, "amount": 1, "source": "Extort"},
                    {"action": "gain_life", "player": ctrl, "amount": 1, "source": "Extort"},
                ]
            # No floating WB — check untapped W or B sources on battlefield.
            for perm in ctrl_player.battlefield:
                if getattr(perm, 'tapped', False):
                    continue
                production = None
                try:
                    production = ctrl_player._get_mana_production(perm) or {}
                except Exception:
                    production = {}
                if production.get('W', 0) >= 1 or production.get('B', 0) >= 1:
                    perm.tapped = True
                    return [
                        {"action": "lose_life", "player": opp, "amount": 1, "source": "Extort"},
                        {"action": "gain_life", "player": ctrl, "amount": 1, "source": "Extort"},
                    ]
            return [{"action": "no_action",
                     "reason": "Extort: no untapped W or B source to pay optional cost"}]

        self._add_card("blind obedience", EffectTemplate(
            name="Blind Obedience",
            description="Extort: whenever you cast a spell, you may pay {W/B}. If you do, each opponent loses 1 life and you gain 1 life for each opponent",
            action_generator=_extort_gen,
        ))

        # June 10 audit (template backlog): "look at the top N cards … put
        # them back in any order" reorder family (Thassa's Oracle ETB,
        # Sphinx variants) — these escalated to Tier 3, burned tokens, and
        # came back with no state change. Library order IS the current
        # order; resolve as an explicit no-op whose reason stays console-only
        # (matches the _INTERNAL_NOOP_PATTERNS suppression list).
        self._add_pattern(
            r"look at the top \w+ cards? of your library.*?"
            r"(?:put (?:them|the rest|it)|in any order)",
            EffectTemplate(
                name="Library Look / Reorder",
                description="Look at top cards and reorder",
                # Returning None (not []) when there's more to the card means
                # "not mine" — resolve_etb keeps scanning and the spell reaches
                # Tier 2/3 with its other clauses intact. Notion Rain matched
                # here on its own surveil reminder text and lost both "draw two
                # cards" and 2 damage to its controller.
                action_generator=lambda ctrl, opp, ctx: (
                    None if has_residual_clause_beyond_library_look(ctx.get('_oracle', ''))
                    else [{"action": "no_action",
                           "reason": "library order not modeled — keeping current order"}]
                )
            )
        )

        # June 10 audit (V27): extort as a KEYWORD pattern. It was registered
        # only under "blind obedience", so Crypt Ghast's extort was announced,
        # pushed on the stack per CR 603.2, then discarded unexecuted. Any
        # extort card now routes to the same payment-checking generator.
        self._add_pattern(
            r"\bextort\b",
            EffectTemplate(
                name="Extort (keyword)",
                description="Extort: pay {W/B} to drain 1 from each opponent",
                action_generator=_extort_gen,
            )
        )

        # Fix 15: Charming Prince — modal ETB
        self._add_card("charming prince", EffectTemplate(
            name="Charming Prince",
            description="When Charming Prince enters, choose one: scry 2; gain 3 life; exile another creature you control, return it at end step",
            action_generator=lambda ctrl, opp, ctx: self._gen_charming_prince(ctrl, opp, ctx),
        ))

        # Fix 16: March of the Multitudes — create X tokens
        self._add_card("march of the multitudes", EffectTemplate(
            name="March of the Multitudes",
            description="Create X 1/1 white Soldier creature tokens with lifelink",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Soldier",
                 "power": 1, "toughness": 1, "types": "Creature — Soldier",
                 "count": max(1, ctx.get('x_value', 1)), "keywords": "lifelink"},
            ],
        ))

        # June 11 audit: Hammer's former no_action claimed auto-equip was
        # "handled by equipment system", but no such watcher existed and the
        # internal sentence leaked to Discord. Its own ETB is resolved here;
        # later Equipment entries are handled by
        # mtg.triggers._check_equipment_etb_watchers.
        def _gen_hammer_of_nazahn(ctrl, opp, ctx):
            creature = ctx.get('best_own_creature', '')
            if not creature:
                return [{"action": "no_action",
                         "reason": "Hammer of Nazahn enters, but there is no creature you control to attach it to"}]
            return [{"action": "equip", "equipment": "Hammer of Nazahn",
                     "creature": creature, "player": ctrl}]

        self._add_card("hammer of nazahn", EffectTemplate(
            name="Hammer of Nazahn",
            description="Whenever Hammer of Nazahn or another Equipment you control enters, you may attach that Equipment to target creature you control",
            action_generator=_gen_hammer_of_nazahn,
        ))

        # Fix 18: Bloodchief Ascension end-step condition
        # June 11 audit: the old template conflated Bloodchief's two separate
        # abilities and drained 2 at end step. Oracle says the end-step trigger
        # adds a quest counter; the drain belongs to an opponent-graveyard
        # event and is not conditional on life lost that turn.
        self._add_card("bloodchief ascension", EffectTemplate(
            name="Bloodchief Ascension",
            description="At the beginning of each end step, if an opponent lost 2 or more life this turn, put a quest counter on Bloodchief Ascension",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "add_counters", "card": "Bloodchief Ascension",
                 "player": ctrl, "counter_type": "quest", "amount": 1,
                 "source": "Bloodchief Ascension"},
            ] if ctx.get('opponent_life_lost_this_turn', 0) >= 2 else [
                {"action": "no_action", "reason": "Bloodchief Ascension: opponent didn't lose 2+ life this turn"},
            ],
        ))


        # Fix 20: Nevermaker LTB — put nonland permanent on top of library
        self._add_card("nevermaker", EffectTemplate(
            name="Nevermaker",
            description="When Nevermaker leaves the battlefield, put target nonland permanent on top of its owner's library",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card",
                 "card": ctx.get('best_opponent_nonland', ctx.get('best_opponent_creature', '')),
                 "from_zone": "battlefield", "to_zone": "library", "position": "top",
                 "player": opp},
            ],
            needs_target=True,
        ))


        self._add_card("mnemonic wall", EffectTemplate(
            name="Mnemonic Wall",
            description="When Mnemonic Wall enters, return target instant or sorcery from your graveyard to your hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card",
                 "card": ctx.get('best_instant_sorcery_in_gy', ''),
                 "from_zone": "graveyard", "to_zone": "hand", "player": ctrl},
            ],
        ))


        # --- Remaining !judge prompt eliminators ---

        # Phylath, World Sculptor — ETB: create Plant tokens for each basic land
        self._add_card("phylath, world sculptor", EffectTemplate(
            name="Phylath, World Sculptor",
            description="When Phylath enters, create a 0/1 green Plant creature token for each basic land you control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Plant",
                 "power": 0, "toughness": 1, "types": "Creature — Plant",
                 "count": max(1, ctx.get('controller_basic_land_count', 3))},
            ],
        ))

        # Roil Elemental — landfall: gain control of target creature
        self._add_card("roil elemental", EffectTemplate(
            name="Roil Elemental",
            description="Landfall — Gain control of target creature",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "steal_permanent", "player": ctrl, "from_player": opp,
                 "card": ctx.get('best_opponent_creature', ''),
                 "source": "Roil Elemental", "until_source_leaves": True},
            ],
            needs_target=True,
        ))

        # Platoon Dispenser — end step: draw if 3+ creatures
        self._add_card("platoon dispenser", EffectTemplate(
            name="Platoon Dispenser",
            description="At end of your turn, if you control 3+ creatures, draw a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ] if ctx.get('controller_creature_count', 0) >= 3 else [
                {"action": "no_action", "reason": "Platoon Dispenser: fewer than 3 creatures"},
            ],
        ))


        # Bug 16: Nahiri's Lithoforming — X-cost land replacement
        self._add_card("nahiri's lithoforming", EffectTemplate(
            name="Nahiri's Lithoforming",
            description="Sacrifice X lands, then draw X cards. You may play X additional lands this turn.",
            action_generator=lambda ctrl, opp, ctx: self._gen_nahiris_lithoforming(ctrl, opp, ctx),
        ))

        # Bug 27: Wrenn and Seven [+1] — reveal top 4, lands to hand, rest to graveyard
        # July 31 batch-11 (brawl reviewer): Wrenn and Seven was a bare
        # name-keyed _add_card whose flat draw-2 was BOTH wrong for the +1
        # (real text: reveal 4, lands to hand, rest to graveyard) AND — the
        # game-visible bug — matched by resolve_pw_ability's resolve_etb
        # fallthrough for EVERY ability, so the [0] "put lands from hand"
        # activation drew 2 cards instead (game_1532532200061403350). PW
        # abilities must live in _pw_ability_templates, keyed by ability
        # snippet (the Wrenn and Six pattern); the bare key is deleted.
        self._pw_ability_templates[("wrenn and seven", "reveal the top four")] = EffectTemplate(
            name="Wrenn and Seven +1",
            description="Reveal top 4: land cards to hand, the rest to graveyard",
            action_generator=lambda ctrl, opp, ctx: self._gen_w7_plus1(ctrl, opp, ctx),
        )
        self._pw_ability_templates[("wrenn and seven", "land cards from your hand onto the battlefield")] = EffectTemplate(
            name="Wrenn and Seven 0",
            description="Put any number of land cards from your hand onto the battlefield tapped",
            action_generator=lambda ctrl, opp, ctx: self._gen_w7_zero(ctrl, opp, ctx),
        )
        self._pw_ability_templates[("wrenn and seven", "green treefolk creature token")] = EffectTemplate(
            name="Wrenn and Seven -3",
            description="Create a green Treefolk with reach, P/T = lands you control",
            action_generator=lambda ctrl, opp, ctx: self._gen_w7_minus3(ctrl, opp, ctx),
        )


        # Bug 29: Embercleave — attach to attacking creature
        self._add_card("embercleave", EffectTemplate(
            name="Embercleave",
            description="When Embercleave enters, attach it to target creature you control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "equip", "equipment": "Embercleave",
                 "creature": ctx.get('best_own_creature', ''),
                 "player": ctrl},
            ],
        ))

        # Mantle of the Ancients — return all Auras/Equipment from GY to battlefield.
        # The engine's auto-attach logic handles attaching them to the enchanted creature
        # when they enter via a non-cast path (move_card graveyard→battlefield).
        def _gen_mantle_of_the_ancients(ctrl, opp, ctx):
            gy = ctx.get('controller_graveyard', [])
            actions = []
            for card in gy:
                tl = getattr(card, 'type_line', '') or ''
                tl_lower = tl.lower()
                if 'aura' in tl_lower or 'equipment' in tl_lower:
                    actions.append({
                        "action": "move_card",
                        "card": card.name,
                        "from_zone": "graveyard",
                        "to_zone": "battlefield",
                        "player": ctrl,
                    })
            if actions:
                return actions
            return [{"action": "no_action", "reason": "Mantle of the Ancients: no Auras or Equipment in graveyard"}]

        self._add_card("mantle of the ancients", EffectTemplate(
            name="Mantle of the Ancients",
            description="Return all Aura and Equipment cards from graveyard to battlefield (auto-attached)",
            action_generator=_gen_mantle_of_the_ancients,
        ))


        # Heavenly Blademaster — ETB attach trigger.
        # "When Heavenly Blademaster enters, attach any number of Auras and
        # Equipment you control to it."
        # The static "+1/+1 for each attached" is layered, NOT a counter — emit
        # only attach actions so the AI doesn't add bogus permanent +1/+1
        # counters on top of the layer-based pump.
        def _gen_heavenly_blademaster(ctrl, opp, ctx):
            bf = ctx.get('controller_battlefield', [])
            actions = []
            for perm in bf:
                tl = ''
                pname = ''
                if isinstance(perm, dict):
                    tl = (perm.get('type_line') or perm.get('type') or '').lower()
                    pname = perm.get('name', '')
                else:
                    tl = (getattr(perm, 'type_line', '') or '').lower()
                    pname = getattr(perm, 'name', '')
                if not pname or pname == 'Heavenly Blademaster':
                    continue
                if 'aura' in tl or 'equipment' in tl:
                    actions.append({
                        "action": "equip", "equipment": pname,
                        "creature": "Heavenly Blademaster", "player": ctrl,
                    })
            return actions
        self._add_card("heavenly blademaster", EffectTemplate(
            name="Heavenly Blademaster",
            description="When it enters, attach any number of your Auras/Equipment to it (static +1/+1 is layered, NOT counters)",
            action_generator=_gen_heavenly_blademaster,
        ))

        # Realm-Scorcher Hellkite — ETB: deal 5 damage to target creature an
        # opponent controls. (The bargained mode lets you divide 5 damage
        # among any targets; we approximate by prioritising the opponent's
        # best creature, falling back to the opponent directly.)
        def _realm_scorcher_gen(ctrl, opp, ctx):
            target = ctx.get('best_opponent_creature')
            if target:
                return [
                    {"action": "deal_damage", "amount": 5,
                     "target_card": target, "target_controller": opp,
                     "source": "Realm-Scorcher Hellkite",
                     "description": f"Realm-Scorcher Hellkite deals 5 damage to {target}"},
                ]
            return [
                {"action": "deal_damage", "amount": 5,
                 "target_player": opp,
                 "source": "Realm-Scorcher Hellkite",
                 "description": f"Realm-Scorcher Hellkite deals 5 damage to {opp}"},
            ]
        self._add_card("realm-scorcher hellkite", EffectTemplate(
            name="Realm-Scorcher Hellkite",
            description="When it enters, deals 5 damage to target creature an opponent controls (or divided if bargained)",
            action_generator=_realm_scorcher_gen,
            needs_target=True,
        ))

        # =================================================================
        # APR 11 AUDIT FIXES — New templates to eliminate Tier 3 API calls
        # =================================================================

        # --- Fix 1: Blizzard Brawl — indestructible THEN fight (correct oracle order) ---
        # Oracle: "Target creature you control gains indestructible until end of turn.
        #          Then it fights target creature you don't control."
        # Bug: previous version applied fight first, then indestructible — creature died
        # before getting protection. Fixed: grant indestructible first, THEN fight.
        def _blizzard_brawl_gen(ctrl, opp, ctx):
            source_name = ctx.get('_source_card_name') or ctx.get('best_own_creature', '')
            source_power = ctx.get('greatest_power', ctx.get('best_own_creature_power', 3))
            target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')
            target_power = resolve_target_power(ctx, target)
            if not source_name or not target:
                return [{"action": "no_action",
                         "reason": "Blizzard Brawl: need a creature you control and a target"}]
            return [
                # Step 1: grant indestructible first (oracle order matters)
                {"action": "grant_keywords", "card": source_name,
                 "keywords": ["Indestructible"], "duration": "end_of_turn"},
                # Step 2: fight (mutual damage)
                {"action": "deal_damage", "amount": source_power,
                 "target_card": target, "target_controller": opp},
                {"action": "deal_damage", "amount": target_power,
                 "target_card": source_name, "target_controller": ctrl},
            ]

        self._add_card("blizzard brawl", EffectTemplate(
            name="Blizzard Brawl",
            description="Target creature you control gains indestructible until end of turn. Then it fights target creature you don't control.",
            action_generator=_blizzard_brawl_gen,
            needs_target=True,
        ))

        # --- Fix 2: Upkeep trigger templates ---
        # These fire every upkeep but previously escalated to !resolve prompts
        # because no template existed. Added in oracle text order.


        # Emeria, the Sky Ruin — at upkeep, if you control 7+ Plains, may return
        # a creature from graveyard to battlefield. Now actually checks the count
        # so the upkeep doesn't spam "use !resolve" every turn when the condition
        # is not met.
        def _emeria_gen(ctrl, opp, ctx):
            bf = ctx.get('controller_battlefield') or ctx.get('battlefield', [])
            plains_count = 0
            for p in bf:
                # Each entry may be a dict or a card-like object
                type_line = ''
                subtypes = []
                if isinstance(p, dict):
                    type_line = (p.get('type_line') or p.get('type') or '').lower()
                    subtypes = [s.lower() for s in (p.get('subtypes') or [])]
                else:
                    type_line = (getattr(p, 'type_line', '') or '').lower()
                    subtypes = [s.lower() for s in (getattr(p, 'subtypes', []) or [])]
                if 'plains' in type_line or 'plains' in subtypes:
                    plains_count += 1
            if plains_count < 7:
                # Silent no-op — don't emit Discord noise every upkeep when the
                # condition can't fire. Print a console trace so autoplay logs
                # show the template IS being reached (not escalating to Tier 3).
                print(f"[ETB-TEMPLATE] Emeria, the Sky Ruin: silent (need 7 Plains, have {plains_count})")
                return []
            gy_creatures = ctx.get('controller_graveyard_creatures', [])
            if not gy_creatures:
                print(f"[ETB-TEMPLATE] Emeria, the Sky Ruin: silent (no creatures in graveyard)")
                return []
            return [
                {"action": "move_card", "card": gy_creatures[0],
                 "from_zone": "graveyard", "to_zone": "battlefield", "player": ctrl},
            ]
        self._add_card("emeria, the sky ruin", EffectTemplate(
            name="Emeria, the Sky Ruin",
            description="At beginning of upkeep, if you control 7+ Plains, may return creature from graveyard",
            action_generator=_emeria_gen,
        ))


        # Sylvan Library — at upkeep, draw 2 extra cards, then choose two of
        # the cards drawn this turn (other than from this) and put them on
        # top of your library, OR pay 4 life per card kept. For autoplay,
        # we approximate: keep one card when above 30 life (typical commander
        # line); below 30 life, return 2 to top via reorder_library so the
        # top of library still reflects useful cards instead of being a
        # no-op (May 24 audit — was emitting "library order not modeled").
        def _sylvan_library_gen(ctrl, opp, ctx):
            ctrl_life = ctx.get('controller_life', 40)
            # Free in commander when above 30 life — typical line is keep one card.
            if ctrl_life >= 30:
                return [
                    {"action": "draw_cards", "player": ctrl, "amount": 1},
                    {"action": "lose_life", "player": ctrl, "amount": 4},
                ]
            # Otherwise net 0: draw 2 and reorder top 2 of library so the
            # next two draws are useful (lands distributed, spells curve-ordered).
            return [
                {"action": "draw_cards", "player": ctrl, "amount": 2},
                {"action": "put_back_from_hand", "player": ctrl, "count": 2,
                 "reason": "Sylvan Library: return 2 to top instead of paying 4 life each"},
            ]
        self._add_card("sylvan library", EffectTemplate(
            name="Sylvan Library",
            description="At upkeep, draw 2; pay 4 life to keep each, else return 2 to top of library",
            action_generator=_sylvan_library_gen,
        ))

        # Inventors' Fair — at upkeep, IF you control 3+ artifacts, gain 1 life
        # Oracle: "At the beginning of your upkeep, if you control three or more artifacts, you gain 1 life."
        def _inventors_fair_gen(ctrl, opp, ctx):
            # Aug 9 audit (CO-1): the old branches read ctx['battlefield']
            # and ctx['controller_artifact_count'] — NEITHER key has a
            # producer anywhere in the tree, so artifact_count was ALWAYS 0
            # and the upkeep life-gain could never fire (Mox Tantalite on
            # the battlefield, "only 0 artifact(s)" every upkeep,
            # game_1535586432385687572). Count from controller_battlefield
            # (real Card objects, populated by build_game_context),
            # mirroring the WORKING tutor gate in mtg/helpers.py
            # activated_ability_restriction_failure exactly.
            artifact_count = sum(
                1 for c in (ctx.get('controller_battlefield') or [])
                if 'artifact' in (getattr(c, 'type_line', '') or '').lower()
                and not getattr(c, '_phased_out', False))
            if artifact_count < 3:
                return [{"action": "no_action",
                         "reason": f"Inventors' Fair: only {artifact_count} artifact(s) — needs 3+"}]
            return [{"action": "gain_life", "player": ctrl, "amount": 1}]

        self._add_card("inventors' fair", EffectTemplate(
            name="Inventors' Fair",
            description="At the beginning of your upkeep, if you control 3+ artifacts, gain 1 life",
            action_generator=_inventors_fair_gen,
        ))


        # Keeper of the Accord — "At the beginning of each opponent's upkeep,
        # if that player controls more creatures than you, create a 1/1 white
        # Soldier creature token. Then if they control more lands than you,
        # search your library for a basic Plains, put it onto the battlefield
        # tapped, then shuffle."
        # Controller uses "each opponent" upkeep path (opp.name is Keeper's
        # controller; ctrl passed here is the upkeep's active player).
        def _keeper_of_the_accord_gen(ctrl, opp, ctx):
            # ctrl = active upkeep player, opp = Keeper's controller (by the
            # cross-player upkeep dispatch; confirm with controller_* keys).
            # Fire only during an opponent's upkeep (ctrl != keeper controller).
            keeper_controller = ctx.get('keeper_controller') or opp
            upkeep_player = ctrl
            if upkeep_player == keeper_controller:
                return []  # Only triggers on OPPONENT's upkeep
            keeper_creature_count = ctx.get('controller_creature_count', 0)
            opp_creature_count = ctx.get('opponent_creature_count', 0)
            keeper_land_count = ctx.get('controller_land_count', 0)
            opp_land_count = ctx.get('opponent_land_count', 0)
            actions: List[Dict] = []
            # Note: "controller_*" here is the active upkeep player (ctrl), and
            # "opponent_*" is Keeper's controller (keeper_controller=opp). So we
            # compare upkeep player's board vs. Keeper's controller's board.
            # "that player (upkeep) controls more creatures than you (keeper)"
            if keeper_creature_count > opp_creature_count:
                actions.append({
                    "action": "create_token", "player": keeper_controller,
                    "name": "Soldier", "power": 1, "toughness": 1,
                    "types": "Creature — Soldier", "count": 1,
                })
            if keeper_land_count > opp_land_count:
                actions.append({
                    "action": "search_library_land", "player": keeper_controller,
                    "basic_only": True, "enters_tapped": True,
                    "land_type": "Plains",
                })
            return actions

        self._add_card("keeper of the accord", EffectTemplate(
            name="Keeper of the Accord",
            description="Opponent upkeep: if they have more creatures, make a 1/1 Soldier; more lands, fetch a Plains",
            action_generator=_keeper_of_the_accord_gen,
        ))


        self._add_pattern(
            r"for each creature token you control, create a token that's a copy of that creature",
            EffectTemplate(
                name="Double creature tokens",
                description="Create a copy of each creature token you control",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "double_creature_tokens", "player": ctrl},
                ],
            ),
        )


        # --- Fix 3: Spell templates (cards showing 'Complex effect:' with no outcome) ---

        # Reanimate — put target creature from any graveyard onto battlefield; lose life = MV
        def _reanimate_gen(ctrl, opp, ctx):
            # June 10 deep-dive (B10a): resolve the ACTUAL card object so the
            # life payment equals its real mana value — the old path charged
            # a fallback constant 5 (one {B} + 5 life for a 2-MV Sakura) and
            # its raw move_card half could silently no-op on an owner
            # mismatch, returning nothing for the cost. Uses the `reanimate`
            # action (searches every graveyard, resets battlefield state).
            ctrl_player = ctx.get('_controller_player')
            opp_player = ctx.get('_opponent_player')
            explicit = (ctx.get('explicit_target_name') or '').strip().lower()
            chosen = None
            for _want_explicit in ((True,) if explicit else (False,)):
                for pl in (ctrl_player, opp_player):
                    if pl is None:
                        continue
                    for c in pl.graveyard:
                        tl = (getattr(c, 'type_line', '') or '').lower()
                        if 'creature' not in tl:
                            continue
                        if _want_explicit:
                            if (c.name or '').lower() == explicit:
                                chosen = c
                                break
                        elif chosen is None or (getattr(c, 'cmc', 0) or 0) > (getattr(chosen, 'cmc', 0) or 0):
                            chosen = c
                    if _want_explicit and chosen is not None:
                        break
                if chosen is not None:
                    break
            if explicit and chosen is None:
                return [{"action": "no_action",
                         "reason": ("Reanimate: declared target is not a "
                                    "creature card in a graveyard")}]

            if chosen is None:
                _name_fallback = ctx.get('best_graveyard_creature', '')
                if _name_fallback:
                    return [
                        {"action": "reanimate", "player": ctrl, "card": _name_fallback},
                        {"action": "lose_life", "player": ctrl, "amount": 1,
                         "source": "Reanimate"},
                    ]
                return [{"action": "no_action",
                         "reason": "Reanimate: no creature card found in any graveyard"}]
            mv = int(getattr(chosen, 'cmc', 0) or 0)
            return [
                {"action": "reanimate", "player": ctrl, "card": chosen.name,
                 "reason": "Reanimate returns it under your control"},
                {"action": "lose_life", "player": ctrl, "amount": max(1, mv),
                 "source": "Reanimate"},
            ]

        self._add_card("reanimate", EffectTemplate(
            name="Reanimate",
            description="Put target creature from any graveyard onto battlefield under your control. Lose life equal to its MV.",
            action_generator=_reanimate_gen,
        ))

        # White Sun's Zenith — create X 2/2 white Cat tokens; shuffle back into library
        self._add_card("white sun's zenith", EffectTemplate(
            name="White Sun's Zenith",
            description="Create X 2/2 white Cat creature tokens. Shuffle White Sun's Zenith into its owner's library.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Cat",
                 "power": 2, "toughness": 2, "types": "Creature — Cat",
                 "count": max(1, ctx.get('x_value', 3))},
                {"action": "move_card", "card": "White Sun's Zenith",
                 "from_zone": "stack", "to_zone": "library", "player": ctrl},
            ],
        ))

        # Increasing Vengeance — copy target instant or sorcery (stack copying not modeled).
        # If no valid copy target exists (i.e. empty stack or no own instant/sorcery spell
        # resolving), the spell fizzles. We can't simulate the copy itself, so we emit a
        # specific narration describing why nothing happened so Discord output is clear.
        def _increasing_vengeance_gen(ctrl, opp, ctx):
            stack_has_own_is = bool(ctx.get('stack_top_is_creature') is False
                                    and ctx.get('stack_top_type_known'))
            msg = ("Increasing Vengeance resolves but stack copying isn't modeled — "
                   "no duplicate was made") if stack_has_own_is else \
                  ("Increasing Vengeance fizzles — no instant or sorcery you control "
                   "on the stack to copy")
            return [{"action": "no_action", "reason": msg}]
        self._add_card("increasing vengeance", EffectTemplate(
            name="Increasing Vengeance",
            description="Copy target instant or sorcery spell you control (twice if cast from graveyard)",
            action_generator=_increasing_vengeance_gen,
        ))

        # Earthquake — deal X damage to each creature without flying and each player
        def _earthquake_gen(ctrl, opp, ctx):
            x = ctx.get('x_value', 0)
            if x <= 0:
                return [{"action": "no_action", "reason": "Earthquake: X=0, no damage dealt"}]
            return [
                {"action": "deal_damage", "amount": x, "target_player": ctrl},
                {"action": "deal_damage", "amount": x, "target_player": opp},
                # Non-flying creatures take X damage — approximate via pump_all_creatures
                # (negative toughness adjustment triggers SBA death checking)
                {"action": "damage_non_flying_creatures", "amount": x,
                 "reason": f"Earthquake deals {x} to each creature without flying"},
            ]

        self._add_card("earthquake", EffectTemplate(
            name="Earthquake",
            description="Deal X damage to each creature without flying and each player",
            action_generator=_earthquake_gen,
        ))

        # =====================================================================
        # WEREWOLF / DAY-NIGHT TRANSFORM UPKEEP TEMPLATES
        # The day/night mechanic requires tracking how many spells were cast
        # last turn, which isn't tracked in the current game state.  These
        # templates return no_action with a descriptive message so the player
        # knows what the trigger is waiting for instead of seeing a bare
        # "!resolve" prompt.  The general regex pattern below catches any
        # werewolf-style "at the beginning of each upkeep … transform" text.
        # =====================================================================


        # Huntmaster of the Fells (day side) — ETB / transform-in trigger.
        # Aug 9 audit (C-F4-1): the Apr 30 rewrite implemented the WRONG
        # FACE's effect from memory — "deal 2 damage to target opponent"
        # belongs to RAVAGER of the Fells' transform trigger. Huntmaster's
        # printed trigger (cache-verified): "create a 2/2 green Wolf
        # creature token and you gain 2 life." Qwen took 2 phantom damage
        # and Rick never got his Wolf (game_1535582267429101618). The
        # Ravager face gets its own entry only WITH the transform-into
        # dispatch (C-F4-2) — a template that can't fire is the
        # dead-registration class.
        self._add_card("huntmaster of the fells", EffectTemplate(
            name="Huntmaster of the Fells",
            description="When Huntmaster of the Fells enters or transforms into Huntmaster, create a 2/2 green Wolf token and gain 2 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Wolf",
                 "power": 2, "toughness": 2, "types": "Creature — Wolf",
                 "colors": ["G"], "count": 1},
                {"action": "gain_life", "player": ctrl, "amount": 2},
            ],
        ))

        # Ravager of the Fells (night side) — "Whenever this creature
        # transforms into Ravager of the Fells, it deals 2 damage to target
        # opponent or planeswalker and 2 damage to up to one target
        # creature." Reachable only via the transform-into dispatch
        # (C-F4-2, mtg/triggers.py) — registered WITH it, per the
        # dead-registration rule. The old JSON entry for this key created a
        # Wolf (the two faces' effects were SWAPPED in both registries).
        def _gen_ravager_of_the_fells(ctrl, opp, ctx):
            actions = [{"action": "deal_damage", "amount": 2,
                        "target_player": ctx.get('explicit_target_player') or opp}]
            _best = ctx.get('best_opponent_creature')
            if _best:
                # "up to one target creature" — take it when one exists
                actions.append({"action": "deal_damage", "amount": 2,
                                "target_card": _best,
                                "target_controller": opp})
            return actions

        self._add_card("ravager of the fells", EffectTemplate(
            name="Ravager of the Fells",
            description="When this transforms into Ravager of the Fells, deal 2 damage to target opponent and 2 to up to one target creature",
            action_generator=_gen_ravager_of_the_fells,
        ))

        # =================================================================
        # APR 14 AUDIT FIXES — Scry/look-at-top cards missing from template library
        # =================================================================


        # =================================================================
        # APR 17 AUDIT FIXES — Gaps flagged by audit
        # =================================================================


        # Sylvan Awakening template lives at line ~5133 above (in the
        # "Animate-lands family" group with Living Lands). The animate_land
        # action handler is in mtg/actions.py — it stamps temporary creature
        # attributes that revert at end-of-turn cleanup.


        # Signet family — all ten guild signets are mana-generating activated
        # abilities. Mana production is handled by mtg_game.py's Signet family
        # logic; templates return no_action for any non-mana misroute so the
        # cascade stays clean instead of falling through to Tier 3.
        for signet_name in (
            "azorius signet", "boros signet", "dimir signet", "golgari signet",
            "gruul signet", "izzet signet", "orzhov signet", "rakdos signet",
            "selesnya signet", "simic signet",
        ):
            self._add_card(signet_name, EffectTemplate(
                name=signet_name.title(),
                description="{1}, {T}: Add two mana of guild colors (handled by mana system)",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action",
                     "reason": "Signet: mana ability handled by mana system, not by effect resolver"},
                ],
            ))
        # Talisman family (similar structure: {T}: add {C}; {T}: lose 1 life,
        # add one of two colors). Same no_action stub.
        for tal_name in (
            "talisman of progress", "talisman of conviction", "talisman of unity",
            "talisman of indulgence", "talisman of impulse", "talisman of resilience",
            "talisman of dominance", "talisman of curiosity", "talisman of creativity",
            "talisman of hierarchy",
        ):
            self._add_card(tal_name, EffectTemplate(
                name=tal_name.title(),
                description="Talisman mana rock (handled by mana system)",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "no_action",
                     "reason": "Talisman: mana ability handled by mana system"},
                ],
            ))

        # =================================================================
        # APR 30 AUDIT FIXES — missing templates from Apr 30 batch
        # =================================================================


        # Kolaghan's Command — modal charm with 4 modes:
        #   1. Destroy target artifact
        #   2. Return target creature card from your graveyard to your hand
        #   3. Target player discards a card
        #   4. Kolaghan's Command deals 2 damage to any target
        # Choose two. Reads ctx['_modes'] (list[int] or list[str]) when AI
        # specifies; otherwise defaults to the always-legal pair (3+4).
        self._add_card("kolaghan's command", EffectTemplate(
            name="Kolaghan's Command",
            description="Choose two: destroy artifact, recur creature from GY, opponent discards, 2 damage",
            action_generator=lambda ctrl, opp, ctx: self._gen_kolaghans_command(ctrl, opp, ctx),
        ))
        self._add_card("blood on the snow", EffectTemplate(
            name="Blood on the Snow",
            description=("Choose creatures or planeswalkers, destroy all of that "
                         "type, then return your creature/planeswalker with mana "
                         "value at most the snow mana spent"),
            action_generator=lambda ctrl, opp, ctx: self._gen_blood_on_the_snow(
                ctrl, opp, ctx),
        ))


        # =====================================================================
        # May 7, 2026 audit: silent-resolve spell templates (round 2)
        # Cards that said "resolved" but produced no game state change in the
        # May 7 batch logs. Most candidate cards from that audit already had
        # templates by then (Reanimate, Living Death, Beast Within, Ephemerate,
        # Momentary Blink, Cathartic Reunion, etc.) — these four were the
        # uncovered remainder.
        # =====================================================================


        # Generic Living Weapon pattern — catches any equipment with the
        # living-weapon keyword (Skinwing, Mortarpod, Bonesplitter variants).
        # Pattern matches both keyword form ("living weapon") and reminder
        # text. Creates the 0/0 Germ token; attach is a manual follow-up.
        self._add_pattern(
            r"living weapon",
            EffectTemplate(
                name="Living Weapon",
                description="Create a 0/0 black Germ creature token (auto-attach via !equip)",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "create_token", "player": ctrl, "name": "Germ",
                     "power": 0, "toughness": 0,
                     "types": "Creature — Germ", "count": 1},
                ],
            )
        )

    def _gen_nahiris_lithoforming(self, ctrl, opp, ctx) -> List[Dict]:
        """Nahiri's Lithoforming: sacrifice X lands, draw X cards, play X additional lands."""
        x = ctx.get('x_value', 0)
        if x <= 0:
            return [{"action": "no_action", "reason": "Nahiri's Lithoforming: X=0, no effect"}]
        actions = []
        # Sacrifice X lands (pick tapped lands first)
        lands_to_sac = ctx.get('controller_tapped_lands', [])[:x]
        if len(lands_to_sac) < x:
            extra_lands = ctx.get('controller_lands', [])
            for l in extra_lands:
                if l not in lands_to_sac and len(lands_to_sac) < x:
                    lands_to_sac.append(l)
        for land_name in lands_to_sac[:x]:
            # Aug 7 batch audit (G1-1 family): "SACRIFICE X lands" — the old
            # destroy emission let indestructible lands (Darksteel Citadel)
            # survive their own sacrifice and never fired "whenever you
            # sacrifice a permanent" (Korvold counts lands).
            actions.append({"action": "sacrifice_permanent", "player": ctrl,
                            "preferred_card": land_name, "only_preferred": True,
                            "reason": "Nahiri's Lithoforming: sacrifice X lands"})
        # Draw X cards
        actions.append({"action": "draw_cards", "player": ctrl, "amount": x})
        return actions

    def _gen_merciless_eviction(self, ctrl, opp, ctx) -> List[Dict]:
        """Merciless Eviction: auto-pick mode that exiles the most opponent permanents."""
        # Count opponent permanents by type to pick the best mode
        opp_creatures = ctx.get('opponent_creature_count', 0)
        opp_artifacts = ctx.get('opponent_artifact_count', 0)
        opp_enchantments = ctx.get('opponent_enchantment_count', 0)
        opp_planeswalkers = ctx.get('opponent_planeswalker_count', 0)
        counts = {
            "creatures": opp_creatures,
            "artifacts": opp_artifacts,
            "enchantments": opp_enchantments,
            "planeswalkers": opp_planeswalkers,
        }
        best_type = max(counts, key=counts.get) if any(counts.values()) else "creatures"
        return [{"action": "exile_all_by_type", "type": best_type}]

    def _gen_werewolf_transform(self, ctrl, opp, ctx) -> List[Dict]:
        """Werewolf day/night transform check.

        Older werewolves: transform if no spells were cast last turn (day → night),
        or transform back if 2+ spells were cast last turn (night → day). Both
        players' spell counts contribute.

        Returns a transform action only when the condition is met; otherwise an
        empty list (template-matched silent no-op — no Discord message).
        """
        oracle = (ctx.get('_oracle') or '').lower()
        prev_turn_total = int(ctx.get('all_players_spells_cast_prev_turn', 0) or 0)
        source_name = ctx.get('_source_card_name', '') or ''

        # Day → night side ("if no player cast a spell last turn, transform")
        if 'no player cast a spell' in oracle and prev_turn_total == 0:
            return [{"action": "transform", "card": source_name,
                     "reason": "no spells cast last turn (day→night)"}]
        # Night → day side ("if a player cast two or more spells last turn, transform")
        if ('two or more spells' in oracle or 'cast two or more' in oracle) and prev_turn_total >= 2:
            return [{"action": "transform", "card": source_name,
                     "reason": "2+ spells cast last turn (night→day)"}]
        # Condition not met — silent no-op (don't spam upkeep messages)
        return []

    def _gen_charming_prince(self, ctrl, opp, ctx) -> List[Dict]:
        """Charming Prince: pick best mode. Flicker > gain 3 > scry 2.

        Each branch emits a leading no_action with a 'mode chosen' reason so
        the player sees which of the four modes Charming Prince picked, even
        when the chosen mode's action handler returns no message (e.g., scry
        keeps all cards on top → silent).
        """
        controller_creatures = ctx.get('controller_creature_count', 0)
        controller_life = ctx.get('controller_life', 40)
        # If we have other creatures with ETB value, flicker one
        if controller_creatures > 1:
            best = ctx.get('best_etb_creature', '')
            if best:
                return [
                    {"action": "no_action",
                     "reason": f"Charming Prince chooses to flicker {best}"},
                    {"action": "flicker", "card": best, "player": ctrl},
                ]
        # If low on life, gain 3
        if controller_life <= 15:
            return [
                {"action": "no_action",
                 "reason": "Charming Prince chooses to gain 3 life"},
                {"action": "gain_life", "player": ctrl, "amount": 3},
            ]
        # Default: scry 2
        return [
            {"action": "no_action",
             "reason": "Charming Prince chooses to scry 2"},
            {"action": "scry", "player": ctrl, "amount": 2},
        ]

    def _gen_sidisi_exploit(self, ctrl, opp, ctx) -> List[Dict]:
        """Sidisi, Undead Vizier: Exploit — sacrifice a creature, search library for any card.

        Aug 7 batch audit (G1-1): exploit is a SACRIFICE (CR 701.19), not a
        destroy (CR 701.4) — the destroy handler early-returns on
        Indestructible (an indestructible creature could never be exploited)
        and "whenever you sacrifice" triggers (Korvold, Mayhem Devil,
        Yawgmoth) never fired from the old destroy emission.
        """
        worst = ctx.get('controller_worst_creature')
        if worst:
            return [
                {"action": "sacrifice_permanent", "player": ctrl,
                 "preferred_card": worst, "only_preferred": True,
                 "type_filter": "creature",
                 "reason": "Sidisi, Undead Vizier: exploit"},
                {"action": "search_library", "player": ctrl, "count": 1,
                 "reason": "Sidisi, Undead Vizier: exploited creature, tutor for any card"},
            ]
        return [
            {"action": "no_action", "reason": "No creature to exploit for Sidisi"},
        ]

    def _gen_meren_end_step(self, ctrl, opp, ctx) -> List[Dict]:
        """Meren of Clan Nel Toth: end-step targeted reanimate bounded by
        experience counters.

        Text: choose target creature in your graveyard. If its mana value is
        ≤ your experience counters, return it to the battlefield. Otherwise
        return it to your hand.

        We look at the full controller graveyard (not the already-sorted
        `controller_graveyard_creatures` which may be trimmed), pick the
        highest-CMC creature that fits under the experience threshold for
        reanimation; if none qualify, return the best creature to hand.
        """
        exp = int(ctx.get('experience_counters', 0) or 0)
        gy = ctx.get('controller_graveyard') or []
        # Pull creature cards out of the graveyard
        gy_creatures = []
        for c in gy:
            try:
                is_creature = c.is_creature() if hasattr(c, 'is_creature') else \
                              'creature' in (getattr(c, 'type_line', '') or '').lower()
            except Exception:
                is_creature = False
            if is_creature:
                try:
                    cmc = int(getattr(c, 'cmc', 0) or 0)
                except (ValueError, TypeError):
                    cmc = 0
                gy_creatures.append((c, cmc))
        if not gy_creatures:
            # Silent no-op — no Discord noise when graveyard is empty.
            return []

        # Sort by CMC desc for "best target"
        gy_creatures.sort(key=lambda t: t[1], reverse=True)

        # Reanimation target: highest-CMC creature with CMC <= exp counters
        reanimate = next((c for (c, cmc) in gy_creatures if cmc <= exp), None)
        if reanimate is not None:
            return [
                {"action": "move_card", "card": reanimate.name,
                 "from_zone": "graveyard", "to_zone": "battlefield",
                 "player": ctrl},
            ]
        # Otherwise return the biggest creature to hand
        best = gy_creatures[0][0]
        return [
            {"action": "move_card", "card": best.name,
             "from_zone": "graveyard", "to_zone": "hand",
             "player": ctrl},
        ]

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _load_json_templates(self):
        """Load the data-driven half of the library from
        data/card_templates.json (see CARD_TEMPLATES_JSON block comment).

        STRICT on purpose: schema errors, unknown events, malformed actions,
        and Python/JSON key collisions all raise — the file is repo data, and
        every pytest run imports this library, so CI fails loudly on a bad
        edit instead of silently dropping a card's template.
        """
        if not CARD_TEMPLATES_JSON.exists():
            # OSS forks may strip the data file; the Python half still works.
            print(f"[TEMPLATE-JSON] {CARD_TEMPLATES_JSON} not found — "
                  f"JSON template half not loaded")
            return
        with open(CARD_TEMPLATES_JSON, encoding="utf-8") as f:
            data = json.load(f)
        registries = {
            "etb": self._card_templates,
            "dies": self._dies_templates,
            "attack": self._attack_templates,
            "upkeep": self._upkeep_templates,
        }
        entries = data.get("templates")
        if not isinstance(entries, list):
            raise ValueError(
                f"{CARD_TEMPLATES_JSON}: top-level 'templates' must be a list")
        seen = set()
        for i, entry in enumerate(entries):
            where = f"{CARD_TEMPLATES_JSON.name} templates[{i}]"
            if not isinstance(entry, dict):
                raise ValueError(f"{where}: entry must be an object")
            missing = {"key", "name", "event", "description", "actions"} - set(entry)
            if missing:
                raise ValueError(f"{where}: missing fields {sorted(missing)}")
            event = entry["event"]
            if event not in registries:
                raise ValueError(
                    f"{where}: unknown event {event!r} "
                    f"(expected one of {sorted(registries)})")
            key = entry["key"]
            if not isinstance(key, str) or not key or key != key.lower().strip():
                raise ValueError(
                    f"{where}: key must be a lowercase trimmed string, "
                    f"got {key!r}")
            if (event, key) in seen:
                raise ValueError(f"{where}: duplicate key {key!r} for {event}")
            seen.add((event, key))
            registry = registries[event]
            if key in registry:
                raise ValueError(
                    f"{where}: key {key!r} ({event}) is registered in BOTH "
                    f"Python (_build_library) and JSON — remove one")
            actions = entry["actions"]
            if (not isinstance(actions, list) or not actions
                    or not all(isinstance(a, dict) and isinstance(a.get("action"), str)
                               for a in actions)):
                raise ValueError(
                    f"{where}: actions must be a non-empty list of objects "
                    f"each with a string 'action' field")
            registry[key] = EffectTemplate(
                name=entry["name"],
                description=entry["description"],
                action_generator=_make_json_action_generator(actions),
                needs_target=bool(entry.get("needs_target", False)),
                mandatory=bool(entry.get("mandatory", True)),
            )

    def _add_card(self, name_lower: str, template: EffectTemplate):
        self._card_templates[name_lower] = template

    def _add_attack_card(self, name_lower: str, template: EffectTemplate):
        """Register an attack-trigger-specific template (checked before ETB templates)."""
        self._attack_templates[name_lower] = template

    def _add_upkeep_card(self, name_lower: str, template: EffectTemplate):
        """Register an upkeep-only template, checked before general templates."""
        self._upkeep_templates[name_lower] = template

    def _add_dies_card(self, name_lower: str, template: EffectTemplate):
        """Register a dies-trigger template (checked when event_type='dies').
        Use this for cards that have BOTH an ETB and a dies trigger so the
        two don't collide on the same _card_templates key (Solemn Simulacrum,
        Wood Elves variants, etc.)."""
        self._dies_templates[name_lower] = template

    def _add_pattern(self, pattern: str, template: EffectTemplate):
        self._pattern_templates.append((pattern, template))

    def _gen_proliferate(self, ctrl: str, opp: str, ctx: Dict) -> Optional[List[Dict]]:
        """Emit a proliferate action. Guards the enter-event case: when this is the
        creature's ETB self-resolution (_event_type == 'etb') but the matched
        'proliferate' belongs to a SCHEDULED trigger ('At the beginning of your
        end step, proliferate' — Atraxa), don't fire on enter; the upkeep/end-step
        dispatcher fires it at the right time. A genuine ETB proliferate ('When ~
        enters, proliferate') still fires because 'enters' precedes the clause."""
        if ctx.get('_event_type', 'etb') == 'etb':
            m = ctx.get('_match')
            oracle = ctx.get('_oracle', '') or ''
            head = oracle[:m.start()] if m else oracle
            if 'enters' not in head[-60:]:
                return None
        return [{"action": "proliferate", "player": ctrl}]

    def _gen_finale_of_devastation(self, ctrl: str, opp: str, ctx: Dict) -> List[Dict]:
        """Finale of Devastation ({X}{G}{G}): creatures you control get +X/+X and
        gain trample until end of turn; if X >= 10, search your library for a
        creature card and put it onto the battlefield, then shuffle. May 30 audit:
        no template existed, so it escalated to Tier 3, which returned no actions —
        the 12-mana finisher did NOTHING and the player saw a bare "resolves."""
        x = ctx.get('x_value', 0) or ctx.get('X', 0) or ctx.get('x', 0) or 0
        try:
            x = int(x)
        except (TypeError, ValueError):
            x = 0
        actions = [{
            "action": "pump_all_creatures", "player": ctrl,
            "power": x, "toughness": x, "keywords": ["Trample"],
        }]
        if x >= 10:
            actions.append({
                "action": "search_library", "player": ctrl,
                "card_type": "Creature", "to_zone": "battlefield", "count": 1,
            })
        return actions

    @staticmethod
    def _word_to_num(w: str) -> int:
        """Delegate to module-level word_to_num (shared with XMageActionTranslator)."""
        return word_to_num(w)

    @staticmethod
    def _scry_n(ctrl: str, n: int) -> List[Dict]:
        """Emit a scry N action.

        The engine's `scry` handler at mtg_game.py uses a smart heuristic
        (bottoms lands when flooding, bottoms high-CMC spells when starved),
        so templates should just hand off the amount and let it decide.
        """
        if n <= 0:
            return [{"action": "no_action", "reason": "Scry 0: nothing to do"}]
        return [{"action": "scry", "player": ctrl, "amount": n}]

    # =========================================================================
    # Smart Action Generators (need game context)
    # =========================================================================

    def _chandra_tod_plus1(self, ctrl, opp, ctx) -> List[Dict]:
        """Chandra, Torch of Defiance +1: Exile top card, you may cast it this turn.
        If you don't, deal 2 damage to each opponent.

        The engine tracks the exact exiled object. Casting it consumes the
        conditional record; otherwise end-turn cleanup deals the damage.
        """
        actions = []
        # Exile top card and mark as playable this turn
        actions.append({"action": "exile_top_play_or_damage", "player": ctrl,
                        "damage": 2, "damage_target": opp})
        return actions

    def _daretti_loot(self, ctrl, opp, ctx) -> List[Dict]:
        """Daretti, Scrap Savant +2: Discard up to 2, then draw that many.

        In autoplay, discard 2 (worst cards) and draw 2 for maximum value.
        If hand has fewer than 2 cards, discard what we have and draw that many.
        """
        hand_size = ctx.get('controller_hand_size', 2)
        discard_count = min(2, hand_size)
        if discard_count <= 0:
            return [{"action": "no_action", "reason": "Daretti +2: no cards in hand to discard"}]
        actions = []
        for _ in range(discard_count):
            actions.append({"action": "discard", "player": ctrl, "card": "worst"})
        actions.append({"action": "draw_cards", "player": ctrl, "amount": discard_count})
        return actions

    def _daretti_weld(self, ctrl, opp, ctx) -> List[Dict]:
        """Daretti -2: Sacrifice an artifact. IF YOU DO, return an artifact
        from your graveyard to the battlefield.

        Aug 9 audit (CO-4): both actions were emitted unconditionally — the
        action list has no conditional linkage, so with no artifact to
        sacrifice the reanimate ran anyway (a free 11/11 infect Blightsteel
        Colossus, game_1535567121029931081). The Gatekeeper kicked-gate
        precedent: check the "if you do" condition HERE, where the ctx is.
        Loyalty stays spent either way (costs paid, ability fizzles).
        """
        _bf = ctx.get('controller_battlefield') or []
        _has_own_artifact = any(
            'artifact' in (getattr(c, 'type_line', '') or '').lower()
            and not getattr(c, '_phased_out', False)
            for c in _bf)
        if not _has_own_artifact:
            return [{"action": "no_action",
                     "reason": "Daretti -2: no artifact to sacrifice — "
                               "\"if you do\" fails, nothing returns"}]
        return [
            {"action": "sacrifice_permanent", "player": ctrl,
             "type_filter": "artifact", "reason": "Daretti -2: sacrifice an artifact"},
            {"action": "reanimate", "player": ctrl, "allow_types": ["artifact"],
             "source": "Daretti -2"},
        ]

    def _gary_drain(self, ctrl, opp, ctx) -> List[Dict]:
        """Gray Merchant: drain = devotion to black. Estimate from context or default to 5."""
        devotion = ctx.get('black_devotion', 5)  # Default estimate
        return [
            {"action": "lose_life", "player": opp, "amount": devotion},
            {"action": "gain_life", "player": ctrl, "amount": devotion}
        ]
    
    def _phyrexian_processor_etb(self, ctrl, opp, ctx) -> List[Dict]:
        """Phyrexian Processor: pay any amount of life on ETB to set token size.

        Auto-pick: min(controller_life - 5, 10) — big enough for decent tokens
        but leaves a 5-life cushion so we don't die immediately.
        """
        life = ctx.get('controller_life', 40)
        pay = max(0, min(life - 5, 10))
        if pay <= 0:
            return [{"action": "no_action",
                     "reason": "Phyrexian Processor: too low on life to pay safely"}]
        return [{"action": "lose_life", "player": ctrl, "amount": pay}]

    def _force_sacrifice_creature(self, ctrl, opp, ctx) -> List[Dict]:
        """Force opponent to sacrifice their weakest creature (Grave Pact, Dictate of Erebos)."""
        # July 24 batch-6 (reviewer A1 #7): name the actual trigger source so
        # the Discord line reads "💀 Butcher of Malakir: 💀 Claude sacrifices
        # X" instead of an unattributed sacrifice (the hardcoded Grave Pact/
        # Dictate path already prefixed its own; this template path didn't).
        _src_card = ctx.get('_source_card')
        _src = (ctx.get('_source_card_name')
                or (getattr(_src_card, 'name', '') if _src_card else ''))
        if ctx.get('worst_opponent_creature') or ctx.get('best_opponent_creature'):
            return [{"action": "sacrifice_permanent", "player": opp,
                     "type_filter": "creature", "source": _src,
                     "reason": f"{_src or 'Grave Pact / Dictate / Butcher'} dies trigger"}]
        return [{"action": "no_action", "reason": f"{opp} has no creatures to sacrifice"}]

    def _avenger_tokens(self, ctrl, opp, ctx) -> List[Dict]:
        """Avenger of Zendikar: tokens = lands you control."""
        land_count = ctx.get('controller_land_count', 5)  # Default estimate
        return [make_token_action(ctrl, "plant_0_1", land_count)]
    
    def _destroy_best_creature(self, ctrl, opp, ctx, exclude_colors=None) -> List[Dict]:
        """Auto-target: destroy opponent's best creature."""
        target = ctx.get('best_opponent_creature')
        if target:
            return [{"action": "destroy", "card": target}]
        return [{"action": "no_action", "reason": "No valid creature target"}]
    
    def _destroy_nonblack_nonartifact_creature(self, ctrl, opp, ctx) -> List[Dict]:
        """Shriekmaw/Nekrataal: destroy target nonartifact, nonblack creature opponent controls.

        Properly filters out black creatures and artifacts (the May 14 audit
        found Shriekmaw destroying Stinkweed Imp — a black creature — because
        this function previously took best_opponent_creature without checking
        color/type at all).
        """
        opp_creatures = ctx.get('_opponent_creatures', [])

        def _is_legal_target(c: Dict) -> bool:
            colors = c.get('colors') or []
            if 'B' in colors:
                return False
            type_line = c.get('type_line', '') or ''
            if 'artifact' in type_line:
                return False
            return True

        # First try the pre-computed best creature — but only if it passes filter
        best = ctx.get('best_opponent_creature')
        if best:
            best_info = next(
                (c for c in opp_creatures if c.get('name') == best), None
            )
            if best_info is None or _is_legal_target(best_info):
                # Either no metadata (allow, defensive default) or metadata says legal
                if best_info is not None:
                    return [{"action": "destroy", "card": best}]
        # Scan for any legal target
        for c in opp_creatures:
            if _is_legal_target(c):
                name = c.get('name', '')
                if name:
                    return [{"action": "destroy", "card": name}]
        return [{"action": "no_action", "reason": "No valid nonartifact, nonblack creature target"}]

    def _destroy_best_noncreature(self, ctrl, opp, ctx) -> List[Dict]:
        """Auto-target: destroy opponent's best non-creature permanent."""
        target = ctx.get('best_opponent_noncreature')
        if target:
            return [{"action": "destroy", "card": target}]
        return [{"action": "no_action", "reason": "No valid non-creature target"}]
    
    def _destroy_best_artifact_enchantment(self, ctrl, opp, ctx) -> List[Dict]:
        # June 10 deep-dive (B10b): honor the AI/player's NAMED target when it
        # is a legal artifact/enchantment — Krosan Grip with a captured legal
        # target (The Abyss) was destroying an unrelated heuristic pick
        # (Orzhov Signet) instead.
        explicit = (ctx.get('explicit_target_name') or '').strip()
        opp_player = ctx.get('_opponent_player')
        if explicit and opp_player is not None:
            for c in opp_player.battlefield:
                if (c.name or '').lower() == explicit.lower():
                    tl = (getattr(c, 'type_line', '') or '').lower()
                    if 'artifact' in tl or 'enchantment' in tl:
                        return [{"action": "destroy", "card": c.name,
                                 "target_controller": opp}]
                    break  # named target exists but wrong type — fall through
        target = ctx.get('best_opponent_artifact_enchantment')
        if target:
            return [{"action": "destroy", "card": target}]
        return [{"action": "no_action", "reason": "No valid artifact/enchantment target"}]
    
    def _destroy_best_nonland(self, ctrl, opp, ctx) -> List[Dict]:
        # Respect explicit target from AI/player targeting, fall back to auto-targeting
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland')
        if target:
            return [{"action": "destroy", "card": target}]
        return [{"action": "no_action", "reason": "No valid nonland target"}]
    
    def _damage_best_creature(self, ctrl, opp, ctx, amount=0) -> List[Dict]:
        target = ctx.get('best_opponent_creature')
        if target:
            return [{"action": "deal_damage", "amount": amount, "target_card": target, "target_controller": opp}]
        return [{"action": "deal_damage", "amount": amount, "target_player": opp}]
    
    def _bounce_best_creature(self, ctrl, opp, ctx) -> List[Dict]:
        target = ctx.get('best_opponent_creature')
        if target:
            return [{"action": "move_card", "card": target, "from_zone": "battlefield", "to_zone": "hand", "player": opp}]
        return [{"action": "no_action", "reason": "No valid creature to bounce"}]
    
    def _exile_creature_with_life(self, ctrl, opp, ctx) -> List[Dict]:
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')
        target_power = resolve_target_power(ctx, target)
        if target:
            return [
                {"action": "move_card", "card": target, "from_zone": "battlefield", "to_zone": "exile", "player": opp},
                {"action": "gain_life", "player": opp, "amount": target_power}
            ]
        return [{"action": "no_action", "reason": "No valid creature to exile"}]
    
    def _exile_best_small_permanent(self, ctrl, opp, ctx, max_mv=4) -> List[Dict]:
        target = ctx.get('best_opponent_small_permanent')
        if target:
            return [{"action": "move_card", "card": target, "from_zone": "battlefield", "to_zone": "exile", "player": opp}]
        return [{"action": "no_action", "reason": "No valid small permanent to exile"}]
    
    def _tap_best_permanent(self, ctrl, opp, ctx) -> List[Dict]:
        # Frost Titan says target permanent, not target creature. Prefer the
        # general threat chosen by context and retain the creature fallback.
        target = ctx.get('best_opponent_threat') or ctx.get('best_opponent_creature')
        if target:
            return [{"action": "tap", "card": target}]
        return [{"action": "no_action", "reason": "No valid permanent to tap"}]
    
    def _return_best_from_graveyard(self, ctrl, opp, ctx) -> List[Dict]:
        target = ctx.get('best_own_graveyard_card')
        if target:
            return [{"action": "move_card", "card": target, "from_zone": "graveyard", "to_zone": "hand", "player": ctrl}]
        return [{"action": "no_action", "reason": f"{ctrl} returns a card from graveyard (use !fix)"}]
    
    # (_reanimate_small deleted Aug 7 registry dedup — its only caller was the
    # shadowed sun titan duplicate; it had neither the MV-3 filter nor the
    # CR 110.1 permanent-card filter the surviving _gen_sun_titan carries.)


    def _knight_of_autumn(self, ctrl, opp, ctx) -> List[Dict]:
        """Knight of Autumn: choose based on board state."""
        # If opponent has artifacts/enchantments, destroy one
        target = ctx.get('best_opponent_artifact_enchantment')
        if target:
            return [{"action": "destroy", "card": target}]
        # Otherwise gain 4 life (safest default)
        return [{"action": "gain_life", "player": ctrl, "amount": 4}]
    
    def _sphinx_of_uthuun(self, ctrl, opp, ctx) -> List[Dict]:
        """Sphinx of Uthuun / Fact or Fiction: reveal top 5, split into 2 piles, choose one.

        Since the opponent normally separates the piles, we simulate by taking a
        roughly fair split: top 3 go to hand, bottom 2 go to graveyard.
        This approximates a "generous opponent" split for autoplay.
        """
        library = ctx.get('controller_library', [])
        if not library:
            return [{"action": "no_action", "reason": f"{ctrl}'s library is empty"}]

        count = min(5, len(library))
        # Take the top N cards from library
        revealed = library[:count]
        card_names = [c.name for c in revealed]

        # Split: first 3 (or ceil(count/2)) go to hand, rest to graveyard
        hand_count = (count + 1) // 2  # 3 of 5, 2 of 4, 2 of 3, etc.
        hand_pile = card_names[:hand_count]
        grave_pile = card_names[hand_count:]

        actions = []
        for name in hand_pile:
            actions.append({"action": "move_card", "card": name, "from_zone": "library", "to_zone": "hand", "player": ctrl})
        for name in grave_pile:
            actions.append({"action": "move_card", "card": name, "from_zone": "library", "to_zone": "graveyard", "player": ctrl})

        return actions

    def _gen_chulane_cast_trigger(self, ctrl, opp, ctx) -> List[Dict]:
        """Chulane, Teller of Tales: "Whenever you cast a creature spell, draw a
        card, then you may put a land card from your hand onto the battlefield."

        Both halves, because the inline cast-trigger draw handler used to match
        the first clause of this ONE sentence and claim the whole trigger — the
        ramp half never resolved and never even reached the unhandled backlog.
        The land drop is free (it is not a land play, so it ignores the
        once-per-turn land limit).
        """
        actions: List[Dict] = [
            {"action": "draw_cards", "player": ctrl, "amount": 1},
        ]
        hand = ctx.get('controller_hand') or []
        land = next((c for c in hand
                     if 'land' in (getattr(c, 'type_line', '') or '').lower()), None)
        if land is not None:
            actions.append({
                "action": "move_card", "card": getattr(land, 'name', ''),
                "from_zone": "hand", "to_zone": "battlefield", "player": ctrl,
            })
        return actions

    def _gen_felidar_retreat(self, ctrl, opp, ctx) -> List[Dict]:
        """Felidar Retreat landfall — "choose one":
            • Create a 2/2 white Cat Beast creature token.
            • Put a +1/+1 counter on each creature you control. Those creatures
              gain vigilance until end of turn.

        Until July 28 2026 this card was resolved by the RAMPAGING BALOTHS
        branch in mtg/triggers.py, whose loose "landfall + create + beast"
        fallback matched on "Cat Beast" — so it made a 4/4 GREEN Beast, and
        because that branch is an elif the modal choice was never offered at
        any tier. Eight wrong tokens across the loose logs.

        Mode choice: counters scale with board width, so take them once there
        are enough creatures to beat a single 2/2; otherwise make the token.
        """
        creatures = int(ctx.get('controller_creature_count', 0) or 0)
        if creatures >= 2:
            # Two actions, because the counters and the keyword live in
            # different handlers: add_counters owns bulk +1/+1 (by identity, so
            # same-name tokens don't stack onto one card), pump_all_creatures
            # owns until-EOT keywords. pump_all_creatures has no "counters" key
            # — emitting one would be silently dropped.
            return [
                {"action": "add_counters", "target": "all_own_creatures",
                 "player": ctrl, "counter_type": "+1/+1", "amount": 1},
                {"action": "pump_all_creatures", "player": ctrl,
                 "power": 0, "toughness": 0,
                 "keywords": ["Vigilance"], "duration": "end_of_turn"},
            ]
        return [
            {"action": "create_token", "player": ctrl, "name": "Cat Beast",
             "power": 2, "toughness": 2,
             "types": "Creature Token — Cat Beast", "colors": "W", "count": 1},
        ]

    def _gen_leonin_vanguard(self, ctrl, opp, ctx) -> List[Dict]:
        """Leonin Vanguard: "At the beginning of combat on your turn, if you
        control three or more creatures, THIS CREATURE gets +1/+1 until end of
        turn and you gain 1 life."

        No template existed, so this escalated to Tier 3 every combat — and
        Tier 3's pump is player-scoped, not card-scoped, so it buffed the whole
        team (up to five creatures), and twice emitted +0/+0, which pumps
        nobody at all. 7 of 7 executed firings were wrong in one game.
        """
        creatures = int(ctx.get('controller_creature_count', 0) or 0)
        if creatures < 3:
            return [{"action": "no_action",
                     "reason": f"Leonin Vanguard: only {creatures} creature(s), needs 3"}]
        # "card" is pump_all_creatures' include_name filter — the card-scoped
        # form. There is no separate "pump_creatures" action; emitting one
        # would be dropped on the floor, which is how Tier 3's team-wide pump
        # got to be the only thing that ever happened here.
        return [
            {"action": "pump_all_creatures", "player": ctrl,
             "card": "Leonin Vanguard", "power": 1, "toughness": 1,
             "duration": "end_of_turn"},
            {"action": "gain_life", "player": ctrl, "amount": 1},
        ]

    def _gen_graveyard_copy_token(self, ctrl, opp, ctx) -> List[Dict]:
        """Feldon-class: "create a token that's a copy of target creature CARD
        in your graveyard".

        Registered as a PATTERN, not a name-keyed template, deliberately: this
        text lives on an activated ability, and the activation path calls
        resolve_etb with event_type="activated", which skips the name-keyed
        lookup entirely (mtg/engine.py, Apr 29 audit). A named template would
        never fire — and would also wrongly fire on the source's own ETB.

        The target is resolved HERE (same shape as Puppeteer Clique) so the
        delayed sacrifice below can name the same card the copy was made from.
        """
        matched = (ctx['_match'].group(0) if ctx.get('_match') else '') or ''
        own_graveyard = 'your graveyard' in matched.lower()

        def _graveyard_of(key, list_key):
            player_obj = ctx.get(key)
            if player_obj is not None and hasattr(player_obj, 'graveyard'):
                return list(player_obj.graveyard or [])
            return list(ctx.get(list_key) or [])

        pool = _graveyard_of('_controller_player', 'controller_graveyard')
        if not own_graveyard:
            pool = pool + _graveyard_of('_opponent_player', 'opponent_graveyard')

        candidates = [c for c in pool
                      if 'creature' in (getattr(c, 'type_line', '') or '').lower()]
        if not candidates:
            return [{"action": "no_action",
                     "reason": "no creature card in the graveyard to copy"}]

        chosen = None
        explicit = (ctx.get('explicit_target_name') or '').strip()
        if explicit:
            chosen = next((c for c in candidates
                           if (getattr(c, 'name', '') or '').lower() == explicit.lower()),
                          None)
        if chosen is None:
            chosen = max(candidates, key=lambda c: int(getattr(c, 'cmc', 0) or 0))

        oracle = (ctx.get('_oracle') or '').lower()
        copy_action = {
            "action": "create_copy_token", "player": ctrl,
            "zone": "graveyard",
            "zone_owner": "controller" if own_graveyard else "any",
            "target": chosen.name, "count": 1,
        }
        # "except it's an artifact in addition to its other types" / "It gains haste."
        if 'artifact in addition' in oracle:
            copy_action["extra_types"] = ["Artifact"]
        if 'gains haste' in oracle:
            copy_action["keywords"] = ["Haste"]

        actions = [copy_action]
        if 'sacrifice it at the beginning of the next end step' in oracle:
            # "the next end step", not "your next end step" — so no phase_of
            # gate (cf. Necropotence, which IS owner-gated).
            # Known approximation: the delayed sacrifice names the copied card,
            # so if a second permanent with that name is on the battlefield the
            # engine may sacrifice that one instead. The original stays in the
            # graveyard, so this only bites with a pre-existing duplicate.
            actions.append({
                "action": "schedule_delayed_trigger", "trigger_at": "end_step",
                "turn_delay": 0,
                "source": ctx.get('_source_card_name') or "Graveyard copy",
                "actions": [{
                    "action": "sacrifice_permanent", "player": ctrl,
                    "type_filter": "creature", "preferred_card": chosen.name,
                    "only_preferred": True,
                    "reason": "token copy is sacrificed at the next end step",
                }],
            })
        return actions

    def _puppeteer_clique_etb(self, ctrl, opp, ctx) -> List[Dict]:
        """Puppeteer Clique: put target creature card from an OPPONENT's
        graveyard onto the battlefield under YOUR control, it gains haste,
        and at the beginning of YOUR next end step, exile it (CR 603.7
        delayed trigger — rides schedule_delayed_trigger's phase_of gate,
        same machinery as Necropotence)."""
        opp_player = ctx.get('_opponent_player')
        opp_gy = list(getattr(opp_player, 'graveyard', []) or [])
        best = None
        best_cmc = -1
        for c in opp_gy:
            tl = (getattr(c, 'type_line', '') or '').lower()
            if 'creature' not in tl:
                continue
            cmc = int(c.cmc) if getattr(c, 'cmc', None) else 0
            if cmc > best_cmc:
                best_cmc = cmc
                best = c
        if best is None:
            return [{"action": "no_action",
                     "reason": f"no creature card in {opp}'s graveyard"}]
        return [
            {"action": "reanimate", "player": ctrl, "card": best.name,
             "from_player": opp, "haste": True},
            {"action": "schedule_delayed_trigger", "trigger_at": "end_step",
             "turn_delay": 0, "phase_of": ctrl, "source": "Puppeteer Clique",
             "actions": [{"action": "move_card", "card": best.name,
                          "from_zone": "battlefield", "to_zone": "exile",
                          "player": ctrl}]},
        ]

    def _gonti_etb(self, ctrl, opp, ctx) -> List[Dict]:
        """Gonti, Lord of Luxury: look at top 4 of opponent's library, exile one face down, rest on bottom.

        We pick the highest-CMC nonland card to exile (the best steal target).
        The remaining cards go to the bottom of the opponent's library.
        """
        library = ctx.get('opponent_library', [])
        if not library:
            return [{"action": "no_action", "reason": f"{opp}'s library is empty"}]

        count = min(4, len(library))
        looked_at = library[:count]

        # Pick best card to exile: highest CMC nonland, or highest CMC overall
        best_card = None
        best_cmc = -1
        for c in looked_at:
            cmc = getattr(c, 'cmc', 0) or 0
            is_land = c.is_land() if hasattr(c, 'is_land') else False
            if not is_land and cmc > best_cmc:
                best_cmc = cmc
                best_card = c
        # Fallback to any card if all are lands
        if best_card is None:
            best_card = looked_at[0]

        actions = []
        # Exile the chosen card face down. July 24 batch-6 audit (reviewer L1):
        # the display named the exiled card ("📦 **Cathars' Crusade** → exile")
        # — hidden information; only Gonti's controller may see it. Same for
        # the bottomed cards (neither player learns which of the four went
        # where). hide_card_name redacts the player-facing line; console
        # log_event keeps the true names for audits.
        actions.append({"action": "move_card", "card": best_card.name, "from_zone": "library", "to_zone": "exile", "player": opp,
                        "hide_card_name": True, "source": "Gonti, Lord of Luxury"})
        # Put the rest on the bottom of the library
        for c in looked_at:
            if c is not best_card:
                # Move from library top to library bottom (remove then append)
                actions.append({"action": "move_card", "card": c.name, "from_zone": "library", "to_zone": "library", "player": opp,
                                "hide_card_name": True})

        return actions

    # =========================================================================
    # Pattern Action Generators (use regex match groups)
    # =========================================================================

    def _gen_fetch_from_pattern(self, ctrl, opp, ctx) -> List[Dict]:
        """Generic fetch pattern: determine land type and tapped state from oracle text."""
        oracle = ctx.get('_oracle', '')
        # Determine if it enters tapped (explicit "tapped" without "untapped" nearby)
        enters_tapped = 'tapped' in oracle and 'untapped' not in oracle
        # Determine if basic-only
        basic_only = 'basic' in oracle
        # Check for life payment ("pay 1 life" or similar)
        actions = []
        life_match = re.search(r'pay (\d+) life', oracle)
        if life_match:
            actions.append({"action": "lose_life", "player": ctrl, "amount": int(life_match.group(1))})
        # Determine land type to search for
        land_type = None
        for lt in ['plains', 'island', 'swamp', 'mountain', 'forest']:
            if lt in oracle:
                land_type = lt.title()
                break
        search_action = {"action": "search_library_land", "player": ctrl,
                         "enters_tapped": enters_tapped}
        if basic_only:
            search_action["basic_only"] = True
        if land_type and not basic_only:
            search_action["land_type"] = land_type
        actions.append(search_action)
        return actions

    def _gen_draw_from_match(self, ctrl, opp, ctx) -> List[Dict]:
        match = ctx.get('_match')
        if match:
            raw = match.group(1)
            # July 21 batch audit (R2-3): "draw X cards" (Gadwick) captured
            # the literal "X", word_to_num silently defaulted it to 1, and
            # the computed X sat unread in ctx — an X=8 Gadwick drew 1
            # (game_1529172161636597770). Read the paid X for X-draws.
            if raw and raw.strip().lower() == 'x':
                n = int(ctx.get('x_value') or 0) or 1
            else:
                n = self._word_to_num(raw)
            return [{"action": "draw_cards", "player": ctrl, "amount": n}]
        return [{"action": "draw_cards", "player": ctrl, "amount": 1}]
    
    def _gen_tokens_from_match(self, ctrl, opp, ctx) -> List[Dict]:
        match = ctx.get('_match')
        if match:
            count = self._word_to_num(match.group(1))
            power = int(match.group(2))
            toughness = int(match.group(3))
            name = match.group(4).strip().title()
            return [
                {"action": "create_token", "player": ctrl, "name": name,
                 "power": power, "toughness": toughness, 
                 "types": f"Creature — {name}", "count": count}
            ]
        return None
    
    def _gen_destroy_from_match(self, ctrl, opp, ctx) -> List[Dict]:
        """Generic destroy from oracle text pattern."""
        match = ctx.get('_match')
        target_type = match.group(1).strip() if match else "permanent"

        # July 24 batch-6 audit (reviewer D1, CRITICAL): 'creature' is a
        # SUBSTRING of 'noncreature', so Woodfall Primus's "destroy target
        # noncreature permanent" destroyed a CREATURE (Doomed Traveler,
        # game_1529985418743910420 — CR 601.2c) while legal artifact/
        # enchantment targets sat on the board. Credit a type only when it
        # isn't negated by a 'non' prefix.
        def _wants(t: str) -> bool:
            return t in target_type and f"non{t}" not in target_type

        # Try to find a target on opponent's board from context
        if _wants("creature"):
            target = ctx.get('best_opponent_creature')
        elif (_wants("artifact") or _wants("enchantment")
                or "noncreature" in target_type):
            # "noncreature permanent" restrictions route here too — the
            # artifact/enchantment pick is the best noncreature choice we
            # have a ctx key for (no opponent-land key; lands are legal
            # targets but blowing up lands is not modeled here).
            target = ctx.get('best_opponent_artifact_enchantment')
        else:
            target = ctx.get('best_opponent_nonland')

        if target:
            return [{"action": "destroy", "card": target}]
        return [{"action": "no_action", "reason": f"No valid {target_type} target"}]
    
    @staticmethod
    def _sanitize_target_type(raw: str) -> str:
        """Clean up a regex-captured target type to avoid dumping oracle text as an error message.
        e.g. 'nonland permanent not named Detention Sphere and all...' -> 'nonland permanent'"""
        # Keep only the first few meaningful words (creature, permanent, nonland permanent, etc.)
        # Stop at 'not', 'with', 'that', 'and', 'you', 'an opponent', 'its', 'this'
        cleaned = re.split(r'\b(?:not|with|that|and|you|an|its|this|if|unless)\b', raw, maxsplit=1)[0].strip()
        # Cap at 30 chars as a safety net
        if len(cleaned) > 30:
            cleaned = cleaned[:30].rsplit(' ', 1)[0]
        return cleaned or "permanent"

    def _gen_exile_from_match(self, ctrl, opp, ctx) -> List[Dict]:
        match = ctx.get('_match')
        target_type = self._sanitize_target_type(match.group(1).strip()) if match else "permanent"
        # Prefer explicit target from AI cast decision
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland')
        target_owner = ctx.get('explicit_target_owner') or opp
        if target:
            return [{"action": "move_card", "card": target, "from_zone": "battlefield", "to_zone": "exile", "player": target_owner}]
        return [{"action": "no_action", "reason": f"No valid {target_type} to exile"}]
    
    def _gen_bounce_from_match(self, ctrl, opp, ctx) -> List[Dict]:
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')
        target_owner = ctx.get('explicit_target_owner') or opp
        if target:
            return [{"action": "move_card", "card": target, "from_zone": "battlefield", "to_zone": "hand", "player": target_owner}]
        return [{"action": "no_action", "reason": "No valid target to bounce"}]

    def _gen_bounce_opponent_permanent(self, ctrl, opp, ctx) -> List[Dict]:
        """Bounce best nonland permanent opponent controls (Into the Roil, Chain of Vapor, etc.)."""
        # Prefer explicit target, then best creature, then any nonland
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature') or ctx.get('best_opponent_nonland')
        # Aug 9 audit (CO-2 second net): a declared LAND slipped the old
        # cast gate and this template bounced it (Into the Roil → Academy
        # Ruins). The gate fix upstream makes this unreachable with a land
        # declared; the guard stays as defense in depth — decline rather
        # than bounce a land for a "nonland permanent" spell.
        if target:
            _all_bf = list(ctx.get('controller_battlefield') or [])
            _opp_pl = ctx.get('_opponent_player')
            if _opp_pl is not None:
                _all_bf += list(getattr(_opp_pl, 'battlefield', []) or [])
            for _c in _all_bf:
                if (getattr(_c, 'name', None) == target
                        and 'land' in (getattr(_c, 'type_line', '') or '').lower()):
                    return [{"action": "no_action",
                             "reason": f"{target} is a land — this spell "
                                       f"targets nonland permanents only"}]
        if target:
            actions = [{"action": "move_card", "card": target,
                        "from_zone": "battlefield", "to_zone": "hand", "player": opp}]
            # "If this spell was kicked, draw a card" (Into the Roil / Blink of
            # an Eye). July 29 batch audit: the draw was UNCONDITIONAL — the
            # old comment said "assume kicked if we can afford it" but no
            # check existed, so every unkicked {1}{U} cast drew a free card,
            # and Chain of Vapor (same generator, no draw clause on any
            # printing) drew too. Gate on the kicker-draw clause actually
            # being in the oracle + the Gatekeeper mana-paid heuristic:
            # both kicker printings are base 2 + kicker 2, so total paid >= 4
            # reads as kicked.
            _oracle = ctx.get('_oracle') or ''
            if ('kicked' in _oracle and 'draw' in _oracle
                    # Aug 1: stamped kicker truth first (see Gatekeeper note)
                    and (ctx['kicked'] if 'kicked' in ctx
                         else (ctx.get('mana_paid_total', 0) or 0) >= 4)):
                actions.append({"action": "draw_cards", "player": ctrl, "amount": 1})
            return actions
        return [{"action": "no_action", "reason": "No nonland permanent to bounce"}]

    def _gen_tymna_main_phase(self, ctrl, opp, ctx) -> List[Dict]:
        """Tymna the Weaver: you may pay X life, draw X cards.

        X = the number of opponents that were dealt combat damage this turn —
        the main-phase scan computes it into ctx['_opponents_dealt_combat_damage']
        (mtg/triggers.py, from Player.dealt_combat_damage_this_turn, which
        mtg/combat.py sets on the player who TOOK the damage). "You may pay"
        is modeled as: decline when the life price would cut below a cushion.
        """
        x = ctx.get('_opponents_dealt_combat_damage', 0) or 0
        if x <= 0:
            return []  # handled no-op — no opponent took combat damage this turn
        life = ctx.get('controller_life', 40)
        if life - x < 5:
            return []  # handled no-op — declines the optional payment
        return [
            {"action": "lose_life", "player": ctrl, "amount": x},
            {"action": "draw_cards", "player": ctrl, "amount": x},
        ]

    # ---- July 30 batch-9: combat-damage trigger generators ----
    # Shape helpers: the two ctx builders populate battlefield/hand lists in
    # DIFFERENT shapes (Card objects vs dicts) — the July 28 Kroxa lesson.

    @staticmethod
    def _cd_type_line(c) -> str:
        tl = getattr(c, 'type_line', None)
        if tl is None and isinstance(c, dict):
            tl = c.get('type_line')
        return (tl or '').lower()

    @staticmethod
    def _cd_name(c) -> str:
        n = getattr(c, 'name', None)
        if n is None and isinstance(c, dict):
            n = c.get('name')
        return n or str(c)

    @staticmethod
    def _cd_power_int(c) -> int:
        p = getattr(c, 'power', None)
        if p is None and isinstance(c, dict):
            p = c.get('power')
        try:
            return int(p)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _cd_cmc(c) -> int:
        v = getattr(c, 'cmc', None)
        if v is None and isinstance(c, dict):
            v = c.get('cmc')
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def _gen_hellkite_tyrant(self, ctrl, opp, ctx) -> List[Dict]:
        """Hellkite Tyrant connects: gain control of ALL that player's artifacts."""
        if not ctx.get('damage_dealt'):
            return []
        opp_player = ctx.get('_opponent_player')
        arts = [c for c in (getattr(opp_player, 'battlefield', []) or [])
                if 'artifact' in self._cd_type_line(c)]
        if not arts:
            return []  # handled no-op — nothing to steal
        return [{"action": "steal_permanent", "player": ctrl,
                 "from_player": opp, "card": self._cd_name(c)} for c in arts]

    def _gen_quartzwood_crasher(self, ctrl, opp, ctx) -> List[Dict]:
        """Quartzwood Crasher: X/X trample Dino, X = the combat damage dealt."""
        dmg = int(ctx.get('damage_dealt') or 0)
        if dmg <= 0:
            return []
        return [{"action": "create_token", "player": ctrl,
                 "name": "Dinosaur Beast", "power": dmg, "toughness": dmg,
                 "types": "Token Creature — Dinosaur Beast", "count": 1,
                 "keywords": ["trample"]}]

    def _gen_wheel_of_misfortune(self, ctrl, opp, ctx) -> List[Dict]:
        """Wheel of Misfortune: each player secretly picks a number ≥ 0; the
        highest chooser(s) take that much damage; everyone except the lowest
        chooser wheels (discard hand, draw 7).

        Deterministic model (the Mana Crypt coin-flip / Ancient Bronze
        Dragon d20 convention — no randomness, reproducible per turn): the
        CASTER wants the wheel and picks high (5-7 from a turn hash), so
        they take that damage and wheel; the OPPONENT picks low (0-2),
        dodges the damage, and as the lowest chooser keeps their hand. The
        true hidden-choice game is not modeled — documented approximation,
        strictly better than the prior misresolution (caster discarded with
        no draw, no damage anywhere).
        """
        turn = ctx.get('turn_number') or ctx.get('turn') or 0
        hi = 5 + (sum(ord(ch) for ch in f"wom-hi-{turn}") % 3)  # 5-7
        return [
            {"action": "deal_damage", "amount": hi, "target_player": ctrl,
             "source": "Wheel of Misfortune"},
            {"action": "discard", "player": ctrl, "card": "all"},
            {"action": "draw_cards", "player": ctrl, "amount": 7},
        ]

    def _gen_reveal_top_land_or_draw(self, ctrl, opp, ctx) -> List[Dict]:
        """Thrasios-class: reveal top of library — land → battlefield tapped,
        otherwise draw a card. Reads the ACTUAL library top from ctx."""
        library = ctx.get('controller_library', []) or []
        if not library:
            return [{"action": "no_action",
                     "reason": "library is empty — nothing to reveal"}]
        top = library[0]
        top_name = getattr(top, 'name', str(top))
        type_line = (getattr(top, 'type_line', '') or '').lower()
        if 'land' in type_line:
            return [{"action": "move_card", "card": top_name,
                     "from_zone": "library", "to_zone": "battlefield",
                     "player": ctrl, "tapped": True}]
        return [{"action": "draw_cards", "player": ctrl, "amount": 1}]

    def _gen_stromkirk_occultist(self, ctrl, opp, ctx) -> List[Dict]:
        """Stromkirk Occultist: connect → impulse-exile the top of YOUR library.

        Same shape as Light Up the Stage's playable exile (the Aug 1 impulse
        machinery): the exiled card is marked playable_from_exile for the
        controller. Gated on damage_dealt so a declare-time scan can't misfire.
        """
        dmg = int(ctx.get('damage_dealt') or 0)
        if dmg <= 0:
            return []
        return [{"action": "exile_top_of_library", "player": ctrl, "count": 1,
                 "playable": True}]

    def _gen_drana_liberator(self, ctrl, opp, ctx) -> List[Dict]:
        """Drana, Liberator of Malakir: connect → +1/+1 counter on each
        attacking creature you control.

        Drana has first strike, so on the printed card the counters land
        between the FS and regular damage steps; our dispatch drains at the
        end of combat resolution, so the pump benefits NEXT combat rather
        than the same swing's regular step — the counters themselves are
        never lost. Gated on damage_dealt like the rest of the family.
        """
        dmg = int(ctx.get('damage_dealt') or 0)
        if dmg <= 0:
            return []
        battlefield = ctx.get('controller_battlefield', []) or []
        # Prefer game.attackers (authoritative combat list) over battlefield
        # .attacking flags — the Boros Elite stale-flag lesson, same batch.
        _g = ctx.get('_game')
        _atk_ids = set(getattr(_g, 'attackers', None) or []) if _g else None
        def _is_attacking(c):
            if _atk_ids is not None:
                return getattr(c, 'id', None) in _atk_ids
            return getattr(c, 'attacking', False)
        actions = [
            {"action": "add_counters", "card": creature.name,
             "counter_type": "+1/+1", "amount": 1}
            for creature in battlefield
            if _is_attacking(creature)
        ]
        return actions

    # --- July 31 batch-10 generators (the batch-15324 refused-trigger tail) ---

    def _gen_attack_self_counter(self, ctrl, opp, ctx) -> List[Dict]:
        """'Whenever this creature attacks, put a +1/+1 counter on it'
        (Predator Ooze). Declare-time: the combat-damage dispatch sharing
        this registry must not re-fire it."""
        if ctx.get('damage_dealt'):
            return []
        name = ctx.get('attacking_name')
        if not name:
            return []
        return [{"action": "add_counters", "card": name,
                 "counter_type": "+1/+1", "amount": 1}]

    def _gen_combat_damage_self_counter(self, ctrl, opp, ctx) -> List[Dict]:
        """'Whenever this creature deals combat damage to a player, put a
        +1/+1 counter on it' (Bloodmad Vampire; the Slith-family shape)."""
        if not ctx.get('damage_dealt'):
            return []
        name = ctx.get('attacking_name')
        if not name:
            return []
        return [{"action": "add_counters", "card": name,
                 "counter_type": "+1/+1", "amount": 1}]

    def _gen_port_razer(self, ctrl, opp, ctx) -> List[Dict]:
        """Port Razer connects: 'untap each creature you control. After this
        phase, there is an additional combat phase.' (bulk-verified July 31).
        Aug 1 deferred slate: the additional combat phase IS granted now via
        the additional_combat action (the Moraug consumption loop runs it on
        the autoplay human path; the Claude path discards with a
        breadcrumb). The 'can't attack a player it has already attacked
        this turn' rider stays unmodeled (needs per-defender attack
        tracking; irrelevant in two-player where the extra combat targets
        the same opponent anyway)."""
        if not ctx.get('damage_dealt'):
            return []
        ctrl_player = ctx.get('_controller_player')
        if ctrl_player is None:
            return []
        actions = [{"action": "untap", "card": self._cd_name(c)}
                   for c in (getattr(ctrl_player, 'battlefield', []) or [])
                   if 'creature' in self._cd_type_line(c)
                   and getattr(c, 'tapped', False)]
        actions.append({"action": "additional_combat", "source": "Port Razer"})
        print(f"[ATTACK-TEMPLATE] Port Razer: untapping {len(actions) - 1} "
              f"creature(s) + granting an additional combat phase")
        return actions

    def _gen_aurelia_warleader(self, ctrl, opp, ctx) -> List[Dict]:
        """Aurelia, the Warleader — 'Whenever Aurelia attacks for the first
        time each turn, untap all creatures you control. After this phase,
        there is an additional combat phase.' (bulk-verified Aug 2).

        Declare-time trigger (damage_dealt-gated out so the combat-damage
        dispatch sharing this registry can't re-fire it). The first-time-
        each-turn condition is tracked in game._attack_trigger_turn_stamps —
        a second Aurelia attack the same turn (only reachable inside an
        extra combat, usually one she granted) finds the stamp and declines
        per the printed condition."""
        if ctx.get('damage_dealt'):
            return []
        g = ctx.get('_game')
        if g is not None:
            stamps = getattr(g, '_attack_trigger_turn_stamps', None)
            turn = getattr(g, 'turn_number', 0)
            if stamps is not None:
                if stamps.get('aurelia, the warleader') == turn:
                    return [{"action": "no_action",
                             "reason": "Aurelia: not her first attack this "
                                       "turn (first-time-each-turn condition)"}]
                stamps['aurelia, the warleader'] = turn
        ctrl_player = ctx.get('_controller_player')
        actions = []
        if ctrl_player is not None:
            actions = [{"action": "untap", "card": self._cd_name(c)}
                       for c in (getattr(ctrl_player, 'battlefield', []) or [])
                       if 'creature' in self._cd_type_line(c)
                       and getattr(c, 'tapped', False)]
        actions.append({"action": "additional_combat",
                        "source": "Aurelia, the Warleader"})
        print(f"[ATTACK-TEMPLATE] Aurelia, the Warleader: untapping "
              f"{len(actions) - 1} creature(s) + granting an additional "
              f"combat phase")
        return actions

    def _gen_frenzied_trapbreaker(self, ctrl, opp, ctx) -> List[Dict]:
        """Frenzied Trapbreaker (Outland Liberator's night face) attacks:
        'destroy target artifact or enchantment defending player controls.'
        Declare-time trigger — the combat-damage dispatch sharing this
        registry must not re-fire it. Mandatory target: pick the defending
        player's best (highest-MV) artifact/enchantment; none = the trigger
        fizzles per CR 603.3c (handled no-op)."""
        if ctx.get('damage_dealt'):
            return []
        opp_player = ctx.get('_opponent_player')
        if opp_player is None:
            return []
        targets = [c for c in (getattr(opp_player, 'battlefield', []) or [])
                   if ('artifact' in self._cd_type_line(c)
                       or 'enchantment' in self._cd_type_line(c))]
        if not targets:
            return []  # no legal target — fizzles (CR 603.3c)

        def _mv(c):
            try:
                return int(getattr(c, 'cmc', 0) or 0)
            except (TypeError, ValueError):
                return 0
        best = max(targets, key=_mv)
        return [{"action": "destroy", "card": self._cd_name(best)}]

    def _gen_boros_elite_battalion(self, ctrl, opp, ctx) -> List[Dict]:
        """Battalion — Whenever this creature and at least two other
        creatures attack, this creature gets +2/+2 until end of turn.
        Declare-time, condition-gated on the REAL attacker count (CR 603.4
        intervening-if class: below three attackers = handled no-op, never a
        Tier-3 escalation the combat-shape guard would refuse anyway).
        The scan-side fix (ability-word prefix strip in
        _is_self_attack_trigger_paragraph) is what makes this reachable —
        the whole Battalion family was silently dropped before July 31."""
        if ctx.get('damage_dealt'):
            return []
        ctrl_player = ctx.get('_controller_player')
        if ctrl_player is None:
            return []
        # Aug 1 batch-12: count from game.attackers (the authoritative
        # per-combat list, cleared unconditionally at each resolution)
        # rather than battlefield .attacking flags — a stale flag from an
        # earlier combat fired Battalion with only TWO declared attackers
        # (game_1532756674203619470: Blade Instructor + Boros Elite, one
        # surviving Cavalry Pegasus carrying a leaked flag). Flag fallback
        # kept for callers with no _game in ctx.
        _g = ctx.get('_game')
        if _g is not None and getattr(_g, 'attackers', None) is not None:
            attackers = len(_g.attackers)
        else:
            attackers = sum(
                1 for c in (getattr(ctrl_player, 'battlefield', []) or [])
                if getattr(c, 'attacking', False))
        if attackers < 3:
            return [{"action": "no_action",
                     "reason": "Battalion: fewer than three attackers"}]
        name = ctx.get('attacking_name') or "Boros Elite"
        # Aug 2 batch-14 audit (I-3): pump THE ATTACKING INSTANCE, not every
        # copy of the name — in 4-of formats a second non-attacking Boros
        # Elite on the battlefield also got +2/+2 (game_1533407519135764574:
        # [LAYERS-PT] showed #6138 AND #5616 modified with one Elite
        # declared). The handler honors include_id when provided; name-only
        # stays as the fallback for callers with no card object in ctx.
        _atk_obj = ctx.get('_attacking_creature')
        _action = {"action": "pump_all_creatures", "player": ctrl,
                   "card": name, "power": 2, "toughness": 2}
        _atk_id = getattr(_atk_obj, 'id', '') if _atk_obj is not None else ''
        if _atk_id:
            _action["include_id"] = _atk_id
        return [_action]

    def _gen_underworld_sentinel_attack(self, ctrl, opp, ctx) -> List[Dict]:
        """Underworld Sentinel attacks: exile target creature card from your
        graveyard (mandatory), linked so the dies trigger can return it."""
        if ctx.get('damage_dealt'):
            return []
        ctrl_player = ctx.get('_controller_player')
        game = ctx.get('_game')
        if ctrl_player is None or game is None:
            return []
        gy_creatures = [c for c in (getattr(ctrl_player, 'graveyard', []) or [])
                        if 'creature' in (getattr(c, 'type_line', '') or '').lower()]
        if not gy_creatures:
            return []  # no legal target — the trigger fizzles (CR 603.3c)

        def _pw(c):
            try:
                return int(getattr(c, 'power', 0) or 0)
            except (TypeError, ValueError):
                return 0
        best = max(gy_creatures, key=_pw)
        key = f"underworld sentinel|{ctrl}"
        game._linked_exiles.setdefault(key, []).append(best.name)
        return [{"action": "move_card", "card": best.name,
                 "from_zone": "graveyard", "to_zone": "exile", "player": ctrl}]

    def _gen_underworld_sentinel_dies(self, ctrl, opp, ctx) -> List[Dict]:
        """Underworld Sentinel dies: put all cards exiled with it onto the
        battlefield. Reads the linkage the attack generator recorded and
        VERIFIES each name is still in the controller's exile, so a failed
        or reordered exile self-heals instead of returning the wrong card."""
        game = ctx.get('_game')
        ctrl_player = ctx.get('_controller_player')
        if game is None or ctrl_player is None:
            return []
        key = f"underworld sentinel|{ctrl}"
        names = game._linked_exiles.pop(key, [])
        if not names:
            return []
        exile_names = {c.name for c in (getattr(ctrl_player, 'exile', []) or [])}
        return [{"action": "move_card", "card": n, "from_zone": "exile",
                 "to_zone": "battlefield", "player": ctrl}
                for n in names if n in exile_names]

    def _gen_yidris_cascade_grant(self, ctrl, opp, ctx) -> List[Dict]:
        """Yidris, Maelstrom Wielder connects: spells cast from your hand
        this turn gain cascade (recorded by grant_hand_cascade; consulted by
        the cascade block in mtg/triggers.py at cast time)."""
        if not ctx.get('damage_dealt'):
            return []
        return [{"action": "grant_hand_cascade", "player": ctrl,
                 "source": "Yidris, Maelstrom Wielder"}]

    def _gen_combat_damage_draw_one(self, ctrl, opp, ctx) -> List[Dict]:
        """Ohran Frostfang / Tovolar: draw 1 when the source itself connects."""
        if not ctx.get('damage_dealt'):
            return []
        return [{"action": "draw_cards", "player": ctrl, "amount": 1}]

    def _gen_neheb_dreadhorde(self, ctrl, opp, ctx) -> List[Dict]:
        """Neheb, Dreadhorde Champion: "may discard any number of cards. If
        you do, draw that many cards and add that much {R}."

        The choice is modeled as: pitch lands beyond the second in hand
        (dead weight late-game) and rummage them into fresh cards + {R}.
        No excess lands = decline the optional discard. The "you don't lose
        this mana as steps and phases end" rider is unmodeled — the engine's
        pool empties on phase change."""
        if not ctx.get('damage_dealt'):
            return []
        hand = ctx.get('controller_hand', []) or []
        lands = [c for c in hand if 'land' in self._cd_type_line(c)]
        excess = lands[2:]
        if not excess:
            return []  # declines the optional discard
        actions = [{"action": "discard", "player": ctrl,
                    "card": self._cd_name(c)} for c in excess]
        actions.append({"action": "draw_cards", "player": ctrl,
                        "amount": len(excess)})
        actions.append({"action": "add_mana", "player": ctrl, "color": "R",
                        "amount": len(excess)})
        return actions

    def _gen_glissa_sunslayer(self, ctrl, opp, ctx) -> List[Dict]:
        """Glissa Sunslayer modal: destroy the opponent's best enchantment if
        one exists, else draw a card and lose 1 (declined below 6 life).
        Mode three (remove counters) is not modeled."""
        if not ctx.get('damage_dealt'):
            return []
        opp_player = ctx.get('_opponent_player')
        ench = [c for c in (getattr(opp_player, 'battlefield', []) or [])
                if 'enchantment' in self._cd_type_line(c)]
        if ench:
            best = max(ench, key=self._cd_cmc)
            return [{"action": "destroy", "card": self._cd_name(best)}]
        life = ctx.get('controller_life', 40)
        if life < 6:
            return []  # handled no-op — declines the life payment mode
        return [{"action": "draw_cards", "player": ctrl, "amount": 1},
                {"action": "lose_life", "player": ctrl, "amount": 1}]

    def _gen_ancient_bronze_dragon(self, ctrl, opp, ctx) -> List[Dict]:
        """Ancient Bronze Dragon: roll a d20, put that many +1/+1 counters on
        up to two target creatures. The roll uses a deterministic hash of the
        turn number (the Mana Crypt coin-flip convention) for reproducible
        autoplay; targets are the controller's two strongest creatures."""
        if not ctx.get('damage_dealt'):
            return []
        turn = ctx.get('turn_number') or ctx.get('turn') or 0
        roll = (sum(ord(ch) for ch in f"abd-roll-{turn}") % 20) + 1
        ctrl_player = ctx.get('_controller_player')
        own = [c for c in (getattr(ctrl_player, 'battlefield', []) or [])
               if 'creature' in self._cd_type_line(c)]
        if not own:
            return []
        own.sort(key=self._cd_cmc, reverse=True)
        return [{"action": "add_counters", "card": self._cd_name(c),
                 "counter_type": "+1/+1", "amount": roll}
                for c in own[:2]]

    def _gen_flaxen_intruder(self, ctrl, opp, ctx) -> List[Dict]:
        """Flaxen Intruder: "you may sacrifice it. When you do, destroy
        target artifact or enchantment." Sacrifice only when the opponent has
        one worth destroying; otherwise decline and keep the creature."""
        if not ctx.get('damage_dealt'):
            return []
        opp_player = ctx.get('_opponent_player')
        targets = [c for c in (getattr(opp_player, 'battlefield', []) or [])
                   if ('artifact' in self._cd_type_line(c)
                       or 'enchantment' in self._cd_type_line(c))
                   and 'land' not in self._cd_type_line(c)]
        if not targets:
            return []  # declines the optional sacrifice
        best = max(targets, key=self._cd_cmc)
        return [{"action": "sacrifice_permanent", "player": ctrl,
                 "preferred_card": "Flaxen Intruder", "only_preferred": True,
                 "source": "Flaxen Intruder",
                 "reason": "Flaxen Intruder: sacrificed after combat damage"},
                {"action": "destroy", "card": self._cd_name(best)}]

    def _gen_spell_exile_target(self, ctrl, opp, ctx) -> List[Dict]:
        """Exile target permanent — for instant/sorcery spells like Utter End, Swords to Plowshares."""
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland')
        target_owner = ctx.get('explicit_target_owner') or opp
        if target:
            return [{"action": "move_card", "card": target, "from_zone": "battlefield", "to_zone": "exile", "player": target_owner}]
        match = ctx.get('_match')
        target_type = self._sanitize_target_type(match.group(1).strip()) if match else "permanent"
        return [{"action": "no_action", "reason": f"No valid {target_type} to exile"}]

    def _gen_fatal_push(self, ctrl, opp, ctx) -> List[Dict]:
        """Fatal Push: destroy creature with MV ≤ 2 (≤ 4 with revolt).

        July 29 batch audit: `explicit_target_mv` had NO producer anywhere
        (build_game_context now writes it), so target_mv was always 0, the
        gate below was dead code, and a cascade Fatal Push destroyed a
        mana-value-5 Solitude. Revolt is not tracked by the engine, so the
        non-revolt limit (2) applies — under-destroying is the safe error
        direction (a wrong no_action beats an illegal destroy).
        """
        has_revolt = ctx.get('revolt', False)
        mv_limit = 4 if has_revolt else 2
        explicit = ctx.get('explicit_target_name')
        if explicit:
            # Honor the declared target; apply the printed condition to IT
            # (CR 608.2b — an illegal-condition target means the spell does
            # nothing, it does not retarget).
            target_mv = int(ctx.get('explicit_target_mv', 0) or 0)
            if target_mv > mv_limit:
                revolt_note = " (revolt active)" if has_revolt else ""
                return [{"action": "no_action",
                         "reason": f"Fatal Push can't destroy {explicit} "
                                   f"(MV {target_mv} > {mv_limit}{revolt_note})"}]
            return [{"action": "destroy", "card": explicit}]
        # No declared target: auto-pick the best LEGAL one (biggest power
        # among opponent creatures with MV inside the limit).
        legal = [c for c in (ctx.get('_opponent_creatures') or [])
                 if int(c.get('cmc', 0) or 0) <= mv_limit]
        if legal:
            best = max(legal, key=lambda c: c.get('power', 0) or 0)
            return [{"action": "destroy", "card": best['name']}]
        return [{"action": "no_action",
                 "reason": f"No opponent creature with MV {mv_limit} or less for Fatal Push"}]

    def _gen_archmages_charm(self, ctrl, opp, ctx) -> List[Dict]:
        """Archmage's Charm: choose counter, draw 2, or steal MV≤1.

        May 14 audit: the stack-check was matching against the resolving
        Archmage's Charm itself (since template dispatch can fire before the
        spell is removed from the stack), so the AI was "countering" itself
        on empty-stack casts. Filter out self-references and any non-creature
        spell that's the same name.
        """
        target = ctx.get('explicit_target_name') or ctx.get('stack_top_spell', '')
        if ctx.get('stack_has_spell') and target and target.lower() != "archmage's charm":
            return [{"action": "counter_spell", "card": target}]
        # Default to draw 2 — this is the most common non-counter mode
        return [{"action": "draw_cards", "player": ctrl, "amount": 2}]

    def _gen_spell_destroy_target(self, ctrl, opp, ctx) -> List[Dict]:
        """Destroy target permanent — for instant/sorcery spells like Vindicate, Hero's Downfall."""
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland')
        target_owner = ctx.get('explicit_target_owner') or opp
        if target:
            return [{"action": "destroy", "card": target}]
        match = ctx.get('_match')
        target_type = self._sanitize_target_type(match.group(1).strip()) if match else "permanent"
        return [{"action": "no_action", "reason": f"No valid {target_type} to destroy"}]

    def _gen_counters_from_match(self, ctrl, opp, ctx) -> List[Dict]:
        match = ctx.get('_match')
        if match:
            n = self._word_to_num(match.group(1))
            # Target is usually self
            source_card = ctx.get('_source_card_name', 'self')
            return [{"action": "add_counters", "card": source_card, "counter_type": "+1/+1", "amount": n}]
        return []

    def _gen_upkeep_token_from_match(self, ctrl, opp, ctx) -> List[Dict]:
        """Generic upkeep token creation from oracle text pattern."""
        oracle = ctx.get('_oracle', '')
        token_count = 1
        if 'for each equipment attached to' in oracle.lower():
            source = ctx.get('_source_card')
            controller = ctx.get('_controller_player')
            if source is None or controller is None:
                return []
            attachment_ids = set(getattr(source, 'attachments', []) or [])
            token_count = sum(
                1 for permanent in controller.battlefield
                if 'equipment' in (permanent.type_line or '').lower()
                and (getattr(permanent, 'attached_to', None) == source.id
                     or permanent.id in attachment_ids)
            )
            if token_count <= 0:
                return []
            token_match = re.search(
                r'(\d+)/(\d+)\s+(\w[\w\s]*?)(?:\s+creature)?\s+tokens?',
                oracle)
            if token_match:
                power = int(token_match.group(1))
                toughness = int(token_match.group(2))
                name = token_match.group(3).strip().title()
                return [{"action": "create_token", "player": ctrl,
                         "name": name, "power": power, "toughness": toughness,
                         "types": f"Creature - {name}", "count": token_count}]
        # June 10 deep-dive (CRITICAL — Marit Lage's Slumber): the generic
        # upkeep-token pattern regexed straight across an intervening-if
        # condition (CR 603.4) — "if you control ten or more snow permanents,
        # sacrifice … create Marit Lage" became an UNCONDITIONAL vanilla
        # 20/20 named "Black Avatar" (no flying/indestructible/legendary, no
        # sacrifice) every single upkeep, and won a game with three of them.
        # Conditional upkeep triggers need a dedicated name-keyed template;
        # the generic generator refuses them.
        if re.search(r'at the beginning of (?:your|each) upkeep,\s*if\b', oracle.lower()):
            return [{"action": "no_action",
                     "reason": "unsupported conditional upkeep trigger (named template absent; CR 603.4)"}]
        # Try to extract token stats from oracle text
        token_match = re.search(r'(\d+)/(\d+)\s+(\w[\w\s]*?)(?:\s+creature)?\s+tokens?', oracle)
        if token_match:
            power = int(token_match.group(1))
            toughness = int(token_match.group(2))
            name = token_match.group(3).strip().title()
            # Try registry first for known token types
            registry_key = f"{name.lower().replace(' ', '_')}_{power}_{toughness}"
            if registry_key in TOKEN_REGISTRY:
                return [make_token_action(ctrl, registry_key, 1)]
            return [{"action": "create_token", "player": ctrl, "name": name,
                     "power": power, "toughness": toughness,
                     "types": f"Creature — {name}", "count": 1}]
        # Non-creature artifact tokens — use registry
        for artifact_key in ['treasure', 'clue', 'food', 'blood']:
            if artifact_key in oracle:
                return [make_token_action(ctrl, artifact_key, 1)]
        # Fallback
        return [{"action": "no_action", "reason": f"Upkeep token creation (use !fix to add tokens)"}]

    # =========================================================================
    # New Helper Methods — for batch-added templates
    # =========================================================================

    def _edict_effect(self, ctrl, opp, ctx) -> List[Dict]:
        """Fleshbag Marauder / Merciless Executioner: each player sacrifices a creature.

        Aug 7 batch audit (G2-2): an edict is a SACRIFICE (CR 701.19), not a
        destroy — the old destroy emissions let indestructible creatures
        survive their own edict and never fired "whenever you sacrifice"
        watchers (Korvold, Mayhem Devil). Routed through sacrifice_permanent
        (which fires sac triggers, queues deaths, and handles the CR 903.9a
        commander redirect); the generator keeps its weakest-pick heuristic
        as the preferred card and lets the handler's own priority order
        (tokens first, lowest CMC) stand in for "of their choice" when the
        preference is unavailable. This also retires the July-24 D1
        simplification note — the source creature IS the controller's
        sacrifice when it's their only creature, via the preferred-card
        fallthrough.
        """
        actions = []

        def _weakest(infos, skip_name=None):
            best, best_p = None, 999
            for info in infos or []:
                if not isinstance(info, dict):
                    continue
                nm = info.get('name', '')
                if skip_name and nm.lower() == skip_name:
                    continue
                p = info.get('power', 0) or 0
                if p < best_p:
                    best_p, best = p, nm
            return best

        # Opponent: sacrifices a creature of their choice.
        opp_creatures = ctx.get('_opponent_creatures', [])
        opp_pick = _weakest(opp_creatures) or ctx.get('best_opponent_creature')
        if opp_pick or opp_creatures:
            actions.append({"action": "sacrifice_permanent", "player": opp,
                            "type_filter": "creature",
                            "preferred_card": opp_pick or "",
                            "reason": ctx.get('_source_card_name') or "edict: sacrifice a creature"})
        else:
            actions.append({"action": "no_action", "reason": f"{opp} has no creatures to sacrifice"})

        # Controller: sacrifices too (symmetric effect). July 24 batch-6
        # audit (reviewer D1): the controller's MANDATORY sacrifice must not
        # be skipped when the opponent had one (CR 701.20); when the source
        # is the controller's only creature, IT is the sacrifice.
        ctrl_creatures = ctx.get('_controller_creatures', [])
        source_name = (ctx.get('_source_card_name') or '').lower()
        if ctrl_creatures:
            ctrl_pick = _weakest(ctrl_creatures, skip_name=source_name) \
                or ctx.get('_source_card_name', '')
            actions.append({"action": "sacrifice_permanent", "player": ctrl,
                            "type_filter": "creature",
                            "preferred_card": ctrl_pick or "",
                            "reason": ctx.get('_source_card_name') or "edict: sacrifice a creature"})
        return actions

    def _craterhoof_pump(self, ctrl, opp, ctx) -> List[Dict]:
        """Craterhoof Behemoth: creatures you control get +X/+X and trample,
        where X = number of creatures you control (including Craterhoof).

        Uses pump_all_creatures action to apply mass +X/+X and trample.
        """
        creature_count = ctx.get('controller_creature_count', 1)
        return [
            {"action": "pump_all_creatures", "player": ctrl,
             "power": creature_count, "toughness": creature_count,
             "keywords": ["Trample"]},
        ]

    def _kroxa_enters_or_attacks(self, ctrl, opp, ctx) -> List[Dict]:
        """The "enters or attacks" half of Kroxa, shared by the ETB and attack
        dispatchers.

        Printed text (Scryfall via data/card_data_cache.json, re-verified
        July 28 2026):

            "Whenever Kroxa enters or attacks, each opponent discards a card,
             then each opponent who didn't discard a nonland card this way
             loses 3 life."

        The life loss is CONDITIONAL, and it fires on the opposite of what the
        old docstring here claimed: an opponent who pitches a NONLAND card is
        SPARED; one who pitches a land — or who has nothing to pitch — loses 3.
        (The old text also said "deals 3 damage", which is a pre-errata wording;
        the card loses life, so it dodges damage prevention and doublers.)
        The July 27 fanout caught this firing unconditionally, corroborated by
        two independent reviewers in two different games.

        WHICH card an opponent discards is THEIR choice, so we model the
        incentive the card creates: pitch the least-castable nonland to dodge
        the drain, and eat the 3 only when the hand is all lands or empty.
        """
        hand = ctx.get('opponent_hand')
        if hand is None:
            # No hand visibility in this context. Under-apply rather than
            # fabricate a life loss we can't justify — a phantom drain is worse
            # than a missed one (cf. the "Tier 3 fabricated a mana payment"
            # class from the June 10 deep dive).
            print("[KROXA] opponent hand not in context — discarding at random "
                  "and skipping the conditional life loss")
            return [{"action": "discard", "player": opp, "card": "random"}]

        # Two ctx builders populate 'opponent_hand' in DIFFERENT shapes: one
        # stores live Card objects, the other a list of {'name','cmc','is_land'}
        # dicts. Handle both or the fix silently no-ops on half the call sites.
        def _name(entry):
            return (entry.get('name', '') if isinstance(entry, dict)
                    else getattr(entry, 'name', '')) or ''

        def _cmc(entry):
            raw = (entry.get('cmc', 0) if isinstance(entry, dict)
                   else getattr(entry, 'cmc', 0))
            return int(raw or 0)

        def _is_land(entry):
            if isinstance(entry, dict):
                return bool(entry.get('is_land'))
            fn = getattr(entry, 'is_land', None)
            if callable(fn):
                return bool(fn())
            return 'land' in (getattr(entry, 'type_line', '') or '').lower()

        nonlands = [c for c in hand if not _is_land(c)]
        if nonlands:
            # Highest CMC == least castable, matching the engine's own "worst
            # card" convention in the discard handler (mtg/actions.py).
            chosen = max(nonlands, key=_cmc)
            return [{"action": "discard", "player": opp, "card": _name(chosen)}]

        actions: List[Dict] = []
        if hand:
            # Hand is all lands — pitching one still leaves them on the hook.
            actions.append({"action": "discard", "player": opp,
                            "card": _name(hand[0])})
        # Empty hand discards nothing, which is likewise "didn't discard a
        # nonland card this way" — they lose 3 either way.
        actions.append({"action": "lose_life", "player": opp, "amount": 3})
        return actions

    def _gen_kroxa_attack(self, ctrl, opp, ctx) -> List[Dict]:
        """Kroxa's attack trigger — the "enters or attacks" half ONLY.

        The sacrifice clause is worded "When Kroxa ENTERS, sacrifice it unless
        it escaped", so attacking never sacrifices him.
        """
        return self._kroxa_enters_or_attacks(ctrl, opp, ctx)

    def _gen_kroxa_etb(self, ctrl, opp, ctx) -> List[Dict]:
        """Kroxa, Titan of Death's Hunger — BOTH printed triggers fire on entry.

        Printed text:
            "When Kroxa enters, sacrifice it unless it escaped."
            "Whenever Kroxa enters or attacks, each opponent discards a card,
             then each opponent who didn't discard a nonland card this way
             loses 3 life."

        The upkeep branch below is DEFENSIVE ONLY — no printing of Kroxa has an
        upkeep trigger. It exists because resolve_upkeep_trigger delegates to
        resolve_etb by name, so a mis-dispatched upkeep event would otherwise
        replay the discard half every turn.
        """
        oracle = (ctx.get('_oracle') or '').lower()
        is_upkeep = 'beginning of your upkeep' in oracle and 'sacrifice' in oracle
        # June 11 audit: `destroy` put commander Kroxa in the graveyard and
        # also incorrectly made the sacrifice vulnerable to indestructible
        # (game 1514629178413154325). Use the real sacrifice path and require
        # the named permanent so another creature can never pay Kroxa's cost.
        sac_action = {
            "action": "sacrifice_permanent",
            "player": ctrl,
            "type_filter": "creature",
            "preferred_card": "Kroxa, Titan of Death's Hunger",
            "only_preferred": True,
            "allow_commander": True,
            "reason": "Kroxa entered without escaping",
        }
        if is_upkeep:
            if ctx.get('was_escaped', False):
                return [{"action": "no_action", "reason": "Kroxa was escaped — survives upkeep"}]
            return [sac_action]
        # ETB path: the "enters or attacks" half, then the sacrifice clause.
        actions = self._kroxa_enters_or_attacks(ctrl, opp, ctx)
        if not ctx.get('was_escaped', False):
            actions.append(sac_action)
        return actions

    def _gen_capricious_hellraiser_etb(self, ctrl, opp, ctx) -> List[Dict]:
        """Queue Hellraiser's random graveyard exile and real spell-copy cast.

        The synchronous action performs the random selection atomically; its
        generated copy is then cast by the shared async free-cast drain.
        """
        return [{"action": "capricious_hellraiser_etb", "player": ctrl}]

    def _gen_rashmi_cast_trigger(self, ctrl, opp, ctx) -> List[Dict]:
        """Rashmi, Eternities Crafter cast trigger: reveal top card of library.
        If it is a lower-MV nonland, queue a real free cast through the stack;
        a declined or failed cast puts the revealed card into its owner's hand.
        Otherwise, put it into hand.

        Per-turn gating ('first spell each turn') is enforced upstream in
        mtg_game.py's cast-trigger condition check — this generator runs only
        when the trigger is eligible.
        """
        library = ctx.get('controller_library', [])
        if not library:
            return [{"action": "no_action", "reason": "Rashmi: library is empty"}]
        top_card = library[0]
        top_name = getattr(top_card, 'name', 'Unknown')
        type_line = (getattr(top_card, 'type_line', '') or '').lower()
        top_cmc = getattr(top_card, 'cmc', 0) or 0
        is_land = 'land' in type_line
        # Triggering spell's MV (the spell whose cast triggered Rashmi)
        spell_mv = ctx.get('cast_spell_mv', ctx.get('spell_mv', ctx.get('mana_paid_total', 0))) or 0
        if (not is_land) and spell_mv and top_cmc < spell_mv:
            return [{"action": "queue_free_cast", "card": top_name,
                     "from_zone": "library", "fallback_zone": "hand",
                     "player": ctrl, "source": "Rashmi, Eternities Crafter",
                     "reason": (f"Rashmi: may cast {top_name} without paying "
                                f"(MV {top_cmc} < {spell_mv})")}]
        # Otherwise (land, MV too high, or unknown spell MV): put into hand
        return [{"action": "move_card", "card": top_name,
                 "from_zone": "library", "to_zone": "hand",
                 "player": ctrl,
                 "reason": f"Rashmi: {top_name} into hand"}]

    def _gen_thrasios_activation(self, ctrl, opp, ctx) -> List[Dict]:
        """Thrasios, Triton Hero {4}: Scry 1, reveal top; land → battlefield tapped, else draw.

        Implemented as a deterministic pair of actions:
          1. Scry 1 (the smart scry already in place).
          2. Peek top card: if it's a land, put it onto the battlefield tapped;
             otherwise, draw a card.

        Resolves fully deterministically so no "Scry N" / judge placeholder leaks
        to Discord.
        """
        actions = self._scry_n(ctrl, 1)
        library = ctx.get('controller_library', [])
        if not library:
            # Library empty — scry alone is legal, the rest has no valid target
            return actions
        top_card = library[0]
        top_name = getattr(top_card, 'name', 'Unknown')
        type_line = (getattr(top_card, 'type_line', '') or '').lower()
        is_land = 'land' in type_line
        if is_land:
            # Put onto the battlefield tapped. Tapped state applied by follow-up
            # "tap" action since move_card can't carry the tapped flag directly.
            actions.append({"action": "move_card", "card": top_name,
                            "from_zone": "library", "to_zone": "battlefield",
                            "player": ctrl,
                            "reason": f"Thrasios: revealed land {top_name}, enters tapped"})
            actions.append({"action": "tap", "card": top_name})
        else:
            actions.append({"action": "draw_cards", "player": ctrl, "amount": 1})
        return actions

    def _gen_sun_titan(self, ctrl, opp, ctx) -> List[Dict]:
        """Sun Titan: return target permanent card with MV 3 or less from graveyard to battlefield."""
        graveyard = ctx.get('controller_graveyard', [])
        best_target = None
        for c in graveyard:
            # July 30 batch-9 reviewer audit: no permanent-type filter — an
            # Instant (Anguished Unmaking) was returned to the battlefield
            # and sat there for 20+ turns (CR 110.1; the card says
            # "permanent card"). game_1532224002137784391.
            _tl = (getattr(c, 'type_line', '') or '').lower()
            if 'instant' in _tl or 'sorcery' in _tl:
                continue
            try:
                cmc = int(getattr(c, 'cmc', 0) or 0)
            except (ValueError, TypeError):
                cmc = 0
            if cmc <= 3:
                # Prefer creatures, then other permanents
                if best_target is None or (c.is_creature() and not getattr(best_target, 'is_creature', lambda: False)()):
                    best_target = c
        if best_target:
            return [{"action": "move_card", "card": best_target.name,
                     "from_zone": "graveyard", "to_zone": "battlefield", "player": ctrl}]
        return [{"action": "no_action", "reason": "Sun Titan: no permanent with MV 3 or less in graveyard"}]

    def _fight_best_creature(self, ctrl, opp, ctx, source_power: int) -> List[Dict]:
        """Fight effect: source deals damage equal to its power to target creature,
        and target creature deals damage equal to its power back.

        No 'fight' action type exists, so we approximate as mutual damage:
        source deals source_power to target, target deals its power to source.
        """
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')
        target_power = resolve_target_power(ctx, target)
        source_name = ctx.get('_source_card_name', 'source')
        if target:
            return [
                {"action": "deal_damage", "amount": source_power,
                 "target_card": target, "target_controller": opp},
                {"action": "deal_damage", "amount": target_power,
                 "target_card": source_name, "target_controller": ctrl},
            ]
        return [{"action": "no_action", "reason": "No valid creature to fight"}]

    def _fight_from_pattern(self, ctrl, opp, ctx) -> List[Dict]:
        """Fight from oracle text pattern match (ETB fight triggers).

        Uses entering creature's power if available (for self-ETB fights),
        otherwise defaults to 3 as a reasonable estimate.
        """
        # The entering creature IS the source — use its power
        source_power = ctx.get('entering_power', 0)
        source_name = ctx.get('_source_card_name', 'source')
        if source_power == 0:
            # Fallback: estimate from greatest_power context
            source_power = ctx.get('greatest_power', 3)
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')
        target_power = resolve_target_power(ctx, target)
        if target:
            return [
                {"action": "deal_damage", "amount": source_power,
                 "target_card": target, "target_controller": opp},
                {"action": "deal_damage", "amount": target_power,
                 "target_card": source_name, "target_controller": ctrl},
            ]
        return [{"action": "no_action", "reason": "No valid creature to fight"}]

    # --- March 22 log audit helper methods ---

    def _gen_shamanic_revelation(self, ctrl, opp, ctx) -> List[Dict]:
        """Shamanic Revelation: draw per creature + ferocious life gain.
        Ferocious: if you control a creature with power 4+, gain life equal
        to the greatest power among creatures you control (flat amount, NOT per creature).
        """
        creature_count = max(1, ctx.get('controller_creature_count', 0))
        greatest_power = ctx.get('greatest_power', 0)
        actions = [{"action": "draw_cards", "player": ctrl, "amount": creature_count}]
        if greatest_power >= 4:
            # Ferocious: gain life equal to greatest power (not per creature)
            actions.append({"action": "gain_life", "player": ctrl, "amount": greatest_power})
        return actions

    # (The April 2026 _gen_life_from_the_loam lived here — it read a ctx key
    # ('_player') no builder populates, so it silently skipped in live games
    # and the card escalated to Tier 3 anyway. The batch-13 audit added a
    # working replacement WITHOUT the grep-for-existing-def step, creating a
    # shadowing duplicate — deleted Aug 2; the duplicate-def structural pin
    # now makes the whole class unshippable.)

    def _gen_finale_of_glory(self, ctrl, opp, ctx) -> List[Dict]:
        """Finale of Glory: Create X 2/2 white Soldier tokens with vigilance.
        If X >= 10, also create X 4/4 white Angel tokens with flying."""
        x = max(1, ctx.get('x_value', 1))
        actions = [
            {"action": "create_token", "player": ctrl, "name": "Soldier",
             "power": 2, "toughness": 2, "types": "Creature — Soldier",
             "count": x},
        ]
        if x >= 10:
            actions.append(
                {"action": "create_token", "player": ctrl, "name": "Angel",
                 "power": 4, "toughness": 4, "types": "Creature — Angel",
                 "count": x}
            )
        return actions

    def _gen_felidar_guardian(self, ctrl, opp, ctx) -> List[Dict]:
        """Felidar Guardian: exile ANOTHER permanent you control, return it."""
        source = (ctx.get('_source_card_name') or '').lower()
        # Prefer explicit target from AI
        target = ctx.get('explicit_target_name', '')
        if not target or target.lower() == source:
            # Pick best ETB creature that isn't Felidar Guardian
            target = ctx.get('best_own_etb_creature', '')
            if target.lower() == source:
                target = ctx.get('best_own_noncreature', '')
        if not target or target.lower() == source:
            return [{"action": "no_action", "reason": "No other permanent to flicker"}]
        return [{"action": "flicker", "player": ctrl, "target": target}]

    def _gen_oath_of_teferi(self, ctrl, opp, ctx) -> List[Dict]:
        """Oath of Teferi: exile another permanent, return it next end step."""
        source = (ctx.get('_source_card_name') or '').lower()
        target = (ctx.get('explicit_target_name', '')
                  or ctx.get('best_own_etb_creature', '')
                  or ctx.get('best_own_noncreature', ''))
        if not target or target.lower() == source:
            return [{"action": "no_action", "reason": "No other permanent to flicker"}]
        return [
            {"action": "move_card", "card": target,
             "from_zone": "battlefield", "to_zone": "exile", "player": ctrl},
            {"action": "schedule_delayed_trigger",
             "trigger_at": "end_step", "source": "Oath of Teferi",
             "actions": [{"action": "move_card", "card": target,
                          "from_zone": "exile", "to_zone": "battlefield",
                          "player": ctrl}],
             "once": True},
        ]

    def _gen_tooth_and_nail(self, ctrl, opp, ctx) -> List[Dict]:
        """Tooth and Nail — 'Choose one — search up to two creature cards to
        hand; or put up to two creature cards from your hand onto the
        battlefield. Entwine {2}.'

        Aug 2 batch-13 (rashmi/mythic reviewer): the old JSON entry resolved
        the ENTWINED result (search 2 → battlefield) unconditionally, so a
        base-cost cast got both modes for {2} less than any legal line. Now
        gated on ctx['entwined'] (stamped by _compute_alt_costs when the
        cost was actually paid — the kicker truth-plumbing pattern). The
        prior Python version of this generator was DEAD (never registered)
        and emitted action types that exist nowhere in actions.py — the
        silent-no-op class; this one uses verified vocabulary only
        (search_library, move_card)."""
        if ctx.get('entwined'):
            return [{"action": "search_library", "player": ctrl, "count": 2,
                     "card_type": "creature", "to_zone": "battlefield",
                     "reason": "Tooth and Nail (entwined): 2 creatures to battlefield"}]
        # Un-entwined: ONE mode. Battlefield mode only when the hand holds a
        # real BOMB (power >= 4 or MV >= 5) — Aug 2 mode-choice refinement:
        # the first heuristic took ANY hand creature, so a pair of mana
        # dorks would eat the put-onto-battlefield mode while searching two
        # threats to hand was strictly better. Otherwise search to hand.
        hand = ctx.get('controller_hand') or []
        bombs = [
            c for c in hand
            if 'creature' in self._cd_type_line(c)
            and (self._cd_power_int(c) >= 4 or self._cd_cmc(c) >= 5)]
        if bombs:
            bombs.sort(key=lambda c: self._cd_power_int(c), reverse=True)
            return [{"action": "move_card", "card": self._cd_name(c),
                     "from_zone": "hand", "to_zone": "battlefield",
                     "player": ctrl}
                    for c in bombs[:2]]
        return [{"action": "search_library", "player": ctrl, "count": 2,
                 "card_type": "creature", "to_zone": "hand",
                 "reason": "Tooth and Nail: search two creature cards to hand"}]

    # --- Aug 2 batch-14 Tier-3 shrink: the batch's top escalations ---
    # Each of these was a real, repeated Claude-API call resolving a
    # deterministic effect. Measured over batch 15334: Sire of Insanity ×17,
    # Song of the Worldsoul ×16, Arclight Phoenix ×14, Glissa ×11, Ozolith ×8.

    # --- Aug 2, 2026: ABILITY-WORD CONDITION cards (CR 207.2c) ---
    # Every one of these used its WEAK half forever, because no delirium /
    # morbid / metalcraft / coven predicate existed anywhere in the engine.

    def _gen_reaper_from_the_abyss(self, ctrl, opp, ctx) -> List[Dict]:
        """Reaper from the Abyss — "Morbid — At the beginning of each end
        step, if a creature died this turn, destroy target non-Demon
        creature." The whole reason to play a 6-mana 6/6, and it fired
        never: no morbid predicate existed and no template matched, so the
        end-step scan produced nothing at all."""
        from mtg.helpers import has_morbid
        game = ctx.get('_game')
        if game is None or not has_morbid(game):
            return [{"action": "no_action",
                     "reason": "Reaper from the Abyss: no creature died this turn"}]
        opp_player = ctx.get('_opponent_player')
        cands = [c for c in (getattr(opp_player, 'battlefield', []) or [])
                 if c.is_creature(game=game)
                 and 'demon' not in (getattr(c, 'type_line', '') or '').lower()
                 and not getattr(c, '_phased_out', False)] if opp_player else []
        if not cands:
            return [{"action": "no_action",
                     "reason": "Reaper from the Abyss: no non-Demon creature to destroy"}]
        best = max(cands, key=lambda c: self._eff_power(c, game))
        print(f"[CONDITION] Reaper from the Abyss: morbid MET — destroying "
              f"{best.name}")
        return [{"action": "destroy", "card": self._cd_name(best),
                 "source": "Reaper from the Abyss"}]

    def _gen_puresteel_paladin_etb(self, ctrl, opp, ctx) -> List[Dict]:
        """Puresteel Paladin — "Whenever an Equipment you control enters, you
        may draw a card." (Its metalcraft half — equip {0} with three or more
        artifacts — is a STATIC cost modification, wired separately in the
        equip-cost path.) Nothing resolved for this card at all before."""
        return [{"action": "draw_cards", "player": ctrl, "amount": 1}]

    def _gen_polukranos_monstrosity(self, ctrl, opp, ctx) -> List[Dict]:
        """Polukranos, World Eater — "When this becomes monstrous, it deals X
        damage divided as you choose among any number of target creatures
        your opponents control. Each of those creatures deals damage equal to
        its power to Polukranos."

        MONSTROSITY (CR 701.32) had no handling: the activation put no
        counters on and never set the monstrous flag, so the fight-like
        trigger could not fire either. X comes from the activation's paid X
        (the July-31 auto-sizing threads it through as ctx['x_value']).
        Divided damage is modelled as the standard "kill what you can"
        heuristic: spend X on the biggest creatures it can actually finish.
        """
        game = ctx.get('_game')
        opp_player = ctx.get('_opponent_player')
        x = int(ctx.get('x_value') or 0)
        if x <= 0 or opp_player is None:
            return [{"action": "no_action",
                     "reason": "Polukranos: monstrosity X was 0"}]
        cands = sorted(
            (c for c in (getattr(opp_player, 'battlefield', []) or [])
             if c.is_creature(game=game)
             and not getattr(c, '_phased_out', False)),
            key=lambda c: self._eff_toughness(c, game))
        actions = []
        remaining = x
        for c in cands:
            need = max(1, self._eff_toughness(c, game)
                       - int(getattr(c, 'damage_marked', 0) or 0))
            if need > remaining:
                continue
            remaining -= need
            actions.append({"action": "deal_damage", "amount": need,
                            "target_card": self._cd_name(c),
                            "target_controller": opp,
                            "source": "Polukranos, World Eater"})
            # CR: each of those creatures deals damage equal to its power
            # back to Polukranos.
            back = self._eff_power(c, game)
            if back > 0:
                actions.append({"action": "deal_damage", "amount": back,
                                "target_card": "Polukranos, World Eater",
                                "target_controller": ctrl,
                                "source": self._cd_name(c)})
        if not actions:
            return [{"action": "no_action",
                     "reason": "Polukranos: X too small to finish any creature"}]
        return actions

    @staticmethod
    def _eff_power(card, game) -> int:
        try:
            return int(card.get_effective_power(game))
        except (AttributeError, TypeError, ValueError):
            try:
                return int(card.power or 0)
            except (TypeError, ValueError):
                return 0

    @staticmethod
    def _eff_toughness(card, game) -> int:
        try:
            return int(card.get_effective_toughness(game))
        except (AttributeError, TypeError, ValueError):
            try:
                return int(card.toughness or 0)
            except (TypeError, ValueError):
                return 1

    def _gen_tragic_slip(self, ctrl, opp, ctx) -> List[Dict]:
        """Tragic Slip — "-1/-1. Morbid: -13/-13 instead if a creature died
        this turn." Without a morbid check this was a permanent -1/-1, i.e.
        a removal spell that removed nothing."""
        from mtg.helpers import has_morbid
        game = ctx.get('_game')
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')
        if not target:
            return [{"action": "no_action",
                     "reason": "Tragic Slip: no creature to target"}]
        morbid = has_morbid(game) if game is not None else False
        amt = -13 if morbid else -1
        print(f"[CONDITION] Tragic Slip: morbid={'MET' if morbid else 'not met'}"
              f" — {amt}/{amt}")
        return [{"action": "pump_all_creatures", "player": opp,
                 "card": target, "power": amt, "toughness": amt,
                 "source": "Tragic Slip"}]

    def _gen_unholy_heat(self, ctrl, opp, ctx) -> List[Dict]:
        """Unholy Heat — 2 damage, or 6 with delirium."""
        from mtg.helpers import has_delirium, graveyard_card_types
        ctrl_player = ctx.get('_controller_player')
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')
        if not target:
            return [{"action": "no_action",
                     "reason": "Unholy Heat: no creature or planeswalker to target"}]
        deli = has_delirium(ctrl_player) if ctrl_player is not None else False
        types = (len(graveyard_card_types(ctrl_player))
                 if ctrl_player is not None else 0)
        print(f"[CONDITION] Unholy Heat: delirium={'MET' if deli else 'not met'}"
              f" ({types} card type(s)) — {6 if deli else 2} damage")
        return [{"action": "deal_damage", "amount": 6 if deli else 2,
                 "target_card": target, "target_controller": opp,
                 "source": "Unholy Heat"}]

    def _gen_traverse_the_ulvenwald(self, ctrl, opp, ctx) -> List[Dict]:
        """Traverse the Ulvenwald — basic land, or any creature/land with
        delirium."""
        from mtg.helpers import has_delirium
        ctrl_player = ctx.get('_controller_player')
        deli = has_delirium(ctrl_player) if ctrl_player is not None else False
        print(f"[CONDITION] Traverse the Ulvenwald: "
              f"delirium={'MET' if deli else 'not met'}")
        # NOTE the handler's filter is a substring match on the type line,
        # so "creature_or_land" (what the old JSON entry asked for) matches
        # NOTHING — that template silently tutored nothing at all. With
        # delirium the strictly better half of "creature or land" is the
        # creature, so search that; the approximation is documented rather
        # than silent.
        return [{"action": "search_library", "player": ctrl,
                 "card_type": "creature" if deli else "basic land",
                 "destination": "hand"}]

    def _gen_sire_of_insanity(self, ctrl, opp, ctx) -> List[Dict]:
        """Sire of Insanity — "At the beginning of each end step, each player
        discards their hand." Symmetric and unconditional; the controller
        discards too."""
        return [
            {"action": "discard", "player": ctrl, "card": "all"},
            {"action": "discard", "player": opp, "card": "all"},
        ]

    def _gen_song_of_the_worldsoul(self, ctrl, opp, ctx) -> List[Dict]:
        """Song of the Worldsoul — "Whenever you cast a spell, populate."

        The populate handler is already CR 701.34a-correct (it copies a
        creature token you control, and does NOTHING when you control none —
        the July 20 fix after Tier 3 fabricated a token out of thin air).
        """
        return [{"action": "populate", "player": ctrl}]

    def _gen_glissa_the_traitor(self, ctrl, opp, ctx) -> List[Dict]:
        """Glissa, the Traitor — "Whenever a creature an opponent controls
        dies, you may return target artifact card from your graveyard to your
        hand."

        The opponent-scope gate lives in the dies scan (July 24), so by the
        time this runs the death is already known to be an opponent's. The
        return is a "may" with a target: no artifact in the graveyard means
        no legal target and the trigger simply does nothing (CR 603.3c).
        """
        ctrl_player = ctx.get('_controller_player')
        if ctrl_player is None:
            return []
        artifacts = [c for c in (getattr(ctrl_player, 'graveyard', []) or [])
                     if 'artifact' in (getattr(c, 'type_line', '') or '').lower()]
        if not artifacts:
            return [{"action": "no_action",
                     "reason": "no artifact card in your graveyard to return"}]

        def _mv(c):
            try:
                return int(getattr(c, 'cmc', 0) or 0)
            except (TypeError, ValueError):
                return 0
        best = max(artifacts, key=_mv)
        return [{"action": "move_card", "card": self._cd_name(best),
                 "from_zone": "graveyard", "to_zone": "hand", "player": ctrl}]

    def _gen_the_ozolith_combat(self, ctrl, opp, ctx) -> List[Dict]:
        """The Ozolith — "At the beginning of combat on your turn, if The
        Ozolith has counters on it, you may move all counters from The
        Ozolith onto target creature."

        CR 603.4 intervening-if: no counters, no trigger. Moves every counter
        TYPE it is holding (it accumulates whatever left the battlefield),
        onto the controller's best creature.
        """
        game = ctx.get('_game')
        ctrl_player = ctx.get('_controller_player')
        if game is None or ctrl_player is None:
            return []
        from mtg.helpers import names_match
        ozolith = next(
            (c for c in (getattr(ctrl_player, 'battlefield', []) or [])
             if names_match(getattr(c, 'name', ''), "The Ozolith")), None)
        counters = dict(getattr(ozolith, 'counters', {}) or {}) if ozolith else {}
        counters = {k: v for k, v in counters.items() if v > 0}
        if not counters:
            return [{"action": "no_action",
                     "reason": "The Ozolith has no counters on it"}]
        target = ctx.get('best_own_creature')
        if not target:
            return [{"action": "no_action",
                     "reason": "The Ozolith: no creature to move counters onto"}]
        actions = []
        for ctype, amount in counters.items():
            actions.append({"action": "remove_counters",
                            "card": "The Ozolith",
                            "counter_type": ctype, "amount": amount})
            actions.append({"action": "add_counters", "card": target,
                            "counter_type": ctype, "amount": amount})
        return actions

    def _gen_arclight_phoenix(self, ctrl, opp, ctx) -> List[Dict]:
        """Arclight Phoenix — "At the beginning of combat on your turn, if
        you've cast three or more instant and sorcery spells this turn,
        return this card from your graveyard to the battlefield."

        Two shapes reach here. When the Phoenix is already ON the battlefield
        (the only zone the beginning-of-combat scan used to walk), the return
        is a no-op — that accounted for all 14 Tier-3 escalations in batch
        15334, every one of them resolving nothing. The graveyard pass added
        alongside this template is what makes the real ability reachable.
        """
        game = ctx.get('_game')
        ctrl_player = ctx.get('_controller_player')
        if game is None or ctrl_player is None:
            return []
        from mtg.helpers import names_match
        in_graveyard = any(
            names_match(getattr(c, 'name', ''), "Arclight Phoenix")
            for c in (getattr(ctrl_player, 'graveyard', []) or []))
        if not in_graveyard:
            return [{"action": "no_action",
                     "reason": "Arclight Phoenix is not in your graveyard"}]
        cast_count = int(
            getattr(ctrl_player, 'instant_sorcery_spells_cast_this_turn', 0) or 0)
        if cast_count < 3:
            return [{"action": "no_action",
                     "reason": (f"Arclight Phoenix: only {cast_count} instant/"
                                f"sorcery spell(s) cast this turn (needs 3)")}]
        return [{"action": "move_card", "card": "Arclight Phoenix",
                 "from_zone": "graveyard", "to_zone": "battlefield",
                 "player": ctrl}]

    def _gen_combustible_gearhulk(self, ctrl, opp, ctx) -> List[Dict]:
        """Combustible Gearhulk — "When this creature enters, target opponent
        MAY have you draw three cards. If the player doesn't, you mill three
        cards, then this creature deals damage to that player equal to the
        total mana value of those cards."

        Aug 2 batch-14 audit (mythic reviewer): no template existed, so the
        generic "when X enters ... draw N cards" pattern swallowed the
        sentence — `.+?`/`.*?` matched straight across "target opponent may
        have you" — and every Gearhulk resolved as an unconditional draw-3
        for its controller, discarding the opponent's choice AND the entire
        mill+burn branch.

        The choice is the OPPONENT's, and they make it WITHOUT seeing the
        cards (CR 601: the mill happens after the decision). Modelling it by
        peeking at the top three would be cheating with hidden information
        and would make the card strictly worse than printed, so the decision
        uses only what that player can actually know: the average mana value
        of the controller's remaining library, times three. Take the damage
        when it is affordable; hand over the cards when the expected hit is
        a serious fraction of remaining life. The RESOLUTION then uses the
        real milled cards, so the damage is exact even though the choice was
        made blind.
        """
        game = ctx.get('_game')
        ctrl_player = ctx.get('_controller_player')
        opp_player = ctx.get('_opponent_player')
        if game is None or ctrl_player is None or opp_player is None:
            # No context to reason with — the printed default for an
            # unmodellable choice is the harmless half.
            return [{"action": "draw_cards", "player": ctrl, "amount": 3}]

        library = list(getattr(ctrl_player, 'library', []) or [])
        if len(library) < 3:
            # Milling three would deck the controller; a rational opponent
            # takes that line every time.
            return [{"action": "mill", "player": ctrl, "amount": 3}]

        def _mv(card):
            try:
                return int(getattr(card, 'cmc', 0) or 0)
            except (TypeError, ValueError):
                return 0

        avg_mv = sum(_mv(c) for c in library) / float(len(library))
        expected = int(round(avg_mv * 3))
        opp_life = int(getattr(opp_player, 'life', 0) or 0)
        # Lethal, or a third of remaining life, is worth three cards.
        gives_cards = opp_life <= expected or expected * 3 >= opp_life
        if gives_cards:
            print(f"[GEARHULK] {opp_player.name} gives {ctrl_player.name} 3 "
                  f"cards rather than risk ~{expected} damage (life {opp_life})")
            return [{"action": "draw_cards", "player": ctrl, "amount": 3}]

        actual = sum(_mv(c) for c in library[:3])
        print(f"[GEARHULK] {opp_player.name} declines (expected ~{expected} "
              f"vs life {opp_life}) — milling 3 for {actual} damage")
        actions = [{"action": "mill", "player": ctrl, "amount": 3}]
        if actual > 0:
            actions.append({"action": "deal_damage", "amount": actual,
                            "target_player": opp,
                            "source": "Combustible Gearhulk"})
        return actions

    def _gen_everflowing_chalice(self, ctrl, opp, ctx) -> List[Dict]:
        """Everflowing Chalice — 'Multikicker {2} ... enters with a charge
        counter for each time it was kicked.' Reads the kicked_times truth
        (stamped by _compute_alt_costs when the multikicker cost was PAID).
        Unkicked = a real zero-counter entry, handled no-op."""
        k = int(ctx.get('kicked_times') or 0)
        if k <= 0:
            return [{"action": "no_action",
                     "reason": "not kicked — enters with no charge counters"}]
        return [{"action": "add_counters", "card": "Everflowing Chalice",
                 "counter_type": "charge", "amount": k}]

    def _gen_chandra_spark_hunter_combat(self, ctrl, opp, ctx) -> List[Dict]:
        """Chandra, Spark Hunter — 'At the beginning of combat on your turn,
        choose up to one target Vehicle you control. Until end of turn, it
        becomes an artifact creature and gains haste.' Picks the biggest
        un-animated Vehicle; none = handled none-chosen no-op."""
        ctrl_player = ctx.get('_controller_player')
        best, best_pw = None, -1
        for c in (getattr(ctrl_player, 'battlefield', None) or []):
            tl = self._cd_type_line(c)
            if 'vehicle' in tl and 'creature' not in tl:
                pw = self._cd_power_int(c)
                if pw > best_pw:
                    best, best_pw = c, pw
        if best is None:
            return [{"action": "no_action",
                     "reason": "no Vehicle to animate (up to one target — "
                               "resolves with none chosen)"}]
        return [{"action": "animate_permanent", "player": ctrl,
                 "scope": "target", "card": self._cd_name(best),
                 "required_type": "artifact", "use_printed_pt": True,
                 "keywords": "haste"}]

    def _gen_life_from_the_loam(self, ctrl, opp, ctx) -> List[Dict]:
        """Life from the Loam — 'Return up to three target land cards from
        your graveyard to your hand.' (Aug 2 batch-13: was a zero-action
        Tier-3 escalation; deterministic template returns up to three lands.
        Dredge stays unmodeled.)"""
        gy = ctx.get('controller_graveyard') or []
        lands = [c for c in gy if 'land' in self._cd_type_line(c)]
        if not lands:
            return [{"action": "no_action",
                     "reason": "no land cards in graveyard (up to-targets — "
                               "resolves with none chosen)"}]
        return [{"action": "move_card", "card": self._cd_name(c),
                 "from_zone": "graveyard", "to_zone": "hand", "player": ctrl}
                for c in lands[:3]]

    def _gen_worldgorger_dragon(self, ctrl, opp, ctx) -> List[Dict]:
        """Worldgorger Dragon: exile all other permanents you control.
        When Worldgorger Dragon LTB, they return (handled by _check_ltb_triggers_sync)."""
        ctrl_creatures = ctx.get('_controller_creatures', [])
        actions = []
        for info in ctrl_creatures:
            name = info.get('name', '') if isinstance(info, dict) else ''
            if name and name.lower() != 'worldgorger dragon':
                actions.append({"action": "move_card", "card": name,
                               "from_zone": "battlefield", "to_zone": "exile", "player": ctrl})
                # Track exiled cards for LTB return
                actions.append({"action": "track_exiled_by", "source": "Worldgorger Dragon",
                               "card": name, "owner": ctrl})
        if not actions:
            return [{"action": "no_action", "reason": "No other permanents to exile"}]
        return actions

    def _gen_oblivion_ring(self, ctrl, opp, ctx) -> List[Dict]:
        """Oblivion Ring / Banishing Light: exile target nonland permanent opponent controls.
        When this LTB, the exiled card returns (handled by _check_ltb_triggers_sync)."""
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland')
        source = ctx.get('_source_card_name', 'Oblivion Ring')
        if target:
            return [
                {"action": "move_card", "card": target,
                 "from_zone": "battlefield", "to_zone": "exile", "player": opp},
                {"action": "track_exiled_by", "source": source,
                 "card": target, "owner": opp},
            ]
        return [{"action": "no_action", "reason": "No nonland permanent to exile"}]

    def _gen_eerie_interlude(self, ctrl, opp, ctx) -> List[Dict]:
        """Eerie Interlude: exile any number of target creatures you control.
        Return them to the battlefield at the beginning of the next end step.
        Uses the delayed trigger system."""
        # Get all controller's creatures to exile
        ctrl_creatures = ctx.get('_controller_creatures', [])
        if not ctrl_creatures:
            return [{"action": "no_action", "reason": "No creatures to exile with Eerie Interlude"}]
        # Exile all creatures and schedule their return
        actions = []
        return_actions = []
        for info in ctrl_creatures:
            name = info.get('name', '') if isinstance(info, dict) else ''
            if name:
                actions.append({"action": "move_card", "card": name,
                               "from_zone": "battlefield", "to_zone": "exile", "player": ctrl})
                return_actions.append({"action": "move_card", "card": name,
                                      "from_zone": "exile", "to_zone": "battlefield", "player": ctrl})
        # Schedule delayed trigger to return them at end step
        if return_actions:
            actions.append({"action": "schedule_delayed_trigger",
                           "trigger_at": "end_step", "source": "Eerie Interlude",
                           "actions": return_actions, "once": True})
        return actions

    def _gen_temur_sabertooth(self, ctrl, opp, ctx) -> List[Dict]:
        """Temur Sabertooth activated ability: {1}{G}, Return another creature you
        control to its owner's hand: Target creature you control gains indestructible
        until end of turn.

        Heuristic: bounce the lowest-MV non-Temur creature (preferably one with an
        ETB so re-casting is value), grant indestructible to the highest-MV
        remaining creature.
        """
        bf = ctx.get('controller_battlefield', []) or []
        source_name = (ctx.get('_source_card_name') or 'temur sabertooth').lower()
        own_creatures = []
        for c in bf:
            type_line = (getattr(c, 'type_line', '') or '').lower()
            if 'creature' in type_line and (getattr(c, 'name', '') or '').lower() != source_name:
                own_creatures.append(c)
        if not own_creatures:
            return [{"action": "no_action",
                     "reason": "Temur Sabertooth: no other creature to return"}]
        # Bounce: lowest-MV creature (cheap to re-cast, often ETB value)
        bounce_target = min(own_creatures,
                            key=lambda c: int(getattr(c, 'cmc', 0) or 0))
        # Indestructible target: highest-MV creature still on board after bounce
        remaining = [c for c in own_creatures if c is not bounce_target]
        # Temur can also target itself for indestructible (it's a creature you control)
        indestructible_candidates = remaining + [c for c in bf
                                                  if (getattr(c, 'name', '') or '').lower() == source_name]
        if not indestructible_candidates:
            indestructible_target = None
        else:
            indestructible_target = max(indestructible_candidates,
                                        key=lambda c: int(getattr(c, 'cmc', 0) or 0))
        actions = [{"action": "move_card", "card": bounce_target.name,
                    "from_zone": "battlefield", "to_zone": "hand",
                    "player": ctrl}]
        if indestructible_target is not None:
            actions.append({"action": "grant_keywords", "player": ctrl,
                            "target_card": indestructible_target.name,
                            "keywords": ["indestructible"]})
        return actions

    def _gen_coiling_oracle(self, ctrl, opp, ctx) -> List[Dict]:
        """Coiling Oracle: reveal top card. If land, put onto battlefield. Otherwise, put into hand."""
        library = ctx.get('controller_library', [])
        if library:
            top_card = library[0]
            type_line = (getattr(top_card, 'type_line', '') or '').lower()
            if 'land' in type_line:
                # Put land directly onto battlefield (tapped to be safe)
                return [{"action": "move_card", "card": top_card.name,
                         "from_zone": "library", "to_zone": "battlefield",
                         "player": ctrl, "enters_tapped": True}]
            else:
                # Put non-land into hand (same as drawing)
                return [{"action": "draw_cards", "player": ctrl, "amount": 1}]
        return [{"action": "draw_cards", "player": ctrl, "amount": 1}]

    def _gen_primal_command(self, ctrl, opp, ctx) -> List[Dict]:
        """Resolve the two modes actually chosen for Primal Command."""
        aliases = {
            "1": 1, "life": 1, "gain life": 1,
            "2": 2, "top": 2, "noncreature": 2, "put on top": 2,
            "3": 3, "graveyard": 3, "shuffle": 3,
            "4": 4, "creature": 4, "search": 4, "tutor": 4,
        }

        def _mode_number(value):
            if isinstance(value, int) and 1 <= value <= 4:
                return value
            text = str(value or "").strip().lower()
            if text in aliases:
                return aliases[text]
            for key, number in aliases.items():
                if len(key) > 1 and key in text:
                    return number
            return None

        raw_modes = ctx.get('_modes') or []
        if not isinstance(raw_modes, (list, tuple)):
            raw_modes = [raw_modes]
        modes = []
        for raw in raw_modes:
            number = _mode_number(raw)
            if number and number not in modes:
                modes.append(number)
            if len(modes) == 2:
                break

        ctrl_player = ctx.get('_controller_player')
        opp_player = ctx.get('_opponent_player')
        game = ctx.get('_game')
        players = [p for p in (ctrl_player, opp_player) if p is not None]
        explicit = [str(name).strip() for name in
                    (ctx.get('explicit_target_names') or []) if str(name).strip()]
        if not explicit and ctx.get('explicit_target_name'):
            explicit = [str(ctx['explicit_target_name']).strip()]

        def _named_player(default):
            for name in explicit:
                for candidate in players:
                    if candidate.name.lower() == name.lower():
                        return candidate
            return default

        def _noncreature_target():
            for name in explicit:
                for owner in players:
                    for permanent in owner.battlefield:
                        if (permanent.name.lower() == name.lower()
                                and not permanent.is_creature(game)):
                            return owner, permanent
            for owner in (opp_player, ctrl_player):
                if owner is None:
                    continue
                legal = [c for c in owner.battlefield
                         if not c.is_creature(game)]
                if legal:
                    return owner, max(legal, key=lambda c: int(c.cmc or 0))
            return None, None

        if not modes:
            target_owner, target_card = _noncreature_target()
            modes = [2, 4] if target_card is not None else [1, 4]
        for fallback in (4, 1, 3, 2):
            if len(modes) == 2:
                break
            if fallback not in modes:
                modes.append(fallback)

        actions = []
        for mode in modes[:2]:
            if mode == 1:
                recipient = _named_player(ctrl_player)
                actions.append({"action": "gain_life", "player": recipient.name,
                                "amount": 7, "source": "Primal Command"})
            elif mode == 2:
                owner, permanent = _noncreature_target()
                if permanent is None:
                    actions.append({"action": "no_action",
                                    "reason": "Primal Command: no legal noncreature permanent target"})
                else:
                    actions.append({"action": "move_card", "card": permanent.name,
                                    "from_zone": "battlefield", "to_zone": "library",
                                    "position": "top", "player": owner.name,
                                    "reason": "Primal Command mode 2"})
            elif mode == 3:
                recipient = _named_player(opp_player or ctrl_player)
                actions.append({"action": "shuffle_graveyard_into_library",
                                "player": recipient.name,
                                "reason": "Primal Command mode 3"})
            elif mode == 4:
                actions.append({"action": "search_library", "player": ctrl,
                                "card_type": "Creature", "count": 1,
                                "to_zone": "hand", "reason": "Primal Command mode 4"})
        return actions

    def _gen_ghostly_flicker(self, ctrl, opp, ctx) -> List[Dict]:
        """Ghostly Flicker: exile TWO target artifacts, creatures, or lands you control, return them.
        Only artifacts, creatures, or lands are legal targets — NOT enchantments."""
        explicit = ctx.get('explicit_target_names', [])
        target1 = explicit[0] if len(explicit) == 2 else ctx.get('best_own_etb_creature', '')
        # Second target must be an artifact, creature, or land — NOT enchantment
        target2 = (explicit[1] if len(explicit) == 2
                   else ctx.get('best_own_flickerable', ''))
        actions = []
        if target1:
            actions.append({"action": "flicker", "player": ctrl, "target": target1})
        if target2 and target2 != target1:
            actions.append({"action": "flicker", "player": ctrl, "target": target2})
        if len(actions) != 2:
            return [{"action": "no_action", "reason": "Ghostly Flicker requires two distinct legal targets"}]
        return actions

    def _gen_restoration_angel(self, ctrl, opp, ctx) -> List[Dict]:
        """Restoration Angel: exile ANOTHER target non-Angel creature you control, return it.
        Cannot target itself or Angel creatures."""
        source = (ctx.get('_source_card_name') or '').lower()
        target = ctx.get('explicit_target_name', '')
        # Don't target itself or Angels
        if not target or target.lower() == source or 'angel' in target.lower():
            target = ctx.get('best_own_etb_creature', '')
        if target.lower() == source or 'angel' in target.lower():
            target = ''
        # Aug 1 batch-12 (reviewer, partner game): the best_own_noncreature
        # fallback that lived here was a copy-paste from the Felidar/Oath
        # "another target permanent" family — Restoration Angel's printed
        # ability targets a CREATURE only, and the fallback flickered
        # Aminatou the planeswalker (CR 601.2c). No creature = the "you may"
        # declines.
        if not target or target.lower() == source:
            return [{"action": "no_action", "reason": "No valid non-Angel creature to flicker"}]
        return [{"action": "flicker", "player": ctrl, "target": target}]

    def _gen_teferis_protection(self, ctrl, opp, ctx) -> List[Dict]:
        """Teferi's Protection: life total can't change + protection from everything + phase out all permanents.
        Phasing is NOT exile — permanents return automatically at next untap."""
        return [
            {"action": "phase_out_all", "player": ctrl,
             "reason": "Teferi's Protection phases out all permanents until next turn"},
            {"action": "prevent_all_damage", "player": ctrl,
             "duration": "until_next_turn",
             "lock_life_total": True,
             "reason": "Teferi's Protection: life total can't change, protection from everything"},
        ]

    def _gen_buried_alive(self, ctrl, opp, ctx) -> List[Dict]:
        """Buried Alive: search library for up to 3 creature cards, put them into graveyard."""
        return [
            {"action": "search_library_to_graveyard", "player": ctrl, "count": 3,
             "card_type": "creature", "reason": "Buried Alive: put 3 creatures from library into graveyard"},
        ]

    # (_gen_reanimate deleted Aug 7 registry dedup — it was dead code shadowed
    # by the _reanimate_gen registration since the Aug 5 verification, and the
    # dedup removed its only registration.)

    def _gen_living_death(self, ctrl, opp, ctx) -> List[Dict]:
        """Living Death: each player exiles all creatures from their graveyard,
        then sacrifices all creatures, then puts all exiled creature cards onto battlefield."""
        return [
            {"action": "living_death", "reason": "Living Death: swap battlefield creatures with graveyard creatures"},
        ]

    def _gen_fact_or_fiction(self, ctrl, opp, ctx) -> List[Dict]:
        """Fact or Fiction: simplified as draw 3, mill 2 (approximation of pile split)."""
        return [
            {"action": "draw_cards", "player": ctrl, "amount": 3},
            {"action": "mill", "player": ctrl, "amount": 2},
        ]

    def _gen_garruk_minus3(self, ctrl, opp, ctx) -> List[Dict]:
        """Garruk, Primal Hunter -3: draw cards equal to greatest power among creatures you control."""
        greatest_power = ctx.get('greatest_power', 0)
        if greatest_power > 0:
            return [{"action": "draw_cards", "player": ctrl, "amount": greatest_power}]
        return [{"action": "no_action", "reason": "No creatures on battlefield for Garruk -3"}]

    def _gen_victimize(self, ctrl, opp, ctx) -> List[Dict]:
        """Victimize: sacrifice a creature you control, return two creatures from your graveyard."""
        worst = ctx.get('controller_worst_creature')
        gy_creatures = ctx.get('controller_graveyard_creatures', [])
        if not gy_creatures:
            return [{"action": "no_action", "reason": "No creature cards in controller's graveyard"}]
        if not worst:
            return [{"action": "no_action", "reason": "No creature available to sacrifice for Victimize"}]

        # June 11 audit (game 1514621888587108423): the former template
        # destroyed the fodder, then returned both targets unconditionally
        # and untapped. Keep the returns nested under the sacrifice action
        # so Victimize's "If you do" clause is enforced by the interpreter.
        returns = [
            {"action": "move_card", "card": creature_name,
             "from_zone": "graveyard", "to_zone": "battlefield",
             "player": ctrl, "tapped": True}
            for creature_name in gy_creatures[:2]
        ]
        return [{
            "action": "sacrifice_permanent",
            "player": ctrl,
            "type_filter": "creature",
            "preferred_card": worst,
            "reason": "Victimize",
            "then_actions": returns,
        }]

    def _gen_goblin_guide_attack(self, ctrl, opp, ctx) -> List[Dict]:
        """Goblin Guide: defending player reveals top card. If land, put into their hand.

        In autoplay, we approximate: ~40% chance top card is a land → opponent draws.
        If library context is available, check the actual top card.
        """
        # Check if we have actual library access
        opp_library = ctx.get('opponent_library', [])
        if opp_library:
            top_card = opp_library[0] if opp_library else None
            if top_card:
                type_line = ''
                if hasattr(top_card, 'type_line'):
                    type_line = (top_card.type_line or '').lower()
                elif isinstance(top_card, dict):
                    type_line = top_card.get('types', '').lower()
                card_name = top_card.name if hasattr(top_card, 'name') else (
                    top_card.get('name', 'unknown') if isinstance(top_card, dict) else str(top_card))
                if 'land' in type_line:
                    return [{"action": "move_card", "card": card_name,
                             "from_zone": "library", "to_zone": "hand", "player": opp,
                             "reason": f"Goblin Guide: revealed {card_name} (land) — {opp} puts it into hand"}]
                return [{"action": "no_action",
                         "reason": f"Goblin Guide: revealed {card_name} (not a land)"}]

        # No library access — default to revealing (the game engine will handle it)
        return [{"action": "reveal_top_card", "player": opp,
                 "if_land": "to_hand",
                 "reason": "Goblin Guide: defending player reveals top card, land goes to hand"}]

    def _gen_jace_brainstorm(self, ctrl, opp, ctx) -> List[Dict]:
        """Jace TMS 0: Draw 3, put 2 back on top of library.

        Uses `put_back_from_hand` rather than baking specific card names into
        move_card actions — the latter relied on a pre-draw hand snapshot that
        went stale after the draw resolved, which could cause zone-integrity
        bugs (cards 'returned' that were never actually in hand).
        """
        return [
            {"action": "draw_cards", "player": ctrl, "amount": 3},
            {"action": "put_back_from_hand", "player": ctrl, "count": 2,
             "reason": "Jace TMS 0: put 2 cards from hand on top of library"},
        ]

    def _gen_aminatou_plus1(self, ctrl, opp, ctx) -> List[Dict]:
        """Aminatou +1: draw a card, then put one from hand on top."""
        return [
            {"action": "draw_cards", "player": ctrl, "amount": 1},
            {"action": "put_back_from_hand", "player": ctrl, "count": 1,
             "reason": "Aminatou +1: put a card from hand on top of library"},
        ]

    def _gen_natures_claim(self, ctrl, opp, ctx) -> List[Dict]:
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_artifact_enchantment', '')
        owner = ctx.get('explicit_target_owner') or opp
        if not target:
            return [{"action": "no_action", "reason": "Nature's Claim has no legal target"}]
        return [
            {"action": "destroy", "card": target},
            {"action": "gain_life", "player": owner, "amount": 4},
        ]

    def _gen_unbreakable_formation(self, ctrl, opp, ctx) -> List[Dict]:
        actions = [{"action": "grant_keywords", "player": ctrl,
                    "target": "all_own_creatures", "keywords": ["Indestructible"],
                    "duration": "end_of_turn"}]
        phase = str(ctx.get('phase', '')).lower()
        if phase in ('main1', 'main2', 'precombat_main', 'postcombat_main'):
            actions.extend([
                {"action": "add_counters", "player": ctrl,
                 "target": "all_own_creatures", "counter_type": "+1/+1", "amount": 1},
                {"action": "grant_keywords", "player": ctrl,
                 "target": "all_own_creatures", "keywords": ["Vigilance"],
                 "duration": "end_of_turn"},
            ])
        return actions

    def _gen_teferi_time_raveler_minus3(self, ctrl, opp, ctx) -> List[Dict]:
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland', '')
        owner = ctx.get('explicit_target_owner') or opp
        actions = []
        if target:
            actions.append({"action": "move_card", "card": target,
                            "from_zone": "battlefield", "to_zone": "hand",
                            "player": owner})
        actions.append({"action": "draw_cards", "player": ctrl, "amount": 1})
        return actions

    def _gen_calix_minus3(self, ctrl, opp, ctx) -> List[Dict]:
        targets = list(ctx.get('_pw_targets') or [])
        exile_target = targets[0] if targets else None
        source_enchantment = targets[1] if len(targets) > 1 else None
        exile_name = getattr(exile_target, 'name', exile_target) or ctx.get('explicit_target_name', '')
        exile_owner = ctx.get('explicit_target_owner') or opp
        if source_enchantment is None:
            for permanent in ctx.get('controller_battlefield', []):
                if 'enchantment' in (getattr(permanent, 'type_line', '') or '').lower():
                    source_enchantment = permanent
                    break
        source_name = getattr(source_enchantment, 'name', source_enchantment) or ''
        if not exile_name or not source_name:
            return [{"action": "no_action", "reason": "Calix -3 requires both legal targets"}]
        return [
            {"action": "move_card", "card": exile_name, "player": exile_owner,
             "from_zone": "battlefield", "to_zone": "exile"},
            {"action": "track_exiled_by", "source": source_name,
             "card": exile_name, "owner": exile_owner},
        ]

    def _gen_the_abyss_upkeep(self, ctrl, opp, ctx) -> List[Dict]:
        game = ctx.get('_game')
        active = getattr(game, 'active_player', None)
        if active is None:
            return [{"action": "no_action", "reason": "The Abyss: no active player"}]
        candidates = [c for c in active.battlefield
                      if c.is_creature()
                      and 'artifact' not in (c.type_line or '').lower()
                      and not getattr(c, '_phased_out', False)]
        if not candidates:
            return [{"action": "no_action",
                     "reason": f"The Abyss: {active.name} controls no nonartifact creature"}]
        # The affected player chooses; autoplay preserves their highest-value
        # creature and destroys their lowest-MV legal choice.
        chosen = min(candidates, key=lambda c: getattr(c, 'cmc', 0) or 0)
        return [{"action": "destroy", "card": chosen.name,
                 "target_controller": active.name}]

    def _gen_aura_shards(self, ctrl, opp, ctx) -> List[Dict]:
        """Aura Shards: you may destroy target artifact or enchantment when a creature enters."""
        best_target = ctx.get('best_opponent_artifact_enchantment', '')
        if not best_target:
            return [{"action": "no_action", "reason": "Aura Shards: no artifact/enchantment to destroy"}]
        return [{"action": "destroy", "card": best_target,
                 "reason": f"Aura Shards triggers: destroy {best_target}"}]

    def _gen_searing_blaze(self, ctrl, opp, ctx) -> List[Dict]:
        """Searing Blaze: 1 dmg (or 3 with landfall) to target creature and its controller.
        Requires a creature target — illegal to cast without one."""
        best_creature = ctx.get('best_opponent_creature')
        if not best_creature:
            return [{"action": "no_action",
                     "reason": "Searing Blaze requires a creature target (opponent controls none)"}]
        landfall = ctx.get('controller_played_land_this_turn', False)
        dmg = 3 if landfall else 1
        return [
            {"action": "deal_damage", "amount": dmg, "target_player": opp},
            {"action": "deal_damage", "amount": dmg,
             "target_card": best_creature, "target_controller": opp},
        ]

    def _gen_martial_coup(self, ctrl, opp, ctx) -> List[Dict]:
        """Martial Coup: create X 1/1 Soldiers; if X >= 5, destroy all other creatures."""
        x = ctx.get('x_value', 0)
        if x <= 0:
            return [{"action": "no_action", "reason": "Martial Coup with X=0"}]
        actions = []
        if x >= 5:
            actions.append({"action": "destroy_all_creatures", "except_tokens": True,
                            "controller": ctrl})
        actions.append({"action": "create_token", "player": ctrl, "name": "Soldier",
                        "power": 1, "toughness": 1, "types": "Creature — Soldier",
                        "count": x})
        return actions

    def _gen_inquisition(self, ctrl, opp, ctx) -> List[Dict]:
        """Inquisition of Kozilek: opponent discards a nonland with MV 3 or less."""
        opp_hand = ctx.get('opponent_hand', [])
        # Pick the best target: highest CMC nonland with MV <= 3
        best = None
        for card_info in opp_hand:
            if isinstance(card_info, dict):
                name = card_info.get('name', '')
                cmc = card_info.get('cmc', 0)
                is_land = card_info.get('is_land', False)
            else:
                name = str(card_info)
                cmc = 0
                is_land = False
            if not is_land and cmc <= 3:
                if best is None or cmc > best[1]:
                    best = (name, cmc)
        if best:
            return [{"action": "discard", "player": opp, "card": best[0]}]
        return [{"action": "no_action", "reason": "No valid targets in opponent's hand"}]

    def _gen_increasing_devotion(self, ctrl, opp, ctx) -> List[Dict]:
        """Increasing Devotion: create 5 Human tokens (10 if cast from graveyard via flashback).

        Doubling Season / Parallel Lives double the token count via the
        replacement engine after this action is emitted.
        """
        # Check if cast from graveyard (flashback) — double the tokens
        from_graveyard = ctx.get('_cast_from_graveyard', False)
        if not from_graveyard:
            # Fall back to inspecting the source card attribute set at cast time.
            src = ctx.get('_source_card')
            if src is not None and getattr(src, '_cast_from_graveyard', False):
                from_graveyard = True
        count = 10 if from_graveyard else 5
        return [{"action": "create_token", "player": ctrl, "name": "Human",
                 "power": 1, "toughness": 1, "types": "Creature — Human", "count": count}]

    def _gen_wrenn_plus1(self, ctrl, opp, ctx) -> List[Dict]:
        """Wrenn and Six +1: Return target land card from graveyard to hand.

        Prefers fetchlands and duals (by CMC approximation) over basics.
        Handles both Card objects and dict representations safely.
        """
        graveyard = ctx.get('controller_graveyard', [])
        # July 29 batch audit: honor a declared land target when it's actually
        # a land in the controller's graveyard (the plan said Wooded
        # Foothills, the heuristic returned Bloodstained Mire — same
        # declared-target-discarded family as Teferi's -3).
        explicit = (ctx.get('explicit_target_name') or '').strip()
        if explicit:
            for c in graveyard:
                _name = c.name if hasattr(c, 'name') else (
                    c.get('name', '') if isinstance(c, dict) else str(c))
                _tl = (getattr(c, 'type_line', None)
                       or (c.get('type_line') if isinstance(c, dict) else '') or '')
                if _name.lower() == explicit.lower() and 'land' in _tl.lower():
                    return [{"action": "move_card", "card": _name,
                             "from_zone": "graveyard", "to_zone": "hand",
                             "player": ctrl}]
        best_land = None
        best_score = -1
        for c in graveyard:
            try:
                is_land = (
                    (hasattr(c, 'is_land') and c.is_land()) or
                    (isinstance(c, dict) and 'land' in (c.get('type_line') or '').lower())
                )
                if not is_land:
                    continue
                is_token = (
                    getattr(c, 'is_token', False) if hasattr(c, 'is_token')
                    else (c.get('is_token', False) if isinstance(c, dict) else False)
                )
                if is_token:
                    continue
                name = c.name if hasattr(c, 'name') else c.get('name', '') if isinstance(c, dict) else str(c)
                # Score: non-basics > basics; fetch lands score highest
                name_lower = name.lower()
                is_basic = hasattr(c, 'is_basic') and c.is_basic() if hasattr(c, 'is_basic') else 'basic' in (getattr(c, 'type_line', '') or '').lower()
                is_fetch = any(k in name_lower for k in ('fetch', 'scalding tarn', 'misty rainforest', 'verdant catacombs', 'marsh flats', 'arid mesa', 'windswept heath', 'wooded foothills', 'bloodstained mire', 'flooded strand', 'polluted delta'))
                score = (2 if is_fetch else 1 if not is_basic else 0)
                if score > best_score:
                    best_score = score
                    best_land = name
            except Exception:
                continue
        if best_land:
            return [{"action": "move_card", "card": best_land,
                     "from_zone": "graveyard", "to_zone": "hand", "player": ctrl}]
        return [{"action": "no_action", "reason": "Wrenn and Six: no land cards in graveyard"}]

    # --- Wrenn and Seven (July 31 batch-11; see the registration comment) ---

    @staticmethod
    def _w7_name_and_type(c):
        name = c.name if hasattr(c, 'name') else (
            c.get('name', '') if isinstance(c, dict) else str(c))
        tl = (getattr(c, 'type_line', None)
              or (c.get('type_line') if isinstance(c, dict) else '') or '')
        return name, tl.lower()

    def _gen_w7_plus1(self, ctrl, opp, ctx) -> List[Dict]:
        """+1: Reveal the top four cards of your library. Put all land cards
        revealed this way into your hand and the rest into your graveyard.
        Deterministic from the actual library top; same-name physical-copy
        ambiguity in move_card is game-equivalent (basics)."""
        library = ctx.get('controller_library')
        if library is None:
            return None  # no library visibility → Tier 3
        actions = []
        for c in list(library)[:4]:
            name, tl = self._w7_name_and_type(c)
            actions.append({"action": "move_card", "card": name,
                            "from_zone": "library",
                            "to_zone": "hand" if 'land' in tl else "graveyard",
                            "player": ctrl})
        if not actions:
            return [{"action": "no_action",
                     "reason": "Wrenn and Seven +1: library is empty"}]
        return actions

    def _gen_w7_zero(self, ctrl, opp, ctx) -> List[Dict]:
        """0: Put any number of land cards from your hand onto the
        battlefield tapped. 'Any number' resolves to ALL — the ability is
        pure upside for the activator."""
        hand = ctx.get('controller_hand')
        if hand is None:
            return None  # no hand visibility → Tier 3
        actions = []
        for c in list(hand):
            name, tl = self._w7_name_and_type(c)
            if 'land' in tl:
                actions.append({"action": "move_card", "card": name,
                                "from_zone": "hand", "to_zone": "battlefield",
                                "player": ctrl})
                actions.append({"action": "tap", "card": name})
        if not actions:
            return [{"action": "no_action",
                     "reason": "Wrenn and Seven 0: no land cards in hand"}]
        return actions

    def _gen_w7_minus3(self, ctrl, opp, ctx) -> List[Dict]:
        """-3: a green Treefolk with reach whose P/T equal lands you control.
        Snapshot approximation of the token's printed CDA (the established
        create_token convention — dynamic P/T text isn't modeled)."""
        lands = int(ctx.get('controller_land_count', 0) or 0)
        return [{"action": "create_token", "player": ctrl, "name": "Treefolk",
                 "power": lands, "toughness": lands,
                 "types": "Token Creature — Treefolk", "count": 1,
                 "keywords": ["reach"]}]

    def _gen_thoughtseize(self, ctrl, opp, ctx) -> List[Dict]:
        """Thoughtseize: opponent discards a nonland card (any MV). Caster loses 2 life."""
        opp_hand = ctx.get('opponent_hand', [])
        best = None
        for card_info in opp_hand:
            if isinstance(card_info, dict):
                name = card_info.get('name', '')
                cmc = card_info.get('cmc', 0)
                is_land = card_info.get('is_land', False)
            else:
                name = str(card_info)
                cmc = 0
                is_land = False
            if not is_land:
                if best is None or cmc > best[1]:
                    best = (name, cmc)
        actions = [{"action": "lose_life", "player": ctrl, "amount": 2}]
        if best:
            actions.append({"action": "discard", "player": opp, "card": best[0]})
        else:
            actions.append({"action": "no_action", "reason": "No nonland cards in opponent's hand"})
        return actions

    def _gen_kolaghans_command(self, ctrl, opp, ctx) -> List[Dict]:
        """Kolaghan's Command — choose two of four modes.

        Modes:
          1 / "artifact"  — Destroy target artifact
          2 / "recur"     — Return target creature card from your graveyard to your hand
          3 / "discard"   — Target player discards a card
          4 / "damage"    — Deal 2 damage to any target

        Reads ctx['_modes'] for explicit selection; otherwise defaults to
        modes 3+4 (both always have legal targets).
        """
        modes = ctx.get('_modes')
        # Normalize modes: accept ints (1-4) or strings ("artifact"/"recur"/"discard"/"damage")
        normalized = []
        if modes:
            for m in modes:
                if isinstance(m, int):
                    normalized.append(m)
                elif isinstance(m, str):
                    name_to_idx = {'artifact': 1, 'recur': 2, 'discard': 3, 'damage': 4,
                                   'destroy': 1, 'return': 2, 'graveyard': 2, 'mill': 3}
                    idx = name_to_idx.get(m.lower())
                    if idx:
                        normalized.append(idx)
        if not normalized:
            normalized = [3, 4]  # always-legal default

        # Cap at 2 (CR 700.2: choose two means at most two)
        normalized = normalized[:2]

        # Per-mode action emission
        actions = []
        opp_artifacts = ctx.get('opponent_artifacts', [])
        ctrl_gy_creatures = ctx.get('controller_graveyard_creatures', [])
        for m in normalized:
            if m == 1 and opp_artifacts:
                # Destroy target artifact
                actions.append({"action": "destroy", "card": opp_artifacts[0]})
            elif m == 2 and ctrl_gy_creatures:
                # Return target creature card from graveyard to hand
                actions.append({"action": "move_card", "card": ctrl_gy_creatures[0],
                                "from_zone": "graveyard", "to_zone": "hand", "player": ctrl})
            elif m == 3:
                actions.append({"action": "discard", "player": opp, "card": "random"})
            elif m == 4:
                actions.append({"action": "deal_damage", "amount": 2, "target_player": opp})
        if not actions:
            # Fallback if all chosen modes had no legal targets (e.g. modes 1+2 with empty
            # opp_artifacts and empty graveyard) — fall back to damage + discard.
            actions = [
                {"action": "deal_damage", "amount": 2, "target_player": opp},
                {"action": "discard", "player": opp, "card": "random"},
            ]
        return actions

    def _gen_blood_on_the_snow(self, ctrl, opp, ctx) -> List[Dict]:
        """Resolve the chosen wipe, then select the capped return afterward."""
        modes = ctx.get('_modes') or []
        if not isinstance(modes, (list, tuple)):
            modes = [modes]
        chosen = None
        for mode in modes:
            is_mode_one = (
                isinstance(mode, int) and not isinstance(mode, bool) and mode == 1)
            is_mode_one = is_mode_one or (
                isinstance(mode, str)
                and mode.lower() in ('1', 'creature', 'creatures'))
            if is_mode_one:
                chosen = 'creatures'
                break
            is_mode_two = (
                isinstance(mode, int) and not isinstance(mode, bool) and mode == 2)
            is_mode_two = is_mode_two or (
                isinstance(mode, str)
                and mode.lower() in ('2', 'planeswalker', 'planeswalkers'))
            if is_mode_two:
                chosen = 'planeswalkers'
                break
        if modes and chosen is None:
            return [{"action": "no_action",
                     "reason": f"Blood on the Snow: invalid mode {modes[0]!r}"}]
        if chosen is None:
            creature_net = (int(ctx.get('opponent_creature_count', 0) or 0)
                            - int(ctx.get('controller_creature_count', 0) or 0))
            walker_net = (int(ctx.get('opponent_planeswalker_count', 0) or 0)
                          - int(ctx.get('controller_planeswalker_count', 0) or 0))
            chosen = 'planeswalkers' if walker_net > creature_net else 'creatures'
        wipe = ({"action": "destroy_all_creatures"}
                if chosen == 'creatures'
                else {"action": "destroy_all_by_type", "type": "planeswalkers"})
        return [
            wipe,
            {"action": "reanimate", "player": ctrl, "own_graveyard": True,
             "allow_types": ["creature", "planeswalker"],
             "max_cmc": int(ctx.get('snow_mana_spent', 0) or 0),
             "_source_card_name": "Blood on the Snow"},
        ]


# =============================================================================
# Context Builder - prepares game context for template resolution
# =============================================================================

def resolve_target_power(ctx: Dict, target_name: str) -> int:
    """Effective power of the creature ACTUALLY being targeted.

    July 26 batch-7 audit. Templates pick their target as
    `explicit_target_name or best_opponent_creature`, but historically read
    the AMOUNT from `best_opponent_creature_power` — which build_game_context
    derives independently as the single highest-power opponent creature. The
    two silently decouple whenever the declared target isn't that creature:
    in game_1530445545447886909 Swords to Plowshares exiled a 0-power Birds of
    Paradise and granted its controller 1 life, because Elvish Mystic (power
    1) was the "best" creature on that battlefield.

    Falls back to `best_opponent_creature_power` when the name isn't found on
    either battlefield, so callers that never had an explicit target keep
    their previous behaviour exactly.
    """
    if target_name:
        want = str(target_name).strip().lower()
        for pool in ('_opponent_creatures', '_controller_creatures'):
            for info in (ctx.get(pool) or []):
                if str(info.get('name', '')).strip().lower() == want:
                    try:
                        return int(info.get('power', 0) or 0)
                    except (TypeError, ValueError):
                        return 0
    try:
        return int(ctx.get('best_opponent_creature_power', 0) or 0)
    except (TypeError, ValueError):
        return 0


def build_game_context(game, player, opponent, card=None, entering_creature=None,
                       dying_creature=None, attacking_creature=None,
                       explicit_target=None, entering_player=None) -> Dict:
    """
    Build a context dict with pre-computed targeting info for template resolution.
    
    This is the bridge between the game state and the template library's
    action generators, which need info like "best opponent creature" etc.
    """
    ctx = {}

    # Source card (for templates that need to inspect cast-time attributes
    # like _cast_from_graveyard for flashback-doubled effects).
    if card is not None:
        ctx['_source_card'] = card
        ctx['snow_mana_spent'] = int(
            getattr(card, '_snow_mana_spent', 0) or 0)
        if getattr(card, '_cast_from_graveyard', False):
            ctx['_cast_from_graveyard'] = True

    # Controller info
    ctx['controller_land_count'] = sum(1 for c in player.battlefield if c.is_land())
    # Basic lands in hand (for Land Tax skip when flooded)
    ctx['controller_basic_lands_in_hand'] = sum(
        1 for c in player.hand
        if c.is_land() and 'basic' in (getattr(c, 'type_line', '') or '').lower()
    )

    # Controller creature stats (for spells like Rishkar's Expertise, Overwhelming Stampede)
    # July 31 batch-10: is_creature(game) — the no-game call bypasses the
    # devotion type-flip, so a sub-threshold god (Thassa at devotion 1/5)
    # was counted as a creature candidate (the June 10 D4 class).
    controller_creatures = [c for c in player.battlefield if c.is_creature(game)]
    ctx['controller_creature_count'] = len(controller_creatures)
    greatest_power = 0
    for c in controller_creatures:
        try:
            p = int(c.power) if c.power else 0
            p += getattr(c, 'power_modifier', 0)
            p += c.counters.get('+1/+1', 0) if hasattr(c, 'counters') else 0
        except (ValueError, TypeError):
            p = 0
        greatest_power = max(greatest_power, p)
    ctx['greatest_power'] = greatest_power
    
    # Black devotion (for Gray Merchant)
    black_devotion = 0
    for c in player.battlefield:
        if c.mana_cost:
            black_devotion += c.mana_cost.count('B')
    ctx['black_devotion'] = black_devotion

    # Blue devotion (for Thassa's Oracle, Thassa, etc.)
    blue_devotion = 0
    for c in player.battlefield:
        if c.mana_cost:
            blue_devotion += c.mana_cost.count('U')
    ctx['devotion_to_blue'] = blue_devotion

    # Library size (for Thassa's Oracle win check, Jace/Lab Man, etc.)
    ctx['library_size'] = len(player.library) if hasattr(player, 'library') else 99

    # Controller graveyard (for Mystic Sanctuary, Regrowth, etc.)
    ctx['controller_graveyard'] = player.graveyard
    ctx['controller_hand'] = player.hand
    ctx['controller_battlefield'] = player.battlefield
    # Aug 9 audit (B-2/CO-1 class): opponent_battlefield was READ at three
    # template sites (Prismatic Ending's mv fallback, the Mystic Confluence
    # bounce fallback, Assassin's Trophy's lands-only fallback) and written
    # NOWHERE — all three fallbacks were dead (Trophy's was itself an Aug-7
    # documented fix that never executed). All three treat it as a fallback
    # after best_opponent_* keys, so making it live is strictly additive.
    ctx['opponent_battlefield'] = opponent.battlefield
    ctx['opponent_hand'] = opponent.hand

    # Player object reference (for templates that need to check optional-cost
    # affordability like Extort's "you may pay {W/B}"). Avoid mutating through
    # this in templates — use it for read-only mana / state checks.
    ctx['_controller_player'] = player
    ctx['_opponent_player'] = opponent  # June 10: Land Tax land-count check
    ctx['_game'] = game
    ctx['phase'] = getattr(getattr(game, 'phase', ''), 'value', getattr(game, 'phase', ''))
    ctx['opponent_life_lost_this_turn'] = int(
        getattr(opponent, 'life_lost_this_turn', 0) or 0)

    # Experience counters on the controller (Meren, Ezuri). Tracked as a plain
    # attribute on the player object — incremented by the "dies" trigger in
    # mtg_game when an experience-granting permanent is on the battlefield.
    ctx['experience_counters'] = int(getattr(player, '_experience_counters', 0) or 0)

    # Total spells cast last turn across all players (for werewolf day/night
    # transform check). engine.end_turn() snapshots active player's count to
    # spells_cast_prev_turn each turn end.
    ctx['all_players_spells_cast_prev_turn'] = sum(
        int(getattr(p, 'spells_cast_prev_turn', 0) or 0) for p in game.players
    )

    # Library references (for Sphinx of Uthuun, Gonti, etc.)
    ctx['controller_library'] = player.library
    ctx['opponent_library'] = opponent.library
    
    # [TARGETING] Helper: check if source card can target a permanent
    controller_name = player.name if hasattr(player, 'name') else ''
    def _can_target(target_card, target_owner):
        """Return True if source card can legally target this permanent."""
        if not _HAS_TARGET_VALIDATION or not card:
            return True
        try:
            legal, _ = _tgt_validate(game, target_card, target_owner, card, controller_name)
            return legal
        except Exception:
            return True  # Permissive on errors

    # Aug 9 adversarial review (B-2 refutation): the raw opponent_battlefield
    # fallbacks (Prismatic auto-pick, Assassin's Trophy lands-only, Mystic
    # Confluence bounce) fall back FROM _can_target-filtered keys, so on a
    # non-empty board their only new firing condition was the ILLEGAL one
    # (hexproof/phased picks — cost-paid-effect-lost at the action layer,
    # and Trophy's unlinked search granted a free ramp land). Export the
    # closure so the fallbacks filter the same way; synthetic ctx without
    # it stays permissive.
    ctx['_can_target'] = _can_target

    # Best opponent creature (highest power) — use get_effective_power for accuracy
    # Skips creatures with hexproof/protection from the source spell
    best_creature = None
    best_power = -1
    for c in opponent.battlefield:
        if c.is_creature():
            # [TARGETING] Skip untargetable creatures
            if not _can_target(c, opponent):
                continue
            try:
                power = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else (int(c.power) if c.power else 0)
            except (ValueError, TypeError):
                power = 0
            if power > best_power:
                best_power = power
                best_creature = c.name
    ctx['best_opponent_creature'] = best_creature
    ctx['best_opponent_creature_power'] = best_power

    # Opponent creatures sorted by power desc (for Dizzying Swoop tap-2 etc.)
    _opp_creatures_pwr = []
    for c in opponent.battlefield:
        if c.is_creature() and not getattr(c, '_phased_out', False):
            try:
                pwr = c.get_effective_power(game) if hasattr(c, 'get_effective_power') else (int(c.power) if c.power else 0)
            except (ValueError, TypeError):
                pwr = 0
            _opp_creatures_pwr.append((pwr, c.name))
    _opp_creatures_pwr.sort(key=lambda t: -t[0])
    ctx['opponent_creatures_by_power'] = [name for _, name in _opp_creatures_pwr]

    # Best opponent nonland permanent with mana value <= 3 (for Abrupt Decay etc.)
    # Prefers creatures (highest power), then planeswalkers, then artifacts/enchantments
    best_le3 = None
    best_le3_score = -1
    for c in opponent.battlefield:
        if c.is_land():
            continue
        if not _can_target(c, opponent):
            continue
        try:
            cmc_val = int(c.cmc) if c.cmc else 0
        except (ValueError, TypeError):
            cmc_val = 0
        if cmc_val > 3:
            continue
        # Score: creatures by effective power, others by CMC (higher = more impactful)
        try:
            if c.is_creature():
                score = 100 + (c.get_effective_power(game) if hasattr(c, 'get_effective_power') else int(c.power or 0))
            elif c.is_planeswalker():
                score = 90
            else:
                score = 50 + cmc_val
        except (ValueError, TypeError):
            score = 50
        if score > best_le3_score:
            best_le3_score = score
            best_le3 = c.name
    ctx['best_opponent_nonland_le3'] = best_le3

    # Best opponent creature toughness + damage marked (for Searing Blood lethality check)
    best_toughness = 0
    best_damage_marked = 0
    for c in opponent.battlefield:
        if c.is_creature() and c.name == best_creature:
            try:
                best_toughness = int(c.toughness) if c.toughness else 0
                best_toughness += c.counters.get('+1/+1', 0) if hasattr(c, 'counters') else 0
            except (ValueError, TypeError):
                best_toughness = 0
            best_damage_marked = getattr(c, 'damage_marked', 0)
            break
    ctx['target_toughness'] = best_toughness
    ctx['target_damage_marked'] = best_damage_marked

    # Full creature lists for edict effects (Fleshbag Marauder etc.) and
    # color/type-restricted removal templates (Shriekmaw, Nekrataal, etc.)
    def _creature_info(c):  # noqa: E306 (see resolve_target_power below)
        return {
            'name': c.name,
            'power': c.get_effective_power(game) if hasattr(c, 'get_effective_power') else 0,
            # Aug 3, 2026: bolster (CR 701.28) chooses the creature with the
            # LEAST TOUGHNESS, which nothing here reported. Effective, not
            # printed — the compute-on-read contract.
            'toughness': (c.get_effective_toughness(game)
                          if hasattr(c, 'get_effective_toughness') else 0),
            'colors': list(getattr(c, 'color_identity', []) or []),
            'type_line': (c.type_line or "").lower() if hasattr(c, 'type_line') else "",
            # July 29: MV-conditioned removal (Fatal Push) needs to pick a
            # LEGAL fallback target, not gate an illegal one into no_action.
            'cmc': int(getattr(c, 'cmc', 0) or 0),
        }
    ctx['_opponent_creatures'] = [
        _creature_info(c) for c in opponent.battlefield if c.is_creature()
    ]
    ctx['_controller_creatures'] = [
        _creature_info(c) for c in player.battlefield if c.is_creature()
    ]

    # June 10 deep-dive: any-permanent fallback for "destroy target
    # permanent" spells — lands ARE legal targets (Beast Within fizzled its
    # destroy half on a land-only board while still granting the 3/3).
    # Highest MV wins, so lands (MV 0) are chosen only as a last resort.
    _bo_any = None
    _bo_any_mv = -1
    for c in opponent.battlefield:
        _mv = getattr(c, 'cmc', 0) or 0
        if _mv > _bo_any_mv:
            _bo_any, _bo_any_mv = c.name, _mv
    ctx['best_opponent_any_permanent'] = _bo_any or ''
    
    # Best opponent noncreature permanent (skips untargetable)
    best_noncreature = None
    for c in opponent.battlefield:
        if not c.is_creature() and not c.is_land():
            if not _can_target(c, opponent):
                continue
            best_noncreature = c.name
            break
    ctx['best_opponent_noncreature'] = best_noncreature

    # Best opponent artifact/enchantment (skips untargetable)
    best_ae = None
    for c in opponent.battlefield:
        type_line = (c.type_line or "").lower() if hasattr(c, 'type_line') else ""
        if "artifact" in type_line or "enchantment" in type_line:
            if not _can_target(c, opponent):
                continue
            best_ae = c.name
            break
    ctx['best_opponent_artifact_enchantment'] = best_ae

    # Best opponent nonland permanent (for Meteor Golem etc., skips untargetable)
    best_nonland = None
    best_nonland_value = -1
    for c in opponent.battlefield:
        if not c.is_land():
            if not _can_target(c, opponent):
                continue
            mv = getattr(c, 'cmc', 0) or 0
            if mv > best_nonland_value:
                best_nonland = c.name
                best_nonland_value = mv
    ctx['best_opponent_nonland'] = best_nonland

    # Best opponent small permanent (MV <= 4 for Skyclave Apparition etc., skips untargetable)
    best_small = None
    best_small_value = -1
    for c in opponent.battlefield:
        if not c.is_land():
            if not _can_target(c, opponent):
                continue
            mv = getattr(c, 'cmc', 0) or 0
            if mv <= 4 and mv > best_small_value:
                best_small = c.name
                best_small_value = mv
    ctx['best_opponent_small_permanent'] = best_small
    
    # Best card in own graveyard (tokens are not cards — CR 110.5g, not legal GY targets)
    best_gy = None
    best_gy_permanent = None
    for c in reversed(player.graveyard):  # Most recently added
        if getattr(c, 'is_token', False):
            continue  # Tokens cease to exist and aren't legal targets for "return a card"
        if best_gy is None:
            best_gy = c.name
        if best_gy_permanent is None and not c.is_instant() if hasattr(c, 'is_instant') else True:
            # Rough check: if not instant/sorcery, it's a permanent card
            type_line = (c.type_line or "").lower() if hasattr(c, 'type_line') else ""
            if "instant" not in type_line and "sorcery" not in type_line:
                mv = getattr(c, 'cmc', 0) or 0
                best_gy_permanent = c.name
    ctx['best_own_graveyard_card'] = best_gy
    ctx['best_own_graveyard_permanent'] = best_gy_permanent
    ctx['best_graveyard_card'] = best_gy  # Alias for Eternal Witness etc.

    # Best creature in ANY graveyard (for Reanimate, Animate Dead, etc.)
    best_gy_creature = None
    best_gy_creature_cmc = 0
    for p in [player, opponent]:
        for c in p.graveyard:
            type_line = (c.type_line or "").lower() if hasattr(c, 'type_line') else ""
            if "creature" in type_line:
                mv = getattr(c, 'cmc', 0) or 0
                if mv > best_gy_creature_cmc:
                    best_gy_creature = c.name
                    best_gy_creature_cmc = mv
    ctx['best_graveyard_creature'] = best_gy_creature or ''
    ctx['best_graveyard_creature_cmc'] = best_gy_creature_cmc

    # June 10 audit (V7): own-graveyard-only variant for "your graveyard"-
    # restricted reanimation (Dread Return). The cross-player key above let
    # Dread Return take the OPPONENT's best creature.
    best_own_gy_creature = None
    best_own_gy_cmc = -1
    for c in player.graveyard:
        if getattr(c, 'is_token', False):
            continue
        type_line = (c.type_line or "").lower() if hasattr(c, 'type_line') else ""
        if "creature" in type_line:
            mv = getattr(c, 'cmc', 0) or 0
            if mv > best_own_gy_cmc:
                best_own_gy_creature = c.name
                best_own_gy_cmc = mv
    ctx['best_own_graveyard_creature'] = best_own_gy_creature or ''
    
    # Source card name and X value (for self-referencing effects and X-cost spells)
    if card:
        ctx['_source_card_name'] = card.name
        # Pass X value from the card for X-cost spells (set by cast_spell_async)
        if hasattr(card, '_x_value') and card._x_value is not None:
            ctx['x_value'] = card._x_value
        elif hasattr(card, '_mana_paid') and card._mana_paid and card.mana_cost and 'X' in card.mana_cost:
            import re
            colored = sum(1 for c in card.mana_cost if c in 'WUBRGC' and c != 'X')
            generic = sum(int(m) for m in re.findall(r'\{(\d+)\}', card.mana_cost))
            x_count = card.mana_cost.count('X')
            if x_count > 0:
                ctx['x_value'] = max(0, (card._mana_paid - colored - generic) // x_count)
    
    # Entering creature info (for ETB triggers)
    if entering_creature:
        ctx['entering_name'] = entering_creature.name
        try:
            ctx['entering_power'] = int(entering_creature.power) if entering_creature.power else 0
            ctx['entering_toughness'] = int(entering_creature.toughness) if entering_creature.toughness else 0
        except (ValueError, TypeError):
            ctx['entering_power'] = 0
            ctx['entering_toughness'] = 0
        # Controller of the entering creature (may differ from trigger's controller).
        # Used by conditional templates like Authority of the Consuls (only fires
        # when an opponent's creature enters) and Guardian Project (only fires when
        # you control the entering creature and it isn't a token / duplicate name).
        ctx['entering_is_token'] = bool(getattr(entering_creature, 'is_token', False))
        ctx['entering_type_line'] = getattr(entering_creature, 'type_line', '') or ''
    if entering_player is not None and hasattr(entering_player, 'name'):
        ctx['entering_controller_name'] = entering_player.name

    # Dying creature info (for dies triggers)
    if dying_creature:
        ctx['_dying_card'] = dying_creature
        # July 20 audit (Athreos, God of Passage): "Whenever another creature
        # YOU OWN dies" — the ownership gate needs to know whether the dying
        # card belongs to the watcher's controller (`player` here is the
        # trigger source's controller in the dies scan). owner_index when
        # set (tokens, stolen cards); else the post-death graveyard location
        # (CR 404.1 — a card dies into its OWNER's graveyard). None when
        # neither signal is available — consumers should NOT fire on None
        # (a missed optional trigger beats an inverted one).
        try:
            _own_idx = getattr(dying_creature, 'owner_index', None)
            if _own_idx is not None and 0 <= _own_idx < len(game.players):
                ctx['dying_owned_by_controller'] = game.players[_own_idx] is player
            elif dying_creature in getattr(player, 'graveyard', []):
                ctx['dying_owned_by_controller'] = True
            elif any(dying_creature in getattr(p, 'graveyard', [])
                     for p in game.players if p is not player):
                ctx['dying_owned_by_controller'] = False
            else:
                ctx['dying_owned_by_controller'] = None
        except Exception:
            ctx['dying_owned_by_controller'] = None
        ctx['dying_name'] = dying_creature.name if hasattr(dying_creature, 'name') else str(dying_creature)
        try:
            ctx['dying_power'] = int(dying_creature.power) if hasattr(dying_creature, 'power') and dying_creature.power else 0
            ctx['dying_toughness'] = int(dying_creature.toughness) if hasattr(dying_creature, 'toughness') and dying_creature.toughness else 0
        except (ValueError, TypeError):
            ctx['dying_power'] = 0
            ctx['dying_toughness'] = 0

    # Attacking creature info (for attack triggers)
    if attacking_creature:
        ctx['_attacking_creature'] = attacking_creature
        ctx['attacking_name'] = attacking_creature.name if hasattr(attacking_creature, 'name') else str(attacking_creature)
        try:
            ctx['attacking_power'] = int(attacking_creature.power) if hasattr(attacking_creature, 'power') and attacking_creature.power else 0
        except (ValueError, TypeError):
            ctx['attacking_power'] = 0

    # Controller's worst creature (for sacrifice effects like Victimize)
    controller_worst = None
    controller_worst_cmc = float('inf')
    for c in player.battlefield:
        if c.is_creature() and not getattr(c, 'is_commander', False):
            mv = getattr(c, 'cmc', 0) or 0
            if mv < controller_worst_cmc:
                controller_worst = c.name
                controller_worst_cmc = mv
    ctx['controller_worst_creature'] = controller_worst

    # Controller's graveyard creatures (sorted by CMC desc, for reanimate effects)
    gy_creatures = []
    for c in player.graveyard:
        type_line = (c.type_line or "").lower() if hasattr(c, 'type_line') else ""
        if "creature" in type_line:
            gy_creatures.append((c.name, getattr(c, 'cmc', 0) or 0))
    gy_creatures.sort(key=lambda x: x[1], reverse=True)
    ctx['controller_graveyard_creatures'] = [name for name, _ in gy_creatures]

    # Landfall check (did controller play a land this turn?)
    ctx['controller_played_land_this_turn'] = getattr(player, 'lands_played_this_turn', 0) > 0

    # Opponent land count (for Land Tax check)
    ctx['opponent_land_count'] = sum(1 for c in opponent.battlefield if c.is_land())

    # Mana paid total (for overload check on Cyclonic Rift etc.)
    if card and hasattr(card, '_mana_paid') and card._mana_paid:
        ctx['mana_paid_total'] = card._mana_paid

    # Kicker truth (Aug 1 2026): _compute_alt_costs stamps _kicked when the
    # kicker cost was actually paid. Surfacing it ALWAYS (True or False)
    # lets consumers stop guessing from mana_paid_total — the guess reads
    # commander-tax and cost-increase mana as "kicked".
    if card is not None:
        ctx['kicked'] = bool(getattr(card, '_kicked', False))
        # Entwine truth (Aug 2 2026, batch-13): same contract as 'kicked' —
        # _compute_alt_costs stamps _entwined when the cost was actually
        # paid; Tooth and Nail's template resolves both modes only then.
        ctx['entwined'] = bool(getattr(card, '_entwined', False))
        # Multikicker truth (Aug 2): same contract — 0 when never kicked.
        ctx['kicked_times'] = int(getattr(card, '_kicked_times', 0) or 0)
        # Converge truth (Aug 3, CR 702.100a): how many distinct COLORS of
        # mana the engine actually committed for this cast. Same contract as
        # 'kicked' — always present, so a converge template never has to
        # guess from the printed cost (which says nothing about what was
        # actually spent when generic mana came off duals).
        from mtg.helpers import colors_spent_count as _csc
        ctx['colors_spent'] = _csc(card)

    # Escape: "sacrifice it unless it escaped" (Kroxa). This key was READ in two
    # places and written NOWHERE in production — the two tests that exercised it
    # injected it by hand, so the pair stayed green over a dead path (the same
    # trap as game._rules_engine). The cast paths now stamp _was_escaped when
    # they pay the exile cost; surface it here so the ETB can finally see it.
    if card is not None:
        ctx['was_escaped'] = bool(getattr(card, '_was_escaped', False))

    # Permanent counts by type (for modal board wipes — Austere Command,
    # Akroma's Vengeance, Merciless Eviction). Phased-out permanents are
    # excluded since they aren't on the battlefield for this purpose.
    def _count_by_type(p):
        out = {'creatures': 0, 'artifacts': 0, 'enchantments': 0, 'planeswalkers': 0}
        for c in p.battlefield:
            if getattr(c, '_phased_out', False):
                continue
            tl = (getattr(c, 'type_line', '') or '').lower()
            if c.is_creature():
                out['creatures'] += 1
            if 'artifact' in tl:
                out['artifacts'] += 1
            if 'enchantment' in tl:
                out['enchantments'] += 1
            if c.is_planeswalker():
                out['planeswalkers'] += 1
        return out
    ctx['own_permanents_by_type'] = _count_by_type(player)
    ctx['opp_permanents_by_type'] = _count_by_type(opponent)
    # Creature lists with cmc, for cmc-filtered modal modes
    ctx['own_creatures'] = [
        {'name': c.name, 'cmc': getattr(c, 'cmc', 0) or 0}
        for c in player.battlefield if c.is_creature() and not getattr(c, '_phased_out', False)
    ]
    ctx['opp_creatures'] = [
        {'name': c.name, 'cmc': getattr(c, 'cmc', 0) or 0}
        for c in opponent.battlefield if c.is_creature() and not getattr(c, '_phased_out', False)
    ]

    # Best own creature with ETB (for flicker effects like Soulherder, Thassa, Conjurer's Closet)
    best_etb = None
    best_etb_score = -1
    etb_keywords = ['enters the battlefield', 'enters,', 'enters under']
    for c in controller_creatures:
        if card and c.name == card.name:
            continue  # "another" target — can't flicker self
        oracle = (c.oracle_text or '').lower()
        has_etb = any(kw in oracle for kw in etb_keywords)
        score = (10 if has_etb else 0) + (getattr(c, 'cmc', 0) or 0)
        if score > best_etb_score:
            best_etb = c.name
            best_etb_score = score
    ctx['best_own_etb_creature'] = best_etb or ''
    ctx['best_own_creature'] = controller_creatures[0].name if controller_creatures else ''

    # Best own noncreature permanent (for various template needs)
    best_own_nc = None
    for c in player.battlefield:
        if not c.is_creature() and not c.is_land():
            best_own_nc = c.name
            break
    ctx['best_own_noncreature'] = best_own_nc or ''

    # Best own flickerable (artifact/creature/land — NOT enchantment) for Ghostly Flicker
    best_own_flickerable = None
    for c in player.battlefield:
        if c.name == best_etb:
            continue  # Skip the primary flicker target
        type_line = (c.type_line or '').lower()
        if c.is_creature() or c.is_land() or 'artifact' in type_line:
            best_own_flickerable = c.name
            break
    ctx['best_own_flickerable'] = best_own_flickerable or ''

    # Opponent's hand info (for discard effects like Inquisition of Kozilek)
    if hasattr(opponent, 'hand'):
        ctx['opponent_hand'] = [
            {'name': c.name, 'cmc': getattr(c, 'cmc', 0) or 0,
             'is_land': c.is_land() if hasattr(c, 'is_land') else False}
            for c in opponent.hand
        ]
    else:
        ctx['opponent_hand'] = []

    # Opponent's artifacts and controller's graveyard creatures (for modal
    # charms like Kolaghan's Command — Apr 30 audit fix #21).
    if hasattr(opponent, 'battlefield'):
        ctx['opponent_artifacts'] = [
            c.name for c in opponent.battlefield
            if c.is_artifact() and not getattr(c, '_phased_out', False)
        ]
    else:
        ctx['opponent_artifacts'] = []
    if hasattr(player, 'graveyard'):
        ctx['controller_graveyard_creatures'] = [
            c.name for c in player.graveyard if c.is_creature()
        ]
    else:
        ctx['controller_graveyard_creatures'] = []

    # Controller life (for Phyrexian Processor, Command the Dreadhorde, etc.)
    ctx['controller_life'] = getattr(player, 'life', 40)

    # Explicit target (from AI cast decision or !play command)
    # Overrides auto-targeting in templates when present
    if explicit_target:
        # Aug 7 batch audit (B-2, CRITICAL): resolve_cast_target resolves
        # the pronoun "opponent" to a PLAYER object (by design, for burn
        # spells) — and a Player has a .name, so it used to fall into the
        # "It's a Card object" branch below, poisoning explicit_target_name
        # with a player's name. Card-name consumers (Assassin's Trophy's
        # destroy) whiffed silently while truthiness-gated side effects
        # still fired (the opponent got a FREE land search,
        # game_1535060193795248229). Duck-test: only Players have a
        # battlefield. The three legitimate player-name consumers (Blue
        # Sun's Zenith, Huntmaster, Shard Volley) read
        # explicit_target_player first now.
        def _is_player_like(obj):
            return hasattr(obj, 'battlefield') and hasattr(obj, 'life')
        if isinstance(explicit_target, (list, tuple)):
            _card_items = [item for item in explicit_target if not _is_player_like(item)]
            _player_items = [item for item in explicit_target if _is_player_like(item)]
            target_names = [getattr(item, 'name', str(item)) for item in _card_items]
            ctx['explicit_target_names'] = target_names
            if target_names:
                ctx['explicit_target_name'] = target_names[0]
            if _player_items:
                ctx['explicit_target_is_player'] = True
                ctx['explicit_target_player'] = getattr(_player_items[0], 'name', '')
        elif _is_player_like(explicit_target):
            ctx['explicit_target_is_player'] = True
            ctx['explicit_target_player'] = getattr(explicit_target, 'name', '')
            ctx['explicit_target_is_creature'] = False
            # Deliberately NOT set: explicit_target_name / _id / _mv / _owner —
            # every card-name consumer must see "no card target declared" and
            # fall to its own heuristics or no_action.
        elif hasattr(explicit_target, 'name'):
            # It's a Card object — find which player owns it
            ctx['explicit_target_name'] = explicit_target.name
            ctx['explicit_target_id'] = getattr(explicit_target, 'id', '')
            # July 27 fanout: `explicit_target_is_creature` is READ by the Rift
            # Bolt and Volcanic Geyser templates but was written NOWHERE outside
            # tests, so both guards were permanently False and both spells always
            # went to the face. The July 23 fix that added those guards had
            # therefore never executed in a live game — its pin passed only
            # because the test hand-injected the key. Game-aware is_creature so
            # a devotion-gated god is classified correctly (F24).
            ctx['explicit_target_is_creature'] = bool(
                explicit_target.is_creature(game)
                if hasattr(explicit_target, 'is_creature') else False)
            # July 29 batch audit: `explicit_target_mv` was READ by Fatal
            # Push's MV gate but written NOWHERE (the exact sibling of the
            # explicit_target_is_creature story above) — target_mv was always
            # 0, the gate could never fail, and a cascade Fatal Push
            # destroyed a mana-value-5 Solitude.
            ctx['explicit_target_mv'] = int(getattr(explicit_target, 'cmc', 0) or 0)
            for p in game.players:
                if explicit_target in p.battlefield:
                    ctx['explicit_target_owner'] = p.name
                    break
        elif isinstance(explicit_target, str):
            ctx['explicit_target_name'] = explicit_target
            for p in game.players:
                for c in p.battlefield:
                    if c.name.lower() == explicit_target.lower():
                        ctx['explicit_target_id'] = getattr(c, 'id', '')
                        ctx['explicit_target_owner'] = p.name
                        ctx['explicit_target_is_creature'] = bool(c.is_creature(game))
                        ctx['explicit_target_mv'] = int(getattr(c, 'cmc', 0) or 0)
                        break
                if ctx.get('explicit_target_owner'):
                    break

    # Stack info (for counterspells: Arcane Denial, Archmage's Charm, etc.)
    if hasattr(game, 'stack') and game.stack:
        ctx['stack_has_spell'] = True
        # Get the top spell name for counter targeting
        top_entry = game.stack[-1]
        top_name = None
        if hasattr(top_entry, 'card') and top_entry.card:
            top_name = top_entry.card.name
        elif isinstance(top_entry, dict):
            top_name = top_entry.get('card_name')
        if top_name:
            ctx['stack_top_spell'] = top_name
        top_card = getattr(top_entry, 'card', None)
        if top_card:
            top_types = (getattr(top_card, 'type_line', '') or '').lower()
            ctx['stack_top_type_known'] = bool(top_types)
            ctx['stack_top_is_creature'] = 'creature' in top_types
            ctx['stack_top_type_line'] = top_types
            ctx['stack_top_cast_origin'] = getattr(top_card, '_cast_origin', 'hand')
        # Get the CMC of the top spell for Mana Drain
        top_cmc = 0
        if hasattr(top_entry, 'card') and top_entry.card:
            top_cmc = getattr(top_entry.card, 'cmc', 0) or 0
        elif isinstance(top_entry, dict):
            top_cmc = top_entry.get('cmc', 0) or 0
        ctx['stack_top_cmc'] = top_cmc
    else:
        ctx['stack_has_spell'] = False

    return ctx


# =============================================================================
# Modal-spell helpers
# =============================================================================

def _austere_command_modes(ctrl, opp, ctx):
    """Pick the two modes for Austere Command that maximize value.

    Modes: A=destroy artifacts, B=destroy creatures CMC ≤3, C=destroy
    creatures CMC ≥4, D=destroy enchantments.

    Strategy: count opposing permanents in each bucket, count own permanents
    in each bucket, score each mode as (opp_count - own_count). Pick the
    top-2 by score; ties broken by raw opp_count.
    """
    opp_perms = ctx.get('opp_permanents_by_type', {}) or {}
    own_perms = ctx.get('own_permanents_by_type', {}) or {}

    # opp_creatures_by_cmc / own_creatures_by_cmc are dicts of cmc → count
    opp_creatures = ctx.get('opp_creatures', []) or []
    own_creatures = ctx.get('own_creatures', []) or []

    opp_low = sum(1 for c in opp_creatures if (c.get('cmc') or 0) <= 3)
    opp_high = sum(1 for c in opp_creatures if (c.get('cmc') or 0) >= 4)
    own_low = sum(1 for c in own_creatures if (c.get('cmc') or 0) <= 3)
    own_high = sum(1 for c in own_creatures if (c.get('cmc') or 0) >= 4)

    opp_artifacts = opp_perms.get('artifacts', 0)
    opp_enchant = opp_perms.get('enchantments', 0)
    own_artifacts = own_perms.get('artifacts', 0)
    own_enchant = own_perms.get('enchantments', 0)

    modes = [
        ("artifacts", opp_artifacts - own_artifacts, opp_artifacts,
         {"action": "destroy_all_by_type", "type": "artifacts"}),
        ("creatures_low", opp_low - own_low, opp_low,
         {"action": "destroy_creatures_by_cmc", "max_cmc": 3}),
        ("creatures_high", opp_high - own_high, opp_high,
         {"action": "destroy_creatures_by_cmc", "min_cmc": 4}),
        ("enchantments", opp_enchant - own_enchant, opp_enchant,
         {"action": "destroy_all_by_type", "type": "enchantments"}),
    ]
    # Sort by net score, then by raw opponent count
    modes.sort(key=lambda m: (m[1], m[2]), reverse=True)
    return [modes[0][3], modes[1][3]]


# =============================================================================
# Singleton for easy import
# =============================================================================

_library_instance = None

def get_effect_library() -> EffectTemplateLibrary:
    """Get the singleton effect template library."""
    global _library_instance
    if _library_instance is None:
        _library_instance = EffectTemplateLibrary()
    return _library_instance
