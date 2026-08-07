"""
Targeting Validation Helpers for mtg_game.py
=============================================

Adapter functions that bridge the Card/Player objects from mtg_game.py
to the TargetingSource/Targetable data classes from rules/targeting.py.

Also provides _spell_requires_targets() and _find_any_valid_target() which
are called from cast_spell_async(), _execute_action(), and
_autoplay_execute_action() to enforce CR 601.2c (spells with targets
can only be cast if legal targets exist).
"""

import re
from typing import Optional

try:
    from rules.targeting import (
        TargetValidator, TargetTextParser, TargetingSource, Targetable,
        TargetRestriction, TargetType, ControllerRestriction, ProtectionAbility
    )
    _HAS_TARGETING_CLASSES = True
except ImportError:
    _HAS_TARGETING_CLASSES = False


def _parse_int_stat(val):
    """Convert Card's str power/toughness to int (handles None, '*', etc.)."""
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


def _friendly_fizzle_reason(target, raw_reason, restriction=None):
    """Convert an internal targeting reason into a player-facing phrase.

    raw_reason comes from TargetValidator.can_target — shapes like
    "X is not a valid target type", "X has wrong controller", etc. We
    rewrite these into something friendlier for Discord while keeping
    the target's name once.
    """
    tname = getattr(target, 'name', str(target))
    reason = raw_reason or ""

    # Determine target's actual type for a clearer hint
    def _type_hint():
        try:
            types = []
            if hasattr(target, 'types'):
                t = target.types
                types = t if isinstance(t, list) else [t] if t else []
            if not types and hasattr(target, 'type_line'):
                tl = (target.type_line or '').lower()
                for keyword in ('creature', 'planeswalker', 'artifact', 'enchantment',
                                'land', 'instant', 'sorcery', 'battle'):
                    if keyword in tl:
                        types.append(keyword)
            if not types and hasattr(target, 'battlefield'):
                return "players are not creatures"
            t_lower = [str(t).lower() for t in types]
            if 'planeswalker' in t_lower:
                return "planeswalkers are not creatures"
            if 'artifact' in t_lower and 'creature' not in t_lower:
                return "artifacts are not creatures"
            if 'enchantment' in t_lower and 'creature' not in t_lower:
                return "enchantments are not creatures"
            if 'land' in t_lower:
                return "lands are not creatures"
        except Exception:
            pass
        return ""

    if "is not a valid target type" in reason:
        hint = _type_hint()
        if hint:
            return f"can't target {tname} ({hint})"
        return f"can't target {tname} (wrong type)"
    if "has wrong controller" in reason:
        return f"can't target {tname} (wrong controller)"
    if "has excluded color" in reason:
        return f"can't target {tname} (protection/color restriction)"
    if "has wrong power/toughness" in reason:
        return f"can't target {tname} (power/toughness restriction)"
    if "has wrong mana value" in reason:
        return f"can't target {tname} (mana value restriction)"
    if "doesn't have required type" in reason:
        return f"can't target {tname} (missing required type)"
    if "doesn't have required keyword" in reason:
        return f"can't target {tname} (missing required keyword)"
    # Strip duplicated target name prefix if present
    clean = reason.replace(f"{tname} ", "") if tname and tname in reason else reason
    return f"{tname} {clean}".strip()


def _card_to_targeting_source(card):
    """Convert a Card to a TargetingSource for the targeting validator."""
    colors = set()
    if card.mana_cost:
        for sym in re.findall(r'\{([^}]+)\}', card.mana_cost):
            for part in sym.split('/'):
                p = part.replace('P', '')
                if p in 'WUBRG':
                    colors.add(p)
    types = set()
    if card.type_line:
        for t in ['instant', 'sorcery', 'creature', 'artifact', 'enchantment', 'planeswalker', 'land']:
            if t in card.type_line.lower():
                types.add(t)
    return TargetingSource(
        id=card.id,
        name=card.name,
        controller="",  # Caller sets this
        colors=colors,
        types=types,
        cmc=card.cmc or 0,
        is_spell=True
    )


def _card_to_targetable(card, ctrl_name, zone="battlefield", game=None):
    """Convert a Card to a Targetable for the targeting validator.

    May 25 audit (F24): pass `game` so devotion-gated Theros gods (Erebos
    isn't a creature unless devotion to black ≥5) report the correct type
    set. The old substring-on-type_line approach treated Erebos as a creature
    regardless of devotion, letting Swords to Plowshares target him at
    devotion=4 in game_1508578146641907722 (CR 115.4 — targeting requires
    legal target type). `game` is optional for backward compat with callers
    that don't have access (graveyard / stack lookups). When None, the bare
    type_line substring check is used as before — that's still correct for
    cards that don't have devotion gating.
    """
    colors = set()
    if card.mana_cost:
        for sym in re.findall(r'\{([^}]+)\}', card.mana_cost):
            for part in sym.split('/'):
                p = part.replace('P', '')
                if p in 'WUBRG':
                    colors.add(p)
    types = set()
    subtypes = set()
    supertypes = set()
    if card.type_line:
        tl = card.type_line.lower()
        for t in ['creature', 'instant', 'sorcery', 'artifact', 'enchantment', 'planeswalker', 'land']:
            if t in tl:
                # F24: for creature type specifically, respect devotion gating
                # when game is provided. Other types (artifact, enchantment,
                # planeswalker, etc.) aren't devotion-gated by any printed card,
                # so the substring check is sufficient.
                if t == 'creature' and game is not None and zone == 'battlefield':
                    if hasattr(card, 'is_creature') and not card.is_creature(game=game):
                        continue
                types.add(t)
        if 'legendary' in tl:
            supertypes.add('legendary')
        if 'basic' in tl:
            supertypes.add('basic')
        dash_idx = card.type_line.find('\u2014')
        if dash_idx < 0:
            dash_idx = card.type_line.find(' - ')
        if dash_idx >= 0:
            for st in card.type_line[dash_idx + 1:].strip().split():
                if st.strip():
                    subtypes.add(st.strip())

    # Coerce to strings — keyword lists occasionally pick up a non-string
    # (e.g. a Card object from a malformed grant_keywords action), and the
    # targeting validator does `{k.lower() for k in keywords}` which then
    # raises `'Card' object has no attribute 'lower'`. Filter defensively.
    kws = {k for k in (set(card.keywords) | set(card.temp_keywords)) if isinstance(k, str)}
    granted = getattr(card, '_granted_keywords', set())
    if granted:
        kws |= {k for k in granted if isinstance(k, str)}

    prot_list = []
    oracle = card.oracle_text or ''
    prot_matches = re.findall(r'protection from (\w+(?:\s+and\s+\w+)?)', oracle.lower())
    cmap = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
    for pt in prot_matches:
        pc = set()
        pq = set()
        for w in pt.split():
            if w in cmap:
                pc.add(cmap[w])
            elif w == 'everything':
                pq.add('everything')
        if pc or pq:
            prot_list.append(ProtectionAbility(from_colors=pc, from_qualities=pq))

    hx = card.has_keyword('hexproof') if hasattr(card, 'has_keyword') else 'Hexproof' in kws
    sh = card.has_keyword('shroud') if hasattr(card, 'has_keyword') else 'Shroud' in kws

    # Ward cost detection — parse "Ward {N}" from oracle text or keywords
    ward_cost = None
    ward_match = re.search(r'ward\s*\{(\d+)\}', oracle.lower())
    if ward_match:
        ward_cost = int(ward_match.group(1))
    elif 'Ward' in kws or card.has_keyword('Ward') if hasattr(card, 'has_keyword') else False:
        ward_cost = 2  # Default ward cost if present but unparsed

    return Targetable(
        id=card.id,
        name=card.name,
        controller=ctrl_name,
        owner=ctrl_name,
        types=types,
        subtypes=subtypes,
        supertypes=supertypes,
        colors=colors,
        power=_parse_int_stat(card.power),
        toughness=_parse_int_stat(card.toughness),
        cmc=card.cmc or 0,
        keywords=kws,
        has_hexproof=hx,
        has_shroud=sh,
        has_ward=ward_cost,
        protection=prot_list,
        is_tapped=getattr(card, 'tapped', False),
        is_attacking=getattr(card, 'attacking', False),
        is_blocking=bool(getattr(card, 'blocking', [])),
        zone=zone
    )


def _player_to_targetable(player):
    """Convert a Player to a Targetable for the targeting validator."""
    hx = False
    for c in player.battlefield:
        o = (c.oracle_text or '').lower()
        if 'you have hexproof' in o or "you can't be the target" in o:
            hx = True
            break
    return Targetable(
        id=player.name,
        name=player.name,
        controller=player.name,
        owner=player.name,
        is_player=True,
        has_hexproof_from_opponents=hx
    )


def _spell_requires_targets(card):
    """True if this instant/sorcery REQUIRES targets to cast (CR 601.2c).

    Excludes creatures (ETB targets are not cast requirements), modal spells
    (may have non-targeted modes), and anything where 'target' only appears
    in reminder text (parentheses).
    """
    if not card.type_line:
        return False
    tl = card.type_line.lower()
    # Only check instants and sorceries
    if 'instant' not in tl and 'sorcery' not in tl:
        return False
    oracle = card.oracle_text or ''
    ol = oracle.lower()
    # Modal spells may have non-targeted modes — but "Choose two target
    # creature cards in your graveyard" (Victimize) is a target COUNT, not a
    # mode choice. July 20 batch-3 audit: the bare substring match let
    # Victimize skip the CR 601.2c gate and get cast 3× with zero legal
    # targets (paid, then fizzled at resolution). Real modal wording puts a
    # dash and mode list after the choose-phrase, never " target" directly.
    if any(p in ol for p in ['choose one', 'choose two', 'choose three', 'choose up to']):
        if not re.search(r'choose (?:one|two|three|up to \w+) target', ol):
            return False
    # Strip reminder text (in parentheses) before checking for "target"
    stripped = re.sub(r'\([^)]*\)', '', ol)
    return 'target' in stripped


def aura_has_legal_target(game, card, caster) -> bool:
    # Existence gate shared by cast validation and actor prompt hints.
    oracle = (getattr(card, 'oracle_text', '') or '').lower()
    type_line = (getattr(card, 'type_line', '') or '').lower()
    if 'aura' not in type_line:
        return True
    # Aug 7 batch audit (backlog item 4): basic-land-subtype auras — Utopia
    # Sprawl's "Enchant Forest" was castable with zero Forests anywhere,
    # paid its mana, and fizzled at the attach step (CR 601.2c). The attach
    # branch honors the subtype; the cast gate didn't exist.
    _lsub = re.search(r'enchant (forest|plains|island|swamp|mountain)\b', oracle)
    if _lsub:
        _sub = _lsub.group(1)
        return any(
            _sub in (permanent.type_line or '').lower() and permanent.is_land()
            for player in game.players
            for permanent in player.battlefield
        )
    if not ('enchant creature' in oracle or 'enchant permanent' in oracle):
        return True

    if 'in a graveyard' in oracle:
        return any(
            grave_card.is_creature(game)
            for player in game.players
            for grave_card in player.graveyard
        )

    creature_only = 'enchant creature' in oracle
    own_only = (
        'enchant creature you control' in oracle
        or 'enchant permanent you control' in oracle
    )
    # Aug 7 (backlog item 4): Daybreak Coronet's "Enchant creature with
    # another Aura attached to it" — the restriction was checked NOWHERE
    # (cast gate treated it as plain enchant-creature; auto-select ignored
    # it). Latent in the aura_equipment deck; never fired live.
    needs_aura_attached = 'with another aura attached' in oracle
    for player in game.players:
        if own_only and player is not caster:
            continue
        for permanent in player.battlefield:
            if permanent.id == card.id:
                continue
            if creature_only and not permanent.is_creature(game):
                continue
            if needs_aura_attached and not _has_aura_attached(game, permanent, exclude_id=card.id):
                continue
            return True
    return False


def _has_aura_attached(game, permanent, exclude_id=None) -> bool:
    """True when at least one Aura (other than exclude_id) is attached to
    the permanent — Daybreak Coronet's enchant restriction."""
    for player in game.players:
        for perm in player.battlefield:
            if (perm.id != exclude_id
                    and getattr(perm, 'attached_to', None) == permanent.id
                    and 'aura' in (getattr(perm, 'type_line', '') or '').lower()):
                return True
    return False


def _find_any_valid_target(game, card, caster_name):
    """Return True if at least one legal target exists for a targeted spell.

    Permissive: returns True on errors or if targeting module is absent.
    Only blocks casts where there are truly ZERO valid targets.
    """
    if not _HAS_TARGETING_CLASSES:
        return True
    try:
        src = _card_to_targeting_source(card)
        src.controller = caster_name

        stripped = re.sub(r'\([^)]*\)', '', (card.oracle_text or '').lower())

        validator = TargetValidator()

        def _restriction_satisfiable(restriction):
            # Check all permanents on the battlefield
            for pl in game.players:
                for perm in pl.battlefield:
                    if getattr(perm, '_phased_out', False):
                        continue
                    t = _card_to_targetable(perm, pl.name, game=game)
                    if validator.can_target(src, t, restriction, caster_name)[0]:
                        return True
            # Check players (for "any target" or "target player" spells)
            if (TargetType.PLAYER in restriction.target_types or
                    TargetType.ANY in restriction.target_types):
                for pl in game.players:
                    t = _player_to_targetable(pl)
                    if validator.can_target(src, t, restriction, caster_name)[0]:
                        return True
            # Check stack for counterspells ("target spell")
            if TargetType.SPELL in restriction.target_types:
                for entry in getattr(game, 'stack', []):
                    if (hasattr(entry, 'card') and entry.card
                            and not getattr(entry, 'countered', False)):
                        t = _card_to_targetable(entry.card, "", zone="stack")
                        t.types.add("spell")
                        if validator.can_target(src, t, restriction, caster_name)[0]:
                            return True
            # Check graveyard for spells targeting cards there
            if restriction.zone == "graveyard":
                for pl in game.players:
                    for gc in pl.graveyard:
                        t = _card_to_targetable(gc, pl.name, zone="graveyard")
                        if validator.can_target(src, t, restriction, caster_name)[0]:
                            return True
            return False

        # July 30 batch-9 reviewer audit: compound-target spells need EVERY
        # mandatory clause satisfiable. The old single re.search captured
        # from the FIRST "target" to sentence end, so Searing Blaze's
        # "target player or planeswalker and 1 damage to target creature
        # that player ... controls" collapsed to {PLAYER, PLANESWALKER} and
        # the mandatory creature clause was silently dropped — advertised
        # castable (and the CR 601.2c gate would have allowed the cast)
        # against a creature-less opponent all game. Split per sentence and
        # per " and " fragment; each fragment containing "target" is its
        # own clause. Also from the same wave: "any target" is always
        # satisfiable (players exist — Comet Storm's "choose any target,"
        # defeated the old `target\s+` capture via the comma and read as
        # unplayable all game), and "up to N target" clauses don't gate the
        # cast (CR 601.2c — mandatory targets only, the July 29 PW rule).
        # July 21's Swan Song whole-phrase capture is preserved: a sentence
        # without " and " is a single fragment captured to sentence end.
        # Searing Blaze has a linked pair: the creature must be controlled
        # by the first target's player/planeswalker controller.
        if card.name.lower() == "searing blaze":
            caster = next((p for p in game.players
                           if p.name == caster_name), None)
            return any(
                p is not caster
                and any(c.is_creature(game) for c in p.battlefield)
                for p in game.players
            )

        clauses = []
        for sentence in re.split(r'[.\n;]', stripped):
            if 'target' not in sentence:
                continue
            for frag in re.split(r'\s+and\s+', sentence):
                if 'target' in frag:
                    clauses.append(frag.strip())
        if not clauses:
            return True  # Can't parse target phrase -> allow
        for clause in clauses:
            if re.search(r'\bany target\b', clause):
                continue  # a player always exists -> satisfiable
            if re.search(r'up to \w+ target', clause):
                continue  # optional targets don't gate the cast
            tm = re.search(r'target\s+([^.\n;]+)', clause)
            if not tm:
                continue  # unparseable fragment -> permissive
            phrase = tm.group(0).strip().rstrip('.,')
            # Qualifier tails confuse the type parser: "target creature THAT
            # player or that planeswalker's controller controls" reads as a
            # PLANESWALKER restriction via the tail. The target TYPE is what
            # precedes the qualifier; dropping it errs permissive (an
            # existence check, not full validation).
            # Aug 2 batch-13 (standard reviewer): "if"/"unless" tails are
            # resolution-time conditions, not targeting restrictions (CR
            # 601.2c) — Prismatic Ending's "target nonland permanent if its
            # mana value is ... to cast this spell" kept the word "spell" in
            # the phrase, parsed as TargetType.SPELL, and read unplayable on
            # every empty stack for the whole game.
            phrase = re.split(r'\s+(?:that|if|unless)\s+', phrase)[0]
            restriction = TargetTextParser.parse(phrase)
            if not _restriction_satisfiable(restriction):
                return False
        return True
    except Exception as e:
        print(f"[TARGETING] Error checking targets for {card.name}: {e}")
        return True


def _validate_target_for_action(game, target_card_or_name, target_owner_or_name,
                                source_card_or_name=None, caster_name=None):
    """Check if target can be targeted by source (hexproof, protection, shroud).

    Accepts Card objects or string names.  When the source is a string name,
    searches the battlefield for the actual card to get color/type info.

    Call forms:
        _validate_target_for_action(game, target_card, owner_player, source_card, caster_name)
        _validate_target_for_action(game, target_card, owner_name_str, source_name_str, caster_name)

    Returns (is_legal, reason).  Permissive on errors — returns (True, "") so
    gameplay isn't blocked by a targeting-module bug.
    """
    if not _HAS_TARGETING_CLASSES:
        return True, ""
    if not source_card_or_name or not caster_name:
        return True, ""
    # Defensive coercion: callers occasionally pass an object that is
    # neither a string nor a Card (e.g. an ability source or tuple). If
    # we can extract a name attribute, treat it as a string source;
    # otherwise punt on validation rather than crash with
    # `'X' object has no attribute 'lower'`.
    if not isinstance(source_card_or_name, str) and not hasattr(source_card_or_name, 'oracle_text'):
        name_attr = getattr(source_card_or_name, 'name', None)
        if isinstance(name_attr, str):
            source_card_or_name = name_attr
        else:
            return True, ""
    try:
        # Build TargetingSource
        if isinstance(source_card_or_name, str):
            # Source is a card name — search battlefield for real card data
            src_name = source_card_or_name
            src = TargetingSource(
                id=src_name, name=src_name,
                controller=caster_name, colors=set(), types=set(), cmc=0, is_spell=True
            )
            for pl in game.players:
                for c in pl.battlefield:
                    if c.name.lower() == src_name.lower():
                        src = _card_to_targeting_source(c)
                        src.controller = caster_name
                        break
        else:
            src = _card_to_targeting_source(source_card_or_name)
            src.controller = caster_name

        # Build Targetable
        if isinstance(target_owner_or_name, str):
            ctrl_name = target_owner_or_name
        else:
            ctrl_name = getattr(target_owner_or_name, 'name', '')

        if isinstance(target_card_or_name, str):
            # Target is a card name — find it on the battlefield.
            # May 14 audit: the original loop only broke the INNER loop, so
            # after target_card_or_name was reassigned to a Card object, the
            # outer-loop's next inner iteration crashed with
            # `'Card' object has no attribute 'lower'`. Use a flag to exit both.
            target_name_lower = target_card_or_name.lower()
            found = False
            for pl in game.players:
                for c in pl.battlefield:
                    if getattr(c, '_phased_out', False):
                        continue
                    if c.name.lower() == target_name_lower:
                        target_card_or_name = c
                        ctrl_name = pl.name
                        found = True
                        break
                if found:
                    break
            if isinstance(target_card_or_name, str):
                return True, ""  # Can't find target card — allow permissively

        # CR 702.26b: a phased-out permanent is treated as though it does not
        # exist, so it cannot be chosen or remain legal as a target.
        if getattr(target_card_or_name, '_phased_out', False):
            return False, f"{getattr(target_card_or_name, 'name', 'Target')} is phased out"

        tgt = _card_to_targetable(target_card_or_name, ctrl_name, game=game)

        # Ward payability check — if target has ward and source is opponent's,
        # check if caster has enough spare mana to pay the ward cost
        if tgt.has_ward is not None and src.controller != tgt.controller:
            ward_cost = tgt.has_ward
            caster_player = None
            for pl in game.players:
                if pl.name == caster_name:
                    caster_player = pl
                    break
            if caster_player:
                spare_mana = caster_player.available_mana() if hasattr(caster_player, 'available_mana') else 0
                src._ward_payable = spare_mana >= ward_cost
            else:
                src._ward_payable = True  # Permissive fallback

        # Parse controller restriction from source card's oracle text
        # Handles "target creature you don't control", "target creature an opponent controls", etc.
        # Default: empty target_types = no type restriction (permissive when we can't parse)
        restriction = TargetRestriction(target_types=set())
        if isinstance(source_card_or_name, str):
            # String source — try to find the card for oracle parsing
            for pl in game.players:
                for c in pl.battlefield:
                    if c.name.lower() == source_card_or_name.lower():
                        parsed = _parse_target_restriction_from_oracle(c)
                        if parsed:
                            restriction = parsed
                        break
        else:
            parsed = _parse_target_restriction_from_oracle(source_card_or_name)
            if parsed:
                restriction = parsed

        validator = TargetValidator()
        legal, reason = validator.can_target(src, tgt, restriction, caster_name)
        return legal, reason
    except Exception as e:
        print(f"[TARGETING] _validate_target_for_action error: {e}")
        return True, ""


def _validate_player_target_for_action(game, target_player, source_card_or_name=None,
                                       caster_name=None):
    """Check if target_player can be targeted by source (Leyline of Sanctity, etc.).

    Accepts Card objects or string names for source.

    Returns (is_legal, reason).  Permissive on errors.
    """
    if not _HAS_TARGETING_CLASSES:
        return True, ""
    if not source_card_or_name or not caster_name:
        return True, ""
    try:
        if isinstance(source_card_or_name, str):
            src = TargetingSource(
                id=source_card_or_name, name=source_card_or_name,
                controller=caster_name, colors=set(), types=set(), cmc=0, is_spell=True
            )
            for pl in game.players:
                for c in pl.battlefield:
                    if c.name.lower() == source_card_or_name.lower():
                        src = _card_to_targeting_source(c)
                        src.controller = caster_name
                        break
        else:
            src = _card_to_targeting_source(source_card_or_name)
            src.controller = caster_name
        tgt = _player_to_targetable(target_player)
        restriction = TargetRestriction(target_types={TargetType.PLAYER})
        if not isinstance(source_card_or_name, str):
            parsed = _parse_target_restriction_from_oracle(
                source_card_or_name)
            if parsed is not None:
                restriction = parsed
        validator = TargetValidator()
        legal, reason = validator.can_target(src, tgt, restriction, caster_name)
        return legal, reason
    except Exception as e:
        print(f"[TARGETING] _validate_player_target_for_action error: {e}")
        return True, ""


def _check_resolution_targets(game, stack_entry):
    """Check if a targeted spell's targets are still valid at resolution time (CR 608.2b).

    If ALL targets are illegal, the spell fizzles (is countered by game rules).
    If SOME targets are still legal, the spell resolves for the remaining targets.

    Returns (should_fizzle: bool, reason: str).
    Permissive: returns (False, "") on errors or if targeting module is absent.
    """
    if not _HAS_TARGETING_CLASSES:
        return False, ""
    try:
        card = stack_entry.card
        if not card:
            return False, ""

        # Only instants/sorceries can fizzle from target loss
        # (Auras also can, but they're handled separately)
        tl = (card.type_line or '').lower()
        if 'instant' not in tl and 'sorcery' not in tl:
            return False, ""

        # Check if this spell requires targets
        if not _spell_requires_targets(card):
            return False, ""

        # The StackEntry stores the target chosen at cast time
        target = stack_entry.target
        if target is None:
            # No target was stored — can't verify, allow resolution
            return False, ""

        caster_name = stack_entry.controller_name
        src = _card_to_targeting_source(card)
        src.controller = caster_name

        # Parse what kind of target this spell expects. Use the shared
        # helper so multi-target spells (Searing Blaze, Ghostly Flicker)
        # get the union of every legal type, not just the first match.
        restriction = _parse_target_restriction_from_oracle(card)
        if restriction is None:
            return False, ""  # Can't parse — allow resolution permissively

        validator = TargetValidator()

        # The target can be a card name (string), a Card object, or a Player object
        if isinstance(target, str):
            # Target is a card name — check if it still exists on the battlefield
            target_found = False
            for pl in game.players:
                for perm in pl.battlefield:
                    if getattr(perm, '_phased_out', False):
                        continue
                    if perm.name.lower() == target.lower() or target.lower() in perm.name.lower():
                        t = _card_to_targetable(perm, pl.name, game=game)
                        legal, reason = validator.can_target(src, t, restriction, caster_name)
                        if legal:
                            return False, ""  # At least one target is still legal
                        target_found = True
                        break
                if target_found:
                    break

            # Also check if it's a player name
            if not target_found:
                for pl in game.players:
                    if pl.name.lower() == target.lower() or target.lower() in pl.name.lower():
                        t = _player_to_targetable(pl)
                        legal, reason = validator.can_target(src, t, restriction, caster_name)
                        if legal:
                            return False, ""
                        target_found = True
                        break

            if target_found:
                # Target was found but is no longer legal
                return True, f"{card.name} fizzles — target is no longer legal"
            else:
                # Target no longer exists on the battlefield (died, exiled, etc.)
                return True, f"{card.name} fizzles — target no longer exists"

        elif hasattr(target, 'battlefield'):
            # Target is a Player object — check if still targetable
            t = _player_to_targetable(target)
            legal, reason = validator.can_target(src, t, restriction, caster_name)
            if not legal:
                friendly = _friendly_fizzle_reason(target, reason, restriction)
                return True, f"{card.name} fizzles — {friendly}"
            return False, ""

        elif hasattr(target, 'name'):
            # Counterspell-style sources target spells on the STACK, not permanents
            # on the battlefield. Search the stack first when the source can target
            # a spell. (CR 115.4 + 608.2b — counter-target spells fizzle only if the
            # target spell is no longer on the stack.)
            targets_a_spell = (
                TargetType.SPELL in restriction.target_types
                or 'counter target' in (card.oracle_text or '').lower()
            )
            if targets_a_spell:
                for entry in getattr(game, 'stack', []):
                    entry_card = getattr(entry, 'card', None)
                    if entry_card is target or (
                        entry_card and getattr(entry_card, 'name', None) == target.name
                        and not getattr(entry, 'countered', False)
                    ):
                        # Target is still on the stack and uncountered — legal.
                        return False, ""
                return True, f"{card.name} fizzles — {target.name} is no longer on the stack"

            # Aug 2 batch-13 (escape/graveyard reviewer): a spell whose LEGAL
            # target lives in a graveyard/exile ("Choose two target creature
            # cards in your graveyard" — Victimize) resolved its target to a
            # Card object at cast time and then this branch, which only ever
            # scanned battlefields, declared it "no longer on the
            # battlefield" — a guaranteed fizzle for the whole class
            # (game_1533284299858514130: Gray Merchant sat untouched in the
            # caster's graveyard). When the source's oracle targets a
            # non-battlefield zone, the target being IN that zone is legal.
            _src_oracle = (card.oracle_text or '').lower()
            _targets_gy = bool(re.search(
                r'target [^.]*card[^.]*\b(?:in|from) (?:a |your |an opponent\'s )?graveyard',
                _src_oracle))
            _targets_exile = 'target' in _src_oracle and 'in exile' in _src_oracle
            if _targets_gy or _targets_exile:
                for pl in game.players:
                    zone = pl.graveyard if _targets_gy else pl.exile
                    if target in zone:
                        return False, ""
            # Otherwise target should be a permanent on the battlefield.
            card_still_on_bf = False
            for pl in game.players:
                if target in pl.battlefield:
                    card_still_on_bf = True
                    if getattr(target, '_phased_out', False):
                        return True, f"{card.name} fizzles — {target.name} is phased out"
                    t = _card_to_targetable(target, pl.name)
                    legal, reason = validator.can_target(src, t, restriction, caster_name)
                    if not legal:
                        friendly = _friendly_fizzle_reason(target, reason, restriction)
                        return True, f"{card.name} fizzles — {friendly}"
                    return False, ""
            if not card_still_on_bf:
                return True, f"{card.name} fizzles — {target.name} is no longer on the battlefield"

        return False, ""
    except Exception as e:
        print(f"[TARGETING] _check_resolution_targets error: {e}")
        return False, ""


def _parse_target_restriction_from_oracle(card):
    """Parse a TargetRestriction from a card's oracle text using TargetTextParser.

    Returns a TargetRestriction if the card has targeting text, or None if it doesn't
    or if the targeting module is unavailable.

    Cards can have multiple target phrases (Searing Blaze: "target player or
    planeswalker and ... target creature ...") or compound types (Ghostly
    Flicker: "two target artifacts, creatures, and/or lands"). The first
    "target X" phrase is the primary restriction, but the union of every
    target-type mentioned across all "target ..." phrases is what we need
    for fizzle checks at resolution: a creature target on Ghostly Flicker
    is legal even though the regex stops at the first comma. Without this
    union, Apr 2026 logs flagged Searing Blaze fizzling on Young Pyromancer
    and Ghostly Flicker fizzling on Mulldrifter — both legal.
    """
    if not _HAS_TARGETING_CLASSES:
        return None
    try:
        oracle = card.oracle_text or ''
        stripped = re.sub(r'\([^)]*\)', '', oracle.lower())
        if 'target' not in stripped:
            return None
        # Aug 2 batch-13 (rashmi/mythic reviewer): "any target" is the modern
        # Oracle templating for creature/player/planeswalker (CR glossary) —
        # the descriptor precedes the word "target", so the primary-phrase
        # capture below starts mid-sentence and picks up garbage instead.
        # July 30 gave _find_any_valid_target (the CAST gate) this
        # recognition; the RESOLUTION-time re-check never got it, so a
        # legally-cast Comet Storm fizzled against a player with "players
        # are not creatures" (game_1533272987539734779).
        if re.search(r'\bany target\b', stripped):
            restriction = TargetRestriction()
            restriction.target_types = {TargetType.CREATURE,
                                        TargetType.PLAYER,
                                        TargetType.PLANESWALKER}
            return restriction
        # Primary restriction (first phrase) — preserves controller, P/T,
        # color, keyword, and zone fields for the fizzle check.
        tm = re.search(r'target\s+([\w\s,/-]+?)(?:\.|;|\band\b|\bor\b|$)', stripped)
        if not tm:
            return None
        restriction = TargetTextParser.parse(tm.group(0).strip().rstrip('.,;'))
        # Sweep for every type-word that appears within ANY "target ..."
        # phrase in the oracle, including those joined by "and/or" or
        # listed with commas. Union them into target_types. We use a
        # broader regex that grabs everything until the next sentence
        # boundary so multi-type lists are captured.
        all_types = set(restriction.target_types or set())
        type_keywords = {
            'creature': TargetType.CREATURE,
            'creatures': TargetType.CREATURE,
            'planeswalker': TargetType.PLANESWALKER,
            'planeswalkers': TargetType.PLANESWALKER,
            'player': TargetType.PLAYER,
            'players': TargetType.PLAYER,
            'opponent': TargetType.PLAYER,
            'opponents': TargetType.PLAYER,
            'artifact': TargetType.ARTIFACT,
            'artifacts': TargetType.ARTIFACT,
            'enchantment': TargetType.ENCHANTMENT,
            'enchantments': TargetType.ENCHANTMENT,
            'land': TargetType.LAND,
            'lands': TargetType.LAND,
            'permanent': TargetType.PERMANENT,
            'permanents': TargetType.PERMANENT,
            'spell': TargetType.SPELL,
            'spells': TargetType.SPELL,
        }
        for phrase_match in re.finditer(r'target\s+([^.;]{1,200})', stripped):
            phrase = phrase_match.group(1)
            # Tokenize on spaces, commas, slashes — capture each word so
            # "artifacts, creatures, and/or lands" yields all three types.
            for word in re.findall(r"[a-z]+", phrase):
                if word in type_keywords:
                    all_types.add(type_keywords[word])
                # Stop scanning after the first conjunction *terminator*
                # (the start of a new clause) so we don't pull in target
                # words from later, unrelated sentences. Keep it loose
                # enough to span "and" / "or" / commas.
                if word in ('then', 'when', 'whenever', 'if', 'unless'):
                    break
        if all_types:
            restriction.target_types = all_types
        return restriction
    except Exception as e:
        print(f"[TARGETING] _parse_target_restriction_from_oracle error: {e}")
        return None
