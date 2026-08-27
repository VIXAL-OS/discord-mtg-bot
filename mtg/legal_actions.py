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
from mtg.helpers import (
    is_aluren_free_cast, is_castable_from_exile, library_top_cast_types,
)

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


_ADDITIONAL_SACRIFICE_RE = re.compile(
    r"as an additional cost to cast this spell, sacrifice "
    r"(?:(a|an|one|two|three)\s+)?(creatures?|artifacts?|lands?|permanents?)\b",
    re.IGNORECASE,
)


def additional_sacrifice_requirement(card):
    """Return (count, permanent_type) for a printed sacrifice cost.

    This is deliberately narrow: the current card inventory has Diabolic
    Intent and Shard Volley in this family. Optional/alternative sacrifice
    wording must not be mistaken for a mandatory additional cost.
    """
    match = _ADDITIONAL_SACRIFICE_RE.search(
        getattr(card, "oracle_text", "") or "")
    if not match:
        return None
    number_word = (match.group(1) or "a").lower()
    count = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}[number_word]
    permanent_type = match.group(2).lower().rstrip("s")
    return count, permanent_type


_ADDITIONAL_DISCARD_RE = re.compile(
    # Aug 26 (the taxonomy audit — Tormenting Voice was a pure "draw 2 for
    # {1}{R}", its printed discard never charged). Bulk-swept before the
    # regex: 17x 'a card' + 1x 'two cards' are the plain fixed-count family
    # this accepts; the lookahead structurally DECLINES every other printed
    # shape — 'X cards' (unthreaded X), 'at random' (a different choice
    # model), 'or pay {N}/N life' (modal cost — Lightning Axe stays castable
    # by its mana half), typed forms ('a land card', 'a red or green card'),
    # and compounds ('and sacrifice a creature'). Declined forms keep the
    # historical under-charge rather than bricking the cast — same posture
    # as buyback's unmodeled cost forms.
    r"as an additional cost to cast this spell, discard "
    r"(a|an|one|two|three) cards?(?=[.,\n])",
    re.IGNORECASE,
)


def additional_discard_requirement(card):
    """Return the card count for a plain printed additional-discard cost
    (Tormenting Voice, Cathartic Reunion), or None for every other shape —
    see the regex comment for the declined tail."""
    match = _ADDITIONAL_DISCARD_RE.search(
        getattr(card, "oracle_text", "") or "")
    if not match:
        return None
    number_word = match.group(1).lower()
    return {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}[number_word]


def can_pay_additional_discard(card, player) -> bool:
    """Whether player holds enough OTHER cards to pay card's printed
    additional-discard cost (CR 601.2g — the spell itself is still in hand
    at decision time and cannot pay its own cost). Shared by advertisement
    and the authoritative cast gate, like its sacrifice sibling."""
    requirement = additional_discard_requirement(card)
    if requirement is None:
        return True
    others = sum(1 for c in getattr(player, "hand", []) if c is not card)
    return others >= requirement


def can_pay_additional_sacrifice(card, player, game=None) -> bool:
    """Whether player can pay card's mandatory sacrifice cost.

    Shared by advertisement and the authoritative cast gate so an action
    cannot appear in CASTABLE NOW only to be rejected for missing fodder.
    """
    requirement = additional_sacrifice_requirement(card)
    if requirement is None:
        return True
    count, permanent_type = requirement

    def matches(permanent):
        if permanent is card:
            return False
        if permanent_type == "creature":
            try:
                return permanent.is_creature(game)
            except TypeError:
                return permanent.is_creature()
        if permanent_type == "artifact":
            return permanent.is_artifact()
        if permanent_type == "land":
            return permanent.is_land()
        return permanent_type == "permanent"

    return sum(1 for permanent in player.battlefield if matches(permanent)) >= count


def _entry(label: str, name: str, zone: str, action: Dict,
           tags: List[str] = None) -> Dict:
    return {"label": label, "name": name, "zone": zone,
            "action": action, "tags": tags or []}


def graveyard_castable_entries(player, mana_by_color: Dict,
                               any_color_mana: int,
                               total_mana: int,
                               turn_number: int = None,
                               game=None) -> List[Dict]:
    """Everything playable from the graveyard or exile.

    Graveyard CASTS — flashback, escape, jump-start, aftermath, and the
    Snapcaster grant. Graveyard ACTIVATIONS — embalm, eternalize, unearth
    (their own action type; they are abilities, not casts). Exile — adventure
    creature halves (CR 715.3), foretold cards (CR 702.143b), and impulse
    exiles.

    `turn_number` is optional so existing callers keep working; without it the
    foretell "not the turn you foretold it" gate can't be applied, so those
    offers are made a turn early rather than not at all.
    """
    from mtg.helpers import (aftermath_half_index, has_jump_start,
                             parse_escape_cost, parse_graveyard_activation)
    results = []

    for card in player.graveyard:
        source_tag = None   # Will be set if castable
        cast_cost = None    # Mana cost string to check affordability
        offer_name = card.name  # What the AI must name to cast it
        extra = {}          # Extra keys threaded into the action dict

        # NATIVE mechanics are read off the card FIRST and are authoritative.
        #
        # Aug 3, 2026: the Snapcaster-grant branch used to run first, keyed on
        # `card.id in player.playable_from_graveyard` — but the escape and
        # flashback branches BELOW add the id to that very list. So on the
        # second build of the turn an escape card matched the grant branch and
        # was advertised at its PRINTED cost under a FLASHBACK tag: Cling to
        # Dust offered as "({B}) [FLASHBACK from graveyard]" when escape
        # actually costs {1}{B} and exiles two cards. Membership is now only
        # consulted as the fallback for a card with no native keyword, which
        # is the one case it genuinely means "granted by Snapcaster".

        # 1. Native flashback — "Flashback {cost}"
        if not card.is_creature() and card.oracle_text:
            fb_match = re.search(r'flashback\s+(\{[^}]+\}(?:\{[^}]+\})*)',
                                 card.oracle_text.lower())
            if fb_match:
                source_tag = "FLASHBACK from graveyard"
                cast_cost = fb_match.group(1).upper()
                # Store flashback cost on card so cast_spell_async uses it
                card._flashback_cost = cast_cost

        # 2. Escape — "Escape—{cost}, exile N other cards from your graveyard"
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

        # 3. Jump-start (CR 702.132) — cast from the graveyard by discarding a
        #    card in addition to the printed cost, then exile it. The keyword
        #    prints no cost of its own, so the mana cost is the printed one;
        #    the discard is paid by the executor and needs a card to pitch.
        if not source_tag and card.oracle_text and has_jump_start(card.oracle_text):
            if any(c is not card for c in player.hand):
                source_tag = "JUMP-START from graveyard, discard a card"
                cast_cost = card.mana_cost

        # 4. Aftermath (CR 702.127) — the second half of a split card, and the
        #    ONLY half castable from the graveyard. The AI is offered the HALF
        #    name (Memory, Dawn, Injury), which is what both executors match
        #    on, and priced at that half's own cost.
        if not source_tag:
            _after = aftermath_half_index(card)
            if _after is not None and (card.split_costs or []):
                try:
                    _half_cost = card.split_costs[_after]
                    _half_name = (card.split_names or [])[_after]
                except IndexError:
                    _half_cost = _half_name = None
                if _half_cost and _half_name:
                    source_tag = "AFTERMATH from graveyard"
                    cast_cost = _half_cost
                    offer_name = _half_name
                    # The action names the FULL card (what the executors'
                    # graveyard scan matches) and routes the half through the
                    # existing `adventure` key, which both executors already
                    # map onto cast_as_split_half. The AI also learns the half
                    # name from the label, and both executors additionally
                    # accept it directly — see their graveyard split scans.
                    extra = {"card": card.name, "adventure": _half_name}

        # 5. Snapcaster-granted flashback — the fallback, for a card with no
        #    native graveyard-cast keyword that something put on the list.
        #
        #    It must NOT claim a card whose own branch above DECLINED. Those
        #    branches decline for real reasons — jump-start with an empty
        #    hand (no card to discard), escape without enough graveyard fuel
        #    — and the id is often already on the list from an earlier build,
        #    so without this guard the card came back offered at its PRINTED
        #    cost with a FLASHBACK tag and its actual cost unpaid.
        if not source_tag and card.id in player.playable_from_graveyard:
            _native = (has_jump_start(card.oracle_text or '')
                       or parse_escape_cost(card.oracle_text or '')
                       or aftermath_half_index(card) is not None)
            if not _native:
                source_tag = "FLASHBACK from graveyard"
                cast_cost = card.mana_cost  # Same cost as original

        if source_tag and cast_cost:
            # Check mana affordability (uses ManaCost engine when available)
            if _check_color_castable(cast_cost, mana_by_color,
                                     any_color_mana, total_mana):
                # Mark playable so the executors' graveyard scan can find it.
                # Done HERE, only for offers we actually make, rather than in
                # each detection branch — the branches must stay side-effect
                # free or their own writes feed back into branch 5.
                if card.id not in player.playable_from_graveyard:
                    player.playable_from_graveyard.append(card.id)
                _action = {"type": "cast", "card": offer_name}
                _action.update(extra)
                results.append(_entry(
                    f"{offer_name} ({cast_cost}) [{source_tag}]",
                    offer_name, "graveyard", _action,
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

    # Graveyard-ACTIVATED recursion (Aug 3): embalm, eternalize, unearth.
    # These are activated abilities of a card in the graveyard, not casts
    # (CR 702.87a / 702.129a / 702.83a) — casting them would wrongly fire the
    # whole cast-trigger family — so they get their own action type, which
    # both executors dispatch. Sorcery-speed legality is enforced at
    # activation; the offer only has to be affordable and in the graveyard.
    for card in player.graveyard:
        parsed = parse_graveyard_activation(getattr(card, 'oracle_text', ''))
        if not parsed:
            continue
        mechanic, cost = parsed
        if not _check_color_castable(cost, mana_by_color, any_color_mana,
                                     total_mana):
            continue
        blurb = {
            'embalm': 'exile from graveyard → token copy, a white Zombie',
            'eternalize': 'exile from graveyard → 4/4 black Zombie token copy',
            'unearth': 'return to the battlefield with haste, exiled at end of turn',
        }[mechanic]
        results.append(_entry(
            f"{card.name} ({cost}) [{mechanic.upper()} from graveyard — {blurb}]",
            card.name, "graveyard",
            {"type": "graveyard_activate", "card": card.name,
             "mechanic": mechanic},
            [mechanic.upper()]))

    # Foretold cards (CR 702.143b): exiled face down on an earlier turn, now
    # castable from exile for the foretell cost. Deliberately NOT keyed on
    # player.playable_from_exile — end_turn expires that list, and a foretold
    # card stays castable for the rest of the game (the _adventure_exiled
    # precedent). CR 702.143b forbids casting it the turn it was foretold.
    for card in getattr(player, 'exile', []) or []:
        if not getattr(card, '_foretold', False):
            continue
        cost = getattr(card, '_foretell_cost', '') or ''
        if not cost:
            continue
        if (turn_number is not None
                and getattr(card, '_foretold_turn', None) == turn_number):
            continue
        if _check_color_castable(cost, mana_by_color, any_color_mana,
                                 total_mana):
            results.append(_entry(
                f"{card.name} ({cost}) [FORETOLD — cast from exile]",
                card.name, "exile",
                {"type": "cast", "card": card.name},
                ["FORETOLD"]))

    # Impulse-exiled cards (Aug 1 — Light Up the Stage's real effect:
    # exile_top_of_library with playable=true marks them on
    # player.playable_from_exile, which BOTH executors already accept as a
    # cast source). end_turn wipes the list, so the live window
    # approximates the printed "until the end of your next turn" — the
    # documented trade at the action handler. Lands are skipped: impulse
    # says "play", but the from-exile path is a CAST path and land-play
    # from exile isn't modeled.
    for card in getattr(player, 'exile', []) or []:
        if game is not None:
            castable = is_castable_from_exile(game, player, card)
        else:
            # Backward-compatible pure-provider calls have no GameState and
            # therefore cannot see card-scoped conditional windows.
            castable = card.id in (
                getattr(player, 'playable_from_exile', None) or [])
        if not castable:
            continue
        if (getattr(card, '_adventure_exiled', False)
                or getattr(card, '_foretold', False)):
            continue  # already offered above under its mechanic label
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

    # Aug 7 queue item Q3: Draugr Necromancer's cross-player permission —
    # cards exiled with ice counters sit in the OPPONENT'S exile, which no
    # scan above ever visits. Affordability uses the REAL spending-aware
    # check (can_pay_mana_cost with the card as spending_card), because the
    # snow-as-any-color waiver makes the per-color aggregates here wrong in
    # both directions for these casts.
    if game is not None:
        for other in getattr(game, 'players', None) or []:
            if other is player:
                continue
            for card in getattr(other, 'exile', None) or []:
                if getattr(card, '_castable_by_player', None) != getattr(
                        player, 'name', None):
                    continue
                if not is_castable_from_exile(game, player, card):
                    continue
                cost = card.mana_cost or ""
                try:
                    _ok = player.can_pay_mana_cost(
                        cost, spending_card=card)[0] if cost else True
                except (AttributeError, TypeError):
                    _ok = False
                if _ok:
                    results.append(_entry(
                        f"{card.name} ({cost}) [DRAUGR — cast from "
                        f"opponent's exile, snow pays any color]",
                        card.name, "exile",
                        {"type": "cast", "card": card.name},
                        ["DRAUGR"]))

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

    # Aug 2 batch-13 (standard reviewer): the advertised total double-counts
    # OR-duals (mana_by_color credits EVERY color a dual can produce) — the
    # July 20 one-tap gate fixed the PAYMENT side, and the July 30
    # legal_actions unification preserved the old computation on the
    # ADVERTISEMENT side, so Solitude ({3}{W}{W}) was offered castable off 3
    # physical sources and the cast failed at payment
    # (game_1533284211195252827). Cap the total at the physical one-tap
    # ceiling; the per-color sums stay as the necessary-condition check
    # (never over-rejects — the tap engine remains the arbiter).
    try:
        total_mana = min(total_mana, player.one_tap_mana_total())
    except AttributeError:
        pass  # duck-typed test players without the method keep their claim

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
        # CR 601.2b/g: do not advertise a spell whose mandatory additional
        # sacrifice cannot be paid. The cast gate calls the same predicate.
        if not can_pay_additional_sacrifice(card, player, game):
            continue
        # Aug 26: the discard sibling — never advertise a cast the CR 601.2g
        # gate will refuse for lack of a card to pitch.
        if not can_pay_additional_discard(card, player):
            continue
        # July 29 batch audit: split cards store the COMBINED Scryfall
        # string ("{3}{U} // {4}{U}{U}"), which parses as one 10-CMC cost
        # — Commit // Memory was invisible to the castable list all game
        # while its 4-mana half was affordable. You cast ONE half
        # (CR 709.3): affordable when either half is.
        _cast_cost = card.mana_cost or ""
        _aluren_free = is_aluren_free_cast(game, card)
        if _aluren_free:
            add(_entry(f"{card.name} (FREE via Aluren)", card.name,
                       "hand", {"type": "cast", "card": card.name},
                       ["free_cast", "flash"]))
        elif " // " in _cast_cost:
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

        # Foretell (CR 702.143a): a special action, not a cast — pay {2} to
        # exile the card face down now and cast it for its (usually cheaper)
        # foretell cost on a later turn. Offered whenever {2} is payable and
        # it's this player's turn, which is when the action is legal.
        if oracle_text_lower and 'foretell' in oracle_text_lower:
            from mtg.helpers import parse_foretell
            _ft = parse_foretell(card.oracle_text)
            if (_ft is not None and total_mana >= 2
                    and getattr(game, 'active_player', None) is player):
                add(_entry(
                    f"{card.name} [FORETELL for {{2}} — exile face down, "
                    f"cast later for {_ft}]",
                    card.name, "hand",
                    {"type": "foretell", "card": card.name},
                    ["foretell"]))

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

    # Cast from the TOP OF LIBRARY (Augur of Autumn's coven half, Vizier of
    # the Menagerie, ...). Computed live and never cached: the top card
    # changes on every draw, mill and scry, so a stale offer would name a
    # card that is no longer there.
    #
    # This lives in castable_entries rather than graveyard_castable_entries
    # because the coven condition needs `game` for effective power, and that
    # function takes no game.
    try:
        _top_types = library_top_cast_types(player, game)
    except (AttributeError, TypeError):
        _top_types = set()
    if _top_types and getattr(player, 'library', None):
        _top = player.library[0]
        _is_creature = 'creature' in (getattr(_top, 'type_line', '') or '').lower()
        if _is_creature and 'creature' in _top_types and not _top.is_land():
            _cost = _top.mana_cost or ""
            if _check_color_castable(_cost, mana_by_color, any_color_mana,
                                     total_mana):
                add(_entry(f"{_top.name} ({_cost}) [TOP OF LIBRARY]",
                           _top.name, "library",
                           {"type": "cast", "card": _top.name},
                           ["TOP OF LIBRARY"]))

    # Graveyard casts + graveyard activations + adventure/foretold/impulse
    # cards waiting in exile.
    entries.extend(graveyard_castable_entries(
        player, mana_by_color, any_color_mana, total_mana,
        turn_number=getattr(game, 'turn_number', None), game=game))

    return entries


def castable_labels(game, player, mana_by_color: Dict, any_color_mana: int,
                    total_mana: int) -> List[str]:
    """The label list the prompt builders consume (exact legacy strings)."""
    return [e["label"] for e in castable_entries(
        game, player, mana_by_color, any_color_mana, total_mana)]
