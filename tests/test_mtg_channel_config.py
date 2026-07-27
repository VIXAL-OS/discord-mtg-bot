"""Per-guild `mtg_channel_id` (July 26, 2026).

`mtg_channel_id` was a single global int, so auto-respond-without-mention
worked in exactly ONE channel bot-wide — every other server needed an
@-mention. Games themselves are thread-keyed and were always multi-server
safe; this was purely a config-shape limit, and it first bites the day the
bot is invited to a second server, which is what OSS launch invites.

The value now accepts BOTH shapes so existing config.json files keep working
untouched:
    123456789               -> {None: 123456789}   legacy: applies everywhere
    {"<guild_id>": 123456}  -> {<guild_id>: 123456}

The parsing/resolution logic is pulled out as pure functions precisely so it
can be tested without standing up a Discord client.
"""
import pytest

# FORK NOTE: the private repo's bot class is named differently; this file
# is therefore fork-diverged and must be ported as a hunk, never copied
# wholesale from upstream.


def _parse(raw):
    from bot import MTGBot
    return MTGBot._parse_mtg_channels(raw)


class _Resolver:
    """Minimal stand-in exercising the real resolver against a parsed map."""

    def __init__(self, raw):
        from bot import MTGBot
        self.mtg_channel_ids = MTGBot._parse_mtg_channels(raw)
        self.mtg_channel_for = MTGBot.mtg_channel_for.__get__(self)


class TestLegacyScalarStillWorks:
    def test_bare_int_applies_everywhere(self):
        assert _parse(123456789) == {None: 123456789}

    def test_numeric_string_is_accepted(self):
        assert _parse("123456789") == {None: 123456789}

    def test_legacy_value_resolves_in_any_guild(self):
        r = _Resolver(123456789)
        assert r.mtg_channel_for(111) == 123456789
        assert r.mtg_channel_for(222) == 123456789
        assert r.mtg_channel_for(None) == 123456789

    def test_null_disables_auto_respond(self):
        assert _parse(None) == {}
        assert _Resolver(None).mtg_channel_for(111) is None


class TestPerGuildMapping:
    def test_mapping_is_keyed_by_int_guild_id(self):
        assert _parse({"111": 900, "222": 901}) == {111: 900, 222: 901}

    def test_each_guild_gets_its_own_channel(self):
        r = _Resolver({"111": 900, "222": 901})
        assert r.mtg_channel_for(111) == 900
        assert r.mtg_channel_for(222) == 901

    def test_unlisted_guild_has_no_channel(self):
        """The whole point: a second server must not inherit the first's."""
        r = _Resolver({"111": 900})
        assert r.mtg_channel_for(222) is None

    @pytest.mark.parametrize("wildcard", ["*", "default", "", None])
    def test_wildcard_key_is_the_everywhere_fallback(self, wildcard):
        r = _Resolver({wildcard: 999, "111": 900})
        assert r.mtg_channel_for(111) == 900, "specific guild must win"
        assert r.mtg_channel_for(222) == 999, "unlisted guild falls back"

    def test_guild_specific_beats_the_fallback(self):
        r = _Resolver({"*": 999, "111": 900})
        assert r.mtg_channel_for(111) == 900

    def test_dm_context_uses_the_fallback_only(self):
        r = _Resolver({"*": 999, "111": 900})
        assert r.mtg_channel_for(None) == 999
        assert _Resolver({"111": 900}).mtg_channel_for(None) is None


class TestMalformedConfigDoesNotCrashStartup:
    """A typo in one guild's entry must not take the whole bot down."""

    def test_bad_entry_is_skipped_not_raised(self):
        assert _parse({"111": 900, "not-an-id": 901}) == {111: 900}

    def test_bad_channel_value_is_skipped(self):
        assert _parse({"111": "channel-name-not-id"}) == {}

    def test_bad_scalar_is_skipped(self):
        assert _parse("general") == {}

    def test_empty_mapping_is_fine(self):
        assert _parse({}) == {}
