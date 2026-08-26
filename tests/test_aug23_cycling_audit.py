"""Aug 23 cube-FFA audit #2 (game_1541162515814416414, sha=2164380).

One live line — `[AUTOPLAY] Dispatching cycle for Decree of Justice` — was the
first real use of the cycle branch, and it produced three defects at once
plus a display gap.

Decree of Justice, printed:

    Create X 4/4 white Angel creature tokens with flying.
    Cycling {2}{W}
    When you cycle this card, you may pay {X}. If you do, create X 1/1 white
    Soldier creature tokens.

Cycling it with no X paid must draw a card and create NOTHING. What happened
was a 4/4 Angel — the MAIN spell's clause — with no flying, which then could
not block Kokusho, the Evening Star, and the whole event was invisible in
Discord.

The root cause is worth stating separately from the card: the cycle handler
correctly narrows `oracle_text` to the cycling trigger, then passes the BARE
CARD NAME alongside it, and a name-keyed template answers on the name
regardless of the text. That is the Aug 10 Wrenn and Seven class — "a bare
name key matches for EVERY ability of that card" — reappearing in the cycling
path.
"""
import pytest

from rules.effect_templates import get_effect_library


TRIGGER = ("When you cycle this card, you may pay {X}. If you do, create X "
           "1/1 white Soldier creature tokens.")
MAIN = "Create X 4/4 white Angel creature tokens with flying."


def _resolve(lib, name, text, x):
    return lib.resolve_etb(card_name=name, oracle_text=text,
                           controller="Rick", opponent="Claude",
                           game_context={"x_value": x})[0]


class TestTheNameKeyCannotHijackACyclingTrigger:

    def test_the_bare_name_still_answers_the_main_spell(self, lib):
        """Control: the main template must keep working for a real cast."""
        actions = _resolve(lib, "Decree of Justice", MAIN, 2)
        assert [a["action"] for a in actions] == ["create_token"]
        assert actions[0]["name"] == "Angel"

    def test_the_suffix_key_answers_the_cycling_trigger(self, lib):
        """The fix: the cycle path looks up '<name> cycling', which the main
        template cannot answer."""
        actions = _resolve(lib, "Decree of Justice cycling", TRIGGER, 3)
        assert [a["action"] for a in actions] == ["create_token"]
        assert actions[0]["name"] == "Soldier"
        assert actions[0]["power"] == 1 and actions[0]["toughness"] == 1

    def test_cycling_without_paying_x_creates_nothing(self, lib):
        """The live bug, exactly: X unpaid produced a 4/4 Angel."""
        actions = _resolve(lib, "Decree of Justice cycling", TRIGGER, 0)
        assert [a["action"] for a in actions] == ["no_action"]

    def test_an_unmodelled_cycling_trigger_falls_through_to_nothing(self, lib):
        """ADVERSE CONTROL. A card whose cycling trigger the library does not
        model must resolve to nothing, not to that card's main spell."""
        actions = _resolve(lib, "Entreat the Angels cycling",
                           "When you cycle this card, do something exotic.", 0)
        assert not actions or all(a.get("action") == "no_action"
                                  for a in actions)


class TestAngelTokensHaveFlying:

    @pytest.mark.parametrize("card,text", [
        ("Decree of Justice", MAIN),
        ("Entreat the Angels",
         "Create X 4/4 white Angel creature tokens with flying."),
    ])
    def test_the_description_and_the_action_agree(self, lib, card, text):
        """Both templates said "with flying" in their description and granted
        no keywords. A live 4/4 Angel was refused as a blocker against a 5/5
        flier because of it."""
        actions = _resolve(lib, card, text, 2)
        token = actions[0]
        assert "flying" in str(token.get("keywords", "")).lower(), (
            "%s promises flying in its description but grants none" % card)

    def test_a_created_angel_can_actually_block_a_flier(
            self, rules, game, make_card):
        """End to end, because the keywords field is only worth anything if
        the create_token handler reads it and can_block honours it."""
        from mtg.actions import execute_action_on_state
        execute_action_on_state(rules, game, {
            "action": "create_token", "player": game.players[0].name,
            "name": "Angel", "power": 4, "toughness": 4,
            "types": "Creature — Angel", "keywords": "flying", "count": 1,
        })
        angel = next(c for c in game.players[0].battlefield
                     if c.name == "Angel")
        flier = make_card("Kokusho, the Evening Star", type_line="Creature — Dragon",
                          oracle_text="Flying", power=5, toughness=5)
        game.players[1].battlefield.append(flier)

        assert angel.has_keyword("Flying", game=game), \
            "the token was created without the keyword it was given"
        assert angel.can_block(flier, game=game), \
            "a flying Angel must be able to block a flier"


class TestCreateXHonoursZero:
    """"Create X ... tokens" with X=0 creates ZERO. A max(1, ...) floor
    silently minted a free body on every one of these."""

    CARDS = [
        ("Decree of Justice", MAIN, "Angel"),
        ("Secure the Wastes",
         "Create X 1/1 white Warrior creature tokens.", "Warrior"),
        ("Entreat the Angels",
         "Create X 4/4 white Angel creature tokens with flying.", "Angel"),
        ("March of the Multitudes",
         "Create X 1/1 white Soldier creature tokens with lifelink.",
         "Soldier"),
        ("White Sun's Zenith",
         "Create X 2/2 white Cat creature tokens.", "Cat"),
    ]

    @pytest.mark.parametrize("card,text,token", CARDS)
    def test_x_zero_creates_no_tokens(self, lib, card, text, token):
        actions = _resolve(lib, card, text, 0)
        for action in actions:
            if action.get("action") == "create_token":
                assert action.get("count", 1) == 0, (
                    "%s created %s token(s) at X=0"
                    % (card, action.get("count")))

    @pytest.mark.parametrize("card,text,token", CARDS)
    def test_a_real_x_still_creates_that_many(self, lib, card, text, token):
        """ADVERSE CONTROL — removing the floor must not break the normal
        case, and must not quietly zero every one of these spells."""
        actions = _resolve(lib, card, text, 4)
        made = [a for a in actions if a.get("action") == "create_token"]
        assert made, "%s created nothing at X=4" % card
        assert made[0]["count"] == 4, (
            "%s created %s tokens at X=4" % (card, made[0]["count"]))


class TestTheCycleHandlerWiring:
    """The production seam, not just the library: a helper pinned only
    through direct calls is not pinned into production."""

    def _source(self):
        import inspect
        from mtg import actions
        return inspect.getsource(actions)

    def test_the_handler_looks_up_the_suffix_key(self):
        src = self._source()
        assert 'card_name=f"{cycle_card.name} cycling"' in src, (
            "the cycle handler is passing the bare card name again, so the "
            "main spell's template can answer its trigger lookup")

    def test_the_handler_threads_x_under_the_key_templates_read(self):
        """`_cycle_x` alone was a key nothing consumed, so a cycling template
        could never see the X actually paid."""
        src = self._source()
        assert "ctx['x_value'] = int(x_value)" in src

    def test_the_autoplay_branch_sends_its_message(self):
        """The caller uses the returned list only as a truthiness flag, so a
        branch that merely RETURNS its message is invisible in Discord."""
        import inspect
        from mtg import autoplay
        src = inspect.getsource(autoplay)
        idx = src.index("Dispatching cycle for")
        window = src[idx:idx + 900]
        assert "_autoplay_send" in window, (
            "the cycle branch returns its message without sending it")


class TestTheXFloorCannotComeBackAnywhere:
    """The five card-level pins above cover the five templates that HAD the
    floor. They would not notice a SIXTH written tomorrow with the same
    `max(1, ...)` shape — which is how this bug got into five templates in
    the first place. A "create X" spell with X=0 creates zero; pin the class,
    not just the instances.
    """

    def test_no_template_floors_an_x_value_up_to_one(self):
        import re
        from pathlib import Path
        src = Path("rules/effect_templates.py").read_text(encoding="utf-8")

        # Control first: a coverage pin whose pattern matches nothing passes
        # vacuously forever. `x_value` must still appear in the file at all.
        assert "x_value" in src, "the scan target vanished — re-locate it"

        offenders = re.findall(r"max\(\s*1\s*,\s*[^)]*x_value[^)]*\)", src)

        # Three sites survive DELIBERATELY and are recorded rather than
        # silently accepted. They are AMOUNTS, not token counts — Toxic
        # Deluge's -X/-X, Blue Sun's Zenith's draw, and a damage-all — and
        # each carries an author-chosen fallback (3 or 4) that suggests
        # x_value is not reliably threaded for them. Removing their floor
        # would turn "X wasn't parsed" into "do nothing" rather than into
        # the intended default, which is a different bug. Verifying that
        # threading is its own piece of work; until then this is a ratchet,
        # not a clean bill of health.
        KNOWN_AMOUNT_FLOORS = 3
        assert len(offenders) <= KNOWN_AMOUNT_FLOORS, (
            "a new max(1, x_value) floor appeared. If it is a TOKEN COUNT, "
            "remove it — 'create X' with X=0 creates zero. If it is an "
            "amount with a deliberate fallback, verify x_value threading "
            "first and then raise the baseline with the reason. Found %d: %s"
            % (len(offenders), offenders))

    def test_no_token_count_is_floored(self):
        """The half that is fully verified: a token COUNT must never floor."""
        import re
        from pathlib import Path
        src = Path("rules/effect_templates.py").read_text(encoding="utf-8")
        assert '"count"' in src, "the scan target vanished — re-locate it"

        # Scoped to x_value, which is the half actually verified. One more
        # site has the same SHAPE on a different variable — Phylath's
        # "a Plant for each basic land you control", floored with a fallback
        # of 3 — and it is left alone deliberately: whether
        # controller_basic_land_count is reliably threaded is a separate
        # question, and Phylath costs enough that a zero-basic board is
        # close to unreachable. Recorded here so it is a known open item
        # rather than something this pin silently blesses.
        floored = re.findall(r'"count":\s*max\(\s*1\s*,[^}]*x_value[^}]*\)', src)
        assert not floored, (
            "a 'create X tokens' template floors X=0 up to 1, minting a "
            "free body: %s" % floored)

    def test_a_generator_docstring_promising_a_keyword_grants_it(self):
        """The first sweep scanned `description=` fields and missed
        _gen_finale_of_glory entirely, which is a generator with a DOCSTRING.
        Same bug, different shape — so scan that shape too."""
        import ast
        from pathlib import Path
        src = Path("rules/effect_templates.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        KW = ("flying", "trample", "vigilance", "lifelink", "deathtouch",
              "haste", "first strike", "menace", "defender", "hexproof")

        checked = 0
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            doc = (ast.get_docstring(node) or "").lower()
            if not any(k in doc for k in KW):
                continue
            segment = ast.get_source_segment(src, node) or ""
            if '"action": "create_token"' not in segment:
                continue
            checked += 1
            if segment.count('"action": "create_token"') > segment.count('"keywords"'):
                offenders.append(node.name)

        assert checked, "the docstring scan matched nothing — re-locate it"
        assert not offenders, (
            "these generators promise a keyword in their docstring and "
            "create tokens without it: %s" % offenders)


def test_the_cycling_suffix_is_known_to_the_card_name_validator():
    """CI regression: a new suffix convention must teach the validator too.

    The "<name> cycling" key is a SYNTHETIC scheduling key, not a card, so
    tools/validate_card_names.py has to strip it before looking the name up.
    It did not, and the card-names workflow went red on 'decree of justice
    cycling' — a real failure, caught only because CI runs the validator with
    the Scryfall bulk that a unit test cannot afford to download.

    This pin is the cheap half: if someone removes the suffix, the break shows
    up here in seconds instead of on the next push.

    Aug 26: the validator now DERIVES its list from the library's
    SANCTIONED_KEY_SUFFIXES enum (one vocabulary, three consumers — see
    tests/test_aug26_suffix_enum.py for the agreement pins), so this pin
    asserts against the LIVE value rather than a source-text tuple literal —
    strictly stronger, and it survives any future representation change.
    """
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "validate_card_names",
        Path("tools/validate_card_names.py").resolve())
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    suffixes = list(mod.SYNTHETIC_SUFFIXES)
    assert " cycling" in suffixes

    key = "decree of justice cycling"
    stripped = key
    for suffix in suffixes:
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)].strip()
            break
    assert stripped == "decree of justice", (
        "the validator must reduce the scheduling key to the real card name")
