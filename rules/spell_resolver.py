"""
MTG Spell Resolution System
============================

Integrates effect parsing, targeting, and execution with the main game.

This handles the full lifecycle of a spell:
1. Announce spell (put on stack)
2. Choose targets
3. Pay costs
4. Pass priority (simplified for casual play)
5. Resolve: execute effects

For casual Discord play, we simplify:
- Stack resolves immediately unless opponent responds
- Targeting is auto-selected when unambiguous
- Claude targets are auto-resolved
- Human targets prompt for selection
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple, Callable
from enum import Enum, auto

from .effects import EffectExecutor, Effect, EffectType, ExecutionContext
from .targeting import TargetValidator, TargetRestriction, TargetType, TargetTextParser, Targetable, TargetingSource


class TargetMode(Enum):
    """How targeting should be handled."""
    AUTO = auto()       # Auto-select best target
    PROMPT = auto()     # Ask player to choose
    SPECIFIED = auto()  # Target already specified


@dataclass
class PendingSpell:
    """A spell waiting for targets or resolution."""
    card: Any  # Card object
    controller: Any  # Player object  
    controller_index: int
    effects: List[Effect]
    targets_needed: List[TargetRestriction]
    targets_selected: List[Any] = field(default_factory=list)
    waiting_for_targets: bool = False
    target_prompt: str = ""


@dataclass 
class SpellResult:
    """Result of resolving a spell."""
    success: bool
    messages: List[str] = field(default_factory=list)
    damage_dealt: Dict[str, int] = field(default_factory=dict)  # target -> amount
    creatures_destroyed: List[str] = field(default_factory=list)
    cards_drawn: Dict[str, int] = field(default_factory=dict)  # player -> amount
    life_changes: Dict[str, int] = field(default_factory=dict)  # player -> change
    counters_added: Dict[str, Dict[str, int]] = field(default_factory=dict)  # permanent -> {type: amount}
    triggered_abilities: List[str] = field(default_factory=list)


def _targets_for(effect, ctx):
    """This clause's own targets, else the spell-wide list (Aug 10, A1).

    MODULE-level, not a staticmethod: the Tier-2 handlers are called unbound
    (with self=None) by existing tests, so reaching this through `self.` would
    raise AttributeError on a path that has nothing to do with targeting.
    """
    return getattr(effect, 'selected_targets', None) or ctx.targets


def target_restrictions_for_text(text: str) -> List[TargetRestriction]:
    """Parse every printed target phrase in `text` into a TargetRestriction.

    Extracted from SpellResolver.get_targets_needed (Aug 10) so the OTHER
    target-selection path can share it. mtg/cog.py's `!activate` Tier-2
    fallback had its own, cruder inline regex that captured only the target
    TYPE and discarded any controller qualifier, then scanned the OPPONENT's
    battlefield unconditionally — so an activated ability reading "target
    creature YOU CONTROL" always pointed at the opponent's creature. That is
    a third independent parse of the same text; this is the one that already
    understands controller restrictions, so it is the one to keep.

    Aug 9 audit (B-4): the pattern list produces OVERLAPPING matches for ONE
    printed target — Snakeskin Veil's "target creature you control" matches
    both the bare `target creature` pattern AND the qualified one, so an AUTO
    branch picking a target PER restriction chose the opponent's same-named
    creature as well (CR 601.2c). Matches that overlap by SPAN are one printed
    target: keep the LONGEST (most specific). Genuinely multi-target text
    ("target creature and target player") has disjoint spans and keeps both.
    """
    oracle = (text or "").lower()
    target_patterns = [
        (r"target (creature|permanent|player|planeswalker|artifact|enchantment|land)", None),
        (r"(any target)", None),
        (r"target (nonblack|nonblue|nonred|nongreen|nonwhite) creature", None),
        (r"target creature (you control|an opponent controls)", None),
        (r"target (attacking|blocking|tapped|untapped) creature", None),
    ]

    spans: List[Tuple[int, int, str]] = []
    for pattern, _ in target_patterns:
        for match in re.finditer(pattern, oracle):
            spans.append((match.start(), match.end(), match.group(0)))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept: List[Tuple[int, int, str]] = []
    for start, end, phrase in spans:
        replaced = False
        for i, (ks, ke, _kt) in enumerate(kept):
            if start < ke and ks < end:  # overlap = same printed target
                if (end - start) > (ke - ks):
                    kept[i] = (start, end, phrase)
                replaced = True
                break
        if not replaced:
            kept.append((start, end, phrase))

    return [TargetTextParser.parse(phrase) for _, _, phrase in kept]


class SpellResolver:
    """
    Handles spell casting and resolution.
    
    Usage:
        resolver = SpellResolver(game_engine, claude_client)
        
        # Cast a spell
        result = await resolver.cast_spell(game, player, card, target="opponent")
        
        # Or with targeting prompt
        pending = await resolver.start_cast(game, player, card)
        if pending.waiting_for_targets:
            # Send prompt to player
            await resolver.select_target(pending, target_choice)
        result = await resolver.resolve(pending)
    """
    
    def __init__(self, game_engine, claude_client=None):
        self.game = game_engine
        self.claude = claude_client
        self.effect_executor = EffectExecutor(claude_client)
        self.target_validator = TargetValidator()
        
        # Pending spells waiting for targets
        self.pending_spells: Dict[int, PendingSpell] = {}  # channel_id -> pending
        
    def parse_card_effects(self, card) -> List[Effect]:
        """Parse effects from a card's oracle text."""
        if not card.oracle_text:
            return []
        return self.effect_executor.parse_effects(card.oracle_text)
    
    def get_targets_needed(self, card, effects: List[Effect]) -> List[TargetRestriction]:
        """Determine what targets a spell needs.

        Aug 9 audit (B-4): the pattern list produced OVERLAPPING restrictions
        for ONE printed target — Snakeskin Veil's "target creature you
        control" matched both the bare `target creature` pattern AND the
        `target creature you control` pattern, so the AUTO branch in
        cast_and_resolve picked a target PER restriction: the unrestricted
        one preferred the OPPONENT's same-named creature, and the executor
        then applied the effect to BOTH (a +1/+1 counter on the opponent's
        Tatyova as well as the caster's — CR 601.2c). Matches that overlap
        by SPAN are one printed target: keep the LONGEST (most specific).
        Genuinely multi-target spells ("target creature and target player")
        have disjoint spans and keep both.
        """
        return target_restrictions_for_text(
            card.oracle_text if card.oracle_text else "")
    
    def get_legal_targets(self, game, player, restriction: TargetRestriction) -> List[Tuple[Any, str]]:
        """Get all legal targets for a restriction."""
        legal = []
        player_idx = game.players.index(player) if player in game.players else 0
        
        # Check battlefield permanents. June 10 deep-dive (Berg Strider):
        # this branch only knew CREATURE/PERMANENT/ANY — an ARTIFACT,
        # ENCHANTMENT, LAND, or NONLAND_PERMANENT restriction fell through
        # every block and returned [], so "tap target artifact or creature
        # an opponent controls" fizzled "No legal targets" with three legal
        # targets on the board (and any mandatory trigger in this class was
        # dropped, CR 603.3d).
        _bf_types = {TargetType.CREATURE, TargetType.PERMANENT, TargetType.ANY,
                     TargetType.ARTIFACT, TargetType.ENCHANTMENT, TargetType.LAND,
                     TargetType.NONLAND_PERMANENT}
        if restriction.target_types & _bf_types:
            for p in game.players:
                for card in p.battlefield:
                    _matches_type = False
                    # ANY = creature/player/planeswalker (enum comment) — on
                    # the battlefield scan that means creatures only, same as
                    # the old behavior.
                    if (TargetType.CREATURE in restriction.target_types
                            or TargetType.ANY in restriction.target_types):
                        _matches_type = _matches_type or card.is_creature()
                    if TargetType.PERMANENT in restriction.target_types:
                        _matches_type = True
                    if TargetType.NONLAND_PERMANENT in restriction.target_types:
                        _matches_type = _matches_type or not card.is_land()
                    if TargetType.ARTIFACT in restriction.target_types:
                        _matches_type = _matches_type or card.is_artifact()
                    if TargetType.ENCHANTMENT in restriction.target_types:
                        _matches_type = _matches_type or card.is_enchantment()
                    if TargetType.LAND in restriction.target_types:
                        _matches_type = _matches_type or card.is_land()
                    if not _matches_type:
                        continue

                    # Check controller restriction
                    card_controller_idx = game.players.index(p)
                    if restriction.controller.name == "YOU" and card_controller_idx != player_idx:
                        continue
                    if restriction.controller.name == "OPPONENT" and card_controller_idx == player_idx:
                        continue
                    
                    # Check color exclusions
                    if restriction.colors_excluded:
                        card_colors = self._get_card_colors(card)
                        if card_colors & restriction.colors_excluded:
                            continue
                    
                    # Full targeting validation (hexproof, shroud, protection, ward)
                    if card_controller_idx != player_idx:
                        try:
                            from rules.targeting_helpers import _validate_target_for_action
                            is_legal, reason = _validate_target_for_action(
                                game, card, p, None, player.name)
                            if not is_legal:
                                continue
                        except ImportError:
                            # Fallback: basic hexproof check
                            if 'Hexproof' in card.keywords:
                                continue

                    legal.append((card, f"{card.name} ({p.name}'s)"))
        
        # Check players
        if restriction.target_types & {TargetType.PLAYER, TargetType.ANY}:
            for p in game.players:
                p_idx = game.players.index(p)
                if restriction.controller.name == "YOU" and p_idx != player_idx:
                    continue
                if restriction.controller.name == "OPPONENT" and p_idx == player_idx:
                    continue
                legal.append((p, f"{p.name} (player)"))
        
        # Check planeswalkers
        if restriction.target_types & {TargetType.PLANESWALKER, TargetType.ANY}:
            for p in game.players:
                for card in p.battlefield:
                    if card.is_planeswalker():
                        card_controller_idx = game.players.index(p)
                        if restriction.controller.name == "YOU" and card_controller_idx != player_idx:
                            continue
                        if restriction.controller.name == "OPPONENT" and card_controller_idx == player_idx:
                            continue
                        legal.append((card, f"{card.name} (planeswalker)"))
        
        return legal
    
    def _get_card_colors(self, card) -> set:
        """Get a card's colors from its mana cost."""
        colors = set()
        if not card.mana_cost:
            return colors
        
        color_map = {'W': 'W', 'U': 'U', 'B': 'B', 'R': 'R', 'G': 'G'}
        for char in card.mana_cost.upper():
            if char in color_map:
                colors.add(color_map[char])
        
        return colors
    
    async def cast_spell(
        self,
        game,
        player,
        card,
        target: Any = None,
        target_mode: TargetMode = TargetMode.AUTO
    ) -> SpellResult:
        """
        Cast a spell and resolve it immediately (simplified for casual play).
        
        Args:
            game: GameState
            player: Player casting the spell
            card: Card being cast
            target: Pre-selected target (or None for auto/prompt)
            target_mode: How to handle targeting
        
        Returns:
            SpellResult with all effects that happened
        """
        result = SpellResult(success=True)
        player_idx = game.players.index(player) if player in game.players else 0
        
        # Parse effects from oracle text
        effects = self.parse_card_effects(card)
        
        if not effects:
            # No parseable effects - might be complex or permanent
            result.messages.append(f"✨ {card.name} resolves (effects not automated)")
            return result
        
        # Determine targets needed
        targets_needed = self.get_targets_needed(card, effects)
        
        # Select targets
        selected_targets = []
        
        if targets_needed:
            if target is not None:
                # Target was specified
                selected_targets = [target] if not isinstance(target, list) else target
            elif target_mode == TargetMode.AUTO:
                # Auto-select targets.
                #
                # Aug 10 deferred (G4): this loop used to abort the WHOLE spell
                # on the first restriction with no legal target. For a MODAL
                # card that is wrong twice over — CR 601.2b/601.2c require legal
                # targets only for the modes actually CHOSEN, and a card like
                # Inscription of Abundance carries an always-legal
                # target-a-player mode alongside a creature mode. A kicked cast
                # paid 5 mana and fizzled with a legal mode available.
                #
                # Now: record each miss, keep going, and fail only if NOTHING
                # was satisfiable. That is a strict improvement in both
                # directions, because the caller also treats a failed result as
                # "reached Tier 3" rather than "resolved" (see the
                # result.success check in mtg/spells.py).
                _missed = []
                for restriction in targets_needed:
                    legal = self.get_legal_targets(game, player, restriction)
                    if legal:
                        # For damage/destroy, prefer opponent's stuff
                        opponent_targets = [t for t, desc in legal if self._is_opponent_controlled(game, player, t)]
                        if opponent_targets:
                            selected_targets.append(opponent_targets[0])
                        else:
                            selected_targets.append(legal[0][0])
                    else:
                        _missed.append(restriction)
                if _missed and not selected_targets:
                    # Nothing at all could be targeted — a real fizzle.
                    result.success = False
                    result.messages.append(f"⚠️ No legal targets for {card.name}")
                    return result
                if _missed:
                    # Partially satisfiable: resolve what we can. NOTE the
                    # remaining limitation, deliberately not papered over —
                    # ExecutionContext.targets is ONE FLAT LIST consumed
                    # positionally by ~20 executors, so a surviving clause can
                    # still be paired with another clause's target (the Aug-2
                    # batch-13 Thought Scour class, mitigated but not solved by
                    # Effect.raw_text). Skipping the miss is strictly better
                    # than fizzling the whole spell, but it is not attribution.
                    print(f"[SPELL_RESOLVER] {card.name}: {len(_missed)} of "
                          f"{len(targets_needed)} target restriction(s) had no "
                          f"legal target — resolving the rest (CR 601.2c)")
        
        # Build execution context
        context = ExecutionContext(
            game_state=game,
            source_card=card,
            source_controller=player,
            targets=selected_targets,
        )

        # Aug 10 (A1): per-clause target ATTRIBUTION. context.targets is one
        # flat list for the whole spell, so a surviving clause could be paired
        # with a DIFFERENT clause's target — the batch-13 Thought Scour class,
        # where an unconditional "Draw a card." was handed the opponent
        # auto-targeted for the separate mill clause.
        #
        # Keyed on each Effect's OWN raw_text, NOT on list position, and that
        # is load-bearing: parse_effects returns effects in PATTERN-DECLARATION
        # order while the restriction list is POSITION-sorted, so an index-zip
        # of the two misaligns for any card with two differently-typed clauses.
        # (get_targets_needed's `effects` parameter is never referenced, so
        # there is no structural link between the lists to lean on either.)
        #
        # An effect whose clause names no target keeps an EMPTY list and its
        # consumer falls back to context.targets, so every non-targeted and
        # mass effect behaves exactly as before.
        if target is None and target_mode == TargetMode.AUTO:
            # ONLY on the auto-selected path. A DECLARED target is the
            # caller's choice and outranks anything re-derived here — the
            # splice path forwards one deliberately (July 21), and attributing
            # afresh pointed Glacial Ray's "any target" at a creature when the
            # caster had declared the opponent's face.
            self._attribute_targets_to_effects(game, player, effects, context)

        # Execute each effect
        for effect in effects:
            messages = await self._execute_effect(effect, context, game)
            result.messages.extend(messages)
        
        # Track what happened for game state
        result.damage_dealt = getattr(context, 'damage_dealt_tracking', {})
        
        return result
    
    def _attribute_targets_to_effects(self, game, player, effects, context) -> None:
        """Give each Effect the targets its OWN clause names (Aug 10, A1).

        Scopes the restriction scan to `effect.raw_text` — the clause sentence
        parse_effects records — and reuses the SAME selection preference the
        flat pass uses (prefer an opponent-controlled legal target), so an
        attributed clause picks what it would have picked anyway; what changes
        is that a DIFFERENT clause's pick can no longer leak into it.

        Deliberately additive: an effect with no target phrase, or one whose
        restriction has no legal target, keeps an empty list and falls back to
        context.targets at the consumer. That keeps mass effects ("destroy all
        creatures"), non-targeted draws, and every Tier-2 path that never had
        targets behaving exactly as before.
        """
        for effect in effects:
            clause = getattr(effect, 'raw_text', '') or ''
            if not clause or 'target' not in clause.lower():
                continue
            attributed = []
            for restriction in target_restrictions_for_text(clause):
                legal = self.get_legal_targets(game, player, restriction)
                if not legal:
                    continue
                opponent_side = [t for t, _d in legal
                                 if self._is_opponent_controlled(game, player, t)]
                attributed.append(opponent_side[0] if opponent_side else legal[0][0])
            if attributed:
                effect.selected_targets = attributed

    def _is_opponent_controlled(self, game, player, target) -> bool:
        """Check if target is controlled by an opponent."""
        player_idx = game.players.index(player) if player in game.players else 0
        
        if hasattr(target, 'is_player') or (hasattr(target, 'life') and hasattr(target, 'hand')):
            # It's a player
            target_idx = game.players.index(target) if target in game.players else -1
            return target_idx != player_idx
        
        # It's a permanent - find its controller
        for i, p in enumerate(game.players):
            if target in p.battlefield:
                return i != player_idx
        
        return False
    
    async def _execute_effect(self, effect: Effect, context: ExecutionContext, game) -> List[str]:
        """Execute a single effect and return messages."""
        messages = []
        
        if effect.effect_type == EffectType.DAMAGE:
            messages.extend(await self._exec_damage(effect, context, game))
        
        elif effect.effect_type == EffectType.DESTROY:
            messages.extend(await self._exec_destroy(effect, context, game))
        
        elif effect.effect_type == EffectType.DRAW:
            messages.extend(await self._exec_draw(effect, context, game))
        
        elif effect.effect_type == EffectType.DISCARD:
            messages.extend(await self._exec_discard(effect, context, game))
        
        elif effect.effect_type == EffectType.LIFE_GAIN:
            messages.extend(await self._exec_life_gain(effect, context, game))
        
        elif effect.effect_type == EffectType.LIFE_LOSS:
            messages.extend(await self._exec_life_loss(effect, context, game))
        
        elif effect.effect_type == EffectType.EXILE:
            messages.extend(await self._exec_exile(effect, context, game))
        
        elif effect.effect_type == EffectType.BOUNCE:
            messages.extend(await self._exec_bounce(effect, context, game))
        
        elif effect.effect_type == EffectType.COUNTER:
            messages.extend(await self._exec_counter(effect, context, game))
        
        elif effect.effect_type == EffectType.PUMP:
            messages.extend(await self._exec_pump(effect, context, game))
        
        elif effect.effect_type == EffectType.FIGHT:
            messages.extend(await self._exec_fight(effect, context, game))
        
        elif effect.effect_type == EffectType.CREATE_TOKEN:
            messages.extend(await self._exec_create_token(effect, context, game))
        
        elif effect.effect_type == EffectType.ADD_COUNTER:
            messages.extend(await self._exec_add_counter(effect, context, game))
        
        elif effect.effect_type == EffectType.TAP:
            messages.extend(await self._exec_tap(effect, context, game))
        
        elif effect.effect_type == EffectType.UNTAP:
            messages.extend(await self._exec_untap(effect, context, game))
        
        elif effect.effect_type == EffectType.MILL:
            messages.extend(await self._exec_mill(effect, context, game))
        
        elif effect.effect_type == EffectType.SACRIFICE:
            messages.extend(await self._exec_sacrifice(effect, context, game))
        
        elif effect.effect_type == EffectType.COMPLEX:
            # SpellResolver couldn't regex-match this effect. Mark it for the
            # downstream filter (spells.py "Complex effect" check) but the
            # PLAYER-VISIBLE text leads with the effect description, not the
            # word "Complex" — that's internal jargon. The marker tag is
            # appended in lowercase parens so the filter still matches but
            # the user sees a cleaner phrasing. spells.py also strips lines
            # starting with the lowercase marker; if that strip fails (Tier 3
            # didn't run, etc.) the user at least sees what the spell does.
            import re as _re
            cleaned = _re.sub(r'\([^)]*\)', '', effect.raw_text or '').strip(' .,;')
            cleaned = _re.sub(r'\s{2,}', ' ', cleaned)
            if self.claude:
                # Marker-as-suffix lets the filter still strip via "complex effect" detection
                # without showing "Complex effect:" as the leading user-visible text.
                messages.append(f"🧙 {cleaned} _(complex effect, escalating)_")
            else:
                messages.append(f"⚠️ {cleaned} _(complex effect, manual resolution needed)_")
        
        return messages
    
    # ==========================================================================
    # Effect Execution Methods
    # ==========================================================================

    def _handle_player_death(self, game, target, messages: List[str]) -> None:
        """Route a life<=0 player through SBA instead of a custom game-over.

        July 12 audit (game #84, burn vs jund): the old inline
        "💀 X has been defeated!" (a) didn't match the standard SBA loss line
        every other kill path produces ("💀 **X** loses the game! (X has 0
        life)"), (b) bypassed can't-lose effects (Platinum Angel), and (c) was
        posted as its own Discord send by the stack-resolution task, which
        races the autoplay main loop's 🏆 summary — the trophy landed between
        the damage line and an orphaned defeat line. Folding the loss into the
        previous message makes the send atomic.
        """
        if target.life > 0 or game.ended:
            return
        rules_engine = getattr(game, '_rules_engine', None)
        if rules_engine is not None and hasattr(rules_engine, 'process_state_based_actions'):
            sba_messages = rules_engine.process_state_based_actions(game)
        else:
            # Standalone fallback (no rules engine wired) — same wording SBA
            # produces, so the discord log stays grep-consistent.
            sba_messages = [f"💀 **{target.name}** loses the game! ({target.name} has 0 life)"]
            game.ended = True
            game.winner = 1 - game.players.index(target)
        if sba_messages:
            if messages:
                messages[-1] += "\n" + "\n".join(sba_messages)
            else:
                messages.extend(sba_messages)

    async def _exec_damage(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute damage effect."""
        messages = []
        amount = effect.amount
        
        # Handle variable damage (amount=-1 means "equal to power" or similar)
        if amount == -1:
            # Find a creature the controller controls to use as damage source
            source_creature = None
            for card in ctx.source_controller.battlefield:
                if card.is_creature():
                    source_creature = card
                    break
            
            if source_creature:
                try:
                    amount = int(source_creature.power) if source_creature.power else 0
                    amount += getattr(source_creature, 'power_modifier', 0)
                    amount += source_creature.counters.get('+1/+1', 0)
                    amount -= source_creature.counters.get('-1/-1', 0)
                except (ValueError, TypeError):
                    amount = 0
            else:
                # No creature found, can't deal damage
                messages.append(f"⚠️ No creature to deal damage with")
                return messages
        
        # June 10 audit (C4): bind the engine ref BEFORE the target loop. It was
        # previously assigned only inside the player-target branch, so a
        # creature-first target list hit an UnboundLocalError at the SBA call in
        # the creature branch — AFTER damage_marked was mutated — and the catch
        # in mtg/spells.py escalated to Tier 3, which re-resolved the spell from
        # scratch (one Lightning Bolt: 3 to a player AND a creature killed by
        # the phantom marked damage; 8 games in the June 10 batch).
        rules_engine = getattr(game, '_rules_engine', None)
        for target in _targets_for(effect, ctx):
            if hasattr(target, 'life') and hasattr(target, 'hand'):
                # It's a player. Route through the engine's centralized damage
                # path when available so replacement effects (Teferi's Protection,
                # Fog), damage prevention, and the [NONCOMBAT-LIFE] audit trail
                # all fire — without it, life totals get reduced silently and
                # damage-doubling/halving (Furnace of Rath, Gisela) is bypassed.
                if rules_engine and hasattr(rules_engine, '_apply_noncombat_damage_to_player'):
                    # Aug 7 (B-4): pass the caster explicitly — the spell has
                    # already been popped off the stack mid-resolution, so
                    # the funnel's battlefield/stack lookups come up blank
                    # and Torbran-class "a source you control" replacements
                    # silently never fired on Tier-2 burn.
                    actual = rules_engine._apply_noncombat_damage_to_player(
                        game, target, amount, ctx.source_card.name,
                        source_controller=getattr(ctx.source_controller,
                                                  'name', '') or '',
                    )
                else:
                    target.life -= amount
                    # Aug 10 (C2): the fallback branch was uninstrumented.
                    target.record_life_loss(
                        amount, game=game,
                        source_name=getattr(getattr(ctx, 'source_card', None),
                                            'name', '') or '')
                    actual = amount
                    # May 16 audit: console mirror for the fallback path so
                    # batches without the rules-engine wiring still log a
                    # grep-able damage tag (matches [NONCOMBAT-LIFE] format).
                    # May 20 audit fix: clamp life to 0 for display per the
                    # May 19 sentinel — game_1506618495641587802:559 printed
                    # `life: -2` because this fallback emit bypassed the
                    # May 19 max(0, life) clamp added to the primary path.
                    print(f"[NONCOMBAT-LIFE] {target.name} takes {actual} noncombat damage "
                          f"from {ctx.source_card.name} (life: {max(0, target.life)})")
                messages.append(f"🔥 {ctx.source_card.name} deals {actual} damage to {target.name} (life: {max(0, target.life)})")

                # Check for death — routed through SBA (see _handle_player_death)
                self._handle_player_death(game, target, messages)
            
            elif (hasattr(target, 'loyalty_counters') and hasattr(target, 'is_planeswalker')
                  and target.is_planeswalker()):
                # It's a planeswalker. June 11 audit: this check MUST come before
                # the damage_marked branch — every Card has damage_marked, so
                # planeswalkers fell into the creature path, compared damage to
                # toughness 0, and "died from lethal damage" at any loyalty
                # (game 1514633271047225385: Daretti at 13 loyalty destroyed by a
                # 3-damage Bolt). CR 120.3c: damage removes that many loyalty
                # counters. If it's ALSO a creature (animated), damage is marked
                # too (CR 120.3). Death is left to SBA (CR 704.5i) so command-zone
                # replacement applies to commander planeswalkers.
                target.loyalty_counters -= amount
                if target.is_creature():
                    target.damage_marked += amount
                messages.append(
                    f"🔥 {ctx.source_card.name} deals {amount} damage to {target.name} "
                    f"({max(0, target.loyalty_counters)} loyalty)")
                if rules_engine is not None and hasattr(rules_engine, 'process_state_based_actions'):
                    try:
                        messages.extend(rules_engine.process_state_based_actions(game) or [])
                    except Exception as _sba_err:
                        print(f"[SPELL-DAMAGE] SBA after PW damage failed for {target.name}: {_sba_err}")
                elif target.loyalty_counters <= 0:
                    messages.append(f"💀 {target.name} is destroyed!")
                    for player in game.players:
                        if target in player.battlefield:
                            player.battlefield.remove(target)
                            player.graveyard.append(target)
                            break

            elif hasattr(target, 'damage_marked'):
                # It's a creature. May 30 audit: mark damage and let SBA resolve any
                # death so totem (Umbra) armor / undying / persist / shield counters
                # all apply (CR 704.5g + 614.6). This path previously removed the
                # creature inline, bypassing every death-replacement save, and used
                # raw int(toughness) instead of effective toughness (so Spider Umbra's
                # +1 was ignored too).
                target.damage_marked += amount
                messages.append(f"🔥 {ctx.source_card.name} deals {amount} damage to {target.name}")
                # Aug 1: "whenever a source deals damage to this creature"
                # fires on noncombat damage too (CR 603.2 — Obliterator vs
                # burn; the scan was combat-only until now). The Tier-2
                # exec KNOWS the source's controller — no heuristic needed.
                # Runs BEFORE the SBA below so a dead Obliterator still got
                # its trigger (the damage was dealt while it lived).
                try:
                    from mtg.triggers import scan_damaged_creature
                    if rules_engine is not None:
                        messages.extend(scan_damaged_creature(
                            rules_engine, game, target, amount,
                            ctx.source_controller))
                except Exception as _dt_err:
                    print(f"[SPELL-DAMAGE] damaged-creature scan failed for "
                          f"{target.name}: {_dt_err}")
                    from mtg.util import maybe_reraise
                    maybe_reraise(_dt_err)
                if rules_engine is not None and hasattr(rules_engine, 'process_state_based_actions'):
                    try:
                        messages.extend(rules_engine.process_state_based_actions(game) or [])
                    except Exception as _sba_err:
                        print(f"[SPELL-DAMAGE] SBA after damage failed for {target.name}: {_sba_err}")
                else:
                    # Fallback (no engine ref): effective-toughness lethal check.
                    try:
                        _tuf = target.get_effective_toughness(game)
                    except Exception:
                        _tuf = int(target.toughness) if target.toughness else 0
                    if target.damage_marked >= _tuf:
                        messages.append(f"💀 {target.name} dies from lethal damage!")
                        for player in game.players:
                            if target in player.battlefield:
                                player.battlefield.remove(target)
                                player.graveyard.append(target)
                                break
            
            elif hasattr(target, 'loyalty_counters'):
                # It's a planeswalker
                target.loyalty_counters -= amount
                messages.append(f"🔥 {ctx.source_card.name} deals {amount} damage to {target.name} ({target.loyalty_counters} loyalty)")
                
                if target.loyalty_counters <= 0:
                    messages.append(f"💀 {target.name} is destroyed!")
                    for player in game.players:
                        if target in player.battlefield:
                            player.battlefield.remove(target)
                            player.graveyard.append(target)
                            break
        
        return messages
    
    async def _exec_destroy(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute destroy effect."""
        messages = []
        
        for target in _targets_for(effect, ctx):
            if hasattr(target, 'has_keyword') and target.has_keyword('Indestructible', game=game):
                messages.append(f"⚠️ {target.name} is indestructible!")
                continue
            
            # Find and remove from battlefield
            for player in game.players:
                if target in player.battlefield:
                    player.battlefield.remove(target)
                    player.graveyard.append(target)
                    messages.append(f"💀 {target.name} is destroyed!")
                    break
        
        return messages
    
    async def _exec_draw(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute draw effect."""
        messages = []
        amount = effect.amount

        # Draw for controller by default
        player = ctx.source_controller

        # Aug 2 batch-13 (delve reviewer): ctx.targets is shared across the
        # WHOLE spell's clauses, so an unconditional "Draw a card." was being
        # redirected to whichever player another clause auto-targeted
        # (Thought Scour: the dropped mill clause targeted the opponent, who
        # then received the caster's draw). Only redirect when the draw
        # clause ITSELF names a target/that player.
        _draw_clause = (effect.raw_text or '').lower()
        _clause_targets_player = bool(re.search(
            r'\b(target|that) (player|opponent)\b[^.]*draw', _draw_clause))
        if _clause_targets_player:
            for target in _targets_for(effect, ctx):
                if hasattr(target, 'life') and hasattr(target, 'hand'):
                    player = target
                    break
        
        drawn = []
        for _ in range(amount):
            if player.library:
                card = player.library.pop(0)
                player.hand.append(card)
                drawn.append(card.name)
        
        if drawn:
            messages.append(f"🎴 {player.name} draws {len(drawn)} card(s)")
        
        return messages
    
    async def _exec_discard(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute discard effect."""
        messages = []
        amount = effect.amount
        
        # Target player or controller
        player = ctx.source_controller
        for target in _targets_for(effect, ctx):
            if hasattr(target, 'life') and hasattr(target, 'hand'):
                player = target
                break
        
        if amount == -1:  # Discard all
            amount = len(player.hand)
        
        discarded = []
        for _ in range(min(amount, len(player.hand))):
            if player.hand:
                # Discard random card (AI) or last card (simplified)
                card = player.hand.pop()
                player.graveyard.append(card)
                discarded.append(card.name)
        
        if discarded:
            messages.append(f"📤 {player.name} discards: {', '.join(discarded)}")
        
        return messages
    
    async def _exec_life_gain(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute life gain effect."""
        messages = []
        amount = effect.amount

        player = ctx.source_controller
        player.life += amount
        # May 25 audit (F29): emit symmetric [LIFE-GAIN] console tag so the
        # post-batch life-total reconciliation can grep all gain events.
        # Lightning Helix's "gain 3 life" clause flows through SpellResolver
        # (Tier 2) rather than mtg/actions.py:gain_life, so the [LIFE-GAIN]
        # tag emitted there at line 472 was skipped. Audit found exactly this
        # gap in game_1508578198718517490 — the gain happened in state but
        # was invisible to log grep.
        src_name = getattr(getattr(ctx, 'source_card', None), 'name', '') or 'spell/ability'
        print(f"[LIFE-GAIN] {player.name}: +{amount} life → {player.life} ({src_name})")
        messages.append(f"💚 {player.name} gains {amount} life (life: {player.life})")

        return messages

    async def _exec_life_loss(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute life loss effect."""
        messages = []
        amount = effect.amount

        # Target or controller
        player = ctx.source_controller
        for target in _targets_for(effect, ctx):
            if hasattr(target, 'life') and hasattr(target, 'hand'):
                player = target
                break

        player.life -= amount
        # Aug 10 deferred (C2): THE gap that mattered. This is the live Tier-2
        # handler for a bare "target player loses N life" — precisely the shape
        # Mindcrank exists to catch — and it never called record_life_loss at
        # all, so both the loses-life bus event and the existing
        # spectacle_available consumer were blind to it whenever a non-damage
        # loss was the only life loss in a turn.
        player.record_life_loss(amount, game=game,
                                source_name=getattr(
                                    getattr(ctx, 'source_card', None), 'name', '') or '')
        # May 25 audit (F29): symmetric [LIFE-LOSS] tag — same gap as the
        # [LIFE-GAIN] above, the SpellResolver path bypasses mtg/actions.py
        # where the [LIFE-LOSS] tag was already added on May 16.
        src_name = getattr(getattr(ctx, 'source_card', None), 'name', '') or 'spell/ability'
        print(f"[LIFE-LOSS] {player.name}: -{amount} life → {player.life} ({src_name})")
        messages.append(f"🖤 {player.name} loses {amount} life (life: {player.life})")
        
        # Death check routed through SBA (see _handle_player_death)
        self._handle_player_death(game, player, messages)

        return messages
    
    async def _exec_exile(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute exile effect."""
        messages = []
        
        for target in _targets_for(effect, ctx):
            for player in game.players:
                if target in player.battlefield:
                    player.battlefield.remove(target)
                    player.exile.append(target)
                    messages.append(f"✨ {target.name} is exiled!")
                    break
                if target in player.graveyard:
                    player.graveyard.remove(target)
                    player.exile.append(target)
                    messages.append(f"✨ {target.name} is exiled from graveyard!")
                    break
        
        return messages
    
    async def _exec_bounce(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute bounce (return to hand) effect."""
        messages = []
        
        for target in _targets_for(effect, ctx):
            for player in game.players:
                if target in player.battlefield:
                    player.battlefield.remove(target)
                    # Return to owner's hand (simplified: controller = owner)
                    player.hand.append(target)
                    # Reset card state
                    target.tapped = False
                    target.summoning_sick = False
                    target.damage_marked = 0
                    messages.append(f"↩️ {target.name} is returned to {player.name}'s hand!")
                    break
        
        return messages
    
    async def _exec_counter(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute counter spell effect.

        Looks at game.stack for a target spell that ISN'T the counterspell
        itself and marks it countered. Apr 29 audit: this used to
        unconditionally fizzle, wasting every counterspell cast through the
        SpellResolver Tier 2 path. The new stack engine in mtg/spells.py does
        support real counters, so we mirror its behavior here.
        """
        messages = []
        caster = ctx.source_controller.name if ctx.source_controller else "Unknown"
        spell_name = ctx.source_card.name if ctx.source_card else "spell"

        stack = getattr(game, 'stack', None) or []
        target = None
        # Skip the counterspell itself — look for a non-self target, top-down.
        # July 29 batch audit: also skip spells the counterspell's own printed
        # restriction can't touch (Mental Misstep's "with mana value 1"
        # countered a mana-value-2 Intangible Virtue through this path).
        from mtg.helpers import counter_restriction_allows
        _counter_oracle = getattr(ctx.source_card, 'oracle_text', '') if ctx.source_card else ''
        for entry in reversed(stack):
            entry_card = getattr(entry, 'card', None)
            entry_name = getattr(entry_card, 'name', None) if entry_card else None
            if entry_name and entry_name == spell_name:
                continue  # don't counter ourselves
            if getattr(entry, 'countered', False):
                continue  # already countered
            if entry_card is not None and not counter_restriction_allows(
                    _counter_oracle, entry_card):
                print(f"[COUNTER] {spell_name} can't counter {entry_name} — "
                      f"printed restriction not met")
                continue
            target = entry
            break

        if target is None:
            messages.append(
                f"🚫 **{spell_name}** resolves with no target — no spell on the stack to counter."
            )
            print(f"[COUNTER] {spell_name} cast by {caster} — no valid target, fizzled")
            return messages

        # Mark target as countered. mtg/spells.py resolution checks this flag.
        target_name = getattr(getattr(target, 'card', None), 'name', None) or 'spell'
        if hasattr(target, 'countered'):
            target.countered = True
        messages.append(f"🚫 **{target_name}** is countered by **{spell_name}**!")
        print(f"[COUNTER] {spell_name} cast by {caster} → countered {target_name}")
        # July 29: Baral-class "counters a spell" triggers.
        try:
            from mtg.triggers import fire_counters_a_spell_triggers
            messages.extend(fire_counters_a_spell_triggers(
                game, ctx.source_controller.name if ctx.source_controller else None))
        except (AttributeError, TypeError) as e:
            print(f"[COUNTER-TRIGGER] scan failed: {e}")
        return messages
    
    async def _exec_pump(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute pump (+X/+X) effect."""
        messages = []
        
        for target in _targets_for(effect, ctx):
            if hasattr(target, 'power_modifier'):
                target.power_modifier = getattr(target, 'power_modifier', 0) + effect.power_mod
            if hasattr(target, 'toughness_modifier'):
                target.toughness_modifier = getattr(target, 'toughness_modifier', 0) + effect.toughness_mod
            
            # Add temporary keywords — into temp_keywords (Aug 2): the
            # message below says "until end of turn" but this wrote the
            # PRINTED list, which aliased the card cache (Tovolar's phantom
            # lowercase 'flying' on disk came from here).
            if effect.keywords_granted:
                for kw in effect.keywords_granted:
                    if kw not in target.temp_keywords:
                        target.temp_keywords.append(kw)
            
            pump_str = f"{effect.power_mod:+}/{effect.toughness_mod:+}"
            kw_str = f" and gains {', '.join(effect.keywords_granted)}" if effect.keywords_granted else ""
            messages.append(f"💪 {target.name} gets {pump_str}{kw_str} until end of turn")

        # July 31 batch-11 (limited reviewer): a NEGATIVE pump had no SBA
        # chokepoint before the until-EOT modifier expired — Disfigure's
        # -2/-2 left a 1/1 Healer's Hawk alive at effective -1 toughness for
        # three more combats (game_1532532194684436573; CR 704.5f). Same
        # missing-SBA sibling as the May 30 D2 damage fix two functions up
        # and the June 10 Toxic Deluge fix on the actions path.
        if effect.toughness_mod < 0:
            rules_engine = getattr(game, '_rules_engine', None)
            if rules_engine is not None and hasattr(rules_engine, 'process_state_based_actions'):
                try:
                    if hasattr(game, 'recalculate_power_toughness'):
                        game.recalculate_power_toughness()
                    messages.extend(rules_engine.process_state_based_actions(game) or [])
                except Exception as e:
                    print(f"[SPELL_RESOLVER] SBA after negative pump failed: {e}")
                    from mtg.util import maybe_reraise
                    maybe_reraise(e)

        return messages
    
    async def _exec_fight(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute fight effect (like Ram Through)."""
        messages = []
        
        # Need source creature and target creature
        source_creature = None
        target_creature = None
        
        # Find a creature the controller controls to be the "fighter"
        for card in ctx.source_controller.battlefield:
            if card.is_creature():
                source_creature = card
                break
        
        # Target is the opponent's creature
        for target in _targets_for(effect, ctx):
            if hasattr(target, 'is_creature') and target.is_creature():
                target_creature = target
                break
        
        if source_creature and target_creature:
            try:
                source_power = int(source_creature.power) if source_creature.power else 0
                # Add modifiers and counters
                source_power += getattr(source_creature, 'power_modifier', 0)
                source_power += source_creature.counters.get('+1/+1', 0) if hasattr(source_creature, 'counters') else 0
                source_power -= source_creature.counters.get('-1/-1', 0) if hasattr(source_creature, 'counters') else 0
                
                target_toughness = int(target_creature.toughness) if target_creature.toughness else 0
                target_toughness += getattr(target_creature, 'toughness_modifier', 0)
                target_toughness += target_creature.counters.get('+1/+1', 0) if hasattr(target_creature, 'counters') else 0
                target_toughness -= target_creature.counters.get('-1/-1', 0) if hasattr(target_creature, 'counters') else 0
            except (ValueError, TypeError):
                source_power, target_toughness = 0, 0
            
            # Source deals damage to target (Ram Through style - source deals, target doesn't fight back)
            target_creature.damage_marked += source_power
            messages.append(f"⚔️ {source_creature.name} deals {source_power} damage to {target_creature.name}!")
            # Aug 1: the damaged-creature scan (CR 603.2 — any source)
            # covers fight damage too; the fighting creature's controller
            # is the source's controller.
            try:
                from mtg.triggers import scan_damaged_creature
                _re_engine = getattr(game, '_rules_engine', None)
                if _re_engine is not None:
                    messages.extend(scan_damaged_creature(
                        _re_engine, game, target_creature, source_power,
                        ctx.source_controller))
            except Exception as _dt_err:
                print(f"[SPELL-DAMAGE] damaged-creature scan failed in fight: {_dt_err}")
                from mtg.util import maybe_reraise
                maybe_reraise(_dt_err)
            
            # Check for trample - excess goes to controller
            if hasattr(source_creature, 'has_keyword') and source_creature.has_keyword('Trample'):
                excess = source_power - target_toughness
                if excess > 0:
                    # Find target's controller
                    for player in game.players:
                        if target_creature in player.battlefield:
                            player.life -= excess
                            player.record_life_loss(excess, game=game)  # Aug 10 (C2)
                            messages.append(f"🦏 {excess} trample damage to {player.name}!")
                            break
            
            # Check if target dies
            if target_creature.damage_marked >= target_toughness:
                messages.append(f"💀 {target_creature.name} dies!")
                for player in game.players:
                    if target_creature in player.battlefield:
                        player.battlefield.remove(target_creature)
                        player.graveyard.append(target_creature)
                        break
        else:
            messages.append(f"⚠️ Fight failed - couldn't find valid creatures")
        
        return messages
    
    async def _exec_create_token(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute create token effect."""
        messages = []
        
        # Would need proper token creation - for now just message
        token_desc = f"{effect.token_power}/{effect.token_toughness} {' '.join(effect.token_types)}"
        if effect.token_keywords:
            token_desc += f" with {', '.join(effect.token_keywords)}"
        
        messages.append(f"🎭 {ctx.source_controller.name} creates {effect.amount or 1} {token_desc} token(s)")
        
        return messages
    
    async def _exec_add_counter(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute add counter effect."""
        messages = []
        
        for target in _targets_for(effect, ctx):
            if not hasattr(target, 'counters'):
                target.counters = {}
            
            current = target.counters.get(effect.counter_type, 0)
            target.counters[effect.counter_type] = current + effect.amount

            # July 31 batch-10 reviewer: the counters-dict write above IS the
            # counter — get_effective_power/toughness read it directly. The
            # old extra power_modifier/toughness_modifier bump here made a
            # Tier-2-placed +1/+1 counter read as +2/+2 until the end-of-turn
            # modifier sweep zeroed the modifier half (Snakeskin Veil on
            # Tovolar, game_1532409540866212023). Counters are permanent
            # state, not until-EOT pumps.

            messages.append(f"⭕ {effect.amount} {effect.counter_type} counter(s) put on {target.name}")
        
        return messages
    
    async def _exec_tap(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute tap effect."""
        messages = []
        
        for target in _targets_for(effect, ctx):
            if hasattr(target, 'tapped'):
                target.tapped = True
                messages.append(f"↪️ {target.name} becomes tapped")
        
        return messages
    
    async def _exec_untap(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute untap effect."""
        messages = []
        
        for target in _targets_for(effect, ctx):
            if hasattr(target, 'tapped'):
                target.tapped = False
                messages.append(f"↩️ {target.name} untaps")
        
        return messages
    
    async def _exec_mill(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute mill effect."""
        messages = []
        amount = effect.amount
        
        # Target or opponent
        player = None
        for target in _targets_for(effect, ctx):
            if hasattr(target, 'library'):
                player = target
                break
        
        if not player:
            # Default to opponent
            player_idx = game.players.index(ctx.source_controller) if ctx.source_controller in game.players else 0
            player = game.players[1 - player_idx]
        
        milled = []
        for _ in range(min(amount, len(player.library))):
            if player.library:
                card = player.library.pop(0)
                player.graveyard.append(card)
                milled.append(card.name)
        
        if milled:
            messages.append(f"📚 {player.name} mills {len(milled)}: {', '.join(milled[:3])}{'...' if len(milled) > 3 else ''}")
        
        return messages
    
    async def _exec_sacrifice(self, effect: Effect, ctx: ExecutionContext, game) -> List[str]:
        """Execute sacrifice effect."""
        messages = []
        
        for target in _targets_for(effect, ctx):
            for player in game.players:
                if target in player.battlefield:
                    player.battlefield.remove(target)
                    player.graveyard.append(target)
                    messages.append(f"🗡️ {player.name} sacrifices {target.name}")
                    break
        
        return messages


# =============================================================================
# Convenience function for integrating with existing code
# =============================================================================

def create_spell_resolver(game_engine, claude_client=None) -> SpellResolver:
    """Create a spell resolver instance."""
    return SpellResolver(game_engine, claude_client)
