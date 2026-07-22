"""July 22, 2026 — reminder text must not be parsed as a cast trigger.

The July 21 batch showed Monastery Swiftspear as 12 of 18
[CAST-TRIGGER-UNHANDLED] tags. Prowess was never actually unhandled — the
dedicated PROWESS block in _check_cast_triggers pumps it correctly. The bug
was that Swiftspear's Prowess REMINDER TEXT ("(Whenever you cast a
noncreature spell, this creature gets +1/+1 until end of turn.)") matched
the generic cast-trigger scanner, whose self-pump handler then deliberately
skips Prowess cards — so the trigger fell through to a wasteful Tier-3
queue. The card got correctly pumped AND redundantly escalated.

Fix: the scanner strips parenthetical reminder text first. Reminder text
only ever restates a keyword; a real un-parenthesized cast trigger (like
Monastery Mentor's token half) still matches.
"""
import asyncio
import contextlib
import io


def _engine():
    from mtg.engine import GameEngine
    return GameEngine(None)


def _cast(engine, game, caster, spell):
    from mtg.triggers import _check_cast_triggers
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(_check_cast_triggers(engine, game, caster, spell))
    return buf.getvalue()


def _bolt(make_card):
    return make_card("Lightning Bolt", type_line="Instant", mana_cost="{R}",
                     cmc=1, oracle_text="Lightning Bolt deals 3 damage to any target.")


class TestReminderTextNotACastTrigger:
    def test_prowess_only_creature_pumps_without_escalating(self, make_game, make_card):
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        swift = make_card(
            "Monastery Swiftspear", type_line="Creature — Human Monk",
            power="1", toughness="2", keywords=["Haste", "Prowess"],
            oracle_text="Haste\nProwess (Whenever you cast a noncreature "
                        "spell, this creature gets +1/+1 until end of turn.)")
        rick.battlefield.append(swift)
        out = _cast(engine, game, rick, _bolt(make_card))
        assert "[PROWESS]" in out, "the dedicated Prowess block must still fire"
        assert "[CAST-TRIGGER-UNHANDLED]" not in out, (
            "a Prowess-only creature must NOT be queued for Tier 3 — its "
            "reminder text is not a distinct triggered ability")
        assert not (getattr(game, "pending_async_triggers", None) or []), (
            "nothing should be queued for async resolution")

    def test_real_unparenthesized_cast_trigger_still_matches(self, make_game, make_card):
        # Monastery Mentor: Prowess (reminder) + a REAL token ability that is
        # not in parentheses. Stripping reminder text must leave the token
        # ability intact.
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        mentor = make_card(
            "Monastery Mentor", type_line="Creature — Human Monk",
            power="2", toughness="2", keywords=["Prowess"],
            oracle_text="Prowess (Whenever you cast a noncreature spell, "
                        "this creature gets +1/+1 until end of turn.)\n"
                        "Whenever you cast a noncreature spell, create a 1/1 "
                        "white Monk creature token with prowess.")
        rick.battlefield.append(mentor)
        out = _cast(engine, game, rick, _bolt(make_card))
        assert "[PROWESS]" in out
        # The token half is a genuine trigger — it must be detected (handled
        # inline or queued), never silently dropped by the reminder strip.
        detected = ("[CAST-TRIGGER]" in out
                    or "[CAST-TRIGGER-UNHANDLED]" in out
                    or bool(getattr(game, "pending_async_triggers", None) or []))
        assert detected, "Mentor's real token ability must survive the strip"

    def test_normal_cast_trigger_unaffected(self, make_game, make_card):
        engine = _engine()
        game = make_game()
        rick = game.players[0]
        talrand = make_card(
            "Talrand, Sky Summoner",
            type_line="Legendary Creature — Merfolk Wizard",
            power="2", toughness="2", keywords=["Flying"],
            oracle_text="Flying\nWhenever you cast an instant or sorcery "
                        "spell, create a 2/2 blue Drake creature token with "
                        "flying.")
        rick.battlefield.append(talrand)
        out = _cast(engine, game, rick, _bolt(make_card))
        detected = ("[CAST-TRIGGER]" in out
                    or bool(getattr(game, "pending_async_triggers", None) or []))
        assert detected, "an un-parenthesized cast trigger must still fire"
