"""The coverage skeleton pin (Aug 26, 2026 — the MaRo design-skeleton import).

data/coverage_skeleton.json maps each mechanic to its designated live vehicle
(deck, card). This test is the standing invariant that converts the Aug-9
seeding sweep into structure: every slot's card must (a) exist in the named
deck JSON, (b) be in the card cache, and (c) where a check is declared, be
recognized by the mechanic's parser/classifier. A deck edit that removes a
mechanic's only vehicle now fails HERE instead of surfacing four audit
cycles later (the Shard Volley near-miss). Editing a deck: keep the slot's
card, or move the slot to the new vehicle in the same commit.
"""

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKELETON = REPO / "data" / "coverage_skeleton.json"


def _load_slots():
    data = json.loads(SKELETON.read_text(encoding="utf-8"))
    return data["slots"]


def _deck_names(deck_file):
    data = json.loads((REPO / "data" / deck_file).read_text(encoding="utf-8"))
    names = set()

    def _collect(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("name", "commander", "partner", "signature_spell",
                         "companion") and isinstance(v, str):
                    names.add(v)
                else:
                    _collect(v)
        elif isinstance(obj, list):
            for i in obj:
                _collect(i)
        elif isinstance(obj, str):
            names.add(obj)
    _collect(data)
    return names


@pytest.fixture(scope="module")
def cache():
    return json.loads(
        (REPO / "data" / "card_data_cache.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def slots():
    return _load_slots()


def _cache_entry(cache, card):
    return (cache.get(card.lower())
            or cache.get(card.split(" // ")[0].lower()))


def _oracle_of(entry):
    oracle = entry.get("oracle_text") or ""
    if not oracle and entry.get("card_faces"):
        oracle = "\n".join((f.get("oracle_text") or "")
                           for f in entry["card_faces"])
    return oracle


def _run_check(check, card, entry):
    """The (c) leg. This registry is the PRODUCTION home of the check
    vocabulary (the emitter was a one-shot scratch script)."""
    from mtg import helpers
    from rules.effect_templates import get_effect_library
    oracle = _oracle_of(entry)
    if check.startswith("text:"):
        return check[5:] in oracle.lower()
    if check == "adventure_faces":
        return any("adventure" in (f.get("type_line") or "").lower()
                   for f in entry.get("card_faces") or [])
    if check == "transform_layout":
        return entry.get("layout") in ("transform", "modal_dfc")
    if check == "tier15":
        return get_effect_library().tier_for_card(card, oracle) in (
            "template", "pattern")
    fn = getattr(helpers, check, None)
    assert fn is not None, f"unknown checker id {check!r}"
    result = fn(oracle)
    return result is not None and result is not False


class TestSkeletonStructure:
    def test_skeleton_is_substantial(self, slots):
        # Scanner control: an empty or truncated skeleton must fail loudly —
        # a coverage pin whose scan returns nothing passes vacuously forever.
        assert len(slots) >= 50
        mechs = [s["mechanic"] for s in slots]
        assert len(mechs) == len(set(mechs)), "duplicate mechanic slots"

    def test_collector_control(self):
        # The deck-name collector must actually find names (a broken
        # collector would make every membership test vacuously fail — but a
        # SILENT format change could also make it return partial sets).
        names = _deck_names("test_aristocrats_korvold.json")
        assert "Korvold, Fae-Cursed King" in names
        assert len(names) > 50

    def test_every_check_id_is_known(self, slots, cache):
        for s in slots:
            if s.get("check"):
                entry = _cache_entry(cache, s["card"])
                assert entry is not None, s
                # unknown ids raise inside _run_check
                _run_check(s["check"], s["card"], entry)


class TestEverySlot:
    def test_membership_cache_and_parser(self, slots, cache):
        failures = []
        for s in slots:
            deck_path = REPO / "data" / s["deck"]
            if not deck_path.exists():
                failures.append(f"{s['mechanic']}: deck {s['deck']} missing")
                continue
            if s["card"] not in _deck_names(s["deck"]):
                failures.append(
                    f"{s['mechanic']}: {s['card']} not in {s['deck']} — a "
                    f"deck edit removed a mechanic's designated vehicle; "
                    f"move the slot or restore the card IN THIS COMMIT")
                continue
            entry = _cache_entry(cache, s["card"])
            if entry is None:
                failures.append(f"{s['mechanic']}: {s['card']} not in cache")
                continue
            if s.get("check") and not _run_check(s["check"], s["card"], entry):
                failures.append(
                    f"{s['mechanic']}: {s['card']} fails check "
                    f"{s['check']!r} — the parser no longer recognizes the "
                    f"designated vehicle (Oracle drift, or the parser "
                    f"regressed)")
        assert not failures, "\n".join(failures)

    def test_the_five_aug26_seeds_are_present(self, slots):
        # The seeds this skeleton shipped with — a targeted safety net over
        # the newest, least-established slots.
        by_mech = {s["mechanic"]: s for s in slots}
        assert by_mech["temp_control"]["card"] == "Act of Treason"
        assert by_mech["frozen_tap"]["card"] == "Frost Lynx"
        assert by_mech["qualified_destroy_keyword"]["card"] == "Plummet"
        assert by_mech["qualified_destroy_power"]["card"] == "Smite the Monstrous"
        assert by_mech["annihilator"]["card"] == "Ulamog's Crusher"
