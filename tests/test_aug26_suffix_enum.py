"""The sanctioned registry-key suffix enum (Aug 26, 2026).

One vocabulary, three consumers — the suffix-key lookup in resolve_etb, the
bespoke construction sites (the cycle handler, the sacrifice-trigger scan),
and the card-names validator's suffix stripping. Each suffix used to be
learned per-consumer one incident at a time ("ltb" missing from the lookup
tuple made two templates doubly dead — Aug 2 I-7; "cycling" broke card-names
CI on Aug 23). These pins are the agreement contract.
"""

import re
from pathlib import Path

from rules.effect_templates import (
    SANCTIONED_KEY_SUFFIXES, SUFFIX_LOOKUP_EVENT_TYPES, get_effect_library,
)

REPO = Path(__file__).resolve().parent.parent


class TestSuffixAgreement:
    def test_lookup_event_types_derive_sanctioned_suffixes(self):
        # The exact I-7 failure shape: an event type whose derived suffix is
        # not sanctioned (or vice versa) means keys registered under it are
        # silently unreachable to one consumer.
        derived = {e.replace("_", "") for e in SUFFIX_LOOKUP_EVENT_TYPES}
        assert derived <= set(SANCTIONED_KEY_SUFFIXES), (
            f"lookup derives unsanctioned suffixes: "
            f"{derived - set(SANCTIONED_KEY_SUFFIXES)}")

    def test_validator_consumes_the_library_enum(self):
        # The validator must DERIVE its strip list from the enum — a private
        # copy is how "cycling" broke CI. Import the module and compare.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "validate_card_names", REPO / "tools" / "validate_card_names.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        expected = tuple(f" {s}" for s in SANCTIONED_KEY_SUFFIXES) + ("_ltb",)
        assert tuple(mod.SYNTHETIC_SUFFIXES) == expected

    def test_every_suffixed_registry_key_uses_a_sanctioned_suffix(self):
        # Scan the live registries: any key whose base (last word stripped)
        # is ITSELF a registered bare key is a suffixed registration — its
        # suffix must come from the enum. This is the drift net for the next
        # ad-hoc suffix invented at a call site.
        lib = get_effect_library()
        all_keys = set()
        for reg in ("_card_templates", "_dies_templates",
                    "_attack_templates", "_upkeep_templates"):
            all_keys |= set(getattr(lib, reg, {}).keys())
        offenders = []
        for key in all_keys:
            if " " not in key:
                continue
            base, _, last = key.rpartition(" ")
            if base in all_keys and last not in SANCTIONED_KEY_SUFFIXES:
                offenders.append(key)
        assert not offenders, (
            f"suffixed keys with unsanctioned suffixes: {offenders} — add "
            f"the suffix to SANCTIONED_KEY_SUFFIXES (one place) or rename")

    def test_scan_control_finds_known_suffixed_keys(self):
        # Scanner control: the offender scan above must actually SEE the
        # known suffixed registrations, or it passes vacuously.
        lib = get_effect_library()
        keys = set(lib._card_templates.keys())
        assert any(k.endswith(" endstep") for k in keys), (
            "no ' endstep' keys found — the scan's premise broke")

    def test_cycle_handler_builds_a_sanctioned_key(self):
        # The bespoke construction site in the cycle handler must use a
        # suffix from the enum (source-level: the site is one f-string).
        src = (REPO / "mtg" / "actions.py").read_text(encoding="utf-8")
        m = re.search(r'card_name=f"\{cycle_card\.name\} (\w+)"', src)
        assert m, "the cycle handler's suffix-key construction site moved"
        assert m.group(1) in SANCTIONED_KEY_SUFFIXES
