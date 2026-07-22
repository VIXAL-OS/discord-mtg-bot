"""data/card_templates.json — the data-driven half of the Tier 1.5 library.

The loader (rules/effect_templates.py:_load_json_templates) is strict and
runs on every library import, so a malformed edit already fails the whole
suite. These tests make the failure modes legible and pin the substitution
contract end-to-end so "add a card = edit a JSON entry" stays safe.
"""
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JSON_PATH = REPO / "data" / "card_templates.json"

REQUIRED_FIELDS = {"key", "name", "event", "description", "actions"}
OPTIONAL_FIELDS = {"needs_target", "mandatory"}
VALID_EVENTS = {"etb", "dies", "attack"}


@pytest.fixture(scope="module")
def entries():
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    return data["templates"]


class TestSchema:
    def test_file_exists_and_has_templates(self, entries):
        assert isinstance(entries, list) and len(entries) >= 150

    def test_every_entry_is_schema_valid(self, entries):
        seen = set()
        for i, entry in enumerate(entries):
            where = f"templates[{i}] ({entry.get('key', '?')})"
            assert REQUIRED_FIELDS <= set(entry), f"{where}: missing fields"
            unknown = set(entry) - REQUIRED_FIELDS - OPTIONAL_FIELDS
            assert not unknown, f"{where}: unknown fields {unknown}"
            assert entry["event"] in VALID_EVENTS, f"{where}: bad event"
            key = entry["key"]
            assert key == key.lower().strip() and key, f"{where}: bad key"
            assert (entry["event"], key) not in seen, f"{where}: duplicate"
            seen.add((entry["event"], key))
            actions = entry["actions"]
            assert isinstance(actions, list) and actions, f"{where}: empty actions"
            for a in actions:
                assert isinstance(a, dict) and isinstance(a.get("action"), str), \
                    f"{where}: malformed action {a}"

    def test_placeholders_are_the_known_two(self, entries):
        # Catch typos like $contoller before they silently reach a game as
        # a literal player name.
        blob = json.dumps(entries, ensure_ascii=False)
        import re
        for placeholder in set(re.findall(r"\$[a-zA-Z_]+", blob)):
            assert placeholder in ("$controller", "$opponent"), \
                f"unknown placeholder {placeholder}"


class TestLoaderIntegration:
    def test_json_templates_are_registered(self, lib, entries):
        registries = {"etb": lib._card_templates, "dies": lib._dies_templates,
                      "attack": lib._attack_templates}
        for entry in entries:
            assert entry["key"] in registries[entry["event"]], \
                f"{entry['key']} ({entry['event']}) not registered"

    def test_substitution_and_isolation(self, lib, entries):
        # Every entry resolves with both placeholders substituted, and each
        # call returns a FRESH structure (the action interpreter enriches
        # actions in place — a shared master copy would leak state between
        # resolutions).
        registries = {"etb": lib._card_templates, "dies": lib._dies_templates,
                      "attack": lib._attack_templates}
        for entry in entries:
            tmpl = registries[entry["event"]][entry["key"]]
            out1 = tmpl.action_generator("Rick", "Claude", {})
            blob = json.dumps(out1, ensure_ascii=False)
            assert "$controller" not in blob and "$opponent" not in blob, \
                f"{entry['key']}: unsubstituted placeholder"
            out1[0]["_interpreter_enrichment"] = "mutated"
            out2 = tmpl.action_generator("Rick", "Claude", {})
            assert "_interpreter_enrichment" not in out2[0], \
                f"{entry['key']}: shared mutable action state between calls"

    def test_known_migrated_template_resolves_end_to_end(self, lib):
        # Altar of the Brood is a stable migrated etb entry: each opponent
        # mills a card whenever another permanent enters.
        actions, desc = lib.resolve_etb(
            "Altar of the Brood",
            "Whenever another permanent you control enters, each opponent "
            "mills a card.",
            "Rick", "Claude")
        assert actions is not None, desc
        assert any(a.get("action") == "mill" and a.get("player") == "Claude"
                   and a.get("amount") == 1
                   for a in actions)
