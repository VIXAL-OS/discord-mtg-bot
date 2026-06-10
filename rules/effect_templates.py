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

import re
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
        # Dies/LTB templates need their own registry so cards with BOTH an ETB
        # AND a dies trigger (Solemn Simulacrum, Mulldrifter, etc.) don't have
        # one registration silently overwriting the other when keyed by name.
        self._dies_templates: Dict[str, EffectTemplate] = {}
        self._build_library()

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
        if event_type in ("upkeep", "end_step", "beginning_combat"):
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
            scheduled_event_types = {"upkeep", "end_step", "beginning_combat"}
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
            oracle_lower = oracle_text.lower()
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

        # --- DIES DRAIN: {card_name: (opponent_loses, you_gain)} ---
        DIES_DRAIN = {
            "blood artist":             (1, 1),
            "zulaport cutthroat":       (1, 1),
            "bastion of remembrance":   (1, 1),
            "kokusho, the evening star": (5, 5),
        }
        for name, (drain, gain) in DIES_DRAIN.items():
            self._add_card(name, EffectTemplate(
                name=name.title(),
                description=f"Each opponent loses {drain} life and you gain {gain} life",
                action_generator=lambda ctrl, opp, ctx, d=drain, g=gain: [
                    {"action": "lose_life", "player": opp, "amount": d},
                    {"action": "gain_life", "player": ctrl, "amount": g},
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
            # Arcane Denial: counter target spell. Its controller draws 2, you draw 1 (at next upkeep).
            # Simplified to immediate draws. If counter fizzles (no target), no draws happen.
            target = ctx.get('stack_top_spell')
            if not target:
                return [{"action": "no_action", "reason": "Arcane Denial: no spell to counter (fizzled)"}]
            return [
                {"action": "counter_spell", "player": ctrl, "target": "stack_top"},
                {"action": "draw_cards", "player": opp, "amount": 2},
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ]

        def _counter_and_drain(ctrl, opp, ctx):
            # Get CMC of the countered spell from context. Look in stack_top_cmc (set by
            # the counterspell resolution path) or countered_cmc, default to 3 as a reasonable
            # fallback since we'd rather give some mana than none.
            cmc = ctx.get('countered_cmc', 0) or ctx.get('stack_top_cmc', 0) or 3
            return [
                {"action": "counter_spell", "player": ctrl, "target": "stack_top"},
                {"action": "add_mana", "player": ctrl, "color": "C", "amount": cmc},
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
            'force of will', 'force of negation',
            "dovin's veto", 'flusterstorm',
        ]
        for name in COUNTERSPELLS:
            self._add_card(name, EffectTemplate(
                name=name.title(), description="Counter target spell",
                action_generator=_counter_action,
            ))

        # Noncreature-only counterspells — fizzle if target is a creature spell
        # Bug fix: Negate/Stubborn Denial were countering creature spells during cascade
        def _noncreature_counter(ctrl, opp, ctx):
            if ctx.get('stack_top_is_creature', False):
                return [{"action": "no_action", "reason": "Fizzle — target is a creature spell (noncreature counter)"}]
            return [{"action": "counter_spell", "player": ctrl, "target": "stack_top"}]

        NONCREATURE_COUNTERS = ['negate', 'stubborn denial']
        for name in NONCREATURE_COUNTERS:
            self._add_card(name, EffectTemplate(
                name=name.title(), description="Counter target noncreature spell",
                action_generator=_noncreature_counter,
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
        self._add_card("swan song", EffectTemplate(
            name="Swan Song", description="Counter target enchantment/instant/sorcery, controller gets 2/2 Bird",
            action_generator=_counter_and_token,
        ))

        # Pact of Negation: counter + delayed trigger (lose the game next upkeep if you can't pay)
        def _pact_of_negation(ctrl, opp, ctx):
            return [
                {"action": "counter_spell", "player": ctrl, "target": "stack_top"},
                # Schedule delayed trigger: at next upkeep, lose the game
                # (simplified for autoplay — actual card says "pay {3}{U}{U} or lose").
                # `lose_the_game` is the dedicated SBA-clean shortcut; the
                # earlier 999-life-loss hack produced "(life: -970)" Discord
                # messages instead of an honest "loses the game" line.
                {"action": "schedule_delayed_trigger", "trigger_at": "upkeep", "turn_delay": 1,
                 "actions": [{"action": "lose_the_game", "player": ctrl,
                              "reason": "failed to pay Pact of Negation cost"}],
                 "source": "Pact of Negation"},
            ]
        self._add_card("pact of negation", EffectTemplate(
            name="Pact of Negation",
            description="Counter target spell. At next upkeep, pay {3}{U}{U} or lose the game.",
            action_generator=_pact_of_negation,
        ))

        # Modal spells — default to best autoplay mode choices
        def _mystic_confluence(ctrl, opp, ctx):
            # "Choose three" — default: draw 3 cards (safest autoplay choice)
            return [{"action": "draw_cards", "player": ctrl, "amount": 3}]

        self._add_card("mystic confluence", EffectTemplate(
            name="Mystic Confluence",
            description="Choose three: counter spell / draw a card / bounce creature. Default: draw 3.",
            action_generator=_mystic_confluence,
        ))

        def _cryptic_command(ctrl, opp, ctx):
            # "Choose two" — default: counter + draw (strongest line in most cases)
            return [
                {"action": "counter_spell", "player": ctrl, "target": "stack_top"},
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ]

        self._add_card("cryptic command", EffectTemplate(
            name="Cryptic Command",
            description="Choose two: counter / bounce / tap all / draw. Default: counter + draw.",
            action_generator=_cryptic_command,
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

        self._add_card("flametongue kavu", EffectTemplate(
            name="Flametongue Kavu",
            description="Deal 4 damage to target creature",
            action_generator=lambda ctrl, opp, ctx: self._damage_best_creature(ctrl, opp, ctx, 4),
            needs_target=True,
        ))

        self._add_card("inferno titan", EffectTemplate(
            name="Inferno Titan",
            description="Deal 3 damage divided among up to 3 targets",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 3, "target_player": opp}
            ],
        ))

        self._add_card("sun titan", EffectTemplate(
            name="Sun Titan",
            description="Return target permanent with MV 3 or less from graveyard to battlefield",
            action_generator=lambda ctrl, opp, ctx: self._reanimate_small(ctrl, opp, ctx, max_mv=3),
        ))

        self._add_card("frost titan", EffectTemplate(
            name="Frost Titan",
            description="Tap target permanent, it doesn't untap next untap step",
            action_generator=lambda ctrl, opp, ctx: self._tap_best_permanent(ctrl, opp, ctx),
            needs_target=True,
        ))

        self._add_card("primeval titan", EffectTemplate(
            name="Primeval Titan",
            description="Search library for two lands, put onto battlefield tapped",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action", "reason": f"{ctrl} searches for 2 lands (use !fix to add them)"}
            ],
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

        self._add_card("eternal witness", EffectTemplate(
            name="Eternal Witness",
            description="Return target card from graveyard to hand",
            action_generator=lambda ctrl, opp, ctx: self._return_best_from_graveyard(ctrl, opp, ctx),
        ))

        self._add_card("meteor golem", EffectTemplate(
            name="Meteor Golem",
            description="Destroy target nonland permanent",
            action_generator=lambda ctrl, opp, ctx: self._destroy_best_nonland(ctrl, opp, ctx),
            needs_target=True,
        ))

        # --- Cards with no_action hints (complex state changes) ---
        self._add_card("plaguecrafter", EffectTemplate(
            name="Plaguecrafter",
            description="Each player sacrifices a creature or planeswalker, or discards a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action", "reason": f"Each player must sacrifice a creature/planeswalker or discard (use !fix)"}
            ],
        ))

        self._add_card("agent of treachery", EffectTemplate(
            name="Agent of Treachery",
            description="Gain control of target permanent (returns when Agent leaves)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "steal_permanent", "player": ctrl, "from_player": opp,
                 "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland', 'target'),
                 "source": "Agent of Treachery"},
            ],
            needs_target=True,
        ))

        # Etali, Primal Storm — attack trigger template is registered below (line ~1251)
        # with a proper etali_trigger action that actually exiles and casts cards

        self._add_card("aurelia, the warleader", EffectTemplate(
            name="Aurelia, the Warleader",
            description="First attack each turn: untap all creatures, get extra combat + main phase",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action", "reason": f"Aurelia: extra combat phase! Untap all creatures, additional combat + main (use !fix)"}
            ],
        ))

        # --- Library interaction (game-state-dependent) ---
        self._add_card("sphinx of uthuun", EffectTemplate(
            name="Sphinx of Uthuun",
            description="Fact or Fiction — reveal top 5, split into 2 piles, choose one for hand",
            action_generator=lambda ctrl, opp, ctx: self._sphinx_of_uthuun(ctrl, opp, ctx),
        ))

        self._add_card("gonti, lord of luxury", EffectTemplate(
            name="Gonti, Lord of Luxury",
            description="Look at top 4 of opponent's library, exile one face down, rest on bottom",
            action_generator=lambda ctrl, opp, ctx: self._gonti_etb(ctrl, opp, ctx),
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

        # --- Life from the Loam: return up to 3 land cards from GY to hand ---
        # Named template takes priority over generic "mill N" pattern that would otherwise
        # match the Dredge 3 reminder text in the oracle and produce the wrong effect.
        self._add_card("life from the loam", EffectTemplate(
            name="Life from the Loam",
            description="Return up to three target land cards from your graveyard to your hand",
            action_generator=self._gen_life_from_the_loam,
        ))

        # --- Apr 28 audit: top template-backlog cards (silent "X resolves") ---

        # Diabolic Intent: sacrifice a creature, search your library for a card
        self._add_card("diabolic intent", EffectTemplate(
            name="Diabolic Intent",
            description="Sacrifice a creature, search your library for a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "sacrifice_permanent", "player": ctrl,
                 "type_filter": "creature", "reason": "Diabolic Intent additional cost"},
                {"action": "search_library", "player": ctrl, "count": 1,
                 "reason": "Diabolic Intent: tutor any card to hand"},
            ] if any(c.is_creature() for c in ctx.get('controller_battlefield', [])) else [
                {"action": "no_action", "reason": "Diabolic Intent: no creature to sacrifice"}
            ],
        ))

        # Abrupt Decay: destroy target nonland permanent with mana value 3 or less
        self._add_card("abrupt decay", EffectTemplate(
            name="Abrupt Decay",
            description="Destroy target nonland permanent with mana value 3 or less",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "destroy", "card": ctx['best_opponent_nonland_le3'],
                  "target_controller": opp,
                  "reason": "Abrupt Decay (CMC ≤ 3, uncounterable)"}]
                if ctx.get('best_opponent_nonland_le3')
                else [{"action": "no_action", "reason": "Abrupt Decay: no legal target (CMC ≤ 3)"}]
            ),
        ))

        # Faithless Looting: draw 2, then discard 2
        self._add_card("faithless looting", EffectTemplate(
            name="Faithless Looting",
            description="Draw 2, then discard 2",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 2},
                {"action": "discard", "player": ctrl, "card": "random"},
                {"action": "discard", "player": ctrl, "card": "random"},
            ],
        ))

        # Thrill of Possibility: discard a card, draw 2 (rummage)
        self._add_card("thrill of possibility", EffectTemplate(
            name="Thrill of Possibility",
            description="Discard a card, then draw 2",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "discard", "player": ctrl, "card": "random"},
                {"action": "draw_cards", "player": ctrl, "amount": 2},
            ],
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

        # Dramatic Reversal: untap all nonland permanents you control
        self._add_card("dramatic reversal", EffectTemplate(
            name="Dramatic Reversal",
            description="Untap all nonland permanents you control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "untap_lands", "player": ctrl,
                 "include_nonlands": True, "exclude_lands": True,
                 "reason": "Dramatic Reversal untaps nonlands"}
            ],
        ))

        # Unbreakable Formation: creatures you control gain indestructible until EOT
        self._add_card("unbreakable formation", EffectTemplate(
            name="Unbreakable Formation",
            description="Creatures you control gain indestructible until EOT",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "grant_keywords", "player": ctrl,
                 "keywords": ["Indestructible"], "duration": "end_of_turn",
                 "reason": "Unbreakable Formation: indestructible until EOT"},
            ],
        ))

        # Past in Flames: target instant/sorcery in your graveyard gains flashback
        # (approximation: grant flashback to the best instant/sorcery in graveyard)
        self._add_card("past in flames", EffectTemplate(
            name="Past in Flames",
            description="Each instant/sorcery in your graveyard gains flashback until EOT",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "grant_flashback", "player": ctrl,
                 "source": "Past in Flames",
                 "reason": "Past in Flames: instants/sorceries gain flashback"},
            ],
        ))

        # Long-Term Plans: search library for a card, shuffle, place that card
        # third from top (approximate: tutor to hand, since "third from top" isn't modeled)
        self._add_card("long-term plans", EffectTemplate(
            name="Long-Term Plans",
            description="Search library for a card, shuffle, place third from top (approximated as tutor-to-hand)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "count": 1,
                 "reason": "Long-Term Plans (third-from-top approximated as tutor-to-hand)"},
            ],
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

        # Traverse the Ulvenwald: search library for creature/land
        self._add_card("traverse the ulvenwald", EffectTemplate(
            name="Traverse the Ulvenwald",
            description="Search library for a creature or land card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "count": 1,
                 "card_type": "creature_or_land",
                 "reason": "Traverse the Ulvenwald: tutor creature or land"},
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

        # Welcome Home (Flaxen Intruder — sorcery): 3× 2/2 green Bear tokens
        self._add_card("welcome home", EffectTemplate(
            name="Welcome Home",
            description="Create three 2/2 green Bear creature tokens",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Bear",
                 "power": 2, "toughness": 2,
                 "types": "Creature — Bear", "count": 3},
            ],
        ))
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
        # Heart's Desire (Lovestruck Beast — sorcery): 1/1 white Human "Heart's Desire"
        self._add_card("heart's desire", EffectTemplate(
            name="Heart's Desire",
            description="Create a 1/1 white Human creature token named Heart's Desire",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Heart's Desire",
                 "power": 1, "toughness": 1,
                 "types": "Creature — Human", "count": 1},
            ],
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
        # Fertile Footsteps (Beanstalk Giant — sorcery): search basic land → battlefield
        self._add_card("fertile footsteps", EffectTemplate(
            name="Fertile Footsteps",
            description="Search library for a basic land card and put it onto the battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library_land", "player": ctrl, "basic_only": True},
            ],
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
        # Cast Off (Realm-Cloaked Giant — sorcery): destroy all non-Giant creatures
        self._add_card("cast off", EffectTemplate(
            name="Cast Off",
            description="Destroy all non-Giant creatures",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy_all_creatures", "exclude_types": ["Giant"],
                 "reason": "Cast Off: destroy all non-Giant creatures"},
            ],
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
        # Granted (Fae of Wishes — sorcery): wishboard fetch — not modeled
        self._add_card("granted", EffectTemplate(
            name="Granted",
            description="Wishboard fetch (not modeled — sideboard not tracked)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action", "reason": "Granted: wishboard not modeled"},
            ],
        ))
        # Treats to Share (Curious Pair — sorcery): create a Food token
        self._add_card("treats to share", EffectTemplate(
            name="Treats to Share",
            description="Create a Food token",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Food",
                 "power": 0, "toughness": 0,
                 "types": "Artifact — Food", "count": 1,
                 "oracle_text": "{2}, {T}, Sacrifice this artifact: You gain 3 life."},
            ],
        ))
        # On Alert (Silverflame Squire — instant): up to 2 Knights get +2/+1 EOT
        # Approximation: pump-all on Knight subtype only
        self._add_card("on alert", EffectTemplate(
            name="On Alert",
            description="Up to two target Knights get +2/+1 until end of turn",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "pump_all_creatures", "player": ctrl,
                 "power": 2, "toughness": 1,
                 "source": "On Alert"},
            ],
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
        # Mesmeric Glare (Hypnotic Sprite — instant): counter target spell with MV ≤ 3
        self._add_card("mesmeric glare", EffectTemplate(
            name="Mesmeric Glare",
            description="Counter target spell with mana value 3 or less",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "counter_spell", "max_mv": 3,
                 "reason": "Mesmeric Glare: counter spell with MV ≤ 3"},
            ],
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
        # Seasonal Ritual (Rosethorn Acolyte — sorcery): add 1 mana of any color
        self._add_card("seasonal ritual", EffectTemplate(
            name="Seasonal Ritual",
            description="Add one mana of any color",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "add_mana", "player": ctrl, "color": "G", "amount": 1,
                 "reason": "Seasonal Ritual (default green; can be any color)"},
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
        # Heuristically check if 2 damage would be lethal by comparing against
        # (toughness - existing_damage_marked).  Not 100% rules-correct (misses
        # indestructible, damage prevention) but covers the common case.
        # Default toughness=99 (unknown) so we do NOT assume lethal when context is missing.
        def _searing_blood_gen(ctrl, opp, ctx):
            target = ctx.get('best_opponent_creature')
            # No opposing creature — Searing Blood requires a creature target,
            # so the spell fizzles and deals NO damage to the player.
            # (Without this guard, the template emitted a creature-damage
            # action with sentinel "target" name, then the second clause
            # incorrectly delivered 3 damage to the player.)
            if not target:
                return [{"action": "no_action",
                         "reason": "Searing Blood: requires a creature target — no legal target"}]
            target_toughness = ctx.get('target_toughness', 99)
            target_damage = ctx.get('target_damage_marked', 0)
            remaining_toughness = target_toughness - target_damage
            actions = [
                {"action": "deal_damage", "amount": 2, "target_card": target, "target_controller": opp},
            ]
            # Heuristic: 2 damage is lethal if remaining toughness <= 2
            if remaining_toughness <= 2:
                actions.append({"action": "deal_damage", "amount": 3, "target_player": opp})
            return actions

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

        # --- Land Tax: search for up to 3 basic lands ---
        # Two skip conditions: (1) opponent doesn't control more lands; (2) we
        # already have enough basic lands in hand to be flooded — searching for
        # more is wasted activation and floods the discard step. If the
        # controller already holds 5+ basic lands in hand, skip silently.
        self._add_card("land tax", EffectTemplate(
            name="Land Tax",
            description="Search library for up to 3 basic land cards, put into hand",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "no_action", "reason": ""}]
                if int(ctx.get('controller_basic_lands_in_hand', 0)) >= 5
                else (
                    [{"action": "search_library", "player": ctrl, "card_type": "basic land",
                      "count": 3, "to_zone": "hand"}]
                    if ctx.get('opponent_land_count', 0) > ctx.get('controller_land_count', 0)
                    else [{"action": "no_action", "reason": "Land Tax: opponent doesn't control more lands"}]
                )
            ),
        ))

        # --- Removal spells that give controller a token ---
        self._add_card("reality shift", EffectTemplate(
            name="Reality Shift",
            description="Exile target creature, its controller manifests top card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_creature') or '',
                 "from_zone": "battlefield", "to_zone": "exile", "player": ctx.get('explicit_target_owner') or opp},
                {"action": "create_token", "player": ctx.get('explicit_target_owner') or opp, "name": "Manifest",
                 "power": 2, "toughness": 2, "types": "Creature", "count": 1},
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
        self._add_card("beast within", EffectTemplate(
            name="Beast Within",
            description="Destroy target permanent. Its controller creates a 3/3 Beast",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland', 'target')},
                {"action": "create_token", "player": ctx.get('explicit_target_owner') or opp, "name": "Beast",
                 "power": 3, "toughness": 3, "types": "Creature - Beast", "count": 1},
            ],
            needs_target=True,
        ))
        self._add_card("generous gift", EffectTemplate(
            name="Generous Gift",
            description="Destroy target permanent. Its controller creates a 3/3 Elephant",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland', 'target')},
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

        # --- Opt: scry 1, draw a card ---
        self._add_card("opt", EffectTemplate(
            name="Opt",
            description="Scry 1. Draw a card.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "scry", "player": ctrl, "amount": 1},
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ],
        ))

        # --- Preordain: scry 2, draw a card ---
        self._add_card("preordain", EffectTemplate(
            name="Preordain",
            description="Scry 2, then draw a card.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "scry", "player": ctrl, "amount": 2},
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ],
        ))

        # --- Serum Visions: draw a card, scry 2 ---
        self._add_card("serum visions", EffectTemplate(
            name="Serum Visions",
            description="Draw a card, then scry 2.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
                {"action": "scry", "player": ctrl, "amount": 2},
            ],
        ))

        # --- Archmage's Charm: modal (counter, draw 2, steal MV≤1) ---
        self._add_card("archmage's charm", EffectTemplate(
            name="Archmage's Charm",
            description="Counter target spell, OR draw two cards, OR gain control of target nonland permanent with MV ≤ 1",
            action_generator=self._gen_archmages_charm,
        ))

        # --- Prismatic Ending: exile nonland permanent with MV ≤ X (colors spent) ---
        self._add_card("prismatic ending", EffectTemplate(
            name="Prismatic Ending",
            description="Exile target nonland permanent with mana value X or less",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card",
                 "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland', ''),
                 "from_zone": "battlefield", "to_zone": "exile",
                 "player": ctx.get('explicit_target_owner') or opp},
            ] if (ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland')) else [
                {"action": "no_action", "reason": "No valid nonland permanent to exile with Prismatic Ending"}
            ],
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

        # --- Shard Volley: deals 3 damage to any target, sacrifice a land as additional cost ---
        self._add_card("shard volley", EffectTemplate(
            name="Shard Volley",
            description="Shard Volley deals 3 damage to any target (sacrifice a land as additional cost)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "sacrifice_land", "player": ctrl},
                {"action": "deal_damage", "amount": 3,
                 "target_player": ctx.get('explicit_target_name') or opp},
            ],
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

        # --- Gitaxian Probe: target player reveals hand, draw a card ---
        self._add_card("gitaxian probe", EffectTemplate(
            name="Gitaxian Probe",
            description="Target player reveals their hand. Draw a card.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ],
        ))

        # --- Heroic Intervention: all permanents gain hexproof + indestructible ---
        self._add_card("heroic intervention", EffectTemplate(
            name="Heroic Intervention",
            description="Your permanents gain hexproof and indestructible until end of turn",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "grant_keywords", "player": ctrl,
                 "keywords": ["Hexproof", "Indestructible"], "target": "all_own_permanents"},
            ],
        ))

        # --- Swords to Plowshares: exile creature, controller gains life equal to its power ---
        self._add_card("swords to plowshares", EffectTemplate(
            name="Swords to Plowshares",
            description="Exile target creature. Its controller gains life equal to its power.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_creature') or '',
                 "from_zone": "battlefield", "to_zone": "exile",
                 "player": ctx.get('explicit_target_owner') or opp},
                {"action": "gain_life", "player": ctx.get('explicit_target_owner') or opp,
                 "amount": ctx.get('best_opponent_creature_power', 0)},
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

        # --- Yorion, Sky Nomad: flicker up to 5 other nonland permanents you own ---
        self._add_card("yorion, sky nomad", EffectTemplate(
            name="Yorion, Sky Nomad",
            description="Exile any number of other nonland permanents you own, return at next end step",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "mass_flicker", "player": ctrl, "count": 5, "exclude_lands": True,
                 "exclude_self": "Yorion, Sky Nomad"},
            ],
        ))

        # --- Spell Queller: exile target spell with MV 4 or less from the stack ---
        self._add_card("spell queller", EffectTemplate(
            name="Spell Queller",
            description="When Spell Queller enters, exile target spell with MV 4 or less",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "exile_from_stack", "controller": ctrl, "max_mv": 4},
            ],
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
        for wipe_name in ["wrath of god", "day of judgment", "damnation", "decree of pain",
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
        self._add_card("toxic deluge", EffectTemplate(
            name="Toxic Deluge",
            description="Pay X life. Each creature gets -X/-X until end of turn",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "lose_life", "player": ctrl, "amount": 4},
                {"action": "pump_all_creatures", "power": -4, "toughness": -4,
                 "duration": "end_of_turn"},
            ],
        ))

        # --- Dread Return: single-target reanimate from graveyard. The
        # "Flashback — Sacrifice three creatures" is an ALTERNATE CAST COST
        # for flashback, not part of the main spell effect. Previously the AI
        # confused the two and Tier 3 resolved it as "return 3 creatures with
        # no sacrifice", producing a 3-for-0 (game_1506623303765463040:867).
        self._add_card("dread return", EffectTemplate(
            name="Dread Return",
            description="Return target creature card from your graveyard to the battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "reanimate", "player": ctrl,
                 "card": ctx.get('best_graveyard_creature', ''),
                 "reason": "Dread Return single-target reanimate"},
            ] if ctx.get('best_graveyard_creature') else [
                {"action": "no_action", "reason": "No creature cards in your graveyard"}
            ],
        ))

        # --- Goblin Rabblemaster: attack trigger creates a 1/1 Goblin
        # token tapped and attacking, plus +1/+1 to other Goblins until EOT.
        # game_1506623254943498252:213 escalated to Tier 3 and silently
        # produced no state change — token never appeared.
        #
        # Uses pump_all_creatures with subtype + exclude_name filters (added
        # May 20). NOTE: `pump_subtype` was a typo in an earlier draft — the
        # action handler doesn't exist; pump_all_creatures with `subtype` is
        # the correct path.
        self._add_attack_card("goblin rabblemaster", EffectTemplate(
            name="Goblin Rabblemaster (attack)",
            description="Create a 1/1 red Goblin tapped attacking; other Goblins get +1/+1 until EOT",
            action_generator=lambda ctrl, opp, ctx: [
                # Token: 1/1 red Goblin, tapped + attacking. The `tapped` and
                # `attacking` flags are honored by create_token (May 20 audit).
                {"action": "create_token", "player": ctrl, "name": "Goblin",
                 "power": 1, "toughness": 1, "types": "Creature — Goblin",
                 "count": 1, "tapped": True, "attacking": True,
                 "colors": ["R"], "attacking_player": opp},
                # Anthem for other Goblins controlled by Rabblemaster's controller.
                # `subtype` filter pumps only Goblins; `exclude` skips Rabblemaster
                # itself (it's not "other Goblins" — see CR 700.5 "another").
                {"action": "pump_all_creatures", "player": ctrl,
                 "subtype": "Goblin", "exclude": "Goblin Rabblemaster",
                 "power": 1, "toughness": 1,
                 "source": "Goblin Rabblemaster attack anthem"},
            ],
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
        self._add_card("supreme verdict", EffectTemplate(
            name="Supreme Verdict",
            description="Destroy all creatures (can't be countered)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy_all_creatures"},
            ],
        ))
        self._add_card("terminus", EffectTemplate(
            name="Terminus",
            description="Put all creatures on the bottom of their owners' libraries",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "tuck_all_creatures"},
            ],
        ))
        self._add_card("hallowed burial", EffectTemplate(
            name="Hallowed Burial",
            description="Put all creatures on the bottom of their owners' libraries",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "tuck_all_creatures"},
            ],
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
        self._add_card("vapor snag", EffectTemplate(
            name="Vapor Snag",
            description="Return target creature to its owner's hand, its controller loses 1 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": ctx.get('best_opponent_creature', ''),
                 "from_zone": "battlefield", "to_zone": "hand", "player": opp},
                {"action": "lose_life", "player": opp, "amount": 1},
            ] if ctx.get('best_opponent_creature') else [
                {"action": "no_action", "reason": "No creature to bounce"}
            ],
        ))
        # Snap: bounce + untap 2 lands
        self._add_card("snap", EffectTemplate(
            name="Snap",
            description="Return target creature to its owner's hand, untap up to two lands",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": ctx.get('best_opponent_creature', ''),
                 "from_zone": "battlefield", "to_zone": "hand", "player": opp},
                {"action": "untap_lands", "player": ctrl, "count": 2},
            ] if ctx.get('best_opponent_creature') else [
                {"action": "no_action", "reason": "No creature to bounce"}
            ],
        ))
        # Chain of Vapor: bounce target nonland permanent
        self._add_card("chain of vapor", EffectTemplate(
            name="Chain of Vapor",
            description="Return target nonland permanent to its owner's hand",
            action_generator=self._gen_bounce_opponent_permanent,
        ))

        # --- Snapcaster Mage: grant flashback to instant/sorcery in graveyard ---
        self._add_card("snapcaster mage", EffectTemplate(
            name="Snapcaster Mage",
            description="Target instant or sorcery in your graveyard gains flashback",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "grant_flashback", "player": ctrl},
            ],
        ))

        # --- Sphinx of Uthuun: Fact or Fiction variant (reveal 5, split into 2 piles) ---
        self._add_card("sphinx of uthuun", EffectTemplate(
            name="Sphinx of Uthuun",
            description="When Sphinx of Uthuun enters, reveal top 5, opponent splits, you choose a pile",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 3},
            ],
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
        self._add_dies_card("protean hulk", EffectTemplate(
            name="Protean Hulk",
            description="When Protean Hulk dies, search library for creatures with total MV ≤ 6, put them onto the battlefield",
            action_generator=_protean_hulk_gen,
        ))

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
        self._add_dies_card("pattern of rebirth", EffectTemplate(
            name="Pattern of Rebirth",
            description="When enchanted creature dies, search library for a creature, put it onto the battlefield",
            action_generator=_pattern_of_rebirth_gen,
        ))

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

        # Solemn Simulacrum dies → draw. Register on the dies-templates path
        # so it doesn't collide with the ETB land-search registration above.
        # May 17 audit: the old `_add_card` registration overwrote the ETB
        # entry, meaning Solemn Sim's land-search never fired in play.
        self._add_dies_card("solemn simulacrum", EffectTemplate(
            name="Solemn Simulacrum",
            description="When Solemn Simulacrum dies, draw a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1}
            ],
        ))

        # --- Upkeep triggers ---
        self._add_card("phyrexian arena", EffectTemplate(
            name="Phyrexian Arena",
            description="At the beginning of your upkeep, draw a card and lose 1 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
                {"action": "lose_life", "player": ctrl, "amount": 1},
            ],
        ))

        self._add_card("bitterblossom", EffectTemplate(
            name="Bitterblossom",
            description="At the beginning of your upkeep, lose 1 life and create a 1/1 Faerie Rogue token with flying",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "lose_life", "player": ctrl, "amount": 1},
                make_token_action(ctrl, "faerie_rogue_1_1", 1),
            ],
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

        # --- Attack triggers ---
        # Bug #18: Goldspan Dragon — create Treasure on attack
        self._add_card("goldspan dragon", EffectTemplate(
            name="Goldspan Dragon",
            description="Whenever Goldspan Dragon attacks, create a Treasure token",
            action_generator=lambda ctrl, opp, ctx: [make_token_action(ctrl, "treasure", 1)],
        ))

        # --- Creature-enters triggers (for other permanents) ---
        # Trostani: whenever a creature enters, gain life equal to its toughness
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

        # ===========================================================
        # NEW TEMPLATES — March 22 log audit
        # ===========================================================

        self._add_card("twinflame tyrant", EffectTemplate(
            name="Twinflame Tyrant",
            description="Deal 5 damage to any target",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 5, "target_player": opp}
            ],
        ))

        self._add_card("skyshroud claim", EffectTemplate(
            name="Skyshroud Claim",
            description="Search for up to two Forest cards, put onto battlefield untapped",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "card_type": "Forest",
                 "destination": "battlefield", "tapped": False},
                {"action": "search_library", "player": ctrl, "card_type": "Forest",
                 "destination": "battlefield", "tapped": False},
            ],
        ))

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

        self._add_card("tooth and nail", EffectTemplate(
            name="Tooth and Nail",
            description="Search for 2 creatures, put 2 from hand onto battlefield (entwined)",
            action_generator=self._gen_tooth_and_nail,
        ))

        # --- Teferi's Protection: phase out all permanents, protection from everything ---
        self._add_card("teferi's protection", EffectTemplate(
            name="Teferi's Protection",
            description="Phase out all permanents you control, protection from everything until next turn",
            action_generator=self._gen_teferis_protection,
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

        # Settle the Wreckage: exile all attacking creatures, defender searches for basics
        self._add_card("settle the wreckage", EffectTemplate(
            name="Settle the Wreckage",
            description="Exile all attacking creatures. Controller searches for that many basics tapped.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action", "reason": "Settle the Wreckage: exile attacking creatures, search for basics tapped (complex)"},
            ],
        ))

        # --- Graveyard spells ---
        self._add_card("buried alive", EffectTemplate(
            name="Buried Alive",
            description="Search library for up to 3 creature cards, put into graveyard",
            action_generator=self._gen_buried_alive,
        ))

        self._add_card("reanimate", EffectTemplate(
            name="Reanimate",
            description="Return target creature from a graveyard to battlefield, lose life equal to CMC",
            action_generator=self._gen_reanimate,
        ))

        # Animate Dead: reanimate from any graveyard (no life loss, aura attaches)
        # Template avoids Tier 3 which incorrectly fires LTB trigger during ETB
        self._add_card("animate dead", EffectTemplate(
            name="Animate Dead",
            description="Return target creature from a graveyard to battlefield under your control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "reanimate", "player": ctrl,
                 "card": ctx.get('best_graveyard_creature', ''),
                 "reason": "Animate Dead returns creature from graveyard"},
            ] if ctx.get('best_graveyard_creature') else [
                {"action": "no_action", "reason": "No creature cards in any graveyard"}
            ],
        ))

        # Sidisi, Undead Vizier: Exploit — sacrifice a creature, search for any card
        self._add_card("sidisi, undead vizier", EffectTemplate(
            name="Sidisi, Undead Vizier",
            description="Exploit: sacrifice a creature, then search library for a card",
            action_generator=lambda ctrl, opp, ctx: self._gen_sidisi_exploit(ctrl, opp, ctx),
        ))

        self._add_card("living death", EffectTemplate(
            name="Living Death",
            description="All players sacrifice all creatures, then return all creature cards from graveyards",
            action_generator=self._gen_living_death,
        ))

        self._add_card("rise of the dark realms", EffectTemplate(
            name="Rise of the Dark Realms",
            description="Put all creature cards from all graveyards onto the battlefield under your control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "rise_of_dark_realms", "player": ctrl,
                 "reason": "Rise of the Dark Realms: reanimate all creatures from all graveyards"},
            ],
        ))

        # --- Open the Vaults: return all artifact and enchantment cards from all graveyards ---
        self._add_card("open the vaults", EffectTemplate(
            name="Open the Vaults",
            description="Return all artifact and enchantment cards from all graveyards to the battlefield under their owners' control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "open_the_vaults", "player": ctrl,
                 "reason": "Open the Vaults: return all artifacts and enchantments from all graveyards"},
            ],
        ))

        # --- Replenish: return all enchantment cards from YOUR graveyard ---
        self._add_card("replenish", EffectTemplate(
            name="Replenish",
            description="Return all enchantment cards from your graveyard to the battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "replenish", "player": ctrl,
                 "reason": "Replenish: return all enchantments from your graveyard"},
            ],
        ))

        # --- Fact or Fiction ---
        self._add_card("fact or fiction", EffectTemplate(
            name="Fact or Fiction",
            description="Reveal top 5, split into piles (simplified: draw 3, mill 2)",
            action_generator=self._gen_fact_or_fiction,
        ))

        # --- Commit // Memory (Commit half — fizzles politely if no target) ---
        # Aftermath card; only the Commit half is castable from hand. The
        # Memory half is hardcoded as cast-from-graveyard-only, which the
        # current engine doesn't model — so we just handle the Commit side.
        # CR text: "Put target spell or nonland permanent into its owner's
        # library second from the top." Engine doesn't model second-from-top
        # exactly, so we approximate as bounce-to-library.
        def _gen_commit(ctrl, opp, ctx):
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

        # --- Inferno Titan / Bogardan Hellkite: divided damage ETB ---
        self._add_card("inferno titan", EffectTemplate(
            name="Inferno Titan",
            description="Deal 3 damage divided among targets on ETB and attack",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 3, "target_player": opp},
            ],
        ))

        self._add_card("bogardan hellkite", EffectTemplate(
            name="Bogardan Hellkite",
            description="Deal 5 damage divided among targets",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 5, "target_player": opp},
            ],
        ))

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

        self._add_card("etali, primal storm", EffectTemplate(
            name="Etali, Primal Storm",
            description="Exile top card of each library, cast nonland cards for free",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "etali_trigger", "player": ctrl,
                 "reason": "Exile top of each library, may cast nonland cards without paying mana costs"}
            ],
        ))

        self._add_card("robber of the rich", EffectTemplate(
            name="Robber of the Rich",
            description="Exile top card of defending player's library",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "move_card", "card": "top_of_library", "from_zone": "library",
                 "to_zone": "exile", "player": opp,
                 "reason": "Robber of the Rich: exile top card"}
            ],
        ))

        # --- END STEP TRIGGERS ---
        self._add_card("soulherder", EffectTemplate(
            name="Soulherder",
            description="End step: exile another creature you control, return to battlefield",
            action_generator=lambda ctrl, opp, ctx: self._gen_felidar_guardian(ctrl, opp, ctx),
        ))

        self._add_card("altar of the brood", EffectTemplate(
            name="Altar of the Brood",
            description="Each opponent mills 1 card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "mill", "player": opp, "amount": 1}
            ],
        ))

        self._add_card("ancient bronze dragon", EffectTemplate(
            name="Ancient Bronze Dragon",
            description="Roll d20, put that many +1/+1 counters on each creature you control",
            action_generator=self._gen_ancient_bronze_dragon,
        ))

        self._add_card("ponder", EffectTemplate(
            name="Ponder",
            description="Look at top 3, reorder, may shuffle. Draw 1",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1}
            ],
        ))

        # ===========================================================
        # NEW TEMPLATES — March 27 Tier 3 gap closure
        # ===========================================================

        # --- Jarad's Orders: search for 2 creatures (one to hand, one to graveyard) ---
        # Can't actually search the library, so hint for manual resolution
        self._add_card("jarad's orders", EffectTemplate(
            name="Jarad's Orders",
            description="Search library for two creature cards: one to hand, one to graveyard, shuffle",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "filter_type": "creature",
                 "to_zone": "hand", "count": 1, "shuffle": False},
                {"action": "search_library", "player": ctrl, "filter_type": "creature",
                 "to_zone": "graveyard", "count": 1, "shuffle": True},
            ],
        ))

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

        # --- Single Combat: each player keeps 1 creature/PW, destroy the rest ---
        self._add_card("single combat", EffectTemplate(
            name="Single Combat",
            description="Each player chooses a creature or planeswalker. Destroy all others.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "single_combat_wipe", "player": ctrl,
                 "reason": "Single Combat: each player keeps their best creature/PW, destroy the rest"}
            ],
        ))

        # --- Rootborn Defenses: populate + indestructible ---
        self._add_card("rootborn defenses", EffectTemplate(
            name="Rootborn Defenses",
            description="Populate. Creatures you control gain indestructible until end of turn.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "populate", "player": ctrl},
                {"action": "grant_keywords", "player": ctrl,
                 "keywords": ["Indestructible"], "target": "all_own_creatures"},
            ],
        ))

        # NOTE: Light Up the Stage is registered later in this file with a
        # working draw-2 approximation (line ~5778). The earlier "exile
        # top_of_library" version that lived here referenced a card name the
        # move_card action doesn't recognize, so it silently produced no state
        # change. Removed during Apr 29 audit — the later registration is the
        # canonical one.

        # --- Flawless Maneuver: creatures gain indestructible ---
        self._add_card("flawless maneuver", EffectTemplate(
            name="Flawless Maneuver",
            description="Creatures you control gain indestructible until end of turn",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "grant_keywords", "player": ctrl,
                 "keywords": ["Indestructible"], "target": "all_own_creatures"},
            ],
        ))

        # --- Species Specialist: choose a creature type (no game action) ---
        self._add_card("species specialist", EffectTemplate(
            name="Species Specialist",
            description="As Species Specialist enters, choose a creature type",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Species Specialist: creature type chosen (draw trigger active for that type dying)"}
            ],
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
        import random
        library = ctx.get('controller_library', [])
        if not library:
            return [{"action": "no_action", "reason": "Growing Rites: library is empty"}]

        look_count = min(4, len(library))
        found_creature = None

        for i in range(look_count):
            card = library[i]
            type_line = (card.type_line or '').lower() if hasattr(card, 'type_line') else ''
            if 'creature' in type_line:
                found_creature = card.name if hasattr(card, 'name') else str(card)
                break

        if found_creature:
            return [{"action": "move_card", "card": found_creature,
                     "from_zone": "library", "to_zone": "hand", "player": ctrl}]
        return [{"action": "no_action",
                 "reason": "Growing Rites of Itlimoc: no creature found in top 4 cards"}]

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
        self._add_pattern(
            r"whenever (?:a|another) .*?creature.*?enters.*?draw a card",
            EffectTemplate(
                name="Creature-enters Draw",
                description="Draw a card when a creature enters",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "draw_cards", "player": ctrl, "amount": 1}
                ]
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
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "deal_damage", "amount": int(ctx['_match'].group(1)), "target_player": opp}
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
                action_generator=lambda ctrl, opp, ctx: self._gen_werewolf_transform(ctrl, opp, ctx),
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

        # Amass: "amass [Zombies/Orcs] N"
        # Simplified: creates a 0/0 Army token + N +1/+1 counters
        # TODO: Full amass should check if an Army token exists and just add counters
        self._add_pattern(
            r"amass (?:\w+ )?(\d+)",
            EffectTemplate(
                name="Amass",
                description="Amass: create or grow Army token with +1/+1 counters",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "create_token", "player": ctrl, "name": "Zombie Army",
                     "power": 0, "toughness": 0, "types": "Creature - Zombie Army", "count": 1},
                    {"action": "add_counters", "card": "Zombie Army", "counter_type": "+1/+1",
                     "amount": int(ctx['_match'].group(1))}
                ]
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
        self._add_pattern(
            r"when .+? enters.*?return (?:a|another) (?:permanent|creature) you control to",
            EffectTemplate(
                name="ETB Bounce Own Permanent",
                description="Return a permanent you control to its owner's hand",
                action_generator=lambda ctrl, opp, ctx: [
                    {"action": "bounce_own_permanent", "player": ctrl,
                     "exclude": ctx.get('_source_card_name', '')}
                ]
            )
        )

        # Scry pattern: "scry N" — covers any card with scry
        # Bug fix: was returning no_action, causing 26 Tier 3 escalations + judge double-calls.
        # Now simulates scry for autoplay: 50% chance bottom the top card, 50% keep.
        # Since there's no "scry" action type, we either move top card to bottom or keep it.
        # May 7 audit fix #4: stash the parsed N on the context so the description
        # template can fill it in (was leaking literal "Scry N" to Discord).
        def _scry_action(ctrl, opp, ctx):
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
        # "create a token that's a copy of [target]" — covers Thousand-Faced Shadow,
        # Helm of the Host, Rite of Replication, Mimic Vat, etc.
        self._add_pattern(
            r"create a token that(?:'s| is) a copy of (?:another )?(?:target )?(?:attacking )?(creature|permanent)",
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

        # Thousand-Faced Shadow — specific card template (ninjutsu + copy on ETB)
        self._add_card("thousand-faced shadow", EffectTemplate(
            name="Thousand-Faced Shadow",
            description="When Thousand-Faced Shadow enters attacking via ninjutsu, create a copy of another attacking creature",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_copy_token", "player": ctrl,
                 "target": "best_attacking_creature", "filter": "attacking", "count": 1}
            ],
        ))

        # Helm of the Host — creates copy of equipped creature at combat
        self._add_card("helm of the host", EffectTemplate(
            name="Helm of the Host",
            description="At the beginning of combat, create a token copy of equipped creature",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_copy_token", "player": ctrl,
                 "target": "best_creature", "filter": "own", "count": 1}
            ],
        ))

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
        self._add_card("spark double", EffectTemplate(
            name="Spark Double",
            description="Enter as a copy of a creature or planeswalker you control with extra +1/+1 counter",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "become_copy", "source": ctx.get('_source_card_name', 'Spark Double'),
                 "target": "best_creature", "filter": "own", "player": ctrl,
                 "extra_counters": {"+1/+1": 1}},
            ],
        ))
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

        # Rite of Replication — create 5 copies if kicked, 1 otherwise
        # Kicker detection: base cost {2}{U}{U} (4 mana), kicked adds {5} (total 9+)
        self._add_card("rite of replication", EffectTemplate(
            name="Rite of Replication",
            description="Create a token copy of target creature (5 if kicked)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_copy_token", "player": ctrl,
                 "target": "best_creature", "filter": "any",
                 "count": 5 if (ctx.get('kicked', False) or (ctx.get('mana_paid_total', 0) or 0) >= 9) else 1}
            ],
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

        # --- Brainstorm: draw 3, put 2 back on top ---
        self._add_card("brainstorm", EffectTemplate(
            name="Brainstorm",
            description="Draw 3 cards, put 2 back on top of library",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 3},
                {"action": "put_back_from_hand", "player": ctrl, "count": 2,
                 "reason": "Brainstorm: put 2 cards from hand on top of library"}
            ],
        ))

        # --- Treasure Cruise: draw 3 ---
        self._add_card("treasure cruise", EffectTemplate(
            name="Treasure Cruise",
            description="Draw three cards",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 3}
            ],
        ))

        # --- Harmonize: draw 3 ---
        self._add_card("harmonize", EffectTemplate(
            name="Harmonize",
            description="Draw three cards",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 3}
            ],
        ))

        # --- Divination: draw 2 ---
        self._add_card("divination", EffectTemplate(
            name="Divination",
            description="Draw two cards",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 2}
            ],
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

        # --- Spectral Procession: create three 1/1 white Spirit tokens with flying ---
        self._add_card("spectral procession", EffectTemplate(
            name="Spectral Procession",
            description="Create three 1/1 white Spirit creature tokens with flying",
            action_generator=lambda ctrl, opp, ctx: [
                make_token_action(ctrl, "spirit_1_1_fly", 3),
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

        # --- Dig Through Time: look at top 7, put 2 in hand (simplified) ---
        self._add_card("dig through time", EffectTemplate(
            name="Dig Through Time",
            description="Look at top seven, put two into hand (simplified as draw 2)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 2}
            ],
        ))

        # --- Peregrine Drake: untap up to 5 lands ---
        self._add_card("peregrine drake", EffectTemplate(
            name="Peregrine Drake",
            description="Untap up to five lands",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "untap_lands", "player": ctrl, "count": 5}
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
                 "target_controller": opp},
            ] if ctx.get('best_opponent_creature') else [
                {"action": "deal_damage", "amount": 3, "target_player": opp},
            ],
        ))

        # --- Kroxa, Titan of Death's Hunger: sacrifice unless escaped ---
        self._add_card("kroxa, titan of death's hunger", EffectTemplate(
            name="Kroxa, Titan of Death's Hunger",
            description="Each opponent discards a card and loses 3 life if nonland; sacrifice Kroxa unless escaped",
            action_generator=self._gen_kroxa_etb,
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

        # --- Satyr Wayfinder: reveal top 4, land to hand, rest to graveyard ---
        # Simplified as drawing 1 card (finding a land)
        self._add_card("satyr wayfinder", EffectTemplate(
            name="Satyr Wayfinder",
            description="Reveal top 4, put a land into hand, rest into graveyard (simplified as draw 1)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1}
            ],
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

        # --- Stitcher's Supplier: mill 3 on ETB ---
        self._add_card("stitcher's supplier", EffectTemplate(
            name="Stitcher's Supplier",
            description="Mill three cards",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "mill", "player": ctrl, "amount": 3}
            ],
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

        # --- Kogla ATTACK trigger: destroy target artifact or enchantment ---
        # Separate from the ETB fight trigger above — uses _add_attack_card so
        # resolve_attack_trigger picks this up instead of the ETB fight template.
        self._add_attack_card("kogla, the titan ape", EffectTemplate(
            name="Kogla, the Titan Ape (attack)",
            description="Destroy target artifact or enchantment defending player controls",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": "BEST_ARTIFACT_OR_ENCHANTMENT",
                 "target_controller": opp}
            ],
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

        # --- Prismatic Vista (pay 1 life, basic land, untapped) ---
        self._add_card("prismatic vista", EffectTemplate(
            name="Prismatic Vista",
            description="Pay 1 life, sacrifice: search for a basic land (untapped)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "lose_life", "player": ctrl, "amount": 1},
                {"action": "search_library_land", "player": ctrl,
                 "basic_only": True, "enters_tapped": False},
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

        # --- Shard Volley: sacrifice a land, deal 3 damage to any target ---
        def _shard_volley(ctrl, opp, ctx):
            # Find a land to sacrifice (prefer non-fetch, cheapest)
            ctrl_lands = []
            for c in ctx.get('_controller_creatures', []):
                pass  # Not relevant
            # We need to look at the battlefield directly via game context
            # Since we don't have game object, use controller_land_count as a check
            # and emit a move_card action for "worst_land" (the engine will find it)
            if ctx.get('controller_land_count', 0) > 0:
                return [
                    {"action": "sacrifice_land", "player": ctrl},
                    {"action": "deal_damage", "amount": 3, "target_player": opp},
                ]
            return [{"action": "no_action", "reason": "No land to sacrifice for Shard Volley"}]
        self._add_card("shard volley", EffectTemplate(
            name="Shard Volley",
            description="Sacrifice a land. Deal 3 damage to any target.",
            action_generator=_shard_volley,
        ))

        # --- Aura Shards: destroy target artifact/enchantment when creature enters ---
        self._add_card("aura shards", EffectTemplate(
            name="Aura Shards",
            description="Destroy target artifact or enchantment when a creature enters",
            action_generator=lambda ctrl, opp, ctx: self._gen_aura_shards(ctrl, opp, ctx),
        ))

        # --- Tatyova, Benthic Druid: landfall draw + gain 1 life ---
        self._add_card("tatyova, benthic druid", EffectTemplate(
            name="Tatyova, Benthic Druid",
            description="Whenever a land enters under your control, draw a card and gain 1 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
                {"action": "gain_life", "player": ctrl, "amount": 1},
            ],
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

        # --- Thoughtseize (like Inquisition but no MV restriction, caster loses 2 life) ---
        self._add_card("thoughtseize", EffectTemplate(
            name="Thoughtseize",
            description="Target opponent reveals hand, you choose a nonland card, they discard it. You lose 2 life.",
            action_generator=lambda ctrl, opp, ctx: self._gen_thoughtseize(ctrl, opp, ctx),
        ))

        # =================================================================
        # RESTRICTED TUTORS — search library for specific card types to hand
        # =================================================================

        # Recruiter of the Guard: search for creature with toughness 2 or less
        self._add_card("recruiter of the guard", EffectTemplate(
            name="Recruiter of the Guard",
            description="Search library for a creature with toughness 2 or less, put into hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "card_type": "Creature",
                 "max_toughness": 2, "to_zone": "hand"}
            ],
        ))

        # Stoneforge Mystic: search for Equipment card
        self._add_card("stoneforge mystic", EffectTemplate(
            name="Stoneforge Mystic",
            description="Search library for an Equipment card, put into hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "card_type": "Equipment",
                 "to_zone": "hand"}
            ],
        ))

        # Trinket Mage: search for artifact with MV 1 or less
        self._add_card("trinket mage", EffectTemplate(
            name="Trinket Mage",
            description="Search library for an artifact card with MV 1 or less, put into hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "card_type": "Artifact",
                 "max_mv": 1, "to_zone": "hand"}
            ],
        ))

        # Trophy Mage: search for artifact with MV exactly 3
        self._add_card("trophy mage", EffectTemplate(
            name="Trophy Mage",
            description="Search library for an artifact card with MV exactly 3, put into hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "card_type": "Artifact",
                 "exact_mv": 3, "to_zone": "hand"}
            ],
        ))

        # Tribute Mage: search for artifact with MV exactly 2
        self._add_card("tribute mage", EffectTemplate(
            name="Tribute Mage",
            description="Search library for an artifact card with MV exactly 2, put into hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "card_type": "Artifact",
                 "exact_mv": 2, "to_zone": "hand"}
            ],
        ))

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

        # Geralf's Messenger: "When Geralf's Messenger enters, target opponent loses
        # 2 life." It ALSO has undying — but undying is SBA-handled, so it must NOT be
        # in the no-action list above (May 26 audit: that list swallowed the ETB drain
        # on the initial cast AND every undying return). CR 603.6c: the undying-returned
        # permanent is a new object whose ETB fires again, so this drain repeats per enter.
        self._add_card("geralf's messenger", EffectTemplate(
            name="Geralf's Messenger",
            description="ETB: target opponent loses 2 life (undying still SBA-handled)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "lose_life", "player": opp, "amount": 2}
            ],
        ))
        # ...but on the DIES event we must NOT re-run the ETB drain. resolve_etb
        # checks _dies_templates first (early return), so register a no-op there;
        # this also preserves the old behavior of suppressing the spurious
        # !resolve prompt on the second death (undying can't re-apply once a
        # +1/+1 counter is present, but the undying text is still in the oracle).
        self._add_dies_card("geralf's messenger", EffectTemplate(
            name="Geralf's Messenger",
            description="Undying handled by SBA engine; ETB drain does not re-fire on death",
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

        # Scute Swarm: landfall — create a 1/1 green Insect creature token
        # (The "6+ lands → copy" mode is too complex for templates; always make 1/1 Insect)
        self._add_card("scute swarm", EffectTemplate(
            name="Scute Swarm",
            description="Landfall: create a 1/1 green Insect creature token",
            action_generator=lambda ctrl, opp, ctx: [
                make_token_action(ctrl, "insect_1_1_plain", 1)
            ],
        ))

        # Goblin Guide: attack trigger — defending player reveals top card,
        # if it's a land, they put it into their hand
        self._add_card("goblin guide", EffectTemplate(
            name="Goblin Guide",
            description="Defending player reveals top card; if land, put into their hand",
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
                {"action": "draw_cards", "player": ctrl,
                 "amount": max(1, ctx.get('x_value', 3))},
                {"action": "move_card", "card": "Blue Sun's Zenith",
                 "from_zone": "graveyard", "to_zone": "library", "player": ctrl},
            ],
        ))

        # Mystical Tutor: search library for instant or sorcery, put on top
        self._add_card("mystical tutor", EffectTemplate(
            name="Mystical Tutor",
            description="Search library for an instant or sorcery card, put on top",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl,
                 "card_type": "Instant or Sorcery", "to_zone": "library_top"}
            ],
        ))

        # Worldly Tutor: search library for a creature, put on top
        self._add_card("worldly tutor", EffectTemplate(
            name="Worldly Tutor",
            description="Search library for a creature card, put on top",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl,
                 "card_type": "Creature", "to_zone": "library_top"}
            ],
        ))

        # Vampiric Tutor: search library for any card, put on top, lose 2 life
        self._add_card("vampiric tutor", EffectTemplate(
            name="Vampiric Tutor",
            description="Search library for any card, put on top; lose 2 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl,
                 "card_type": "Any", "to_zone": "library_top"},
                {"action": "lose_life", "player": ctrl, "amount": 2},
            ],
        ))

        # Demonic Tutor: search library for any card, put into hand
        self._add_card("demonic tutor", EffectTemplate(
            name="Demonic Tutor",
            description="Search library for any card, put into hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl,
                 "card_type": "Any", "to_zone": "hand"}
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

        # --- Enlightened Tutor: search for artifact or enchantment, put on top ---
        self._add_card("enlightened tutor", EffectTemplate(
            name="Enlightened Tutor",
            description="Search library for an artifact or enchantment card, put on top",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl,
                 "card_type": "Artifact or Enchantment", "to_zone": "library_top"}
            ],
        ))

        # --- Entomb: search library for a card, put into graveyard ---
        self._add_card("entomb", EffectTemplate(
            name="Entomb",
            description="Search library for a card and put it into your graveyard",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl,
                 "card_type": "Any", "to_zone": "graveyard"}
            ],
        ))

        # --- Apr 29 audit additions: gap-filling tutors ---

        # Idyllic Tutor: search library for an enchantment, put into hand
        self._add_card("idyllic tutor", EffectTemplate(
            name="Idyllic Tutor",
            description="Search library for an enchantment card, put into hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl,
                 "card_type": "Enchantment", "to_zone": "hand"}
            ],
        ))

        # Eladamri's Call: search library for a creature, put into hand
        self._add_card("eladamri's call", EffectTemplate(
            name="Eladamri's Call",
            description="Search library for a creature card, put into hand",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl,
                 "card_type": "Creature", "to_zone": "hand"}
            ],
        ))

        # --- Increasing Devotion: create 5 (or 10 from graveyard) 1/1 Human tokens ---
        self._add_card("increasing devotion", EffectTemplate(
            name="Increasing Devotion",
            description="Create 5 (or 10 if from graveyard) 1/1 Human tokens",
            action_generator=lambda ctrl, opp, ctx: self._gen_increasing_devotion(ctrl, opp, ctx),
        ))

        # Endrek Sahr, Master Breeder: cast trigger creates X 1/1 Thrull tokens
        # (X = cast creature's MV). NOT an ETB — the cast trigger is handled elsewhere.
        # Register as no-ETB so templates don't fall through to Tier 3.
        # Also has "sacrifice when you control 7+ Thrulls" — needs SBA handling, not template.
        self._add_card("endrek sahr, master breeder", EffectTemplate(
            name="Endrek Sahr, Master Breeder",
            description="No ETB action (cast trigger creates Thrull tokens; 7+ Thrull check needs SBA)",
            action_generator=lambda ctrl, opp, ctx: [],
        ))

        # Korvold, Fae-Cursed King: ETB — sacrifice another permanent
        # (The "draw a card + +1/+1 counter on sacrifice" trigger is ongoing, handled separately)
        self._add_card("korvold, fae-cursed king", EffectTemplate(
            name="Korvold, Fae-Cursed King",
            description="When Korvold enters or attacks, sacrifice another permanent",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "sacrifice_permanent", "player": ctrl,
                 "exclude": "Korvold, Fae-Cursed King",
                 "reason": "Korvold, Fae-Cursed King: sacrifice another permanent"}
            ],
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

        # Mikaeus, the Lunarch: — already exists elsewhere.
        # Mikaeus, Archon of Sun's Grace: "Whenever an enchantment enters
        # under your control, create a 1/1 white and black Spirit creature
        # token with flying." (Own-ETB trigger via Aura or Sigarda variant;
        # here as a simple template emitting the Spirit token on self-ETB AND
        # triggered via enchantment-enters scan.)
        self._add_card("archon of sun's grace", EffectTemplate(
            name="Archon of Sun's Grace",
            description="Create a 1/1 white/black Spirit with flying when an enchantment enters",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Spirit",
                 "power": 1, "toughness": 1,
                 "types": "Creature — Spirit", "count": 1,
                 "keywords": ["Flying"]}
            ],
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

        # Draconic Destiny: "Enchanted creature gets +3/+3 and is a Dragon in
        # addition to its other types." Aura; the pump is static via layers,
        # not a combat-start trigger. Template is a safe no-op so Tier 3
        # doesn't spin up trying to resolve the static ability.
        self._add_card("draconic destiny", EffectTemplate(
            name="Draconic Destiny",
            description="Static +3/+3 aura — handled via layers, not an ETB action",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Draconic Destiny: static pump handled by layers engine"}
            ],
        ))

        # =================================================================
        # EDICT EFFECTS — Plaguecrafter and similar
        # =================================================================

        # Plaguecrafter: each player sacrifices a creature or planeswalker,
        # each player who can't discards a card
        self._add_card("plaguecrafter", EffectTemplate(
            name="Plaguecrafter",
            description="Each player sacrifices a creature or planeswalker. Can't? Discard.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "edict_sacrifice", "types": "creature_or_planeswalker",
                 "fallback": "discard"}
            ],
        ))

        # Demon's Disciple: same as Fleshbag but also includes planeswalkers
        self._add_card("demon's disciple", EffectTemplate(
            name="Demon's Disciple",
            description="Each player sacrifices a creature or planeswalker",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "edict_sacrifice", "types": "creature_or_planeswalker"}
            ],
        ))

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
            description="Add {R}{R}{R}, exile top 3 of library, play one (approx: draw 1)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "add_mana", "player": ctrl, "color": "R", "amount": 3},
                {"action": "draw_cards", "player": ctrl, "amount": 1},
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

        # Garruk, Primal Hunter -3: "Draw cards equal to greatest power among creatures you control."
        self._pw_ability_templates[("garruk", "draw cards equal to the greatest power")] = EffectTemplate(
            name="Garruk -3 Draw",
            description="Draw cards equal to greatest power among creatures you control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl,
                 "amount": max(ctx.get('greatest_power', 1), 1)}
            ],
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

        # Aminatou, the Fateshifter +1: "The top card of each player's library
        # becomes that player's library bottom card." May 20 audit found this
        # template was implementing the WRONG ability (draw-a-card-stack-top,
        # which is Jace-flavored) for the entire history of the bot. The
        # card_data_cache.json oracle_text was also wrong and has been corrected.
        self._pw_ability_templates[("aminatou", "becomes that player's library bottom card")] = EffectTemplate(
            name="Aminatou +1",
            description="Top card of each player's library becomes that player's bottom card",
            action_generator=lambda ctrl, opp, ctx: self._gen_aminatou_plus1(ctrl, opp, ctx),
        )
        # Looser match in case future oracle reprints reword the snippet
        self._pw_ability_templates[("aminatou", "library bottom card")] = EffectTemplate(
            name="Aminatou +1",
            description="Top card of each player's library becomes that player's bottom card",
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
        self._pw_ability_templates[("aminatou", "exile another target permanent")] = EffectTemplate(
            name="Aminatou -1 Flicker",
            description="Exile another permanent you own, then return it to the battlefield",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "flicker", "player": ctrl,
                  "target": ctx.get('best_own_etb_creature') or ctx.get('best_own_creature') or ''}]
                if (ctx.get('best_own_etb_creature') or ctx.get('best_own_creature'))
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
                [{"action": "grant_keywords", "player": ctrl,
                  "target_card": ctx.get('best_own_creature', ''),
                  "keywords": ["Vigilance", "Reach"],
                  "duration": "end_of_turn",
                  "source": "Vivien, Champion of the Wilds"}]
                if ctx.get('best_own_creature') else
                [{"action": "no_action",
                  "reason": "Vivien's +1 has no legal target (no creature you control)"}]
            ),
        )
        self._pw_ability_templates[("vivien, champion of the wilds", "look at the top three")] = EffectTemplate(
            name="Vivien Champion -2 Exile Top",
            description="Look at top 3, exile one face down for later casting (approx: draw 1)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
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

        # Teferi, Time Raveler +1: "Until your next turn, you may cast sorcery spells
        # as though they had flash." + "Draw a card."
        self._pw_ability_templates[("teferi, time raveler", "draw a card")] = EffectTemplate(
            name="Teferi T3 +1",
            description="Draw a card (sorcery flash is a continuous effect, handled by layers)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ],
        )

        # Teferi, Time Raveler -3: "Return up to one target artifact, creature, or
        # enchantment to its owner's hand. Draw a card."
        self._pw_ability_templates[("teferi, time raveler", "return up to one")] = EffectTemplate(
            name="Teferi T3 -3",
            description="Bounce a nonland permanent, then draw a card",
            action_generator=lambda ctrl, opp, ctx: (
                [
                    {"action": "move_card",
                     "card": ctx.get('best_opponent_nonland', ''),
                     "from_zone": "battlefield", "to_zone": "hand",
                     "player": opp},
                    {"action": "draw_cards", "player": ctrl, "amount": 1},
                ] if ctx.get('best_opponent_nonland') else [
                    {"action": "draw_cards", "player": ctrl, "amount": 1},
                ]
            ),
        )
        # Also match "return up to one target"
        self._pw_ability_templates[("teferi, time raveler", "return up to one target")] = EffectTemplate(
            name="Teferi T3 -3",
            description="Bounce a nonland permanent, then draw a card",
            action_generator=lambda ctrl, opp, ctx: (
                [
                    {"action": "move_card",
                     "card": ctx.get('best_opponent_nonland', ''),
                     "from_zone": "battlefield", "to_zone": "hand",
                     "player": opp},
                    {"action": "draw_cards", "player": ctrl, "amount": 1},
                ] if ctx.get('best_opponent_nonland') else [
                    {"action": "draw_cards", "player": ctrl, "amount": 1},
                ]
            ),
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

        # --- Flame Rift: deals 4 damage to each player ---
        self._add_card("flame rift", EffectTemplate(
            name="Flame Rift",
            description="Flame Rift deals 4 damage to each player",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 4, "target_player": ctrl},
                {"action": "deal_damage", "amount": 4, "target_player": opp},
            ],
        ))

        # --- Thoughtseize: target player reveals hand, you choose nonland, they discard. You lose 2 life. ---
        self._add_card("thoughtseize", EffectTemplate(
            name="Thoughtseize",
            description="Target player discards a nonland card. You lose 2 life.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "discard", "player": opp, "card": "best_nonland"},
                {"action": "lose_life", "player": ctrl, "amount": 2},
            ],
        ))

        # --- Brago, King Eternal: combat damage trigger (blink nonland permanents) ---
        # Fires when Brago deals combat damage to a player — used as attack template
        # because combat damage triggers go through resolve_attack_trigger
        self._add_attack_card("brago, king eternal", EffectTemplate(
            name="Brago, King Eternal (combat damage)",
            description="Exile any number of target nonland permanents you control, return them",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "mass_flicker", "player": ctrl, "filter": "nonland",
                 "reason": "Brago, King Eternal blinks nonland permanents"},
            ],
        ))

        # --- Cavalry Pegasus attack trigger ---
        # "Whenever Cavalry Pegasus attacks, each attacking Knight and each other
        # attacking Pegasus gain flying until end of turn."
        self._add_attack_card("cavalry pegasus", EffectTemplate(
            name="Cavalry Pegasus (attack)",
            description="Each attacking Knight and each other attacking Pegasus gain flying until end of turn",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "grant_keywords", "player": ctrl,
                 "keywords": ["Flying"], "duration": "end_of_turn",
                 "filter": "attacking_knights_and_other_pegasi",
                 "reason": "Cavalry Pegasus grants flying to attacking Knights and other attacking Pegasi"},
            ],
        ))

        # --- Assassin's Trophy: destroy nonland permanent, controller searches for basic land ---
        self._add_card("assassin's trophy", EffectTemplate(
            name="Assassin's Trophy",
            description="Destroy target nonland permanent. Its controller searches for a basic land tapped.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland') or ''},
                {"action": "search_library_land", "player": ctx.get('explicit_target_owner') or opp,
                 "basic_only": True, "enters_tapped": True},
            ] if (ctx.get('explicit_target_name') or ctx.get('best_opponent_nonland')) else [
                {"action": "no_action", "reason": "No nonland permanent to target"}
            ],
            needs_target=True,
        ))

        # --- Aura Shards: duplicate registration removed (working version is at line ~3142) ---

        # --- Archmage's Charm: modal spell (counter / draw 2 / steal MV<=1) ---
        # Default to draw-two when no stack target exists; counter when stack has spell
        # May 14 audit: filter self from stack_top so an empty-stack cast doesn't
        # end up "countering itself".
        self._add_card("archmage's charm", EffectTemplate(
            name="Archmage's Charm",
            description="Choose one: Counter target spell; Draw two cards; Gain control of target nonland permanent MV 1 or less",
            action_generator=lambda ctrl, opp, ctx: (
                [{"action": "counter_spell", "target": ctx.get('stack_top_spell', ''),
                  "controller": ctrl}]
                if ctx.get('stack_top_spell') and ctx.get('stack_top_spell', '').lower() != "archmage's charm" else
                [{"action": "draw_cards", "player": ctrl, "amount": 2}]
            ),
        ))

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

        # --- Halimar Depths: scry 3 (look at top 3, reorder) ---
        self._add_card("halimar depths", EffectTemplate(
            name="Halimar Depths",
            description="Look at the top 3 cards of your library, put them back in any order (scry 3)",
            action_generator=lambda ctrl, opp, ctx: self._scry_n(ctrl, 3),
        ))

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

        # --- Obscura Storefront: sacrifice, search for basic land, gain 1 life ---
        self._add_card("obscura storefront", EffectTemplate(
            name="Obscura Storefront",
            description="Sacrifice, search for basic Plains/Island/Swamp tapped, gain 1 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy", "card": "Obscura Storefront"},
                {"action": "search_library_land", "player": ctrl,
                 "basic_only": True, "enters_tapped": True},
                {"action": "gain_life", "player": ctrl, "amount": 1},
            ],
        ))

        # --- Cathartic Reunion: as additional cost discard 2, draw 3 ---
        self._add_card("cathartic reunion", EffectTemplate(
            name="Cathartic Reunion",
            description="Discard 2 cards, draw 3 cards",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "discard", "player": ctrl, "card": "random"},
                {"action": "discard", "player": ctrl, "card": "random"},
                {"action": "draw_cards", "player": ctrl, "amount": 3},
            ],
        ))

        # --- Tooth and Nail (entwined): search 2 creatures, put them onto the battlefield ---
        # Entwined cost is normally paid in Commander autoplay; collapse both
        # halves ("search 2 to hand" + "put up to 2 from hand onto battlefield")
        # into a single search-straight-to-battlefield so the message actually
        # names the two creatures that enter the battlefield.
        self._add_card("tooth and nail", EffectTemplate(
            name="Tooth and Nail",
            description="Search for 2 creatures and put them onto the battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "count": 2,
                 "card_type": "creature", "to_zone": "battlefield",
                 "reason": "Tooth and Nail (entwined): 2 creatures to battlefield"},
            ],
        ))

        # --- Yawgmoth's Will: play from graveyard this turn ---
        self._add_card("yawgmoth's will", EffectTemplate(
            name="Yawgmoth's Will",
            description="Until end of turn, you may play lands and cast spells from your graveyard",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "grant_flashback", "player": ctrl, "duration": "end_of_turn",
                 "source": "Yawgmoth's Will",
                 "reason": "Yawgmoth's Will: cast spells from graveyard this turn"},
            ],
        ))

        # --- Armored Skyhunter: attack trigger, look at top 6 for Equipment/Aura ---
        self._add_attack_card("armored skyhunter", EffectTemplate(
            name="Armored Skyhunter (attack)",
            description="Look at top 6 cards, put an Aura or Equipment onto the battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": f"{ctrl} looks at top 6 cards for Aura/Equipment (autoplay: no equipment found)"},
            ],
        ))

        # --- Courser of Kruphix: reveal top card of library (static), landfall gain 1 life ---
        self._add_card("courser of kruphix", EffectTemplate(
            name="Courser of Kruphix",
            description="Landfall: gain 1 life when a land enters",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "gain_life", "player": ctrl, "amount": 1},
            ],
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

        # --- Wilderness Reclamation: at beginning of your end step, untap all lands ---
        self._add_card("wilderness reclamation", EffectTemplate(
            name="Wilderness Reclamation",
            description="At the beginning of your end step, untap all lands you control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "untap_lands", "player": ctrl, "count": 99},
            ],
        ))

        # --- Trostani Discordant: ETB creates two 1/1 Soldier tokens with lifelink ---
        # NOTE: Trostani's static "+1/+1 to other creatures" is an anthem
        # registered by the layers system, NOT an ETB action. Earlier versions
        # of this template also granted Lifelink UEOT to all creatures — that
        # was wrong (the printed card has no such clause) and caused the
        # May 14 "Rhys at +2/+2 and '+1/+1 UEOT' message" audit finding.
        self._add_card("trostani discordant", EffectTemplate(
            name="Trostani Discordant",
            description="When Trostani Discordant enters, create two 1/1 white Soldier tokens with lifelink.",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Soldier",
                 "power": 1, "toughness": 1, "types": "Creature — Soldier", "count": 2,
                 "keywords": "lifelink"},
            ],
        ))
        # --- Trostani Discordant: end step return stolen creatures ---
        self._add_card("trostani discordant endstep", EffectTemplate(
            name="Trostani Discordant",
            description="At the beginning of your end step, each player gains control of all creatures they own",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Trostani Discordant: creatures return to their owners' control (no stolen creatures to return)"},
            ],
        ))

        # --- Korvold sacrifice trigger: whenever you sacrifice, +1/+1 counter + draw ---
        # (Korvold ETB already exists above — sacrifice another permanent)
        # This trigger fires from the Korvold card itself whenever ANY permanent is sacrificed
        self._add_card("korvold, fae-cursed king sacrifice", EffectTemplate(
            name="Korvold Sacrifice Trigger",
            description="Whenever you sacrifice a permanent, put a +1/+1 counter on Korvold and draw a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "add_counters", "card": "Korvold, Fae-Cursed King",
                 "counter_type": "+1/+1", "amount": 1},
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ],
        ))

        # --- Vindictive Vampire: whenever another creature you control dies, drain 1 ---
        self._add_card("vindictive vampire", EffectTemplate(
            name="Vindictive Vampire",
            description="Whenever another creature you control dies, each opponent loses 1 life and you gain 1 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "lose_life", "player": opp, "amount": 1},
                {"action": "gain_life", "player": ctrl, "amount": 1},
            ],
        ))

        # --- Sythis, Harvest's Hand: cast enchantment → gain 1 life + draw a card ---
        self._add_card("sythis, harvest's hand", EffectTemplate(
            name="Sythis, Harvest's Hand",
            description="Whenever you cast an enchantment spell, you gain 1 life and draw a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "gain_life", "player": ctrl, "amount": 1},
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ],
        ))

        # --- Kambal, Consul of Allocation: opponent casts noncreature → drain 2 ---
        self._add_card("kambal, consul of allocation", EffectTemplate(
            name="Kambal, Consul of Allocation",
            description="Whenever an opponent casts a noncreature spell, that player loses 2 life and you gain 2 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "lose_life", "player": opp, "amount": 2},
                {"action": "gain_life", "player": ctrl, "amount": 2},
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

        # --- Icebreaker Kraken: ETB freezes opponent's creatures+artifacts ---
        # "When this creature enters, artifacts and creatures target opponent
        # controls don't untap during that player's next untap step."
        # Approximation: tap all opponent creatures+artifacts now AND set
        # _skip_next_untap so they stay tapped through the opponent's next
        # untap step. (Strict CR would have you choose specific targets, but
        # the deck plays it for the freeze effect — close enough.)
        self._add_card("icebreaker kraken", EffectTemplate(
            name="Icebreaker Kraken",
            description="Tap each artifact and creature target opponent controls — they don't untap next turn",
            action_generator=lambda ctrl, opp, ctx: [{
                "action": "tap",
                "scope": "creatures_and_artifacts",
                "target_player": opp,
                "skip_next_untap": True,
                "source": "Icebreaker Kraken",
            }],
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

        # --- Syr Konrad, the Grim: whenever a creature dies or creature card enters/leaves
        # graveyard from anywhere, deal 1 damage to each opponent ---
        self._add_card("syr konrad, the grim", EffectTemplate(
            name="Syr Konrad, the Grim",
            description="Whenever another creature dies or a creature card enters/leaves graveyard, deal 1 to each opponent",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 1, "target_player": opp},
            ],
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
        self._add_card("ophiomancer", EffectTemplate(
            name="Ophiomancer",
            description="At the beginning of each upkeep, if you control no Snakes, create a 1/1 black Snake token with deathtouch",
            action_generator=lambda ctrl, opp, ctx: [
                make_token_action(ctrl, "snake_1_1_dt", 1),
            ],
        ))

        # --- Eidolon of Blossoms: constellation — whenever this or another enchantment
        # enters the battlefield under your control, draw a card ---
        self._add_card("eidolon of blossoms", EffectTemplate(
            name="Eidolon of Blossoms",
            description="Constellation: Whenever Eidolon of Blossoms or another enchantment enters under your control, draw a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ],
        ))

        # --- Setessan Champion: constellation — whenever an enchantment enters
        # under your control, put a +1/+1 counter on Setessan Champion and draw a card ---
        self._add_card("setessan champion", EffectTemplate(
            name="Setessan Champion",
            description="Constellation: Whenever an enchantment enters under your control, +1/+1 counter on Setessan Champion and draw a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "add_counters", "card": "Setessan Champion",
                 "counter_type": "+1/+1", "amount": 1},
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ],
        ))

        # =====================================================================
        # APR 6 AUDIT FIXES — New templates to eliminate Tier 3 API calls
        # =====================================================================

        # Fix 5: Dusk // Dawn — destroy creatures with power >= 3
        self._add_card("dusk // dawn", EffectTemplate(
            name="Dusk // Dawn",
            description="Destroy all creatures with power 3 or greater",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy_by_power", "min_power": 3},
            ],
        ))
        self._add_card("dusk", EffectTemplate(
            name="Dusk",
            description="Destroy all creatures with power 3 or greater",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "destroy_by_power", "min_power": 3},
            ],
        ))

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

        # Fix 17: Hammer of Nazahn — auto-equip when equipment enters
        self._add_card("hammer of nazahn", EffectTemplate(
            name="Hammer of Nazahn",
            description="Whenever an Equipment enters under your control, you may attach it to a creature you control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Hammer of Nazahn: auto-equip handled by equipment system"},
            ],
        ))

        # Fix 18: Bloodchief Ascension — end step drain
        self._add_card("bloodchief ascension", EffectTemplate(
            name="Bloodchief Ascension",
            description="At end step, if an opponent lost 2+ life this turn and has 3+ quest counters, each opponent loses 2 life and you gain 2 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "lose_life", "player": opp, "amount": 2},
                {"action": "gain_life", "player": ctrl, "amount": 2},
            ] if ctx.get('opponent_life_lost_this_turn', 0) >= 2 else [
                {"action": "no_action", "reason": "Bloodchief Ascension: opponent didn't lose 2+ life this turn"},
            ],
        ))

        # Fix 19: Nightpack Ambusher — end step wolf token
        self._add_card("nightpack ambusher", EffectTemplate(
            name="Nightpack Ambusher",
            description="At end step, if you cast no spells this turn, create a 2/2 green Wolf token",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Wolf",
                 "power": 2, "toughness": 2, "types": "Creature — Wolf", "count": 1},
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

        # Additional Tier 3 eliminators from audit
        self._add_card("torrential gearhulk", EffectTemplate(
            name="Torrential Gearhulk",
            description="When Torrential Gearhulk enters, you may cast an instant from your graveyard without paying its mana cost",
            action_generator=lambda ctrl, opp, ctx: [
                # May 24 audit fix: source name passed so the action handler
                # doesn't mis-attribute the flashback grant to Snapcaster Mage.
                {"action": "grant_flashback", "player": ctrl, "source": "Torrential Gearhulk"},
            ],
        ))

        self._add_card("goblin chainwhirler", EffectTemplate(
            name="Goblin Chainwhirler",
            description="When Goblin Chainwhirler enters, it deals 1 damage to each opponent and each creature and planeswalker they control",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 1, "target_player": opp},
            ],
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

        self._add_card("rest in peace", EffectTemplate(
            name="Rest in Peace",
            description="When Rest in Peace enters, exile all cards from all graveyards",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "exile_all_graveyards"},
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
                 "source": "Roil Elemental"},
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

        # Detention Sphere LTB — return exiled cards
        self._add_card("detention sphere ltb", EffectTemplate(
            name="Detention Sphere",
            description="When Detention Sphere leaves the battlefield, return the exiled cards to the battlefield",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Detention Sphere LTB: exiled cards return (tracked by exile association)"},
            ],
        ))

        # Bug 16: Nahiri's Lithoforming — X-cost land replacement
        self._add_card("nahiri's lithoforming", EffectTemplate(
            name="Nahiri's Lithoforming",
            description="Sacrifice X lands, then draw X cards. You may play X additional lands this turn.",
            action_generator=lambda ctrl, opp, ctx: self._gen_nahiris_lithoforming(ctrl, opp, ctx),
        ))

        # Bug 27: Wrenn and Seven [+1] — reveal top 4, lands to hand, rest to graveyard
        self._add_card("wrenn and seven", EffectTemplate(
            name="Wrenn and Seven",
            description="Reveal top 4 cards, put all land cards into hand, rest into graveyard",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl,
                 "amount": min(2, ctx.get('controller_library_size', 4))},
            ],
        ))

        # Bug 28: Open the Armory — search for Aura or Equipment
        self._add_card("open the armory", EffectTemplate(
            name="Open the Armory",
            description="Search your library for an Aura or Equipment card, reveal it, put it into your hand, then shuffle",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "count": 1,
                 "card_type": "aura_or_equipment",
                 "reason": "Open the Armory: search for an Aura or Equipment"},
            ],
        ))

        self._add_card("steelshaper's gift", EffectTemplate(
            name="Steelshaper's Gift",
            description="Search your library for an Equipment card, reveal it, put it into your hand, then shuffle",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "count": 1,
                 "card_type": "equipment",
                 "reason": "Steelshaper's Gift: search for an Equipment"},
            ],
        ))

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

        # Sylvan Awakening — until end of turn, all lands you control become
        # 2/2 Elemental creatures with reach, indestructible, and haste while
        # remaining lands. Implementation: animate_land action stamps temporary
        # creature attributes; on_end_step reverts them.
        self._add_card("sylvan awakening", EffectTemplate(
            name="Sylvan Awakening",
            description="All lands you control become 2/2 creatures with reach, indestructible, haste until EOT",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "animate_land", "player": ctrl, "scope": "all",
                 "power": 2, "toughness": 2,
                 "keywords": "reach,indestructible,haste"},
            ],
        ))

        # Living Lands — same template family (lands → 1/1, no haste).
        # (Less common but covered by the same action.)
        self._add_card("living lands", EffectTemplate(
            name="Living Lands",
            description="Forests you control become 1/1 creatures while remaining lands",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "animate_land", "player": ctrl, "scope": "all",
                 "power": 1, "toughness": 1},
            ],
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
            target_power = ctx.get('best_opponent_creature_power', 0)
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

        # Search for Azcanta — look at top card, may put in graveyard
        # May 24 audit fix: was a no_action with "library order not modeled"
        # reason that leaked dev-language to Discord. The reorder_library
        # action (mtg/actions.py:704) is mana-curve-aware and does the
        # right thing for autoplay: keeps a useful card on top when the
        # controller is mana-light. Modeled as a scry-1 (look at top 1
        # and reorder). The "may put in graveyard" branch is rare in
        # practice — most players keep the look at the top of the library.
        self._add_card("search for azcanta", EffectTemplate(
            name="Search for Azcanta",
            description="At beginning of upkeep, look at top card; reorder for best draw",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "reorder_library", "player": ctrl, "amount": 1},
            ],
        ))

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

        # Koma, Cosmos Serpent — create a 3/3 blue Serpent token at each upkeep
        self._add_card("koma, cosmos serpent", EffectTemplate(
            name="Koma, Cosmos Serpent",
            description="At the beginning of each upkeep, create a 3/3 blue Serpent token named Koma's Coil",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Koma's Coil",
                 "power": 3, "toughness": 3, "types": "Creature — Serpent", "count": 1},
            ],
        ))

        # Mirri's Guile — look at top 3, put back in any order
        # May 24 audit fix: was a no_action that leaked dev-language. Now
        # uses reorder_library (mana-curve-aware heuristic at mtg/actions.py:704).
        self._add_card("mirri's guile", EffectTemplate(
            name="Mirri's Guile",
            description="At beginning of upkeep, look at top 3 cards and reorder them for best draws",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "reorder_library", "player": ctrl, "amount": 3},
            ],
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
            bf = ctx.get('battlefield', [])
            artifact_count = 0
            if bf:
                for c in bf:
                    if isinstance(c, dict) and c.get('controller', '') == ctrl:
                        types_str = c.get('types', '') or c.get('type_line', '') or ''
                        if 'artifact' in types_str.lower():
                            artifact_count += 1
            else:
                # Also try the explicit count if surfaced by build_game_context
                artifact_count = ctx.get('controller_artifact_count', 0)
            if artifact_count < 3:
                return [{"action": "no_action",
                         "reason": f"Inventors' Fair: only {artifact_count} artifact(s) — needs 3+"}]
            return [{"action": "gain_life", "player": ctrl, "amount": 1}]

        self._add_card("inventors' fair", EffectTemplate(
            name="Inventors' Fair",
            description="At the beginning of your upkeep, if you control 3+ artifacts, gain 1 life",
            action_generator=_inventors_fair_gen,
        ))

        # --- Light Up the Stage (spectacle sorcery) ---
        # Oracle: "Exile the top two cards of your library. Until the end of your next turn,
        # you may play those cards." Has spectacle {R}.
        # Tier 1.5 approximation: "cast from exile until end of next turn" isn't modeled,
        # so we draw 2 cards as an equivalent-value stand-in. The spectacle-cost alternate
        # casting is handled by the alt-cost machinery, not this template.
        self._add_card("light up the stage", EffectTemplate(
            name="Light Up the Stage",
            description="Exile top 2 of library, play until end of next turn (approx as draw 2)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 2,
                 "reason": "[TEMPLATE-APPROX] Light Up the Stage: approximated exile-to-play as draw 2"},
            ],
        ))

        # Tendershoot Dryad — create a 1/1 green Saproling token at each upkeep
        self._add_card("tendershoot dryad", EffectTemplate(
            name="Tendershoot Dryad",
            description="At the beginning of each upkeep, create a 1/1 green Saproling creature token",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Saproling",
                 "power": 1, "toughness": 1, "types": "Creature — Saproling", "count": 1},
            ],
        ))

        # Jorn, God of Winter — ATTACK trigger per Scryfall:
        # "Whenever Jorn attacks, untap each snow permanent you control."
        #
        # May 24 Tier-2 audit fix: the May 20 audit registered this as an
        # upkeep trigger based on a hallucinated card-text claim. The local
        # Scryfall cache (data/card_data_cache.json key
        # "jorn, god of winter") confirms the real text is "Whenever Jorn
        # attacks". The previous registration sent every Jorn-as-controller
        # turn through `[DRAIN-ATTACK] Resolving Jorn ... via Tier 3` and
        # `[RESOLVE-REFUSED] Combat-shaped resolve rejected` — 21 fires in
        # one snow game, ZERO untaps. The snow deck's signature mechanic
        # was completely broken.
        self._add_attack_card("jorn, god of winter", EffectTemplate(
            name="Jorn, God of Winter (attack)",
            description="Whenever Jorn attacks, untap each snow permanent you control",
            action_generator=lambda ctrl, opp, ctx: [
                # `filter_supertype="snow"` matches the May 24 audit addition
                # to untap_lands; `include_nonlands=True` extends past basic
                # snow lands to Coldsteel Heart, Arcum's Astrolabe, snow
                # creatures, etc. (snow deck has 5+ non-land snow permanents).
                {"action": "untap_lands", "player": ctrl, "count": 99,
                 "include_nonlands": True, "filter_supertype": "snow",
                 "reason": "Jorn attack: untap each snow permanent you control"},
            ],
        ))

        # Phyrexian Arena — draw a card and lose 1 life at upkeep
        # Important: this is a common Commander staple that fires every turn.
        self._add_card("phyrexian arena", EffectTemplate(
            name="Phyrexian Arena",
            description="At the beginning of your upkeep, draw a card and lose 1 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "draw_cards", "player": ctrl, "amount": 1},
                {"action": "lose_life", "player": ctrl, "amount": 1},
            ],
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

        # Grave Titan — "Whenever Grave Titan attacks, create two 2/2 black
        # Zombie creature tokens." (The ETB side already creates two Zombies
        # via TOKEN_ETBS above; this handles the recurring attack trigger.)
        self._add_attack_card("grave titan", EffectTemplate(
            name="Grave Titan (attack)",
            description="Whenever Grave Titan attacks, create two 2/2 black Zombie tokens",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Zombie",
                 "power": 2, "toughness": 2, "types": "Creature — Zombie",
                 "count": 2},
            ],
        ))

        # Isochron Scepter — imprint-on-ETB and tap-to-copy activation.
        # Fully modeling imprint + copy-from-exile is beyond the template tier;
        # register a silent ETB so the upkeep/ETB scanners don't escalate this
        # card to Tier 3 every game. The actual imprint selection and the
        # {2}, Tap activation are handled by the human/AI activation paths.
        self._add_card("isochron scepter", EffectTemplate(
            name="Isochron Scepter",
            description="Imprint is handled at activation time; ETB is a no-op",
            action_generator=lambda ctrl, opp, ctx: [],
        ))

        # Beastmaster Ascension — combat (attack) trigger.
        # Oracle: "Whenever a creature you control attacks, put a quest counter on
        # Beastmaster Ascension. As long as Beastmaster Ascension has seven or more
        # quest counters on it, creatures you control get +5/+5."
        # The +5/+5 anthem is wired by the layers engine; this template just tracks
        # the quest counter additions silently. Returns no message text so we don't
        # spam Discord on every attack — the static anthem effect is already visible
        # in P/T totals when the threshold trips.
        self._add_card("beastmaster ascension", EffectTemplate(
            name="Beastmaster Ascension",
            description="Add a quest counter on attack (silent)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "add_counters", "card": "Beastmaster Ascension",
                 "counter_type": "quest", "amount": 1, "_silent": True},
            ],
        ))

        # --- Fix 3: Spell templates (cards showing 'Complex effect:' with no outcome) ---

        # Reanimate — put target creature from any graveyard onto battlefield; lose life = MV
        def _reanimate_gen(ctrl, opp, ctx):
            # Find best creature in any graveyard
            all_gy = ctx.get('all_graveyards', [])
            target = ctx.get('explicit_target_name')
            if not target:
                # Auto-pick: look through graveyard lists for creatures
                for gy_entry in all_gy:
                    cards = gy_entry.get('cards', []) if isinstance(gy_entry, dict) else []
                    for card in cards:
                        if isinstance(card, dict) and 'creature' in card.get('type_line', '').lower():
                            target = card.get('name', '')
                            break
                    if target:
                        break
            if not target:
                # Fall back to explicit context keys
                target = ctx.get('best_graveyard_creature', '')
            if not target:
                return [{"action": "no_action",
                         "reason": "Reanimate: no creature card found in any graveyard"}]
            # Use MV from context if available, else approximate 5 (reasonable average)
            mv = ctx.get('target_cmc', ctx.get('reanimation_target_cmc', 5))
            target_owner = ctx.get('explicit_target_owner', opp)
            return [
                {"action": "move_card", "card": target,
                 "from_zone": "graveyard", "to_zone": "battlefield",
                 "player": ctrl},
                {"action": "lose_life", "player": ctrl, "amount": max(1, mv)},
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

        # Duskwatch Recruiter (day side) — transform if no spells cast last turn
        self._add_card("duskwatch recruiter", EffectTemplate(
            name="Duskwatch Recruiter",
            description="At the beginning of each upkeep, if no spells were cast last turn, transform",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Duskwatch Recruiter: day/night transform — check if no spells were cast last turn to transform into Krallenhorde Howler (use !fix transform if needed)"},
            ],
        ))
        # Krallenhorde Howler (night side) — transform back if 2+ spells cast last turn
        self._add_card("krallenhorde howler", EffectTemplate(
            name="Krallenhorde Howler",
            description="At the beginning of each upkeep, if a player cast two or more spells last turn, transform",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Krallenhorde Howler: day/night transform — check if 2+ spells were cast last turn to transform back into Duskwatch Recruiter (use !fix transform if needed)"},
            ],
        ))

        # Lambholt Pacifist (day side)
        self._add_card("lambholt pacifist", EffectTemplate(
            name="Lambholt Pacifist",
            description="At the beginning of each upkeep, if no spells were cast last turn, transform",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Lambholt Pacifist: day/night transform — check if no spells were cast last turn to transform into Lambholt Butcher (use !fix transform if needed)"},
            ],
        ))
        # Lambholt Butcher (night side)
        self._add_card("lambholt butcher", EffectTemplate(
            name="Lambholt Butcher",
            description="At the beginning of each upkeep, if a player cast two or more spells last turn, transform",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Lambholt Butcher: day/night transform — check if 2+ spells were cast last turn to transform back into Lambholt Pacifist (use !fix transform if needed)"},
            ],
        ))

        # Huntmaster of the Fells (day side) — ETB / transform-in trigger
        # Apr 30 audit: previous template returned no_action with "use !fix"
        # guidance that leaked into Discord. Now actually fires the trigger:
        # deal 2 damage to opponent, gain 2 life. The day/night transform itself
        # is still tracked elsewhere; this template covers the ETB-like effect.
        self._add_card("huntmaster of the fells", EffectTemplate(
            name="Huntmaster of the Fells",
            description="When Huntmaster of the Fells enters or transforms into Huntmaster, deal 2 damage to target opponent and gain 2 life",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "deal_damage", "amount": 2,
                 "target_player": ctx.get('explicit_target_name') or opp},
                {"action": "gain_life", "player": ctrl, "amount": 2},
            ],
        ))
        # Ravager of the Fells (night side) — when transformed-into, create wolf token.
        # The transform-in trigger is what creates the wolf in canonical rules.
        self._add_card("ravager of the fells", EffectTemplate(
            name="Ravager of the Fells",
            description="When this transforms into Ravager of the Fells, create a 2/2 green Wolf creature token",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Wolf",
                 "power": 2, "toughness": 2, "types": "Creature — Wolf", "count": 1},
            ],
        ))

        # =================================================================
        # APR 14 AUDIT FIXES — Scry/look-at-top cards missing from template library
        # =================================================================

        # Omen of the Sea — Flash enchantment ETB: scry 2, then draw a card
        # (The generic ETB scry pattern catches the scry part, but misses the draw.
        #  Named template takes priority and handles both actions together.)
        self._add_card("omen of the sea", EffectTemplate(
            name="Omen of the Sea",
            description="When Omen of the Sea enters, scry 2, then draw a card",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "scry", "player": ctrl, "amount": 2},
                {"action": "draw_cards", "player": ctrl, "amount": 1},
            ],
        ))

        # Azcanta, the Sunken Ruin — transformed side of Search for Azcanta.
        # Activated ability: {T}, look at top 4 cards of your library; you may put an
        # instant, sorcery, or land card from among them into your hand; put the rest
        # on the bottom in any order. Library order not tracked — produce no_action.
        self._add_card("azcanta, the sunken ruin", EffectTemplate(
            name="Azcanta, the Sunken Ruin",
            description="Activated: look at top 4, may put instant/sorcery/land in hand, rest on bottom",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Azcanta, the Sunken Ruin: look at top 4, put instant/sorcery/land in hand — library order not tracked"},
            ],
        ))

        # Temple of the False God — colorless land, no ETB trigger.
        # Taps for {C}{C} if you control 5+ lands (mana ability, not a triggered ability).
        # Adding a no_action template so any mistaken ETB lookup returns cleanly.
        self._add_card("temple of the false god", EffectTemplate(
            name="Temple of the False God",
            description="No ETB trigger (taps for 2 colorless if 5+ lands — mana ability handled by mana system)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Temple of the False God: no ETB effect (tap-for-mana ability, not a trigger)"},
            ],
        ))

        # =================================================================
        # APR 17 AUDIT FIXES — Gaps flagged by audit
        # =================================================================

        # Night of Souls' Betrayal — static Layer 7c negative anthem.
        # Creatures get -1/-1. Real registration happens via mtg_game.py's
        # anthem_patterns oracle scan (which now includes the bare
        # "creatures get -N/-N" pattern). Template returns no_action so a
        # stray cast-time lookup returns cleanly rather than escalating.
        self._add_card("night of souls' betrayal", EffectTemplate(
            name="Night of Souls' Betrayal",
            description="Creatures get -1/-1 (static, handled by layers engine)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Night of Souls' Betrayal: static -1/-1 to all creatures handled by layers engine"},
            ],
        ))

        # Sylvan Awakening template lives at line ~5133 above (in the
        # "Animate-lands family" group with Living Lands). The animate_land
        # action handler is in mtg/actions.py — it stamps temporary creature
        # attributes that revert at end-of-turn cleanup.

        # Leyline of Anticipation — "You may cast spells as though they had flash."
        # Requires a can_cast_as_flash flag on the controller wired through the
        # cast-timing check. No such flag exists, and adding one touches the cast
        # validator in mtg_game.py. Stub returns no_action so autoplay doesn't
        # escalate to Tier 3 for a static effect it can't yet honor.
        self._add_card("leyline of anticipation", EffectTemplate(
            name="Leyline of Anticipation",
            description="You may cast spells as though they had flash (static; not yet wired into cast-timing check)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Leyline of Anticipation: cast-as-flash flag not yet wired; spells still cast at sorcery speed"},
            ],
        ))

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

        # Maja, Bretagard Protector — landfall: create a 1/1 white Human
        # Warrior token (printed P/T; the layers engine boosts it to 2/2
        # via Maja's "other creatures you control get +1/+1" static).
        # May 7 audit (Bug 2): previous template hardcoded 2/2 to bake in
        # the anthem, which double-counts whenever the layers engine also
        # applies the anthem (making the token 3/3). Drop to printed 1/1
        # and trust the layers system.
        self._add_card("maja, bretagard protector", EffectTemplate(
            name="Maja, Bretagard Protector",
            description="Landfall: create a 1/1 white Human Warrior token",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Human Warrior",
                 "power": 1, "toughness": 1, "types": "Creature — Human Warrior",
                 "count": 1},
            ],
        ))

        # Glacial Chasm — ETB sacrifices a land. Cumulative upkeep-pay-2-life
        # is enforced separately by the pay-or-sacrifice logic. The ETB-side
        # sacrifice was the !judge-prompt source.
        self._add_card("glacial chasm", EffectTemplate(
            name="Glacial Chasm",
            description="When Glacial Chasm enters, sacrifice a land",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "sacrifice_land", "player": ctrl},
            ],
        ))

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

        # Gravebreaker Lamia — ETB tutor a creature card into graveyard.
        # The cost-reduction static ability (creature spells from graveyard
        # cost {1} less) is handled elsewhere; this template fires the ETB.
        self._add_card("gravebreaker lamia", EffectTemplate(
            name="Gravebreaker Lamia",
            description="When Gravebreaker Lamia enters, search library for a creature card, put it into your graveyard, then shuffle",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "search_library", "player": ctrl, "filter_type": "creature",
                 "to_zone": "graveyard", "count": 1, "shuffle": True},
            ],
        ))

        # Spell Queller LTB — when Spell Queller dies, the exiled spell can
        # be cast by its owner without paying mana. We approximate by
        # returning the exiled card to its owner's hand.
        # May 18 audit: the previous no_action was aspirational — it said
        # "exiled spell returns to its owner's hand" but no code actually did
        # that, so Negate exiled by Queller A stayed in exile permanently and
        # appeared to "end up in graveyard" when audit reconciliation ran.
        # Now uses release_queller_exile action which looks up the link in
        # `game._queller_exiles` and actually moves the card back to hand.
        self._add_card("spell queller_ltb", EffectTemplate(
            name="Spell Queller (LTB)",
            description="When Spell Queller leaves, the exiled card returns to its owner's hand (approximation of free-cast LTB)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "release_queller_exile", "source": "Spell Queller"},
            ],
        ))

        # =====================================================================
        # May 7, 2026 audit: silent-resolve spell templates (round 2)
        # Cards that said "resolved" but produced no game state change in the
        # May 7 batch logs. Most candidate cards from that audit already had
        # templates by then (Reanimate, Living Death, Beast Within, Ephemerate,
        # Momentary Blink, Cathartic Reunion, etc.) — these four were the
        # uncovered remainder.
        # =====================================================================

        # --- Lingering Souls: create two 1/1 white Spirit tokens with flying ---
        # Mirrors Spectral Procession (3 tokens); flashback is a casting
        # mechanic, not part of the spell effect, so we don't model it here.
        self._add_card("lingering souls", EffectTemplate(
            name="Lingering Souls",
            description="Create two 1/1 white Spirit creature tokens with flying",
            action_generator=lambda ctrl, opp, ctx: [
                make_token_action(ctrl, "spirit_1_1_fly", 2),
            ],
        ))

        # --- Hellkite Courser: ETB cheat a commander out of the command zone ---
        # Real card: "When Hellkite Courser enters, you may put a commander you
        # own from the command zone onto the battlefield. Return that commander
        # to the command zone at the beginning of the next end step."
        # The engine's move_card doesn't support command_zone↔battlefield
        # transitions, so we emit a descriptive no_action rather than silently
        # producing nothing. (Tier 3 wouldn't reliably do this either.)
        self._add_card("hellkite courser", EffectTemplate(
            name="Hellkite Courser",
            description="ETB: put a commander from the command zone onto the battlefield (returns at end step)",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Hellkite Courser: command zone → battlefield not modeled; commander stays in command zone"},
            ],
        ))

        # --- Blood on the Snow: X-cost board wipe with snow-mana reanimate kicker ---
        # Real card: "Destroy all creatures and planeswalkers with mana value
        # X or less. If {S} was spent to cast this spell, return a creature
        # or planeswalker card with mana value X or less from your graveyard
        # to the battlefield."
        # There's no destroy_by_mana_value action and the engine doesn't track
        # which colors of mana were spent on a spell, so we emit a no_action
        # with explanation rather than silently producing nothing.
        self._add_card("blood on the snow", EffectTemplate(
            name="Blood on the Snow",
            description="Destroy creatures/planeswalkers with MV ≤ X; snow kicker: reanimate one with MV ≤ X",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "no_action",
                 "reason": "Blood on the Snow: destroy-by-mana-value action not supported by engine (X-cost wipe + snow reanimate kicker)"},
            ],
        ))

        # --- Living Weapon pattern: Batterskull, Mortarpod, Skinwing family ---
        # Oracle: "Living weapon (When this Equipment enters, create a 0/0
        # black Germ creature token, then attach this to it.)"
        # We model the Germ token creation; the attach step is approximated as
        # a no_action hint (the equipment lands separately, and the +X/+Y on
        # attached creature will make the 0/0 Germ viable once the player runs
        # !equip). Note: Umezawa's Jitte itself doesn't have living weapon —
        # it's older — so we only register modern living-weapon equipment by
        # name. The regex pattern below covers the whole family.
        self._add_card("batterskull", EffectTemplate(
            name="Batterskull",
            description="Living weapon: create a 0/0 black Germ creature token, attach Batterskull to it",
            action_generator=lambda ctrl, opp, ctx: [
                {"action": "create_token", "player": ctrl, "name": "Germ",
                 "power": 0, "toughness": 0,
                 "types": "Creature — Germ", "count": 1},
                {"action": "no_action",
                 "reason": "Living weapon: Batterskull should attach to the Germ (use !equip)"},
            ],
        ))

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
            actions.append({"action": "destroy", "card": land_name})
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
        """Sidisi, Undead Vizier: Exploit — sacrifice a creature, search library for any card."""
        worst = ctx.get('controller_worst_creature')
        if worst:
            return [
                {"action": "destroy", "card": worst},
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

    def _add_card(self, name_lower: str, template: EffectTemplate):
        self._card_templates[name_lower] = template

    def _add_attack_card(self, name_lower: str, template: EffectTemplate):
        """Register an attack-trigger-specific template (checked before ETB templates)."""
        self._attack_templates[name_lower] = template

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

        In autoplay, we exile the card and mark it as playable. The AI will attempt
        to cast it; if it can't (e.g. wrong colors, too expensive), the 2 damage
        is dealt at end of turn. For simplicity, we exile + deal damage immediately
        since autoplay can't track "if you don't cast it" reliably.
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
        """Daretti -2: Sacrifice an artifact, return an artifact from graveyard to battlefield."""
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
        worst = ctx.get('worst_opponent_creature')
        if worst:
            return [{"action": "destroy", "card": worst}]
        # Fallback: try best_opponent_creature (destroy strongest if worst not available)
        target = ctx.get('best_opponent_creature')
        if target:
            return [{"action": "destroy", "card": target}]
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
        target = ctx.get('best_opponent_creature')
        target_power = ctx.get('best_opponent_creature_power', 0)
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
        target = ctx.get('best_opponent_creature')
        if target:
            return [{"action": "tap", "card": target}]
        return [{"action": "no_action", "reason": "No valid permanent to tap"}]
    
    def _return_best_from_graveyard(self, ctrl, opp, ctx) -> List[Dict]:
        target = ctx.get('best_own_graveyard_card')
        if target:
            return [{"action": "move_card", "card": target, "from_zone": "graveyard", "to_zone": "hand", "player": ctrl}]
        return [{"action": "no_action", "reason": f"{ctrl} returns a card from graveyard (use !fix)"}]
    
    def _reanimate_small(self, ctrl, opp, ctx, max_mv=3) -> List[Dict]:
        target = ctx.get('best_own_graveyard_permanent')
        if target:
            return [{"action": "move_card", "card": target, "from_zone": "graveyard", "to_zone": "battlefield", "player": ctrl}]
        return [{"action": "no_action", "reason": f"{ctrl} returns a permanent (MV≤{max_mv}) from graveyard (use !fix)"}]
    
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
        # Exile the chosen card face down
        actions.append({"action": "move_card", "card": best_card.name, "from_zone": "library", "to_zone": "exile", "player": opp})
        # Put the rest on the bottom of the library
        for c in looked_at:
            if c is not best_card:
                # Move from library top to library bottom (remove then append)
                actions.append({"action": "move_card", "card": c.name, "from_zone": "library", "to_zone": "library", "player": opp})

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
            n = self._word_to_num(match.group(1))
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
        
        # Try to find a target on opponent's board from context
        if "creature" in target_type:
            target = ctx.get('best_opponent_creature')
        elif "artifact" in target_type or "enchantment" in target_type:
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
        if target:
            actions = [{"action": "move_card", "card": target,
                        "from_zone": "battlefield", "to_zone": "hand", "player": opp}]
            # Draw a card for kicked bounce spells (Into the Roil, Blink of an Eye)
            # In autoplay, always assume kicked if we can afford it
            actions.append({"action": "draw_cards", "player": ctrl, "amount": 1})
            return actions
        return [{"action": "no_action", "reason": "No nonland permanent to bounce"}]

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
        """Fatal Push: destroy creature with MV ≤ 2 (≤ 4 with revolt)."""
        target = ctx.get('explicit_target_name') or ctx.get('best_opponent_creature')
        if not target:
            return [{"action": "no_action", "reason": "No valid creature target for Fatal Push"}]
        # Check mana value — get from game context
        target_mv = ctx.get('explicit_target_mv', 0)
        has_revolt = ctx.get('revolt', False)
        mv_limit = 4 if has_revolt else 2
        if target_mv > mv_limit:
            revolt_note = " (revolt active)" if has_revolt else ""
            return [{"action": "no_action",
                     "reason": f"Fatal Push can't destroy {target} (MV {target_mv} > {mv_limit}{revolt_note})"}]
        return [{"action": "destroy", "card": target}]

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

        We destroy the opponent's weakest creature (lowest power) and leave a
        hint for the controller to sacrifice one of theirs manually if needed.
        In autoplay the controller's sacrifice is handled by the AI decision loop.
        """
        actions = []
        # Opponent: destroy their weakest creature
        opp_creatures = ctx.get('_opponent_creatures', [])
        if opp_creatures:
            # Pick weakest by power (lowest is least valuable to lose)
            weakest = None
            weakest_power = 999
            for info in opp_creatures:
                p = info.get('power', 0) if isinstance(info, dict) else 0
                if p < weakest_power:
                    weakest_power = p
                    weakest = info.get('name') if isinstance(info, dict) else None
            if weakest:
                actions.append({"action": "destroy", "card": weakest})
            else:
                # Fall back to best_opponent_creature if _opponent_creatures not populated
                target = ctx.get('best_opponent_creature')
                if target:
                    actions.append({"action": "destroy", "card": target})
        else:
            # Context not populated — use best_opponent_creature
            target = ctx.get('best_opponent_creature')
            if target:
                actions.append({"action": "destroy", "card": target})
            else:
                actions.append({"action": "no_action", "reason": f"{opp} has no creatures to sacrifice"})
        # Controller: sacrifice their weakest creature too (symmetric effect)
        ctrl_creatures = ctx.get('_controller_creatures', [])
        source_name = (ctx.get('_source_card_name') or '').lower()
        if ctrl_creatures:
            # Pick weakest by power that isn't the source creature itself
            weakest = None
            weakest_power = 999
            for info in ctrl_creatures:
                name = info.get('name', '') if isinstance(info, dict) else ''
                p = info.get('power', 0) if isinstance(info, dict) else 0
                if name.lower() != source_name and p < weakest_power:
                    weakest_power = p
                    weakest = name
            if weakest:
                actions.append({"action": "destroy", "card": weakest})
            elif not any(a.get('action') == 'destroy' for a in actions):
                # If no other creature available, sacrifice the source itself
                source = ctx.get('_source_card_name', '')
                if source:
                    actions.append({"action": "destroy", "card": source})
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

    def _gen_kroxa_etb(self, ctrl, opp, ctx) -> List[Dict]:
        """Kroxa, Titan of Death's Hunger triggers.

        Two abilities, both routed through this generator (resolve_upkeep_trigger
        delegates to resolve_etb by name). We distinguish ETB vs upkeep via the
        oracle text snippet in ctx['_oracle']:

        ETB: "When Kroxa, Titan of Death's Hunger enters, each opponent discards
        a card. If a player discarded a nonland card this way, Kroxa deals 3
        damage to that player." Then upkeep clause sacrifices unless escaped.

        Upkeep: "At the beginning of your upkeep, if Kroxa isn't escaped,
        sacrifice it." No discard, no damage — just the sac check.
        """
        oracle = (ctx.get('_oracle') or '').lower()
        is_upkeep = 'beginning of your upkeep' in oracle and 'sacrifice' in oracle
        # Use destroy for self-sacrifice — the action interpreter has no plain
        # "sacrifice" action type, so the original {"action": "sacrifice"} got
        # silently dropped, and Kroxa never sacrificed himself when not escaped.
        # destroy on a commander correctly routes to command zone via CR 903.9.
        sac_action = {"action": "destroy", "card": "Kroxa, Titan of Death's Hunger",
                      "target_controller": ctrl}
        if is_upkeep:
            if ctx.get('was_escaped', False):
                return [{"action": "no_action", "reason": "Kroxa was escaped — survives upkeep"}]
            return [sac_action]
        # ETB path
        actions = [
            {"action": "discard", "player": opp, "card": "random"},
            {"action": "lose_life", "player": opp, "amount": 3},
        ]
        if not ctx.get('was_escaped', False):
            actions.append(sac_action)
        return actions

    def _gen_rashmi_cast_trigger(self, ctrl, opp, ctx) -> List[Dict]:
        """Rashmi, Eternities Crafter cast trigger: reveal top card of library.
        If it's a nonland card with lesser MV than the triggering spell, cast it free
        (approximated as move to battlefield for permanents, otherwise to hand).
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
        permanent_types = ('creature', 'artifact', 'enchantment', 'planeswalker')
        is_nonland_permanent = (not is_land) and any(t in type_line for t in permanent_types)

        if (not is_land) and spell_mv and top_cmc < spell_mv:
            # Cast free: for permanents, move to battlefield; instants/sorceries
            # can't meaningfully "cast free" without a stack model — route to hand.
            if is_nonland_permanent:
                return [{"action": "move_card", "card": top_name,
                         "from_zone": "library", "to_zone": "battlefield",
                         "player": ctrl,
                         "reason": f"Rashmi: cast {top_name} free (MV {top_cmc} < {spell_mv})"}]
            return [{"action": "move_card", "card": top_name,
                     "from_zone": "library", "to_zone": "hand",
                     "player": ctrl,
                     "reason": f"Rashmi: {top_name} revealed (free-cast of non-permanent routed to hand)"}]
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
        target_power = ctx.get('best_opponent_creature_power', 0)
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
        target_power = ctx.get('best_opponent_creature_power', 0)
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

    def _gen_life_from_the_loam(self, ctrl, opp, ctx) -> List[Dict]:
        """Life from the Loam: return up to 3 land cards from your graveyard to hand.
        Dredge 3 in the oracle text would otherwise be mis-matched by the generic mill pattern.
        """
        player = ctx.get('_player')
        if player is None:
            # May 7 audit fix #7: this is an internal context-missing diagnostic,
            # not something a Discord user can act on. Log to console instead
            # and return an empty action list so nothing leaks to Discord.
            print("[TEMPLATE] Life from the Loam: no _player context — skipping")
            return []
        # Pick up to 3 land cards from graveyard (non-token)
        def _is_land(c):
            if hasattr(c, 'is_land') and callable(c.is_land):
                return c.is_land()
            return 'land' in (c.type_line or '').lower()

        lands = [
            c for c in player.graveyard
            if not getattr(c, 'is_token', False) and _is_land(c)
        ]
        if not lands:
            return [{"action": "no_action", "reason": "Life from the Loam: no land cards in graveyard"}]
        actions = []
        for land in lands[:3]:
            actions.append({"action": "move_card", "card": land.name,
                            "from_zone": "graveyard", "to_zone": "hand", "player": ctrl})
        return actions

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
        """Oath of Teferi: exile another permanent you control, return immediately."""
        source = (ctx.get('_source_card_name') or '').lower()
        target = ctx.get('best_own_etb_creature', '') or ctx.get('best_own_noncreature', '')
        if not target or target.lower() == source:
            return [{"action": "no_action", "reason": "No other permanent to flicker"}]
        return [{"action": "flicker", "player": ctrl, "target": target}]

    def _gen_tooth_and_nail(self, ctrl, opp, ctx) -> List[Dict]:
        """Tooth and Nail (entwined): search 2 creatures + put 2 from hand onto battlefield."""
        return [
            {"action": "search_library_creature", "player": ctrl, "count": 2,
             "destination": "hand", "reason": "Tooth and Nail: search for 2 creatures"},
            {"action": "put_creatures_from_hand", "player": ctrl, "count": 2,
             "reason": "Tooth and Nail: put 2 creatures from hand onto battlefield"}
        ]

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

    def _gen_ghostly_flicker(self, ctrl, opp, ctx) -> List[Dict]:
        """Ghostly Flicker: exile TWO target artifacts, creatures, or lands you control, return them.
        Only artifacts, creatures, or lands are legal targets — NOT enchantments."""
        target1 = ctx.get('best_own_etb_creature', '')
        # Second target must be an artifact, creature, or land — NOT enchantment
        target2 = ctx.get('best_own_flickerable', '') or ctx.get('best_own_noncreature', '')
        actions = []
        if target1:
            actions.append({"action": "flicker", "player": ctrl, "target": target1})
        if target2 and target2 != target1:
            actions.append({"action": "flicker", "player": ctrl, "target": target2})
        if not actions:
            return [{"action": "no_action", "reason": "No valid targets for Ghostly Flicker"}]
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
        # Try non-creature permanent as fallback
        if not target:
            target = ctx.get('best_own_noncreature', '')
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

    def _gen_reanimate(self, ctrl, opp, ctx) -> List[Dict]:
        """Reanimate: put target creature card from a graveyard onto the battlefield,
        lose life equal to its mana value."""
        # Find best creature in any graveyard
        best = ctx.get('best_graveyard_creature', '')
        best_cmc = ctx.get('best_graveyard_creature_cmc', 0)
        if not best:
            return [{"action": "no_action", "reason": "No creature cards in any graveyard"}]
        return [
            {"action": "reanimate", "player": ctrl, "card": best,
             "reason": f"Reanimate: return {best} from graveyard to battlefield"},
            {"action": "lose_life", "player": ctrl, "amount": best_cmc},
        ]

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

    def _gen_ancient_bronze_dragon(self, ctrl, opp, ctx) -> List[Dict]:
        """Ancient Bronze Dragon: roll d20, put that many +1/+1 counters on each creature."""
        import random
        roll = random.randint(1, 20)
        return [
            {"action": "pump_all_creatures", "player": ctrl,
             "power": roll, "toughness": roll,
             "keywords": []},
        ]

    def _gen_victimize(self, ctrl, opp, ctx) -> List[Dict]:
        """Victimize: sacrifice a creature you control, return two creatures from your graveyard."""
        actions = []
        # Sacrifice the controller's worst creature (lowest CMC non-commander)
        # Use "destroy" action since there's no "sacrifice" action type in the engine
        worst = ctx.get('controller_worst_creature')
        if worst:
            actions.append({"action": "destroy", "card": worst})
        # Reanimate two best creatures from controller's graveyard
        gy_creatures = ctx.get('controller_graveyard_creatures', [])
        for creature_name in gy_creatures[:2]:
            actions.append({"action": "move_card", "card": creature_name,
                            "from_zone": "graveyard", "to_zone": "battlefield", "player": ctrl})
        if not gy_creatures:
            return [{"action": "no_action", "reason": "No creature cards in controller's graveyard"}]
        return actions

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
        """Aminatou +1: 'The top card of each player's library becomes that
        player's library bottom card.' (Fateshift, the namesake mechanic.)

        May 20 audit corrected this generator. The previous implementation
        was a Jace-style 'draw 1, put one back on top' — a fundamentally
        different (and much stronger) effect that gave Rick ~16 free cards
        per Aminatou game in the May 19 batch.
        """
        return [{"action": "fateshift"}]

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


# =============================================================================
# Context Builder - prepares game context for template resolution
# =============================================================================

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
    controller_creatures = [c for c in player.battlefield if c.is_creature()]
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

    # Player object reference (for templates that need to check optional-cost
    # affordability like Extort's "you may pay {W/B}"). Avoid mutating through
    # this in templates — use it for read-only mana / state checks.
    ctx['_controller_player'] = player
    ctx['_game'] = game

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
    def _creature_info(c):
        return {
            'name': c.name,
            'power': c.get_effective_power(game) if hasattr(c, 'get_effective_power') else 0,
            'colors': list(getattr(c, 'color_identity', []) or []),
            'type_line': (c.type_line or "").lower() if hasattr(c, 'type_line') else "",
        }
    ctx['_opponent_creatures'] = [
        _creature_info(c) for c in opponent.battlefield if c.is_creature()
    ]
    ctx['_controller_creatures'] = [
        _creature_info(c) for c in player.battlefield if c.is_creature()
    ]
    
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
        ctx['dying_name'] = dying_creature.name if hasattr(dying_creature, 'name') else str(dying_creature)
        try:
            ctx['dying_power'] = int(dying_creature.power) if hasattr(dying_creature, 'power') and dying_creature.power else 0
            ctx['dying_toughness'] = int(dying_creature.toughness) if hasattr(dying_creature, 'toughness') and dying_creature.toughness else 0
        except (ValueError, TypeError):
            ctx['dying_power'] = 0
            ctx['dying_toughness'] = 0

    # Attacking creature info (for attack triggers)
    if attacking_creature:
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
        if hasattr(explicit_target, 'name'):
            # It's a Card object — find which player owns it
            ctx['explicit_target_name'] = explicit_target.name
            for p in game.players:
                if explicit_target in p.battlefield:
                    ctx['explicit_target_owner'] = p.name
                    break
        elif isinstance(explicit_target, str):
            ctx['explicit_target_name'] = explicit_target

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
