"""The legal-actions provider — ONE castability/affordability computation.

July 30, 2026. Arena's architecture insight applied: the server computes
the legal-action set and every consumer renders from it. Before this
module, the castable-list computation lived TWICE inside
mtg/claude_player.py (the decide_action and plan_turn prompt builders) —
and they had already diverged: the July 29 split-card fix, the adventure
half, cycling, the token-in-hand skip, and free-cast effects all reached
only the decide_action copy, so plan_turn could not see Commit // Memory
at the lethal moment. The React frontend's websocket layer would have
been a THIRD (fourth counting graveyard logic) divergent copy.

This module is now the single provider:

    castable_entries(game, player, mana_by_color, any_color_mana,
                     total_mana) -> List[dict]

Each entry: {"label": str, "name": str, "zone": str, "action": dict,
"tags": [str]} — `label` is byte-identical to what the old decide_action
builder produced (the AI prompts are built from labels; pins depend on
the exact strings), `action` is the advisory action-dict shape the
executors accept, and the structure is what the frontend will consume.

Downstream stages that deliberately stay in claude_player for now:
`_annotate_castable_with_legality` (appends "[unplayable: ...]" notes)
and the prompt-only hint blocks (fetchlands, suspend, stack-dependence).
The frontend should call the annotate stage on these labels too rather
than growing its own legality copy.
"""
import re
from typing import Dict, List

from mtg.constants import COMMAND_ZONE_FORMATS

try:
    from rules.mana import ManaCost
    HAS_MANA_ENGINE = True
except ImportError:
    HAS_MANA_ENGINE = False
    ManaCost = None


def _check_color_castable(mana_cost_str: str, mana_by_color: dict,
                          any_color_mana: int, total_mana: int) -> bool:
    """Check if a spell with the given mana cost is castable given available mana.

    Uses ManaCost.parse() for proper color requirement extraction (handles
    hybrid, phyrexian, colorless-matters correctly).  Falls back to inline
    {W}/{U}/... counting when the mana engine isn't loaded.

    Args:
        mana_cost_str: Scryfall mana cost string e.g. "{2}{W}{U}"
        mana_by_color: dict of available mana per color {'W': 3, 'U': 1, ...}
        any_color_mana: amount of flexible "any color" mana (Command Tower, etc.)
        total_mana: total mana available (sum of mana_by_color values)
    """
    if not mana_cost_str:
        return True

    if HAS_MANA_ENGINE:
        try:
            parsed = ManaCost.parse(mana_cost_str)
            # Check total mana first (quick reject)
            if parsed.cmc > total_mana:
                return False
            # Check each color requirement, allowing flexible mana to fill gaps
            flexible_remaining = any_color_mana
            for color_enum, needed in parsed.color_requirements.items():
                # Map ManaColor enum to dict key
                color_key = color_enum.value  # 'W', 'U', 'B', 'R', 'G', 'C'
                have = mana_by_color.get(color_key, 0)
                shortfall = needed - have
                if shortfall > 0:
                    if shortfall <= flexible_remaining:
                        flexible_remaining -= shortfall
                    else:
                        return False
            return True
        except Exception:
            pass  # Fall through to inline

    # Inline fallback — same logic used before mana engine was wired in
    cost = mana_cost_str.upper()
    flexible_remaining = any_color_mana
    for color in ['W', 'U', 'B', 'R', 'G']:
        needed = cost.count(f'{{{color}}}')
        have = mana_by_color.get(color, 0)
        shortfall = needed - have
        if shortfall > 0:
            if shortfall <= flexible_remaining:
                flexible_remaining -= shortfall
            else:
                return False
    # Parse CMC inline for total check
    cmc = 0
    for sym in re.findall(r'\{([^}]+)\}', cost):
        if sym.isdigit():
            cmc += int(sym)
        elif sym == 'X':
            pass
        else:
            cmc += 1
    if cmc > total_mana:
        return False
    return True


# [FIX-7] Suspend-only cards (no castable mana cost + have suspend) can't be
# cast normally — they can only be put into exile with suspend. Mox
# Tantalite ({0} mana cost) was causing 13-34 cast failures per game.
SUSPEND_ONLY_CARDS = {
    "Mox Tantalite", "Lotus Bloom", "Ancestral Vision",
    "Living End", "Restore Balance", "Hypergenesis",
}


def _entry(label: str, name: str, zone: str, action: Dict,
           tags: List[str] = None) -> Dict:
    return {"label": label, "name": name, "zone": zone,
            "action": action, "tags": tags or []}


def graveyard_castable_entries(player, mana_by_color: Dict,
                               any_color_mana: int,
                               total_mana: int) -> List[Dict]:
    """Cards castable from the graveyard (flashback, escape, Snapcaster
    grant) + adventure creature halves waiting in exile (CR 715.3).

    Moved verbatim from ClaudePlayer._get_graveyard_castable (July 30) —
    the label strings are unchanged.
    """
    from mtg.helpers import parse_escape_cost
    results = []

    for card in player.graveyard:
        source_tag = None  # Will be set if castable
        cast_cost = None   # Mana cost string to check affordability

        # 1. Snapcaster-granted flashback (playable_from_graveyard list)
        if card.id in player.playable_from_graveyard:
            source_tag = "FLASHBACK from graveyard"
            cast_cost = card.mana_cost  # Same cost as original

        # 2. Native flashback — card has "Flashback {cost}" in oracle text
        elif not card.is_creature() and card.oracle_text:
            fb_match = re.search(r'flashback\s+(\{[^}]+\}(?:\{[^}]+\})*)',
                                 card.oracle_text.lower())
            if fb_match:
                source_tag = "FLASHBACK from graveyard"
                cast_cost = fb_match.group(1).upper()
                # Store flashback cost on card so cast_spell_async uses it
                card._flashback_cost = cast_cost
                # Mark it as playable so _execute_action can find it
                if card.id not in player.playable_from_graveyard:
                    player.playable_from_graveyard.append(card.id)

        # 3. Escape — "Escape—{cost}, exile N other cards from your graveyard"
        if not source_tag and card.oracle_text:
            _escape = parse_escape_cost(card.oracle_text)
            if _escape:
                escape_cost_str, exile_count = _escape
                # Check if enough other cards in graveyard to exile
                other_gy_count = len(player.graveyard) - 1  # Exclude this card
                if other_gy_count >= exile_count:
                    source_tag = f"ESCAPE from graveyard, exile {exile_count}"
                    cast_cost = escape_cost_str
                    # Stash the cost so the payment stage charges the ESCAPE
                    # cost, not the printed one — mirroring the flashback
                    # branch above. Without this an escaped Kroxa would be
                    # charged {B}{R} instead of {2}{B}{B}{R}{R}.
                    card._escape_cost = escape_cost_str
                    if card.id not in player.playable_from_graveyard:
                        player.playable_from_graveyard.append(card.id)

        if source_tag and cast_cost:
            # Check mana affordability (uses ManaCost engine when available)
            if _check_color_castable(cast_cost, mana_by_color,
                                     any_color_mana, total_mana):
                results.append(_entry(
                    f"{card.name} ({cast_cost}) [{source_tag}]",
                    card.name, "graveyard",
                    {"type": "cast", "card": card.name},
                    [source_tag.split(" ")[0]]))

    # Adventure creature halves waiting in exile (CR 715.3). The castable
    # builder scanned hand, command zone, companion zone, free-cast effects
    # and the graveyard — but never exile, so even once the cast gates
    # accepted these the AI was never told they existed and would never
    # propose one.
    for card in getattr(player, 'exile', []) or []:
        if not getattr(card, '_adventure_exiled', False):
            continue
        cost = card.mana_cost or ""
        if _check_color_castable(cost, mana_by_color, any_color_mana,
                                 total_mana):
            results.append(_entry(
                f"{card.name} ({cost}) [CREATURE HALF from exile]",
                card.name, "exile",
                {"type": "cast", "card": card.name},
                ["CREATURE HALF"]))

    # Impulse-exiled cards (Aug 1 — Light Up the Stage's real effect:
    # exile_top_of_library with playable=true marks them on
    # player.playable_from_exile, which BOTH executors already accept as a
    # cast source). end_turn wipes the list, so the live window
    # approximates the printed "until the end of your next turn" — the
    # documented trade at the action handler. Lands are skipped: impulse
    # says "play", but the from-exile path is a CAST path and land-play
    # from exile isn't modeled.
    for card in getattr(player, 'exile', []) or []:
        if card.id not in (getattr(player, 'playable_from_exile', None) or []):
            continue
        if getattr(card, '_adventure_exiled', False):
            continue  # already offered above under its adventure label
        if card.is_land():
            continue
        cost = card.mana_cost or ""
        if cost and _check_color_castable(cost, mana_by_color,
                                          any_color_mana, total_mana):
            results.append(_entry(
                f"{card.name} ({cost}) [IMPULSE from exile]",
                card.name, "exile",
                {"type": "cast", "card": card.name},
                ["IMPULSE"]))

    return results


def castable_entries(game, player, mana_by_color: Dict, any_color_mana: int,
                     total_mana: int) -> List[Dict]:
    """The canonical castability computation (see the module docstring).

    Logic moved verbatim from the decide_action prompt builder — the more
    complete of the two former copies (split halves, adventure halves,
    cycling, token skip, free-cast effects). plan_turn consuming this list
    GAINS those branches, which is the point: the two paths must agree.
    """
    entries: List[Dict] = []
    labels_seen: List[str] = []

    def add(e: Dict):
        entries.append(e)
        labels_seen.append(e["label"])

    player_index = (game.players.index(player)
                    if player in game.players else 0)

    for card in player.hand:
        if card.is_land():
            continue
        oracle_text_lower = (card.oracle_text or '').lower()
        # Suspend-only cards can't be cast from hand — the suspend hint
        # block (claude_player) offers the suspend action instead.
        is_suspend_only = (
            card.name in SUSPEND_ONLY_CARDS
            or (
                (not card.mana_cost or card.mana_cost in ("", "0", "{0}"))
                and 'suspend' in oracle_text_lower
                and 'you may cast' not in oracle_text_lower  # not a free-cast from exile
            )
        )
        if is_suspend_only:
            continue
        # Tokens in hand can't be cast (CR 110.5g — cease to exist in
        # non-battlefield zones)
        if getattr(card, 'is_token', False):
            continue
        # July 29 batch audit: split cards store the COMBINED Scryfall
        # string ("{3}{U} // {4}{U}{U}"), which parses as one 10-CMC cost
        # — Commit // Memory was invisible to the castable list all game
        # while its 4-mana half was affordable. You cast ONE half
        # (CR 709.3): affordable when either half is.
        _cast_cost = card.mana_cost or ""
        if " // " in _cast_cost:
            for _half in _cast_cost.split(" // "):
                if _check_color_castable(_half.strip(), mana_by_color,
                                         any_color_mana, total_mana):
                    add(_entry(f"{card.name} ({card.mana_cost})", card.name,
                               "hand", {"type": "cast", "card": card.name},
                               ["split"]))
                    break
        elif _check_color_castable(_cast_cost, mana_by_color,
                                   any_color_mana, total_mana):
            add(_entry(f"{card.name} ({card.mana_cost})", card.name,
                       "hand", {"type": "cast", "card": card.name}))
        else:
            # Spectacle (CR 702.137, Aug 1): unaffordable at the printed
            # cost but castable for the spectacle cost when an opponent
            # lost life this turn — the pre-gate and cost stage both honor
            # it, so the list must OFFER it or the AI never proposes the
            # cast (the FoW filter lesson).
            from mtg.helpers import spectacle_available
            _spec = spectacle_available(game, player, card)
            if _spec and _check_color_castable(_spec, mana_by_color,
                                               any_color_mana, total_mana):
                add(_entry(
                    f"{card.name} ({_spec}) [SPECTACLE — an opponent lost "
                    f"life this turn]",
                    card.name, "hand",
                    {"type": "cast", "card": card.name},
                    ["spectacle"]))

        # Also check adventure half castability
        if card.adventure_name and card.adventure_cost:
            if _check_color_castable(card.adventure_cost, mana_by_color,
                                     any_color_mana, total_mana):
                add(_entry(
                    f"{card.adventure_name} ({card.adventure_cost}) "
                    f"[adventure of {card.name}]",
                    card.name, "hand",
                    {"type": "cast", "card": card.name,
                     "adventure": card.adventure_name},
                    ["adventure"]))

        # May 20 audit (Bug G): also surface CYCLING as a castable option.
        # Cycling is an activated ability from hand; parse the cost from
        # oracle text and check it against the mana pool.
        if oracle_text_lower and 'cycling' in oracle_text_lower:
            _cyc_m = re.search(r'cycling\s*((?:\{[^}]+\})+)',
                               oracle_text_lower)
            if _cyc_m:
                _cyc_cost = _cyc_m.group(1).upper()
                # X-cost cycling (Shark Typhoon: Cycling {X}{1}{U}):
                # substitute a representative X for the castability check.
                if '{X}' in _cyc_cost:
                    _cyc_check_cost = _cyc_cost.replace('{X}', '{2}')
                else:
                    _cyc_check_cost = _cyc_cost
                if _check_color_castable(_cyc_check_cost, mana_by_color,
                                         any_color_mana, total_mana):
                    add(_entry(
                        f"{card.name} [cycle for {_cyc_cost} — discard to "
                        f"draw a card"
                        f"{' + token via cycling trigger' if 'when you cycle' in oracle_text_lower else ''}]",
                        card.name, "hand",
                        {"type": "cycle", "card": card.name},
                        ["cycling"]))

    # Check command zone for castable commanders
    if player.command_zone and game.format in COMMAND_ZONE_FORMATS:
        for card in player.command_zone:
            if card.mana_cost:
                tax = card.times_cast_from_command_zone * 2
                total_cmd_cost = (card.cmc or 0) + tax
                if total_cmd_cost <= total_mana:
                    # Color check on base cost (tax is generic)
                    if _check_color_castable(card.mana_cost, mana_by_color,
                                             any_color_mana, total_mana):
                        tax_str = f" +{{{tax}}} tax" if tax > 0 else ""
                        # Oathbreaker signature spells live in the command
                        # zone but are only castable while the oathbreaker
                        # is on the battlefield; omit entirely otherwise.
                        if (getattr(card, 'is_signature_spell', False)
                                and game.format == "oathbreaker"):
                            oathbreaker_present = any(
                                getattr(c, 'is_commander', False)
                                and not getattr(c, 'is_signature_spell', False)
                                for c in player.battlefield
                            )
                            if oathbreaker_present:
                                add(_entry(
                                    f"{card.name} ({card.mana_cost}{tax_str}) "
                                    f"[SIGNATURE_SPELL — oathbreaker on battlefield]",
                                    card.name, "command",
                                    {"type": "cast", "card": card.name},
                                    ["signature_spell"]))
                        else:
                            add(_entry(
                                f"{card.name} ({card.mana_cost}{tax_str}) [COMMANDER]",
                                card.name, "command",
                                {"type": "cast", "card": card.name},
                                ["commander"]))

    # Check companion zone — pay {3} to move companion to hand
    if player.companion_zone and total_mana >= 3:
        for card in player.companion_zone:
            add(_entry(f"{card.name} ({{3}} to move to hand) [COMPANION]",
                       card.name, "companion",
                       {"type": "companion", "card": card.name},
                       ["companion"]))

    # Check for free-cast turn effects and add those cards as castable
    free_cast_labels: List[str] = []
    if hasattr(game, 'turn_effects'):
        for te in game.turn_effects:
            if (te.get('type') == 'free_cast'
                    and te.get('controller') == player_index
                    and not te.get('used', False)):
                max_mv = te.get('max_mv', 5)
                source = te.get('source', 'ability')
                for card in player.hand:
                    if not card.is_land() and (card.cmc or 0) <= max_mv:
                        label = f"{card.name} (FREE via {source})"
                        if (label not in free_cast_labels
                                and f"{card.name} ({card.mana_cost})" not in labels_seen):
                            free_cast_labels.append(label)
                            add(_entry(label, card.name, "hand",
                                       {"type": "cast", "card": card.name},
                                       ["free_cast"]))

    # Graveyard (flashback/escape/Snapcaster) + adventure halves in exile
    entries.extend(graveyard_castable_entries(
        player, mana_by_color, any_color_mana, total_mana))

    return entries


def castable_labels(game, player, mana_by_color: Dict, any_color_mana: int,
                    total_mana: int) -> List[str]:
    """The label list the prompt builders consume (exact legacy strings)."""
    return [e["label"] for e in castable_entries(
        game, player, mana_by_color, any_color_mana, total_mana)]
