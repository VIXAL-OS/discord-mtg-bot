"""Queue items from the Aug 8, 2026 session (R1-R3).

R1 — strategist memo cap 800 → 1000 (two consecutive flash batches ran
     cap_binding=yes at 41.5%/42.7% chopping GOOD content).
R2 — the cog !play path adopts helpers.find_castable_exile_card with
     holder-aware removal/rollback (the Q3 review #9 third-executor gap),
     plus the foretold-marker restore its rollback always lacked.
R3 — the [CONVERGE] tag renamed to [COLORS-SPENT] (it prints for EVERY
     cast that spent colors, not only converge cards — audits kept
     tripping on the name).
"""

import io
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


class TestR1MemoCap:
    def test_cap_sites_agree_at_1000(self):
        src = io.open(_ROOT / "mtg" / "claude_player.py",
                      encoding="utf-8").read()
        assert "return cleaned[:1000]" in src
        assert "memo[:1000]" in src
        assert "_memo_raw_len > 1000" in src
        # The old cap must be fully gone from the PIPELINE (prompt phrasing
        # deliberately stays at ~800 — the model's target is fine).
        assert "cleaned[:800]" not in src
        assert "memo[:800]" not in src


class TestR2CogDraugrAdoption:
    def test_cog_play_uses_the_holder_aware_finder(self):
        src = io.open(_ROOT / "mtg" / "cog.py", encoding="utf-8").read()
        assert "find_castable_exile_card(game, player, actual_card_name)" in src
        # The old own-exile-only CASTABILITY scan must be gone (the one
        # remaining own-exile find_card is the "in exile but can't be
        # played" diagnostic, which is fine own-side).
        assert "if exile_card and is_castable_from_exile(" not in src

    def test_cog_removals_and_rollbacks_are_holder_aware(self):
        src = io.open(_ROOT / "mtg" / "cog.py", encoding="utf-8").read()
        # Two removal sites (land + spell) and two rollback sites must
        # route through the holder; a bare player.exile.remove/append in
        # the !play from_exile flow would re-open the gap.
        assert src.count("(_exile_holder if _exile_holder is not None") >= 4

    def test_cog_rollback_restores_foretell_markers(self):
        # The engine executor has restored _foretold/_face_down on a failed
        # exile cast since the alt-cost wave; the cog (third executor)
        # stripped them, leaving the card face-up, uncastable, and
        # permanently discounted.
        src = io.open(_ROOT / "mtg" / "cog.py", encoding="utf-8").read()
        m = re.search(
            r"If failed and was from exile.*?_cast_via_foretell', False\):"
            r".*?card\._foretold = True.*?card\._face_down = True",
            src, re.S)
        assert m, "cog !play failure rollback must restore foretell markers"

    def test_cog_spends_the_draugr_permission_on_success(self):
        src = io.open(_ROOT / "mtg" / "cog.py", encoding="utf-8").read()
        assert "card._castable_by_player = None" in src
        assert "card._snow_as_any_color = False" in src


class TestR3ColorsSpentTag:
    def test_tag_renamed_and_old_tag_gone(self):
        src = io.open(_ROOT / "mtg" / "spells.py", encoding="utf-8").read()
        assert "[COLORS-SPENT]" in src
        assert "[CONVERGE]" not in src
