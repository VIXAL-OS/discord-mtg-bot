"""Chat-path prompt caching (Nov 7, 2026).

FORK NOTE: fork-own file. The chat path here is MTGBot._chat_reply, whose
prompt is the persona plus the live game state, so this suite is written
against MTGBot directly rather than shared with any other deployment.

Before this change `_chat_reply` appended the game state to the END of the
system prompt. That kept the persona text first, but Anthropic caching is a
prefix match over tools -> system -> messages, so the system block as a whole
still changed on every move and nothing ahead of the history could be cached.
The game state now rides at the tail, on the newest user turn, and the persona
prompt carries an ephemeral cache_control breakpoint.

Honest caveat, measured with count_tokens on Nov 7, 2026: the persona prompt
is 536 tokens (plain) / 782 (the Ressapanda example) against claude-sonnet-5's
1,024-token minimum cacheable prefix, so on the shipped personas the marker is
inert -- the API ignores it, with no error and no write premium. The structure
is what this suite pins: the prefix is stable, the volatile text still reaches
the model, the stored history is never edited, and cache tokens are priced
correctly whenever they do appear.

No Discord, no network.
"""
import re
from pathlib import Path

import pytest


def _src():
    return (Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")


@pytest.fixture
def append_volatile():
    from bot import MTGBot
    return MTGBot.append_volatile_context


@pytest.fixture
def system_param():
    from bot import MTGBot
    return MTGBot.claude_system_param


class _PersonaHost:
    """Just enough of MTGBot to run the real build_base_prompt."""

    def __init__(self, persona):
        from bot import MTGBot
        self.persona = persona
        for name in ("_persona_intro", "_persona_traits_block",
                     "_persona_mannerisms_block", "build_base_prompt"):
            setattr(self, name, getattr(MTGBot, name).__get__(self))


@pytest.fixture(params=["plain", "ressapanda"])
def persona_host(request):
    from bot import MTGBot
    persona = MTGBot.load_persona.__get__(_PersonaHost({}))(request.param)
    return _PersonaHost(persona)


# --------------------------------------------------------------------------- #
# The stable prefix                                                           #
# --------------------------------------------------------------------------- #

class TestStablePrefix:
    def test_base_prompt_is_byte_identical_across_calls(self, persona_host):
        assert persona_host.build_base_prompt() == persona_host.build_base_prompt()

    def test_base_prompt_carries_no_game_state(self, persona_host):
        prompt = persona_host.build_base_prompt()
        assert "Current MTG Game" not in prompt

    def test_chat_path_no_longer_appends_game_state_to_the_system_prompt(self):
        """The old line, `system_prompt += "...--- Current MTG Game ---..."`
        unconditionally, is what changed the system block on every move."""
        src = _src()
        body = src[src.index("async def _chat_reply"):]
        body = body[:body.index("\n    async def ", 10)]
        assert 'system_prompt += "\\n\\n--- Current MTG Game ---\\n" + game_context' not in body
        # The only remaining append is the guarded no-user-turn fallback.
        assert "if volatile and not landed:" in body


# --------------------------------------------------------------------------- #
# The volatile tail                                                           #
# --------------------------------------------------------------------------- #

class TestAppendVolatileContext:
    def test_lands_on_the_newest_user_turn(self, append_volatile):
        history = [
            {"role": "user", "content": "what should I attack with?"},
            {"role": "assistant", "content": "The 3/3."},
            {"role": "user", "content": "and now?"},
        ]
        out, landed = append_volatile(history, "--- Current MTG Game ---\nTurn 4")
        assert landed is True
        assert out[-1]["content"].startswith("and now?")
        assert "Turn 4" in out[-1]["content"]
        assert out[:-1] == history[:-1]

    def test_never_mutates_the_stored_history(self, append_volatile):
        stored = [{"role": "user", "content": "hello"}]
        snapshot = [dict(m) for m in stored]
        out, _ = append_volatile(stored, "Turn 9")
        assert stored == snapshot
        assert out is not stored and out[-1] is not stored[-1]

    def test_successive_turns_do_not_stack(self, append_volatile):
        stored = [{"role": "user", "content": "hello"}]
        a, _ = append_volatile(stored, "Turn 1")
        b, _ = append_volatile(stored, "Turn 2")
        assert "Turn 1" not in b[-1]["content"]
        assert a[-1]["content"].count("Turn 1") == 1

    def test_list_content_gets_a_trailing_text_block(self, append_volatile):
        stored = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        out, landed = append_volatile(stored, "Turn 2")
        assert landed and len(out[-1]["content"]) == 2
        assert len(stored[0]["content"]) == 1

    def test_falls_back_when_newest_turn_is_not_a_user_turn(self, append_volatile):
        stored = [{"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "hey"}]
        out, landed = append_volatile(stored, "Turn 2")
        assert landed is False and out is stored

    def test_no_volatile_means_no_change(self, append_volatile):
        stored = [{"role": "user", "content": "hi"}]
        out, landed = append_volatile(stored, "")
        assert landed is False and out is stored

    def test_block_says_it_is_not_part_of_the_conversation(self, append_volatile):
        out, _ = append_volatile([{"role": "user", "content": "hi"}], "Turn 2")
        assert "not part of" in out[-1]["content"]


# --------------------------------------------------------------------------- #
# The breakpoint and the call site                                            #
# --------------------------------------------------------------------------- #

class TestBreakpoint:
    def test_marks_an_ephemeral_breakpoint(self, system_param):
        assert system_param("P") == [{
            "type": "text", "text": "P", "cache_control": {"type": "ephemeral"},
        }]

    def test_cache_false_is_a_plain_string(self, system_param):
        assert system_param("P", cache=False) == "P"

    def test_chat_call_uses_the_copy_and_the_breakpoint(self):
        src = _src()
        assert "system=self.claude_system_param(system_prompt, cache=cache_prefix)" in src
        assert "messages=request_messages," in src
        assert "messages=history," not in src

    def test_cache_control_is_set_in_exactly_one_place(self):
        assert _src().count('"cache_control"') == 1


# --------------------------------------------------------------------------- #
# Accounting                                                                  #
# --------------------------------------------------------------------------- #

class _Usage:
    def __init__(self, read=0, write=0):
        self.input_tokens = 10
        self.output_tokens = 5
        self.cache_read_input_tokens = read
        self.cache_creation_input_tokens = write


class _Tracker:
    """Every counter get_cost_summary reads, defaulted to zero."""

    def __init__(self):
        from bot import MTGBot
        self.saved = 0
        self._record_cache_usage = MTGBot._record_cache_usage.__get__(self)
        self.get_cost_summary = MTGBot.get_cost_summary.__get__(self)

    def __getattr__(self, name):
        return 0.0 if name.endswith("cost") else 0

    def _save_persistent_costs(self):
        self.saved += 1


class TestCacheAccounting:
    def test_reads_and_writes_land_in_their_own_buckets(self):
        t = _Tracker()
        t.chat_cache_read_tokens = 0
        t.chat_cache_write_tokens = 0
        t._record_cache_usage(_Usage(read=900, write=100))
        assert (t.chat_cache_read_tokens, t.chat_cache_write_tokens) == (900, 100)
        assert t.saved == 1

    def test_kept_out_of_the_full_rate_input_total(self):
        """get_cost_summary prices `total - mtg_game` at the full Sonnet rate,
        so a cache read added there would bill at 10x its real price."""
        t = _Tracker()
        t.total_input_tokens = 0
        t.chat_cache_read_tokens = 0
        t.chat_cache_write_tokens = 0
        t._record_cache_usage(_Usage(read=5000))
        assert t.total_input_tokens == 0

    def test_uncached_and_missing_fields_change_nothing(self):
        t = _Tracker()
        t.chat_cache_read_tokens = 0
        t._record_cache_usage(_Usage())
        t._record_cache_usage(None)

        class _Bare:
            input_tokens = 1
            output_tokens = 1

        t._record_cache_usage(_Bare())
        assert t.chat_cache_read_tokens == 0 and t.saved == 0

    def test_cost_summary_prices_the_cache_and_counts_it_in_the_total(self):
        from bot import CONFIG
        t = _Tracker()
        t.chat_cache_read_tokens = 9_000
        t.chat_cache_write_tokens = 1_000
        s = t.get_cost_summary()
        expected = (9_000 * CONFIG.sonnet_cache_read_cost_per_million
                    + 1_000 * CONFIG.sonnet_cache_write_cost_per_million) / 1_000_000
        assert "prompt cache: 9,000 read / 1,000 written" in s
        assert f"${expected:.4f}" in s
        total = float(re.search(r"Total: \$([0-9.]+)", s).group(1))
        assert total == pytest.approx(expected, abs=5e-5)

    def test_rates_are_claude_sonnet_5s(self):
        from bot import CONFIG
        assert CONFIG.model_default == "claude-sonnet-5"
        assert CONFIG.sonnet_cache_read_cost_per_million == pytest.approx(0.20)
        assert CONFIG.sonnet_cache_write_cost_per_million == pytest.approx(2.50)

    def test_counters_persist(self):
        src = _src()
        assert '"chat_cache_read_tokens", "chat_cache_write_tokens",' in src
