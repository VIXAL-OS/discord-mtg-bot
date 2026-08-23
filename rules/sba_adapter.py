"""
Adapter to bridge mtg_game.py's GameState with rules/state_based_actions.py
for side-by-side diagnostic comparison of SBA checking.

This module provides:
- _build_sba_state(): Convert mtg_game GameState -> SBA GameState
- _compare_with_rules_sba(): Run both checkers and log discrepancies
"""

from rules.state_based_actions import (
    StateBasedActionChecker as RulesSBAChecker,
    GameState as SBAGameState,
    Player as SBAPlayer,
    Permanent as SBAPermanent,
    SBAType,
)


def parse_pt_value(val) -> int:
    """Parse a power/toughness value to int (handles '*', None, strings)."""
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if s == '*':
        return 0
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


def _count_saga_chapters(card) -> int:
    """Count total chapters in a saga by parsing Roman numerals from oracle text."""
    import re
    oracle = getattr(card, 'oracle_text', '') or ''
    roman_to_int = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5}
    chapters = re.findall(r'(?:^|\n)([IVX,\s]+)\s*[—–-]', oracle)
    max_chapter = 0
    for label in chapters:
        parts = [p.strip() for p in label.split(',')]
        for p in parts:
            max_chapter = max(max_chapter, roman_to_int.get(p, 0))
    return max_chapter if max_chapter > 0 else 3


def convert_sba_results(rules_results, game, rules=None):
    """Convert List[SBAResult] from rules module to List[Dict] for process_state_based_actions().

    Maps SBAType enum values to the dict format the game engine already consumes.
    Builds a reverse lookup from card.id to (card.name, player_index) from the game state.

    `rules` is the RulesEngine instance — needed for totem-armor lookup
    (`rules._has_totem_armor` / `rules._remove_totem_armor`). If None, totem-armor
    saves are skipped (cards die instead of being saved). Default None preserves
    backward compat with any call site that hasn't been updated yet.
    """
    # Build reverse lookup: card_id -> (name, player_index)
    card_lookup = {}
    for i, player in enumerate(game.players):
        for card in player.battlefield:
            card_lookup[card.id] = (card.name, i)

    actions = []
    for result in rules_results:
        stype = result.sba_type
        desc = result.description

        if stype in (SBAType.PLAYER_LOSES_ZERO_LIFE, SBAType.PLAYER_LOSES_POISON,
                     SBAType.PLAYER_LOSES_DRAW_EMPTY):
            for obj_id in result.affected_objects:
                # obj_id is player ID string (e.g., "0", "1")
                try:
                    idx = int(obj_id)
                except (ValueError, TypeError):
                    continue
                # Check "can't lose" effects
                cant_lose = False
                for perm in game.players[idx].battlefield:
                    oracle = (perm.oracle_text or '').lower()
                    if "you can't lose the game" in oracle or "your life total can't change" in oracle:
                        cant_lose = True
                        break
                if not cant_lose:
                    actions.append({'type': 'player_loses', 'player_index': idx, 'reason': desc})

        elif stype in (SBAType.CREATURE_ZERO_TOUGHNESS, SBAType.CREATURE_LETHAL_DAMAGE,
                       SBAType.CREATURE_DEATHTOUCH):
            # May 20 audit (CRITICAL): previously this skipped shield counter
            # and totem armor checks because they only lived in the inline
            # fallback at mtg/sba.py:227-252. Under normal operation (rules
            # module loaded successfully), creature deaths from
            # CREATURE_LETHAL_DAMAGE / CREATURE_DEATHTOUCH never honored
            # those replacement-style effects.
            # game_1506623254943498252:801,974 showed Young Wolf (1/1 with
            # Mammoth Umbra totem armor) take 10 damage and trigger undying
            # — totem armor should have absorbed the damage instead. CR 614.6
            # says totem armor replaces "destroy" with "remove all damage +
            # destroy the aura". CR 614 also covers shield counters analogously.
            # NOTE: zero-toughness deaths (CREATURE_ZERO_TOUGHNESS) bypass
            # both shield counters and totem armor per CR 704.5f — emit
            # plain creature_dies for those.
            for obj_id in result.affected_objects:
                name, pi = card_lookup.get(obj_id, ('Unknown', 0))
                if stype == SBAType.CREATURE_ZERO_TOUGHNESS:
                    actions.append({'type': 'creature_dies', 'card_id': obj_id, 'card_name': name,
                                    'player_index': pi, 'reason': desc})
                    continue
                # Find the actual card and its controller for shield/totem-armor checks.
                card_obj = None
                owner = None
                try:
                    if 0 <= pi < len(game.players):
                        owner = game.players[pi]
                        for c in owner.battlefield:
                            if c.id == obj_id:
                                card_obj = c
                                break
                except Exception:
                    card_obj = None
                if card_obj is None:
                    # Couldn't resolve — fall back to plain dies.
                    actions.append({'type': 'creature_dies', 'card_id': obj_id, 'card_name': name,
                                    'player_index': pi, 'reason': desc})
                    continue
                # Indestructible blocks damage-based dies entirely.
                if card_obj.has_keyword('Indestructible', game=game):
                    continue
                # Shield counters absorb the damage instead of destroying.
                if card_obj.counters.get('shield', 0) > 0:
                    card_obj.counters['shield'] -= 1
                    card_obj.damage_marked = 0
                    card_obj.deathtouch_damage = 0
                    actions.append({'type': 'shield_removed', 'card_id': obj_id,
                                    'card_name': name, 'player_index': pi,
                                    'reason': 'shield counter removed instead of destruction'})
                    continue
                # Totem armor: destroy the aura, save the creature.
                if rules is not None and hasattr(rules, '_has_totem_armor'):
                    try:
                        if rules._has_totem_armor(card_obj, owner, game):
                            aura = rules._remove_totem_armor(card_obj, owner, game)
                            card_obj.damage_marked = 0
                            card_obj.deathtouch_damage = 0
                            actions.append({'type': 'totem_armor', 'card_id': obj_id,
                                            'card_name': name,
                                            'aura_name': aura.name if aura else '?',
                                            'player_index': pi,
                                            'reason': f'totem armor ({aura.name if aura else "?"}) destroyed instead'})
                            continue
                    except Exception as _ta_err:
                        print(f"[SBA-ADAPTER] totem armor check failed for {name}: {_ta_err}")
                # No protection — creature dies.
                actions.append({'type': 'creature_dies', 'card_id': obj_id, 'card_name': name,
                                'player_index': pi, 'reason': desc})

        elif stype == SBAType.PLANESWALKER_ZERO_LOYALTY:
            for obj_id in result.affected_objects:
                name, pi = card_lookup.get(obj_id, ('Unknown', 0))
                actions.append({'type': 'planeswalker_dies', 'card_id': obj_id, 'card_name': name,
                                'player_index': pi, 'reason': desc})

        elif stype == SBAType.LEGEND_RULE:
            # CR 704.5j: controller chooses one to keep. June 10 audit: was
            # keep-most-recent, which binned a 13-loyalty Teferi to keep a
            # fresh 4-loyalty copy. Keep-BEST instead: highest loyalty, then
            # most counters, then most-recently-added as the tiebreak (which
            # preserves the old behavior for vanilla legends — any sane
            # controller chooses the developed copy).
            affected = list(result.affected_objects)
            to_destroy = []
            if len(affected) > 1:
                def _keep_score(obj_id):
                    for _p in game.players:
                        for _c in _p.battlefield:
                            if _c.id == obj_id:
                                return (getattr(_c, 'loyalty_counters', 0) or 0,
                                        sum((_c.counters or {}).values()),
                                        affected.index(obj_id))
                    return (0, 0, affected.index(obj_id))
                keeper = max(affected, key=_keep_score)
                to_destroy = [o for o in affected if o != keeper]
            for obj_id in to_destroy:
                name, pi = card_lookup.get(obj_id, ('Unknown', 0))
                actions.append({'type': 'legend_rule', 'card_id': obj_id, 'card_name': name,
                                'player_index': pi, 'reason': desc})

        elif stype == SBAType.COUNTERS_CANCEL:
            for obj_id in result.affected_objects:
                name, pi = card_lookup.get(obj_id, ('Unknown', 0))
                # Find the card to get counter amounts
                amount = 0
                for p in game.players:
                    for c in p.battlefield:
                        if c.id == obj_id:
                            amount = min(c.counters.get('+1/+1', 0), c.counters.get('-1/-1', 0))
                            break
                actions.append({'type': 'counter_cancel', 'card_id': obj_id, 'card_name': name,
                                'player_index': pi, 'amount': amount, 'reason': desc})

        elif stype == SBAType.AURA_INVALID:
            for obj_id in result.affected_objects:
                name, pi = card_lookup.get(obj_id, ('Unknown', 0))
                actions.append({'type': 'aura_invalid', 'card_id': obj_id, 'card_name': name,
                                'player_index': pi, 'reason': desc})

        elif stype == SBAType.SAGA_COMPLETE:
            for obj_id in result.affected_objects:
                name, pi = card_lookup.get(obj_id, ('Unknown', 0))
                actions.append({'type': 'saga_complete', 'card_id': obj_id, 'card_name': name,
                                'player_index': pi, 'reason': desc})

        elif stype == SBAType.WORLD_RULE:
            for obj_id in result.affected_objects:
                name, pi = card_lookup.get(obj_id, ('Unknown', 0))
                actions.append({'type': 'world_rule', 'card_id': obj_id, 'card_name': name,
                                'player_index': pi, 'reason': desc})

        elif stype == SBAType.EQUIPMENT_INVALID:
            # 704.5n: Equipment attached to illegal permanent becomes unattached.
            # Covers: target zoned out (exile/graveyard/hand), target is no longer a
            # creature (Humility, type-loss), target was a token that ceased to exist,
            # target was phased out (target not in sba_state.battlefield snapshot).
            for obj_id in result.affected_objects:
                name, pi = card_lookup.get(obj_id, ('Unknown', 0))
                actions.append({'type': 'equipment_invalid', 'card_id': obj_id, 'card_name': name,
                                'player_index': pi, 'reason': desc})

    return actions


def _get_equipment_power_bonus(card, game) -> int:
    """Get total power bonus from attached equipment for SBA toughness checks.
    Mirrors Card._get_equipment_bonuses() but works with game-level find."""
    if not card.attachments or game is None:
        return 0
    bonus = 0
    import re
    for equip_id in card.attachments:
        result = game.find_card_global(equip_id)
        if not result:
            continue
        equip_card = result[0]
        oracle = (getattr(equip_card, 'oracle_text', '') or '').lower()
        pt_match = re.search(r'(?:gets?|has)\s*\+(\d+)/\+(\d+)', oracle)
        if pt_match:
            bonus += int(pt_match.group(1))
    return bonus


def _get_equipment_toughness_bonus(card, game) -> int:
    """Get total toughness bonus from attached equipment for SBA toughness checks.
    Mirrors Card._get_equipment_bonuses() but works with game-level find."""
    if not card.attachments or game is None:
        return 0
    bonus = 0
    import re
    for equip_id in card.attachments:
        result = game.find_card_global(equip_id)
        if not result:
            continue
        equip_card = result[0]
        oracle = (getattr(equip_card, 'oracle_text', '') or '').lower()
        pt_match = re.search(r'(?:gets?|has)\s*\+(\d+)/\+(\d+)', oracle)
        if pt_match:
            bonus += int(pt_match.group(2))
    return bonus


def _compute_pt_for_sba(card, game):
    """Return dict of P/T-related kwargs for SBAPermanent construction.

    For CDA creatures (Tarmogoyf, Mortivore, Maro) whose raw power/toughness
    contains '*', parse_pt_value() returns 0 which causes spurious
    CREATURE_ZERO_TOUGHNESS SBAs. Use get_effective_power/toughness() instead
    and collapse all components into base_* with modifier/counters zeroed out.
    This avoids double-counting since the effective methods already include
    counters, modifiers, and equipment.

    For normal integer-P/T creatures, use the original component breakdown so
    the COUNTERS_CANCEL SBA (CR 704.5q) can still see individual counter counts.
    """
    raw_p = str(card.power or '').strip()
    raw_t = str(card.toughness or '').strip()
    is_cda = '*' in raw_p or '*' in raw_t

    # July 31 batch-11 (brawl reviewer): animate_land stamps
    # _animated_power/_animated_toughness that only get_effective_* read —
    # printed P/T on a land is blank, so this adapter parsed 0/0 and the
    # delegated CR 704.5f check destroyed all six Sylvan Awakening lands the
    # moment they animated (game_1532532200061403350). The May 17 fix
    # patched the inline path; this adapter is the second sibling (the
    # June 10 Death's Shadow shape, again).
    if not is_cda and (getattr(card, '_animated_until_eot', False)
                       or getattr(card, '_animated_permanent', False)):
        is_cda = True

    # Death's Shadow-style dynamic debuff ("gets -X/-X, where X is your life
    # total") prints integer P/T, so the '*' check misses it — but the debuff
    # lives only in get_effective_* (the layers engine skips dynamic
    # magnitudes). Without collapsing here, the delegated SBA checker saw a
    # 13/13 at ANY life total and the zero-toughness death promised by
    # mtg/models.py:_get_life_total_debuff never fired
    # (tests/test_models.py::TestDeathsShadow caught this June 10).
    if not is_cda and game is not None:
        try:
            if card._get_life_total_debuff(game) > 0:
                is_cda = True
        except Exception:
            pass
        try:
            # Integer-P/T cards can still have a dynamic conditional bonus.
            # Serra Ascendant's live 6 toughness exists only through the
            # effective-P/T helpers, so collapse it like a CDA for SBA checks.
            if card._get_life_threshold_bonus(game) != (0, 0):
                is_cda = True
        except Exception:
            pass

    if is_cda and hasattr(card, 'get_effective_power') and hasattr(card, 'get_effective_toughness'):
        # CDA: fold everything into base and zero the rest to avoid double-counting
        try:
            eff_p = card.get_effective_power(game)
        except Exception:
            eff_p = 0
        try:
            eff_t = card.get_effective_toughness(game)
        except Exception:
            eff_t = 0
        return dict(
            base_power=eff_p, base_toughness=eff_t,
            power_modifier=0, toughness_modifier=0,
            plus_counters=0, minus_counters=0,
        )
    else:
        return dict(
            base_power=parse_pt_value(card.power),
            base_toughness=parse_pt_value(card.toughness),
            # [LIVING-WEAPON] Include equipment P/T bonuses in the modifier so
            # the SBA checker sees the correct effective toughness. Without this,
            # 0/0 Germ tokens with equipment (Batterskull +4/+4) are killed by
            # the delegated SBA checker which doesn't know about equipment.
            # [LAYERS] Include _layers_power_mod / _layers_toughness_mod so that
            # anthem-style continuous effects (Massacre Wurm -2/-2, Elesh Norn,
            # Glorious Anthem +1/+1, etc.) are visible to the SBA checker.
            # recalculate_power_toughness() stores these mods on the card object;
            # without them the checker never sees layer-applied P/T changes and
            # creatures that drop to 0 toughness from anthems survive illegally.
            power_modifier=(card.power_modifier
                            + _get_equipment_power_bonus(card, game)
                            + getattr(card, '_layers_power_mod', 0)),
            toughness_modifier=(card.toughness_modifier
                                + _get_equipment_toughness_bonus(card, game)
                                + getattr(card, '_layers_toughness_mod', 0)),
            plus_counters=card.counters.get('+1/+1', 0),
            minus_counters=card.counters.get('-1/-1', 0),
        )


def build_sba_state(game, rules_engine=None):
    """Convert mtg_game GameState into rules/state_based_actions.py format for comparison.

    Args:
        game: mtg_game.py GameState instance
        rules_engine: Optional RulesEngine instance (unused currently, reserved for CDA)
    """
    sba_state = SBAGameState()
    for i, player in enumerate(game.players):
        sba_state.players[str(i)] = SBAPlayer(
            id=str(i), name=player.name, life=player.life,
            poison_counters=player.poison,
            attempted_draw_from_empty=bool(
                getattr(player, 'attempted_draw_from_empty', False)
                or player.name in getattr(game, '_library_loss', set())
            ),
            has_lost=bool(getattr(player, 'eliminated', False)),
        )
        sba_state.library[str(i)] = len(player.library)

    for i, player in enumerate(game.players):
        for card in player.battlefield:
            if getattr(card, '_phased_out', False):
                continue
            tl = getattr(card, 'type_line', '') or ''
            tl_lower = tl.lower()
            perm = SBAPermanent(
                id=card.id, name=card.name,
                controller=str(i), owner=str(getattr(card, 'owner_index', i)),
                is_creature=card.is_creature(game),
                is_planeswalker=card.is_planeswalker(),
                is_artifact=card.is_artifact(),
                is_enchantment=card.is_enchantment(),
                is_land=card.is_land(),
                is_legendary='legendary' in tl_lower,
                # July 23 audit (#14): "Enchant player" Auras (Curses —
                # Curse of Bloodletting et al.) attach to a PLAYER, which this
                # object-only SBA model has no slot for, so they always looked
                # unattached and 704.5m destroyed them the turn they resolved
                # (game_1529677634588377108: a {3}{R}{R} damage doubler with
                # zero effect). CR 704.5m only destroys an Aura attached to an
                # illegal object OR player, or attached to nothing — a player
                # attachment is legal, so exempt curses from the object check.
                # Their replacement effects are already controller-relative
                # (rules/replacement.py "curse of bloodletting"), so nothing
                # downstream needs the attach target.
                is_aura=('aura' in tl_lower
                         and 'enchant player' not in (card.oracle_text or '').lower()),
                is_equipment='equipment' in tl_lower,
                is_saga='saga' in tl_lower,
                is_world='world' in tl_lower and 'enchantment' in tl_lower,
                saga_chapters=_count_saga_chapters(card) if 'saga' in tl_lower else 0,
                **_compute_pt_for_sba(card, game),
                loyalty_counters=getattr(card, 'loyalty_counters', 0),
                lore_counters=card.counters.get('lore', 0),
                damage_marked=card.damage_marked,
                deathtouch_damage=card.deathtouch_damage,
                attached_to=card.attached_to,
                attachments=list(card.attachments),
                enchanting=card.attached_to if 'aura' in tl_lower else None,
                is_token=getattr(card, 'is_token', False),
                # July 24 batch-6 audit (reviewer L1, CRITICAL): without
                # game=, has_keyword skips its Humility branch and reads the
                # PRINTED keyword list — Athreos survived exactly-lethal
                # damage for ~30 turns while Humility should have stripped
                # its Indestructible (CR 613.6 layer 6; 704.5g). The sibling
                # field above (is_creature) already threads game.
                has_indestructible=card.has_keyword('Indestructible', game=game),
            )
            sba_state.battlefield[card.id] = perm

    return sba_state


def compare_with_rules_sba(game, inline_actions):
    """Run rules/state_based_actions.py checker and compare with inline results.

    Logs discrepancies as [SBA-RULES] for diagnostic purposes. No state mutation.

    Args:
        game: mtg_game.py GameState instance
        inline_actions: List[Dict] from check_state_based_actions()
    """
    sba_state = build_sba_state(game)
    checker = RulesSBAChecker()
    rules_results = checker.check(sba_state)

    # Map inline actions to (category, object_id) tuples
    inline_set = set()
    for a in inline_actions:
        atype = a.get('type', '')
        if atype == 'player_loses':
            inline_set.add(('player_loss', str(a.get('player_index', ''))))
        elif atype == 'creature_dies':
            inline_set.add(('creature_death', a.get('card_id', '')))
        elif atype == 'planeswalker_dies':
            inline_set.add(('pw_death', a.get('card_id', '')))
        elif atype == 'counter_cancel':
            inline_set.add(('counter_cancel', a.get('card_id', '')))
        elif atype == 'aura_invalid':
            inline_set.add(('aura_invalid', a.get('card_id', '')))
        elif atype == 'legend_rule':
            inline_set.add(('legend_rule', a.get('card_id', '')))

    # Map rules module results
    rules_set = set()
    for r in rules_results:
        stype = r.sba_type
        for obj_id in r.affected_objects:
            if stype in (SBAType.PLAYER_LOSES_ZERO_LIFE, SBAType.PLAYER_LOSES_POISON,
                         SBAType.PLAYER_LOSES_DRAW_EMPTY):
                rules_set.add(('player_loss', obj_id))
            elif stype in (SBAType.CREATURE_ZERO_TOUGHNESS, SBAType.CREATURE_LETHAL_DAMAGE,
                           SBAType.CREATURE_DEATHTOUCH):
                rules_set.add(('creature_death', obj_id))
            elif stype == SBAType.PLANESWALKER_ZERO_LOYALTY:
                rules_set.add(('pw_death', obj_id))
            elif stype == SBAType.COUNTERS_CANCEL:
                rules_set.add(('counter_cancel', obj_id))
            elif stype == SBAType.AURA_INVALID:
                rules_set.add(('aura_invalid', obj_id))
            elif stype == SBAType.LEGEND_RULE:
                rules_set.add(('legend_rule', obj_id))
            elif stype == SBAType.EQUIPMENT_INVALID:
                rules_set.add(('equipment_invalid', obj_id))

    # Report discrepancies
    only_inline = inline_set - rules_set
    only_rules = rules_set - inline_set

    if only_inline:
        for cat, obj_id in only_inline:
            print(f"[SBA-RULES] INLINE-ONLY: {cat} for {obj_id}")
    if only_rules:
        for cat, obj_id in only_rules:
            print(f"[SBA-RULES] RULES-ONLY: {cat} for {obj_id}")
    if not only_inline and not only_rules and (inline_set or rules_set):
        print(f"[SBA-RULES] OK: {len(inline_set)} SBAs agree")
    elif not only_inline and not only_rules and not inline_set and not rules_set:
        pass  # Both found nothing — normal, no log needed
    # Log summary of what rules module found (for diagnostic visibility)
    if rules_results:
        summary = ', '.join(f"{r.sba_type.name}({len(r.affected_objects)})" for r in rules_results[:5])
        print(f"[SBA-RULES] Rules module found: {summary}")
