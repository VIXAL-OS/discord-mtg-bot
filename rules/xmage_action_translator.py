"""
XMage Action Translator
========================

Converts XMage trigger ability text strings into JSON action dicts
compatible with RulesEngine._execute_action_on_state().

This bridges the gap between:
- XMage's get_triggers() output: {"source": "Card", "ability": "text...", ...}
- The engine's action format: {"action": "deal_damage", "amount": N, ...}

Three-level translation:
1. Try the existing EffectTemplateLibrary (reuse tier 1.5 patterns)
2. Try dedicated XMage-specific regex patterns
3. Return None to signal "needs resolve_effect()" (tier 3 fallback)

Console tags: [XMAGE-TRANSLATE]
"""

import re
from typing import Optional, List, Dict, Tuple


class XMageActionTranslator:
    """Translates XMage ability text to engine JSON action format."""

    def __init__(self, effect_library=None):
        """
        Args:
            effect_library: Optional EffectTemplateLibrary instance for
                           reusing tier 1.5 pattern matching (Strategy A).
        """
        self.effect_library = effect_library
        self._patterns = self._build_patterns()

    def translate_trigger(
        self,
        source_card: str,
        ability_text: str,
        controller: str,
        opponent: str,
        game_context: Optional[Dict] = None,
        entering_creature_name: str = "",
        entering_creature_power: int = 0,
    ) -> Tuple[Optional[List[Dict]], str]:
        """
        Attempt to translate a trigger ability text into JSON actions.

        Args:
            source_card: Name of the card whose trigger fired
            ability_text: The ability text from XMage (ability.getRule())
            controller: Name of the trigger's controller
            opponent: Name of the opponent
            game_context: Optional context dict from build_game_context()
            entering_creature_name: Name of creature that caused the trigger
            entering_creature_power: Power of entering creature (for "damage equal to power")

        Returns:
            (actions, explanation) if translated successfully
            (None, "") if translation failed — caller should fall through to tier 3
        """
        ctx = game_context or {}

        # Strategy A: Try the existing template library
        # XMage's ability.getRule() returns text similar to oracle text,
        # so the template library's regex patterns often match directly.
        if self.effect_library:
            try:
                if entering_creature_name:
                    ctx['entering_name'] = entering_creature_name
                    ctx['entering_power'] = entering_creature_power

                actions, explanation = self.effect_library.resolve_etb(
                    card_name=source_card,
                    oracle_text=ability_text,
                    controller=controller,
                    opponent=opponent,
                    game_context=ctx,
                )
                if actions is not None:
                    return actions, f"[template] {explanation}"
            except Exception as e:
                print(f"[XMAGE-TRANSLATE] Template library error for {source_card}: {e}")

        # Strategy B: Dedicated regex patterns for XMage ability text
        ability_lower = ability_text.lower()

        for pattern, handler in self._patterns:
            match = re.search(pattern, ability_lower)
            if match:
                try:
                    actions, explanation = handler(
                        match, source_card, controller, opponent,
                        entering_creature_name, entering_creature_power, ctx
                    )
                    if actions is not None:
                        return actions, f"[regex] {explanation}"
                except Exception as e:
                    print(f"[XMAGE-TRANSLATE] Pattern handler error for {source_card}: {e}")

        # Strategy C: Signal that tier 3 (resolve_effect) is needed
        return None, ""

    def _build_patterns(self):
        """
        Build regex patterns for common XMage ability text formats.

        These catch abilities that the template library might miss because
        XMage's getRule() formatting can differ slightly from Scryfall oracle text.
        """
        return [
            # "deals damage equal to that creature's power to any target"
            # Matches: Terror of the Peaks, Warstorm Surge
            (r"deals damage equal to (?:that|the) creature.s power",
             self._handle_damage_equal_to_power),

            # "deals N damage to each opponent"
            # Matches: Impact Tremors, Purphoros
            (r"deals (\d+) damage to each opponent",
             self._handle_fixed_damage_opponents),

            # "deals N damage to any target" / "deals N damage to target"
            (r"deals? (\d+) damage to (?:any )?target",
             self._handle_fixed_damage_any),

            # "you gain N life"
            # Matches: Soul Warden, Essence Warden, Impassioned Orator
            (r"you gain (\d+) life",
             self._handle_gain_life),

            # "each opponent loses N life"
            # Matches: Blood Artist (partial), Zulaport Cutthroat (partial)
            (r"each opponent loses (\d+) life",
             self._handle_lose_life_opponents),

            # "draw a card" / "draw N cards"
            # Matches: Soul of the Harvest, Beast Whisperer, Garruk's Packleader
            (r"draw (\w+) cards?",
             self._handle_draw),

            # "create N X/Y [type] creature tokens"
            # Matches: various token makers
            (r"create (\w+) (\d+)/(\d+) (\w[\w\s]*?) (?:creature |artifact creature )?tokens?",
             self._handle_create_tokens),

            # "put a +1/+1 counter on" / "put N +1/+1 counters on"
            (r"put (?:a|(\w+)) \+1/\+1 counters? on",
             self._handle_plus_counters),

            # "target player discards a card" / "target opponent discards N cards"
            (r"(?:target )?opponent discards? (\w+) cards?",
             self._handle_discard),

            # "you may draw a card"
            # Matches: optional card draw triggers
            (r"you may draw a card",
             self._handle_may_draw),

            # "target creature gets +N/+N until end of turn"
            (r"gets? \+(\d+)/\+(\d+) until end of turn",
             self._handle_pump),

            # "each opponent loses N life and you gain that much life"
            # Matches: Gray Merchant style drain
            (r"each opponent loses (\d+) life and you gain",
             self._handle_drain),

            # "mill N cards" / "mills N cards" / "puts the top N cards into their graveyard"
            (r"(?:mills?|puts? the top) (\w+) cards?",
             self._handle_mill),
        ]

    # =========================================================================
    # Pattern handlers
    # Each returns (actions, explanation) or (None, "") if it can't handle
    # =========================================================================

    @staticmethod
    def _word_to_num(word: str) -> int:
        """Convert English number words to ints. Handles digits too."""
        mapping = {
            'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3,
            'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
            'nine': 9, 'ten': 10,
        }
        try:
            return int(word)
        except (ValueError, TypeError):
            return mapping.get(word.lower(), 1)

    def _handle_damage_equal_to_power(self, match, source, ctrl, opp,
                                       entering_name, entering_power, ctx):
        """Terror of the Peaks / Warstorm Surge: damage = entering creature's power."""
        if entering_power > 0:
            return (
                [{"action": "deal_damage", "amount": entering_power, "target_player": opp}],
                f"{source} deals {entering_power} damage to {opp} (entering {entering_name})"
            )
        return (
            [{"action": "no_action", "reason": f"{entering_name} has 0 power, no damage dealt"}],
            f"{source}: entering creature has 0 power"
        )

    def _handle_fixed_damage_opponents(self, match, source, ctrl, opp,
                                        entering_name, entering_power, ctx):
        """Impact Tremors / Purphoros: fixed damage to each opponent."""
        dmg = int(match.group(1))
        return (
            [{"action": "deal_damage", "amount": dmg, "target_player": opp}],
            f"{source} deals {dmg} damage to {opp}"
        )

    def _handle_fixed_damage_any(self, match, source, ctrl, opp,
                                  entering_name, entering_power, ctx):
        """Fixed damage to any target — default to opponent."""
        dmg = int(match.group(1))
        return (
            [{"action": "deal_damage", "amount": dmg, "target_player": opp}],
            f"{source} deals {dmg} damage to {opp}"
        )

    def _handle_gain_life(self, match, source, ctrl, opp,
                           entering_name, entering_power, ctx):
        """Soul Warden / Essence Warden: gain life on creature entering."""
        amount = int(match.group(1))
        return (
            [{"action": "gain_life", "player": ctrl, "amount": amount}],
            f"{ctrl} gains {amount} life from {source}"
        )

    def _handle_lose_life_opponents(self, match, source, ctrl, opp,
                                     entering_name, entering_power, ctx):
        """Each opponent loses N life."""
        amount = int(match.group(1))
        return (
            [{"action": "lose_life", "player": opp, "amount": amount}],
            f"{opp} loses {amount} life from {source}"
        )

    def _handle_draw(self, match, source, ctrl, opp,
                      entering_name, entering_power, ctx):
        """Draw N cards."""
        n = self._word_to_num(match.group(1))
        return (
            [{"action": "draw_cards", "player": ctrl, "amount": n}],
            f"{ctrl} draws {n} card(s) from {source}"
        )

    def _handle_create_tokens(self, match, source, ctrl, opp,
                               entering_name, entering_power, ctx):
        """Create N X/Y creature tokens."""
        count = self._word_to_num(match.group(1))
        power = int(match.group(2))
        tough = int(match.group(3))
        token_name = match.group(4).strip().title()
        return (
            [{"action": "create_token", "player": ctrl, "name": token_name,
              "power": power, "toughness": tough,
              "types": f"Creature - {token_name}", "count": count}],
            f"Create {count} {power}/{tough} {token_name} token(s) from {source}"
        )

    def _handle_plus_counters(self, match, source, ctrl, opp,
                               entering_name, entering_power, ctx):
        """Put +1/+1 counters on source or entering creature."""
        n = self._word_to_num(match.group(1)) if match.group(1) else 1
        # Default: put counters on the entering creature if it's a self-buff,
        # otherwise on the source card
        target = entering_name if entering_name else source
        return (
            [{"action": "add_counters", "card": target,
              "counter_type": "+1/+1", "amount": n}],
            f"Put {n} +1/+1 counter(s) on {target} from {source}"
        )

    def _handle_discard(self, match, source, ctrl, opp,
                         entering_name, entering_power, ctx):
        """Target opponent discards N cards."""
        n = self._word_to_num(match.group(1))
        actions = [{"action": "discard", "player": opp, "card": "random"}
                   for _ in range(n)]
        return (
            actions,
            f"{opp} discards {n} card(s) from {source}"
        )

    def _handle_may_draw(self, match, source, ctrl, opp,
                          entering_name, entering_power, ctx):
        """'You may draw a card' — auto-draw (may is always yes in casual)."""
        return (
            [{"action": "draw_cards", "player": ctrl, "amount": 1}],
            f"{ctrl} draws a card from {source} (may draw)"
        )

    def _handle_pump(self, match, source, ctrl, opp,
                      entering_name, entering_power, ctx):
        """Target creature gets +N/+N until end of turn."""
        power_buff = int(match.group(1))
        tough_buff = int(match.group(2))
        # Default to buffing the entering creature
        target = entering_name if entering_name else source
        return (
            [{"action": "pump", "card": target,
              "power": power_buff, "toughness": tough_buff}],
            f"{target} gets +{power_buff}/+{tough_buff} until end of turn from {source}"
        )

    def _handle_drain(self, match, source, ctrl, opp,
                       entering_name, entering_power, ctx):
        """Each opponent loses N life and you gain that much."""
        amount = int(match.group(1))
        return (
            [
                {"action": "lose_life", "player": opp, "amount": amount},
                {"action": "gain_life", "player": ctrl, "amount": amount},
            ],
            f"{source} drains {amount} from {opp} to {ctrl}"
        )

    def _handle_mill(self, match, source, ctrl, opp,
                      entering_name, entering_power, ctx):
        """Mill N cards — opponent mills by default for triggers."""
        n = self._word_to_num(match.group(1))
        return (
            [{"action": "mill", "player": opp, "amount": n}],
            f"{opp} mills {n} card(s) from {source}"
        )
