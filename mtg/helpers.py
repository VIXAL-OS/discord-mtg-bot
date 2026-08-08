"""Module-level helper functions shared between GameEngine and MTGGameCog.

These were originally top-level functions in mtg_game.py used across the
GameEngine class and the Discord cog. Extracted here so engine.py and
cog.py can both import them without round-tripping through mtg_game.

Helpers:

    get_mdfc_info — Look up MDFC pathway face data by card name.
    _normalize_pw_ability_idx — Convert AI-provided PW ability index/cost
        string into a canonical int index.
    _resolve_player_or_card_target — Resolve an AI-provided target string
        ("opponent", "you", a player name, or a card name) to the actual
        Player or Card object.
    _collapse_repeated_life_gain — Collapse consecutive identical life-gain
        messages into one line with a multiplier (Soul Warden cascade).
    _should_emit_resolve_hint — Whether to emit a !judge/!resolve hint
        for an effect (always in human play, once per effect in autoplay).
    sanitize_oracle_for_display — Flatten newlines, drop reminder text,
        and truncate oracle text for safe Discord italic-formatted display.

Extracted from mtg_game.py during the Phase 1 OSS-readability refactor.
"""

import re

from mtg.constants import MDFC_PATHWAYS, Zone


def normalize_card_name(name) -> str:
    """Canonical form for card-name comparison: lowercased, whitespace-trimmed.

    Deliberately does NOT strip apostrophes or punctuation — "Cathars' Crusade"
    vs "Cathar's Crusade" being different strings is the point (the May 17
    apostrophe bug class is caught by tools/validate_card_names.py, not
    papered over here).
    """
    return (name or "").strip().lower()


def response_text(response) -> str:
    """Joined text blocks of an Anthropic-style API response.

    Claude 5 models (claude-sonnet-5 is the bot's live default) may lead
    response.content with thinking blocks that carry no .text attribute, so
    the old `response.content[0].text` idiom raises AttributeError
    ('ThinkingBlock' object has no attribute 'text' — 20 games in the
    July 16 batch after the parallel client-restore race flipped them onto
    the Anthropic client). Same semantics as bot._extract_anthropic_text
    (which mtg/ can't import without inverting the dependency direction):
    concatenate every string .text block, skipping thinking blocks.
    DeepSeek/OpenRouter adapter responses always have exactly one text
    block, so this is behavior-identical for them.
    """
    return "".join(
        text for text in (
            getattr(block, 'text', None)
            for block in (getattr(response, 'content', None) or [])
        )
        if isinstance(text, str)
    )


def sanitize_action_bullets(actions):
    # Strip engine-internal diagnostics from player-facing action bullets.
    if not actions:
        return actions
    debug_prefixes = (
        "[PLAN-VALIDATE]", "[EXECUTE]", "[RESOLVE]", "[DEBUG]",
        "[ETB-", "[TRIGGER-", "[XMAGE", "[SPELL_RESOLVER]",
        "[SEMANTIC]", "[ACCUMULATOR]", "[OPP-CAST-TRIGGER]",
        "[LANDFALL]", "[COMBAT]", "[DAMAGE-PREVENTED]",
        "[AUTO-DRAFT]", "[DRAFT-CLAUDE]", "[AI-RESOLVE]",
        "[AUTOPLAY-JUDGE]", "[AUTOPLAY]", "[STRIP]", "[JUDGE-FIX]",
        "[EARLY-CAST]",
    )
    exception_markers = (
        "Traceback (most recent", " at 0x",
        "KeyError:", "AttributeError:", "TypeError:", "ValueError:",
        "IndexError:", "NameError:", "RuntimeError:", "AssertionError:",
    )
    cleaned = []
    for action in actions:
        if not action:
            continue
        text = str(action).strip()
        if not text or any(text.startswith(p) for p in debug_prefixes):
            continue
        if any(marker in text for marker in exception_markers):
            continue
        cleaned.append(action)
    return cleaned


def names_match(a, b) -> bool:
    """Exact normalized card-name equality.

    Use this instead of substring checks (`'painter' in card.name.lower()`)
    when deciding whether a permanent IS a specific card — the substring
    pattern caused the Coldsteel Heart → Painter's Servant misfire (May 17
    audit). Substring matching remains fine for oracle-TEXT pattern scans;
    this is for card-NAME identity only. Migration policy: convert loose
    name checks to names_match whenever you touch the surrounding code.
    """
    na, nb = normalize_card_name(a), normalize_card_name(b)
    return bool(na) and na == nb


def format_activate_line(card_name: str, loyalty_cost, ability_text: str,
                         game=None, max_chars: int = 300) -> str:
    """Format a planeswalker activation header with oracle-text dedup.

    May 18 audit: Aminatou's "+1: Draw a card, then put a card from your hand
    on top of your library" was printed 5 times in one game's discord log
    across 8 activations. The `_oracle_shown_keys` dedup that format_trigger_line
    uses was bypassed because PW activations build their own header in
    mtg/cog.py:_activate_planeswalker.

    First emission per `(card_name, ability_text)` pair shows the full oracle
    text. Subsequent emissions show just the loyalty change header.

    Args:
        card_name: planeswalker card name as it should appear
        loyalty_cost: signed int loyalty change (e.g. +1, -3, 0)
        ability_text: raw oracle paragraph for this ability
        game: optional GameState for cross-emission dedup
        max_chars: cap for sanitized inline text
    """
    sign = '+' if (isinstance(loyalty_cost, int) and loyalty_cost > 0) else ''
    bracket = f"[{sign}{loyalty_cost}]"
    sanitized = sanitize_oracle_for_display(ability_text or '', max_chars=max_chars) if ability_text else ''
    if game is not None and sanitized:
        try:
            shown = game._oracle_shown_keys
        except AttributeError:
            shown = set()
            game._oracle_shown_keys = shown
        # Key on (card, ability bracket, first 60 chars of oracle) so the
        # +1 and -3 abilities of the same PW each get their own first-emission
        # full-text print, but a re-fire of the same ability dedups.
        key = (card_name, bracket, sanitized[:60])
        if key in shown:
            # Repeat activation: show a short reminder instead of nothing.
            # June 11 audit: 192/313 PW activations displayed as a bare
            # "[+2] ability" — players read that as an ability with no text.
            # July 20: truncate at a word boundary — the raw [:69] slice
            # produced mid-word cuts like "…as though they had …" / "Un…"
            # on every repeat activation in the July 12+16 logs.
            if len(sanitized) > 72:
                snippet = sanitized[:69]
                if ' ' in snippet[40:]:
                    snippet = snippet.rsplit(' ', 1)[0]
                return f"⚡ **{card_name}** activates {bracket} ability: _{snippet}…_"
            return f"⚡ **{card_name}** activates {bracket} ability: _{sanitized}_"
        shown.add(key)
    if sanitized:
        return f"⚡ **{card_name}** activates {bracket} ability: _{sanitized}_"
    return f"⚡ **{card_name}** activates {bracket} ability"


def format_trigger_line(emoji: str, source_name: str, trigger_text: str,
                        game=None, max_chars: int = 300, suffix: str = "") -> str:
    """Format a `⚡ **Source** triggers: *oracle*` line, deduping oracle text.

    The first time a given (source_name, trigger_text) pair is shown in a
    game, this emits the full sanitized oracle text. Subsequent times, it
    emits only `⚡ **Source** triggers{suffix}` — saving readers from
    re-reading the same 200-char paragraph 21 times (Aminatou / Emeria /
    Thassa / Soulherder are the worst offenders).

    The dedup is per-game when `game` is provided (uses
    `game._oracle_shown_keys`). With `game=None` the line always shows the
    full text (caller is responsible for any dedup).

    Args:
        emoji: leading glyph, e.g. "⚡" or "📍" or "💀"
        source_name: card name as it should appear bolded
        trigger_text: raw oracle paragraph to embed
        game: optional GameState for cross-emission dedup
        max_chars: cap for sanitized inline text
        suffix: extra trailing text (e.g. " — Rick draws a card")
    """
    sanitized = sanitize_oracle_for_display(trigger_text, max_chars=max_chars)
    if game is not None:
        try:
            shown = game._oracle_shown_keys
        except AttributeError:
            shown = set()
            game._oracle_shown_keys = shown
        # Key on (source, first 60 chars of trigger) to dedup the COMMON case
        # of the same ability text being re-quoted while still keeping
        # different abilities on the same source distinguishable.
        key = (source_name, sanitized[:60])
        if key in shown:
            return f"{emoji} **{source_name}** triggers{suffix}"
        shown.add(key)
    return f"{emoji} **{source_name}** triggers: *{sanitized}*{suffix}"


def strip_dangling_articles(text: str) -> str:
    """Clean up "The .", "A .", "An ." artifacts left by aggressive regex
    strippers elsewhere in the pipeline (the "strategically best target"
    truncation in mtg/judge.py is the main offender). Handles three forms:

    - Sentence-start: "...graveyard. The . However, ..." → "...graveyard. However, ..."
    - Mid-clause: "X is destroyed. A . Then Y" → "X is destroyed. Then Y"
    - Trailing: "X resolves. The" → "X resolves."

    Safe to call on any user-facing text. Idempotent."""
    if not text:
        return text
    out = text
    # Case 1: dangling article followed by period + continuation (most common)
    out = re.sub(
        r'(?:^|(?<=[.!?]))\s*\b(?:The|A|An)\s*[.!?]\s+(?=(?:[A-Z]|However|But|And|Then|So|Therefore|Thus|Yet))',
        ' ',
        out,
    )
    # Case 2: dangling article with NO period before next sentence start
    out = re.sub(
        r'(?:^|(?<=[.!?]))\s*\b(?:The|A|An)\s+(?=[A-Z])',
        '',
        out,
        count=0,  # Apply all matches
    )
    # Wait — case 2 is too aggressive (would eat "The Restoration of Eiganjo").
    # Revert and use a more conservative form: only catch when followed by
    # ANOTHER capitalized word that ISN'T the start of a card name. Better:
    # only catch in conjunction with the period form below.
    out = text
    # Re-apply only case 1, plus tail trimming.
    out = re.sub(
        r'(?:^|(?<=[.!?]))\s*\b(?:The|A|An)\s*[.!?]\s+(?=(?:[A-Z]|However|But|And|Then|So|Therefore|Thus|Yet))',
        ' ',
        out,
    )
    # Tail: text ending with " The" / " A" / " An" with no continuation
    out = re.sub(r'\s+\b(?:The|A|An)\s*$', '', out).rstrip()
    # Collapse the leftover "  ." / orphan period.
    out = re.sub(r'\s+\.', '.', out)
    # Collapse multiple spaces left by the substitutions.
    out = re.sub(r'  +', ' ', out)
    return out


def sanitize_judge_prose(text: str) -> str:
    """Strip judge/scratchpad scaffolding from a ruling/explanation string.

    Catches the May 20 audit failure modes that the existing scrubbers in
    mtg/judge.py missed:

    1. Header-shaped lines like ``ACTIONS:``, ``RULING:``, ``Applied changes:``
       that leak from the prompt template when the model paraphrases section
       headers as content (game_1506618495738052648_discord.log:220-226).
    2. Conjunction-leading sentences ("Since...", "But...", "However...",
       "Note that...") that survive when the hedge-stripper above eats their
       subject and leaves an orphan dependent clause
       (game_1506608500023754882_discord.log:264 "but no specific card is named").
    3. Parenthetical fragments with no matching open paren (e.g.
       "controller mills 3). Since the trigger..." in
       game_1506608500044857375_discord.log:263-264 — the "controller mills 3)"
       fragment is the AI's scratchpad bleeding through).
    4. Post-strip subject validation: if what remains starts mid-sentence
       (conjunction or lowercase word with no leading noun/pronoun), the
       strippers ate the subject and we should fall back to a generic
       "Effect resolved" message.

    Returns the cleaned text. Caller decides what to do with empty output.
    """
    if not text:
        return ""

    # Split into lines, drop header-shaped scaffolding lines.
    lines = text.splitlines()
    keep_lines = []
    header_re = re.compile(
        r'^\s*(?:ACTIONS|RULING|Applied\s+changes|Mechanical\s+steps|'
        r'Relevant\s+rules|Resolution\s+order|Player\s+choices|'
        r'Order\s+of\s+resolution|Reasoning|Analysis|Note)\s*:',
        re.IGNORECASE,
    )
    for ln in lines:
        if header_re.search(ln):
            continue
        keep_lines.append(ln)
    text = '\n'.join(keep_lines).strip()
    if not text:
        return ""

    # Drop unmatched-close-paren fragments: e.g. "Word word word). Rest of
    # sentence." has a `)` with no matching `(` earlier. Splice out the
    # leading fragment up to and including the orphan `)`.
    while True:
        m = re.search(r'^(.*?)\)', text, re.DOTALL)
        if not m:
            break
        # Count open vs close parens in m.group(1) — if there are MORE
        # closes than opens encountered so far, this is an orphan close.
        prefix = m.group(1)
        opens = prefix.count('(')
        closes = prefix.count(')')
        if opens > closes:
            break  # The `)` is matched; not an orphan
        # Orphan close: drop everything up to and including the `)`.
        text = text[m.end():].lstrip(' .,;')
        if not text:
            return ""

    # Drop sentence-starting conjunctions when they're orphan dependent
    # clauses (the hedge-stripper above sometimes leaves these). Pattern:
    # "But/However/Since/Note that/Therefore..." as the FIRST word followed
    # by lowercase prose. We strip the whole leading clause up to the next
    # sentence boundary.
    text = re.sub(
        r'^(?:But|However|Therefore|Thus|So|Since|Note\s+that|Also|And)\b[^.!?]*[.!?]\s*',
        '',
        text,
        flags=re.IGNORECASE,
    ).strip()

    if not text:
        return ""

    # Subject validation: the residue should start with either a capital
    # noun/pronoun, a card-name-like token, or a recognized opening verb
    # (e.g. "Exile X", "Destroy Y"). If it starts lowercase OR with a
    # bare verb-fragment lacking a subject ("then shuffles", "draws a
    # card"), return empty so the caller can fall back to a clean message.
    first_word_m = re.match(r'^\s*([A-Za-z]+)', text)
    if first_word_m:
        first = first_word_m.group(1)
        # Lowercase first word = mid-sentence fragment.
        if first[0].islower():
            return ""
        # Recognized clause-starters that lack a subject. These survive the
        # conjunction-strip above when the next sentence boundary wasn't
        # found (single-sentence fragment).
        BAREVERB_PREFIXES = {
            "then", "now", "next", "first", "second", "third", "finally",
        }
        if first.lower() in BAREVERB_PREFIXES:
            # Need to check if the NEXT word is a verb or a subject.
            rest = re.match(r'^\s*\w+\s+(\w+)', text)
            if rest and rest.group(1).lower() in {
                'shuffles', 'shuffle', 'draws', 'draw', 'casts', 'cast',
                'destroys', 'destroy', 'exiles', 'exile', 'gains', 'gain',
                'loses', 'lose', 'taps', 'tap', 'sacrifices', 'sacrifice',
                'returns', 'return', 'enters', 'enter',
            }:
                # Bare verb fragment with no subject — drop.
                return ""

    return text


def sanitize_oracle_for_display(text: str, max_chars: int = 300) -> str:
    """Make oracle text safe to embed in a Discord italic line.

    Multi-line oracle text inside `*…*` markdown breaks the italics, so we
    flatten newlines to ` · `, strip parenthetical reminder text, collapse
    whitespace, and truncate. Used by trigger/LTB/cast-trigger display
    formatters across mtg/triggers.py.
    """
    if not text:
        return ""
    cleaned = text
    # Strip parenthetical reminder text — most trigger display contexts
    # don't need (Indestructible means damage and rules saying...) etc.
    cleaned = re.sub(r'\s*\([^)]{4,}\)\s*', ' ', cleaned)
    # May 16 audit: when callers split oracle text on '.', the trailing
    # ".)" of a reminder gets eaten by `.split('.')`, leaving an UNCLOSED
    # parenthetical like "Prowess (Whenever you cast a noncreature spell,
    # this creature gets +1/+1 until end of turn" with no `)`. The paired-
    # paren regex above can't match these. Strip them separately.
    cleaned = re.sub(r'\s*\([^)]{4,}$', '', cleaned)
    # Flatten newlines with a clear separator so successive ability clauses
    # remain readable.
    cleaned = re.sub(r'\s*\n+\s*', ' · ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars - 1].rstrip() + '…'
    return cleaned


def damage_prevention_disabled(game) -> bool:
    """True while "damage can't be prevented this turn" is active.

    Insult // Injury's first clause (July 23 follow-up). Set by the
    register_turn_damage_doubler action; matched on an exact turn number so it
    self-expires with no cleanup pass, the same trick the paired damage-doubler
    replacement uses. Consulted at every damage-prevention gate (mtg/combat.py
    x2, mtg/actions.py, mtg/rules_engine.py) so Fog / Teferi's Protection /
    Glacial Chasm can't blank the doubled damage.
    """
    if game is None:
        return False
    return (getattr(game, '_damage_prevention_off_turn', -1)
            == getattr(game, 'turn_number', -2))


def drain_pending_messages(game):
    """Drain game._pending_messages into a fresh list (slice 2b, July 21).

    PERMANENT_ENTERED subscribers surface display lines via
    game._pending_messages; former direct-scan call sites call this at the
    exact position the old scan call occupied so Discord ordering is
    unchanged. Returns [] when nothing is pending.
    """
    pq = getattr(game, '_pending_messages', None)
    if not pq:
        return []
    drained = list(pq)
    pq.clear()
    return drained


def cmc_of_cost_string(cost: str) -> int:
    """Mana value of a single-face cost string (CR 202.3).

    Counts {N} at face value, {X} as 0 (CR 202.3b), monocolored hybrid
    {2/W} at its higher half (CR 202.3f), and every other symbol —
    {W}, {G/W}, {W/P}, {S} — as 1.

    July 21 batch audit: the adventure/creature-half/split-half CMC
    recomputes in mtg/spells.py counted only digits + plain single-color
    pips, so Bring Back ({G/W}{G/W}{G/W}{G/W}) computed CMC 0 and the
    payment failure printed "needs 0 = 0 total"
    (game_1529165073443197190).
    """
    total = 0
    for sym in re.findall(r'\{([^}]+)\}', cost or ''):
        if sym.isdigit():
            total += int(sym)
            continue
        if sym.upper() == 'X':
            continue
        parts = sym.split('/')
        digit_parts = [int(p) for p in parts if p.isdigit()]
        total += max(digit_parts) if digit_parts else 1
    return total


def loyalty_from_commander_casts(game, player, card) -> int:
    """Jeska, Thrice Reborn class: "enters with a loyalty counter on her for
    each time you've cast a commander from the command zone this game."

    July 20 audit (game_1527448352298500096): printed loyalty is 0 and the
    engine initialized her to exactly that — instant SBA death with Daretti
    cast twice from the command zone. Counts times_cast_from_command_zone
    across the player's commander cards wherever they sit; if THIS cast is
    itself from the command zone the counter hasn't incremented yet, so it
    adds one (the ruling: her own commander cast counts).
    """
    oracle_l = (getattr(card, 'oracle_text', '') or '').lower()
    if "for each time you've cast a commander" not in oracle_l:
        return 0
    total = 0
    seen = set()
    zones = [getattr(player, 'battlefield', []), getattr(player, 'command_zone', None) or [],
             getattr(player, 'graveyard', []), getattr(player, 'hand', []),
             getattr(player, 'exile', []), getattr(player, 'library', [])]
    for zone in zones:
        for c in zone:
            if getattr(c, 'is_commander', False) and id(c) not in seen:
                seen.add(id(c))
                total += getattr(c, 'times_cast_from_command_zone', 0)
    if getattr(card, 'cast_from_command_zone', False):
        total += 1
    return total


def get_mdfc_info(card_name: str) -> dict:
    """
    Check if a card is an MDFC pathway and return face info.
    
    Returns dict with front_name, back_name, front_produces, back_produces
    or None if not an MDFC pathway.
    """
    name_lower = card_name.lower().strip()
    for front, (back, front_color, back_color) in MDFC_PATHWAYS.items():
        if front in name_lower or back in name_lower:
            return {
                "front_name": front.title(),
                "back_name": back.title(),
                "front_produces": front_color,
                "back_produces": back_color,
            }
    return None


def _normalize_pw_ability_idx(ability_idx, abilities):
    """Normalize AI-provided PW ability index. Accepts 0/1/2... (index) or "-3"/"+1" (loyalty cost).

    Returns an int index into the abilities list, or None if unresolvable.
    """
    if ability_idx is None:
        return 0
    if isinstance(ability_idx, bool):
        return None
    if isinstance(ability_idx, str):
        s = ability_idx.strip().replace('−', '-')
        try:
            parsed = int(s)
        except ValueError:
            return None
        # Treat as loyalty cost if it's negative or >= len(abilities) (typical +/- costs)
        if parsed < 0 or parsed >= len(abilities):
            match = next((a for a in abilities if a.loyalty_cost == parsed), None)
            return match.index if match is not None else None
        # Ambiguous positive in range: prefer index interpretation, but if no ability has
        # that index and one has that loyalty cost, fall through to cost match.
        if parsed < len(abilities):
            return parsed
        match = next((a for a in abilities if a.loyalty_cost == parsed), None)
        return match.index if match is not None else None
    try:
        parsed = int(ability_idx)
    except (TypeError, ValueError):
        return None
    if parsed < 0 or parsed >= len(abilities):
        match = next((a for a in abilities if a.loyalty_cost == parsed), None)
        return match.index if match is not None else None
    return parsed


def coerce_ai_string(value, _depth=0):
    """Best-effort coercion of an AI-provided value to a plain string.

    The LLM occasionally returns structured values where the action schema
    expects a bare string — e.g. {"name": "Shriekmaw"}, ["opponent"], or a
    bare int. Never raise; return '' when nothing string-like can be found.
    """
    if value is None or _depth > 3:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ('name', 'card', 'target', 'player', 'permanent', 'value'):
            if key in value:
                coerced = coerce_ai_string(value[key], _depth + 1)
                if coerced:
                    return coerced
        return ''
    if isinstance(value, (list, tuple)):
        for item in value:
            coerced = coerce_ai_string(item, _depth + 1)
            if coerced:
                return coerced
        return ''
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ''


def _resolve_player_or_card_target(game, activating_player, target_name):
    """Resolve an AI-provided target string to a Player or Card object.

    Accepts common AI conventions:
      - "opponent", "them", "enemy" → the other player
      - "you", "yourself", "self", "me", "my" → activating player
      - player display name (case-insensitive) → that player
      - card name on any battlefield (fuzzy) → that card

    Returns the resolved object, or None if unresolvable.
    """
    target_name = coerce_ai_string(target_name)
    if not target_name:
        return None
    t = target_name.strip().lower()
    # Player-keyword resolution
    if t in ('you', 'yourself', 'self', 'me', 'my', 'controller', 'caster'):
        return activating_player
    if t in ('opponent', 'opp', 'them', 'enemy', 'target player', 'target opponent',
             'another player', 'each opponent'):
        for p in game.players:
            if p is not activating_player:
                return p
    # Player name match (case-insensitive, allow substring for display names)
    for p in game.players:
        pname = (getattr(p, 'name', '') or '').lower()
        if pname and (pname == t or pname.startswith(t) or t.startswith(pname)):
            return p
    # Exact card match on any battlefield
    for p in game.players:
        try:
            c = p.find_card(target_name, Zone.BATTLEFIELD)
            if c:
                return c
        except Exception:
            pass
    # Fuzzy card match across all battlefields
    for p in game.players:
        for c in getattr(p, 'battlefield', []) or []:
            cname = (getattr(c, 'name', '') or '').lower()
            if cname and (t in cname or cname.startswith(t)):
                return c
    return None


def parse_suspend(oracle_text):
    """Parse "Suspend N—{cost}" → (n_counters, cost_str) or None.

    July 30 (batch-9 reviewer R2): suspend INITIATION was structurally
    unreachable for the AI/autoplay — only the manual !suspend command
    existed, it parsed just the count, and it never charged the cost.
    Scryfall prints digits and an em-dash ("Suspend 1—{R}"); tolerate a
    plain hyphen/en-dash. The cost group is re-uppercased because callers
    lowercase oracle text ({1}{u} would fail the mana engine).
    """
    m = re.search(r'suspend (\d+)\s*[—–\-]\s*((?:\{[^}]+\})+)',
                  (oracle_text or '').lower())
    if not m:
        return None
    return int(m.group(1)), m.group(2).upper()


def pw_any_target_legal(game, ability_text, target_obj):
    """CR 109.5: "any target" means a creature, player, planeswalker, or
    battle — an unanimated LAND is not one.

    July 30 deferred item (batch-8 origin): Wrenn and Six's -1 ("deals 1
    damage to any target") accepted a land through the PW forward path —
    _resolve_player_or_card_target matches any battlefield card by name and
    no caller type-checked the result. Applies only when the ability text
    says "any target"; other phrasings keep their own validation. Returns
    (legal, reason).
    """
    text = (ability_text or '').lower()
    if 'any target' not in text:
        return True, ""
    if not hasattr(target_obj, 'type_line'):
        return True, ""  # a Player — always a legal "any target"
    tl = (getattr(target_obj, 'type_line', '') or '').lower()
    if 'planeswalker' in tl or 'battle' in tl:
        return True, ""
    try:
        if target_obj.is_creature(game):
            return True, ""
    except TypeError:
        if target_obj.is_creature():
            return True, ""
    return False, (f"{getattr(target_obj, 'name', target_obj)} is not a "
                   f"legal 'any target' (CR 109.5 — creature, player, "
                   f"or planeswalker)")


_SELF_TARGET_WORDS = frozenset((
    'you', 'yourself', 'self', 'me', 'my', 'controller', 'caster'))
_OPPONENT_TARGET_WORDS = frozenset((
    'opponent', 'opp', 'them', 'enemy', 'target player', 'target opponent',
    'another player', 'each opponent'))


def resolve_cast_target(game, caster, card, target_name):
    """Resolve a declared cast target string to a StackEntry, Card or Player.

    ONE resolver for both cast paths. Two divergent copies existed — the AI one
    in mtg/engine.py and the pretend-human one in mtg/autoplay.py — and each had
    a gap the other didn't (the July 27 fanout found both):

      * the autoplay copy never searched graveyards, so every graveyard-targeting
        spell Rick cast silently lost its declared target and the reanimation
        fallback grabbed the biggest creature in ANY graveyard (CR 608.2b). The
        AI copy got that branch in the July 20 audit; this one never did.
      * NEITHER copy understood the pronoun "opponent", which is what the AI
        actually emits for burn (`target: "opponent"`). It resolved to None, and
        `get_legal_targets` lists creatures before players for "any target", so
        Lightning Bolt aimed at the face killed a creature instead.

    Resolution order is CR-shaped: stack (counterspells target spells, not
    permanents) -> battlefield -> graveyard (only for graveyard-targeting
    spells) -> player.

    Returns the resolved object, or None if nothing matches.
    """
    if not target_name:
        return None
    target_name = coerce_ai_string(target_name)
    if not target_name:
        return None
    lowered = target_name.strip().lower()
    oracle = (getattr(card, 'oracle_text', '') or '').lower()

    # 1. Counterspells target objects on the stack.
    if 'counter target' in oracle and 'spell' in oracle:
        for entry in reversed(getattr(game, 'stack', []) or []):
            entry_name = getattr(getattr(entry, 'card', None), 'name', None) or (
                entry.get('card_name') if isinstance(entry, dict) else None)
            if entry_name and entry_name.lower() == lowered:
                return entry

    # 2. Permanents.
    for p in game.players:
        found = p.find_card(target_name, Zone.BATTLEFIELD)
        if found:
            return found

    # 3. Graveyards — ONLY when the spell actually reaches there, so an
    #    ordinary removal spell can't accidentally "target" a dead card.
    if any(phrase in oracle for phrase in
           ('in a graveyard', 'in your graveyard',
            'from your graveyard', 'from a graveyard')):
        for p in game.players:
            found = p.find_card(target_name, Zone.GRAVEYARD)
            if found:
                return found

    # 4. Players, including the pronouns the AI actually emits. Deliberately
    #    NOT the fuzzy card fallback that _resolve_player_or_card_target uses:
    #    a card name that merely starts with a player's name must not silently
    #    become that player.
    if lowered in _SELF_TARGET_WORDS:
        return caster
    if lowered in _OPPONENT_TARGET_WORDS:
        for p in game.players:
            if p is not caster:
                return p
    for p in game.players:
        if (getattr(p, 'name', '') or '').lower() == lowered:
            return p
    return None


def _collapse_repeated_life_gain(messages):
    """Collapse consecutive identical life-gain messages for the same player into
    one line with an ×N multiplier. Handles the Soul Warden / Impact Tremors
    style cascade where N copies of a triggered ability each emit their own line.

    Input format: "💚 **{name}** gains {amount} life (life: {final})"
    Output format: "💚 **{name}** gains {amount} life ×N (life: {final})"

    Only collapses runs of identical prefixes (same player, same amount). The
    trailing life total is taken from the LAST message in the run (reflects the
    cumulative total after all triggers fired).
    """
    if not messages or len(messages) < 2:
        return messages

    import re as _re
    pat = _re.compile(r'^(💚 \*\*[^*]+\*\* gains (\d+) life) \(life: (\d+)\)$')
    out = []
    i = 0
    while i < len(messages):
        m = pat.match(messages[i] or "")
        if not m:
            out.append(messages[i])
            i += 1
            continue
        prefix, amount, _life = m.group(1), m.group(2), m.group(3)
        # Scan forward for consecutive identical-prefix messages
        run_end = i
        last_life = _life
        while run_end + 1 < len(messages):
            m2 = pat.match(messages[run_end + 1] or "")
            if not m2 or m2.group(1) != prefix:
                break
            last_life = m2.group(3)
            run_end += 1
        count = run_end - i + 1
        if count > 1:
            out.append(f"{prefix} x{count} (life: {last_life})")
        else:
            out.append(messages[i])
        i = run_end + 1
    return out


def _should_emit_resolve_hint(game, effect_key: str) -> bool:
    """Check if a !judge/!resolve hint should be shown for this effect.

    In autoplay: only emit once per unique effect (prevents triple-judge spam).
    In normal play: always emit (human needs the prompt).
    """
    if not getattr(game, 'is_autoplay', False):
        return True  # Always show in human games
    hints = getattr(game, '_judge_hints_emitted', None)
    if hints is None:
        game._judge_hints_emitted = set()
        hints = game._judge_hints_emitted
    if effect_key in hints:
        return False
    hints.add(effect_key)
    return True


def owner_of(game, card, fallback):
    """Resolve a card's OWNER — the player it started the game under.

    CR 400.3 / 404.3: a card that leaves the battlefield goes to its owner's
    zone, not its controller's. Everywhere the engine says `owner` it usually
    means whichever battlefield list held the card, which is the CONTROLLER —
    the same conflation `command_zone_owner` below was written to fix for
    commanders (June 10, C7). The July 27 fanout found the ordinary-permanent
    twin: a stolen Sun Titan destroyed under the thief's control landed in the
    THIEF's graveyard, permanently changing hands.

    Behaviour-neutral whenever owner == controller, i.e. every permanent that
    was never stolen — the blast radius is exactly control-change effects.
    """
    idx = getattr(card, 'owner_index', None)
    try:
        if idx is not None and 0 <= int(idx) < len(game.players):
            return game.players[int(idx)]
    except (TypeError, ValueError):
        pass
    return fallback


_NUMBER_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
}


def parse_escape_cost(oracle_text):
    """Parse "Escape—{cost}, Exile N other cards from your graveyard".

    Returns (cost_string, exile_count) or None.

    THE bug this exists to fix: four separate copies of this regex all required
    `(\\d+)` for the count, and Scryfall spells it as an English WORD on every
    printing — "Exile five other cards", "three", "four", "eight", "two". The
    pattern matched 0 of the 7 escape cards in the cache, so detection never
    fired and every downstream escape branch (cost selection, the graveyard
    castable list, the exile payment in all three cast paths) was unreachable
    dead code. The mechanic did not work at all; the test deck built for it
    could not exercise it. One parser now, so the four copies cannot drift.
    """
    if not oracle_text:
        return None
    match = re.search(
        r'escape.{1,3}(\{[^}]+\}(?:\{[^}]+\})*),?\s*exile\s+'
        r'(\d+|one|two|three|four|five|six|seven|eight|nine|ten)'
        r'\s+other\s+cards?\s+from\s+your\s+graveyard',
        oracle_text.lower())
    if not match:
        return None
    raw = match.group(2)
    count = int(raw) if raw.isdigit() else _NUMBER_WORDS.get(raw)
    if not count:
        return None
    return match.group(1).upper(), count


def parse_madness_cost(oracle_text):
    """Parse "Madness {cost}" (CR 702.35). Returns the cost string
    ("{1}{R}", "{X}{R}", "{0}") or None.

    Cache-verified shapes (Aug 1, 2026): every printing is the keyword
    followed directly by consecutive brace groups — "Madness {1}{R} (If you
    discard this card, discard it into exile. ...)". The reminder text's own
    "madness cost" phrases never put a brace after the word, so anchoring on
    `madness` + an immediate brace group can't false-positive on them.
    """
    if not oracle_text:
        return None
    match = re.search(
        r'\bmadness\s*(?:—\s*)?((?:\{[^}]+\})+)',
        oracle_text.lower())
    if not match:
        return None
    return match.group(1).upper()


def madness_discard_to_exile(game, player, card):
    """CR 702.35: a discarded card with madness is discarded into EXILE
    instead of the graveyard; its owner then casts it for the madness cost
    or it goes to the graveyard.

    THE shared choke-point half of the mechanic (Aug 1, 2026 — madness was
    entirely unimplemented until then; batch-15325's Wheel of Fortune and
    Reforge the Soul both discarded madness cards silently to graveyard).
    Caller contract: the card is already REMOVED from hand; call this
    INSTEAD of graveyard.append. Returns the display message when the card
    was redirected (exile + pending recorded + the Anje-family untap scan
    run), or None when the card has no madness — the caller then proceeds
    with its normal graveyard discard.

    The cast-or-graveyard choice resolves at the next async drain
    (mtg/spells.py:resolve_pending_madness) because discard sites are sync
    and casting is not — the established sync-gap bridge.
    """
    # General "whenever you discard" watchers fire on EVERY discard, madness
    # or not, so they must run ABOVE the madness early-return below (which is
    # also why the Anje scan further down could never serve them). Their
    # messages ride game._pending_messages because this function's return
    # value is reserved for the madness redirect line and most callers
    # discard anything else.
    from mtg.triggers import fire_discard_triggers
    _watcher_msgs = fire_discard_triggers(game, player, card)
    if _watcher_msgs:
        if getattr(game, '_pending_messages', None) is None:
            game._pending_messages = []
        game._pending_messages.extend(_watcher_msgs)

    cost = parse_madness_cost(getattr(card, 'oracle_text', '') or '')
    if cost is None:
        # Containment Construct / Conspiracy Theorist shape. The general
        # discard watcher runs before callers append the discarded card to a
        # graveyard, so this shared zone-handoff is the first authoritative
        # point where the optional exile can resolve. Autoplay takes the
        # strictly-upside option and grants the existing impulse permission.
        impulse_source = None
        for perm in player.battlefield:
            oracle = (getattr(perm, 'oracle_text', '') or '').lower()
            containment_shape = (
                'whenever you discard a card' in oracle
                and 'may exile that card from your graveyard' in oracle
                and 'may play that card this turn' in oracle)
            theorist_shape = (
                'whenever you discard one or more nonland cards' in oracle
                and 'may exile one of them from your graveyard' in oracle
                and 'may cast it this turn' in oracle
                and not card.is_land())
            if containment_shape or theorist_shape:
                # A wheel is one discard event even though the engine hands
                # its cards through this choke point one at a time. When the
                # caller supplies an event id, Conspiracy Theorist may exile
                # only one of those cards (Containment Construct triggers per
                # card and therefore deliberately has no such gate).
                event_id = getattr(game, '_active_discard_event_id', None)
                if (theorist_shape and event_id is not None
                        and getattr(perm, '_last_impulse_discard_event', None)
                        == event_id):
                    continue
                impulse_source = perm
                if theorist_shape and event_id is not None:
                    perm._last_impulse_discard_event = event_id
                break
        if impulse_source is not None:
            player.exile.append(card)
            if card.id not in player.playable_from_exile:
                player.playable_from_exile.append(card.id)
            print(f"[DISCARD-TRIGGER] {impulse_source.name} exiles "
                  f"{card.name} from graveyard; playable this turn")
            return (f"**{impulse_source.name}** exiles **{card.name}** - "
                    f"{player.name} may play it this turn")
        return None
    card._madness_cost = cost
    player.exile.append(card)
    try:
        owner_idx = game.players.index(player)
    except ValueError:
        owner_idx = 0
    game._madness_pending.append((card, owner_idx))
    print(f"[MADNESS] {player.name} discards {card.name} into exile "
          f"(madness {cost})")
    extra = ""
    # Anje Falkenrath's own trigger ("Whenever you discard a card, if it
    # has madness, untap Anje Falkenrath") — the engine.py comment marked
    # this as known-missing since the discard cost itself didn't exist.
    # Narrow to the untap-on-madness-discard family; generic "whenever you
    # discard" watchers (Bone Miser class) need a real DISCARD event on the
    # bus and stay evidence-gated.
    for perm in player.battlefield:
        _o = (getattr(perm, 'oracle_text', '') or '').lower()
        if ('whenever you discard a card' in _o and 'madness' in _o
                and 'untap' in _o):
            if getattr(perm, 'tapped', False):
                perm.tapped = False
                extra += f" — {perm.name} untaps"
                print(f"[MADNESS] {perm.name} untaps (madness-discard trigger)")
    return (f"🗑️ **{player.name}** discards **{card.name}** into exile "
            f"(madness {cost}){extra}")


def parse_spectacle_cost(oracle_text):
    """Parse "Spectacle {cost}" (CR 702.137). Returns the cost string or
    None. Same anchor discipline as parse_madness_cost: the keyword is
    followed directly by brace groups; the reminder text's "spectacle cost"
    phrases carry no brace after the word."""
    if not oracle_text:
        return None
    match = re.search(
        r'\bspectacle\s*(?:—\s*)?((?:\{[^}]+\})+)',
        oracle_text.lower())
    if not match:
        return None
    return match.group(1).upper()


def spectacle_available(game, player, card):
    """Return the spectacle cost when the card has one AND its condition is
    met — "if an opponent lost life this turn" (CR 702.137a; the tracking
    is Player.life_lost_this_turn, reset in end_turn). None otherwise.

    Aug 1, 2026: spectacle was a documented gap with three live sightings
    (Light Up the Stage cast at full price in batch 15324). One predicate
    so the pre-gate, the cost stage, and the castable list can't drift.
    """
    cost = parse_spectacle_cost(getattr(card, 'oracle_text', '') or '')
    if cost is None:
        return None
    if any(getattr(p, 'life_lost_this_turn', 0) > 0
           for p in game.players if p is not player):
        return cost
    return None


def counter_restriction_allows(counter_oracle, target_card):
    """Does this counterspell's printed restriction allow countering target_card?

    July 29 batch audit: Mental Misstep ("Counter target spell with mana
    value 1") countered a mana-value-2 Intangible Virtue — three independent
    paths (response auto-target, cast validation, Tier-2 resolution) all
    skipped restriction enforcement. Handles the two big families:
      - "with mana value N" / "with mana value N or less"
      - "counter target <qualifier> spell" (noncreature / creature / ...)
    Unknown shapes return True (no restriction detected) — this is a gate
    against ILLEGAL counters, not a full targeting model.
    """
    ot = (counter_oracle or '').lower()
    if 'counter target' not in ot:
        return True
    tcmc = int(getattr(target_card, 'cmc', 0) or 0)
    ttype = (getattr(target_card, 'type_line', '') or '').lower()
    m = re.search(r'counter target [^.\n]*?with mana value (\d+)( or less)?', ot)
    if m:
        n = int(m.group(1))
        if m.group(2):
            if tcmc > n:
                return False
        elif tcmc != n:
            return False
    m = re.search(r'counter target (\w+) spell', ot)
    if m and m.group(1) not in ('target',):
        q = m.group(1)
        if q.startswith('non'):
            if q[3:] in ttype:
                return False
        elif q != 'spell' and q not in ttype:
            return False
    # Aug 7 confirmation-batch audit (CO-1): Tale's End's multi-clause
    # "Counter target activated ability, triggered ability, or LEGENDARY
    # spell" defeats the single-word regex above (no "counter target X
    # spell" shape), so no restriction was detected and a response cast was
    # allowed at a declared NON-legendary spell (Counterspell) while the
    # only legal spell target was Search for Azcanta — {0}{U} + the card
    # burned, fizzle at resolution (CR 601.2c,
    # game_1535212567969144902). When an ability-counter names "legendary
    # spell" as its only SPELL clause, a spell target must be legendary.
    # This helper is only ever handed a CARD (a spell on the stack), so for
    # the Tale's End shape any card target must itself be legendary; the
    # ability-counter half never routes a card through here. Disallow /
    # Voidslime print "counter target spell" with no legendary qualifier and
    # are untouched.
    if ('legendary spell' in ot
            and ('activated' in ot or 'triggered' in ot)
            and 'legendary' not in ttype):
        return False
    return True


def find_castable_exile_card(game, player, card_name):
    """Aug 7 queue item Q3: locate a castable exiled card by name across ALL
    players' exiles, returning (card, holder) — the Draugr permission lets a
    player cast cards sitting in the OPPONENT'S exile, and the executors'
    own-exile-only scans could never find them. The caster's own exile is
    searched first so ordinary impulse/foretell casts are unchanged. The
    HOLDER matters: the physical removal (and any failure rollback) must
    touch the exile list that actually contains the card — owner-index
    discipline says never assume it is the caster's.
    """
    if not card_name:
        return None
    _wanted = card_name.lower()
    ordered = [player] + [p for p in getattr(game, 'players', []) or []
                          if p is not player]
    for holder in ordered:
        for c in getattr(holder, 'exile', None) or []:
            if c.name.lower() != _wanted:
                continue
            # Q3 adversarial review #2 (CRITICAL): the _adventure_exiled /
            # _foretold branches of is_castable_from_exile are
            # controller-blind — they were only ever safe because every
            # caller scanned the caster's OWN exile. A cross-player hit is
            # legal ONLY under the explicit Draugr stamp.
            if (holder is not player
                    and getattr(c, '_castable_by_player', None)
                    != getattr(player, 'name', None)):
                continue
            if is_castable_from_exile(game, player, c):
                return c, holder
    return None


def board_wipe_on_empty_board(game, player_index, card) -> bool:
    """Aug 7 confirmation-batch audit (CO-4): True when casting *card* would
    be a board wipe into a battlefield with zero creatures anywhere — the
    shape _validate_plan_mana has rejected since Apr 2026, but the inline
    decide_action fallback had no guard (the two-path divergence): in
    game_1535228649845030952 the plan cast was rejected and the inline path
    then cast Day of Judgment into a 0-creature board anyway. Shared
    predicate so the plan guard, the inline veto, and the pin agree.
    """
    if card is None or card.is_creature():
        return False
    _ol = (getattr(card, 'oracle_text', '') or '').lower()
    if 'destroy all creatures' not in _ol and 'each creature' not in _ol:
        return False
    total = sum(
        len([c for c in p.battlefield if c.is_creature(game=game)])
        for p in game.players)
    return total == 0


def player_skips_draw_step(player):
    """Live scan: does a permanent this player controls print 'skip your draw
    step'? Returns the source card's name, or None.

    July 29 batch audit (CR 611.2c): Solitary Confinement's draw-skip was a
    sticky Player flag with NO expiry and no removal cleanup — after Claude
    exiled it (game_1531564136121503818), Rick never drew again for the rest
    of the game. A static ability exists exactly while its source is on the
    battlefield, so compute it from the battlefield. Bonus: this also honors
    Necropotence's printed "Skip your draw step.", which no code modeled.
    """
    for c in getattr(player, 'battlefield', None) or []:
        if getattr(c, '_phased_out', False):
            continue
        if 'skip your draw step' in (getattr(c, 'oracle_text', '') or '').lower():
            return c.name
    return None


def player_has_prevent_all_static(player):
    """Live scan: an unconditional "prevent all damage that would be dealt to
    you" STATIC (Solitary Confinement, Glacial Chasm). Same CR 611.2c story
    as player_skips_draw_step — the old sticky `_damage_prevented` flag
    outlived the permanent (batch 15315 prevented 6 combat damage the same
    turn Solitary Confinement was exiled). `_damage_prevented` remains the
    channel for EVENT-scoped prevention (Teferi's Protection, Fog), which
    genuinely persists after the spell resolves.
    """
    for c in getattr(player, 'battlefield', None) or []:
        if getattr(c, '_phased_out', False):
            continue
        ot = (getattr(c, 'oracle_text', '') or '').lower()
        if 'prevent all damage that would be dealt to you' in ot:
            return True
    return False


def parse_escapes_with_counters(oracle_text):
    """Parse the escape rider "escapes with N +1/+1 counter(s) on it".

    CR 702.139e. Sibling of parse_escape_cost, with the same word-number
    lesson applied up front: every printing spells the count as "a" or an
    English word ("escapes with a +1/+1 counter", "escapes with two +1/+1
    counters"), never a digit. Returns 0 when there is no rider.
    """
    if not oracle_text:
        return 0
    match = re.search(
        r'escapes with (\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten)'
        r'\s+\+1/\+1 counters?',
        oracle_text.lower())
    if not match:
        return 0
    raw = match.group(1)
    if raw.isdigit():
        return int(raw)
    if raw in ('a', 'an'):
        return 1
    return _NUMBER_WORDS.get(raw, 0)


def owns_card(card, player_index):
    """Does the player at `player_index` OWN this card?

    Unknown ownership (owner_index == -1, i.e. a Card built at runtime rather
    than stamped by deck load) counts as owned by whoever controls it — the
    caller only ever asks about a permanent on that player's battlefield, and
    treating unstamped cards as "someone else's" would silently break the
    you-own-it gates on Aminatou and Yorion.
    """
    idx = getattr(card, 'owner_index', -1)
    if idx is None or idx < 0:
        return True
    return idx == player_index


def parse_kicker(oracle_text):
    """Parse a printed "Kicker {cost}" line (CR 702.33). Returns the kicker
    cost string ("{2}", "{1}{W}", ...) or None.

    v1 scope (Aug 1, 2026): single kicker only — multikicker (negative
    lookbehind below) and the "Kicker {A} and/or {B}" double-kicker shape
    are not modeled; no card in the test inventory carries either. The
    match anchors on the ability word + brace costs, so "if this spell was
    kicked" condition text and reminder text never match.
    """
    if not oracle_text:
        return None
    m = re.search(r'(?<!multi)\bkicker ((?:\{[^}]+\})+)', oracle_text,
                  re.IGNORECASE)
    return m.group(1) if m else None


def parse_crew(oracle_text):
    """Parse a printed "Crew N" line (CR 702.121). Returns int N or None.

    Aug 2, 2026 (the corners-of-corners pass): Vehicles became non-creatures
    at the PW-token fix (batch-13) — crew is what makes them usable again.
    Anchors on the keyword + number so reminder text can't false-positive.
    """
    if not oracle_text:
        return None
    m = re.search(r'\bcrew (\d+)\b', oracle_text, re.IGNORECASE)
    return int(m.group(1)) if m else None


# Multikicker consumers with a template that actually READS kicked_times —
# the auto-kick gate is registry-limited so a card whose kicked mode isn't
# modeled (Comet Storm's extra targets) never overpays for nothing.
MULTIKICKER_MODELED = frozenset({"everflowing chalice"})


def parse_multikicker(oracle_text):
    """Parse a printed "Multikicker {cost}" line (CR 702.33c). Returns the
    cost string or None. The single-kicker parser deliberately excludes
    multikicker (its lookbehind); this is the other half."""
    if not oracle_text:
        return None
    m = re.search(r'\bmultikicker ((?:\{[^}]+\})+)', oracle_text, re.IGNORECASE)
    return m.group(1) if m else None


def parse_entwine(oracle_text):
    """Parse a printed "Entwine {cost}" line (CR 702.42). Returns the entwine
    cost string ("{2}", "{1}{G}", ...) or None.

    Aug 2, 2026 (batch-13 rashmi/mythic reviewer): Tooth and Nail's template
    granted BOTH modes at the base cost — entwine appeared nowhere in the
    codebase. Same additive-optional-cost family as kicker (the parse
    anchors on the ability word + brace costs, so reminder text never
    matches)."""
    if not oracle_text:
        return None
    m = re.search(r'\bentwine ((?:\{[^}]+\})+)', oracle_text, re.IGNORECASE)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Aug 3, 2026 — THE ALTERNATE-COST / GRAVEYARD-CASTING CLUSTER.
#
# buyback, embalm, eternalize, foretell, unearth, jump-start, aftermath (wave
# 1) and miracle + dredge (wave 2). Every main EFFECT in this cluster already
# resolved before this pass; what was missing in each case was the alternate
# COST or the zone-change ability, so the cards were played at their printed
# cost or not at all.
#
# ONE anchor discipline for the whole family, because Scryfall's bulk data
# shows the same trap on every one of these keywords: a card can GRANT the
# ability to other cards ("Each Sliver creature card in your graveyard has
# unearth {2}", "Land cards in your graveyard have dredge 2", "Each instant
# and sorcery card in your hand has miracle {2}"). A naive
# `<keyword>\s*(\{...\})` match reads the grant as the SOURCE's own cost —
# the July-21 Yidris cascade-grant class, which the wave-1 attack-keyword
# parser hit and which mutation testing caught there. _own_keyword_clause is
# the shared guard so the eight parsers below cannot drift apart on it.
# ---------------------------------------------------------------------------

_GRANT_LANGUAGE = re.compile(r'\b(have|has|gains?|gain)\b')


def _own_keyword_clause(oracle_text: str, keyword: str):
    """Text following the card's OWN printed `keyword`, or None.

    Reminder text is stripped first (every one of these keywords ships a
    parenthetical that repeats the keyword). A line whose text BEFORE the
    keyword carries grant language is skipped: that line gives the ability
    to other cards and says nothing about this one.
    """
    if not oracle_text:
        return None
    pattern = re.compile(r'\b' + re.escape(keyword) + r'\b', re.IGNORECASE)
    for raw in oracle_text.split('\n'):
        line = re.sub(r'\([^)]*\)', '', raw).strip()
        if not line:
            continue
        match = pattern.search(line)
        if not match:
            continue
        if _GRANT_LANGUAGE.search(line[:match.start()].lower()):
            continue
        return line[match.end():]
    return None


def _own_keyword_cost(oracle_text: str, keyword: str):
    """The mana cost printed directly after the card's own `keyword`.

    Accepts the plain form ("Embalm {5}{W}") and the em-dash form
    ("Eternalize—{2}{W}{W}, Discard a card."); returns None when the
    keyword is present but not followed by a cost, which is how the
    static-grant and rules-text lines ("Buyback costs cost {2} less",
    "...rather than pay the unearth cost...") decline to match.
    """
    clause = _own_keyword_clause(oracle_text, keyword)
    if clause is None:
        return None
    match = re.match(r'\s*(?:[—–-]\s*)?((?:\{[^}]+\})+)', clause)
    return match.group(1).upper() if match else None


def _has_bare_keyword(oracle_text: str, keyword: str) -> bool:
    """True when `keyword` appears as a costless keyword ability of this card.

    jump-start and aftermath print no cost, so they are read off the keyword
    LINE (via the shared tokenizer, which already drops grant lines and any
    line carrying sentence punctuation). That rejects every rules-text
    mention: "Spells you cast with jump-start aren't exiled",
    "Vehicles in your graveyard have jump-start".
    """
    return keyword.lower() in set(_keyword_line_tokens(oracle_text or ''))


def parse_buyback(oracle_text):
    """Buyback (CR 702.26) — an optional ADDITIONAL cost; if paid, the spell
    returns to its owner's HAND as it resolves instead of going to the
    graveyard.

    Returns a dict describing the cost, or None:
        {'mana': '{2}{U}'}   — the common printed form
        {'discard': 2}       — "Buyback—Discard two cards." (Forbid, the
                               only buyback card in the test inventory)

    Deliberately returns None for the life-payment and sacrifice forms
    ("Buyback—Pay 4 life.", "Buyback—Sacrifice a land."): they exist on
    real cards but none are in any deck, and buying back for an unmodeled
    cost would be a free recursion engine. Declining is the safe direction —
    the spell simply resolves to the graveyard as it does today.
    """
    cost = _own_keyword_cost(oracle_text, 'buyback')
    if cost:
        return {'mana': cost}
    clause = _own_keyword_clause(oracle_text, 'buyback')
    if clause is None:
        return None
    match = re.match(
        r'\s*[—–-]\s*discard\s+(\d+|a|an|one|two|three|four|five)\s+cards?\b',
        clause, re.IGNORECASE)
    if not match:
        return None
    raw = match.group(1).lower()
    if raw.isdigit():
        count = int(raw)
    elif raw in ('a', 'an'):
        count = 1
    else:
        count = _NUMBER_WORDS.get(raw, 0)
    return {'discard': count} if count else None


# The three graveyard-activated recursion mechanics. All share one shape:
# an activated ability of a card IN A GRAVEYARD, sorcery-speed only, that
# exiles the card as part of the cost. Grouping them lets the offer list and
# both executors carry ONE branch instead of three near-copies.
GRAVEYARD_ACTIVATION_KEYWORDS = ('embalm', 'eternalize', 'unearth')


def parse_graveyard_activation(oracle_text):
    """(mechanic, mana_cost) for embalm / eternalize / unearth, else None.

    embalm     (CR 702.87) — exile from GY: token copy, white Zombie <types>,
                             no mana cost, same P/T.
    eternalize (CR 702.129) — exile from GY: token copy, 4/4 black Zombie
                             <types>, no mana cost.
    unearth    (CR 702.83) — return the card itself to the battlefield with
                             haste; exile it at the next end step or if it
                             would leave the battlefield.

    All three are ACTIVATED abilities, not casts (CR 702.87a etc.) — casting
    them would wrongly fire Rhystic Study and the rest of the cast-trigger
    family, which is why they are dispatched as their own action type rather
    than folded into the graveyard-cast branch that serves flashback/escape.
    """
    for mechanic in GRAVEYARD_ACTIVATION_KEYWORDS:
        cost = _own_keyword_cost(oracle_text, mechanic)
        if cost:
            return mechanic, cost
    return None


def parse_foretell(oracle_text):
    """Foretell (CR 702.143). Returns the foretell cost string or None.

    Two halves: any card in hand may be exiled FACE DOWN for {2} during your
    turn, and a foretold card may be cast from exile for its foretell cost on
    a later turn. "{0}" is a real printed cost (Foretell {0}), so the caller
    must test `is not None` rather than truthiness.
    """
    return _own_keyword_cost(oracle_text, 'foretell')


def parse_miracle(oracle_text):
    """Miracle (CR 702.94). Returns the miracle cost string or None.

    "You may cast this card for its miracle cost when you draw it if it's
    the first card you drew this turn." Costs range from {0} through {3}{W}
    and include bare single pips ({W}, {R}, {U}, {G}) — again, test against
    None, not truthiness.
    """
    return _own_keyword_cost(oracle_text, 'miracle')


def parse_dredge(oracle_text):
    """Dredge (CR 702.52). Returns int N or None.

    "If you would draw a card, you may mill N cards instead. If you do,
    return this card from your graveyard to your hand." A replacement on the
    draw, so it needs the same draw-time hook miracle does — which is why
    the two shipped as one wave.
    """
    clause = _own_keyword_clause(oracle_text, 'dredge')
    if clause is None:
        return None
    match = re.match(r'\s*(\d+)\b', clause)
    return int(match.group(1)) if match else None


def has_jump_start(oracle_text) -> bool:
    """Jump-start (CR 702.132): cast from your graveyard by discarding a card
    in addition to paying its other costs, then exile it. No printed cost of
    its own — the cost is the printed mana cost plus the discard."""
    return _has_bare_keyword(oracle_text, 'jump-start')


def aftermath_half_index(card):
    """Index of the AFTERMATH half of a split card, or None (CR 702.127).

    An aftermath half may be cast ONLY from the graveyard, and the card is
    exiled after it resolves; the non-aftermath half may not be cast from
    the graveyard at all. Reads `split_texts`, which the deck loader fills
    from Scryfall's `card_faces` — the cache's top-level `oracle_text` for a
    split card is face 0 only, so this deliberately does not consult it.
    """
    texts = getattr(card, 'split_texts', None) or []
    for index, text in enumerate(texts):
        if _has_bare_keyword(text, 'aftermath'):
            return index
    return None


# ---------------------------------------------------------------------------
# Aug 3, 2026 — WAVE 2: the two DRAW-time mechanics.
#
# Miracle (CR 702.94) and dredge (CR 702.52) both hook the draw, which is why
# they shipped together and separately from wave 1: dredge REPLACES the draw
# (it must run before the card leaves the library), miracle reacts to it (the
# card must already be in hand and be the first one drawn this turn).
#
# COVERAGE, stated honestly. Hooked: GameEngine.draw_cards (the draw step and
# most effect draws), the `draw_cards` ACTION handler (every template and
# Tier-3 draw), the `cycle` action (cycling into a miracle is the classic
# line) and the Baral-class "counters a spell -> draw" trigger. That is every
# draw-to-hand inside the mtg/ package.
#
# NOT hooked: the raw `library.pop(0)` draws in rules/effects.py,
# rules/spell_resolver.py and rules/planeswalker.py — independent draw
# implementations in the other package. Routing those through one choke point
# is a real refactor and is deliberately NOT smuggled into this wave;
# tests/test_aug3_altcost_wave2.py pins both the covered set AND the known
# gap, so the gap stays visible and the pin fails loudly if someone closes it.
#
# An unhooked draw is not merely a missed miracle: cards_drawn_this_turn goes
# out of step, so miracle can both mis-miss (the real first draw wasn't
# counted) and mis-fire (a later draw looks like the first).
# ---------------------------------------------------------------------------

def dredge_candidates(player):
    """[(card, N)] for every dredge card in the player's graveyard whose mill
    the library can actually afford (CR 702.52a — you may only dredge if your
    library has at least N cards)."""
    out = []
    for card in (getattr(player, 'graveyard', []) or []):
        n = parse_dredge(getattr(card, 'oracle_text', '') or '')
        if n and len(getattr(player, 'library', []) or []) >= n:
            out.append((card, n))
    return out


def try_dredge(game, player):
    """Replace a draw with a dredge (CR 702.52a). Returns the returned card,
    or None to let the draw happen normally.

    v1 policy, and its reasoning: dredge is a "may", and always taking it
    means never drawing a new card again — strictly worse for most decks. It
    is capped at ONE replaced draw per turn, which in practice is the draw
    step, matching how the mechanic is actually played, and requires the mill
    to leave a real library behind so it can never deck its own controller.
    Among affordable candidates it takes the LARGEST N: the decks holding
    these cards (Meren, Kroxa escape, the cube's Loam pile) want graveyard
    fuel, which is the whole reason to dredge instead of drawing.
    """
    # Per PLAYER, not per game: a wheel routed through the draw_cards action
    # with player="all" would otherwise let the first player dredge and block
    # everyone else for the rest of the turn, as would a non-active player
    # drawing on the active player's turn.
    try:
        seat = game.players.index(player)
    except ValueError:
        return None
    dredged = getattr(game, '_dredged_this_turn', None)
    if not isinstance(dredged, set):
        dredged = set()
        game._dredged_this_turn = dredged
    if seat in dredged:
        return None
    candidates = [(c, n) for c, n in dredge_candidates(player)
                  if len(player.library) >= n + 10]
    if not candidates:
        return None
    card, n = max(candidates, key=lambda cn: cn[1])
    milled = []
    for _ in range(n):
        if not player.library:
            break
        milled.append(player.library.pop(0))
    player.graveyard.extend(milled)
    player.graveyard.remove(card)
    player.hand.append(card)
    dredged.add(seat)
    print(f"[DREDGE] {player.name} dredges {card.name} (mills {len(milled)} "
          f"instead of drawing)")
    game._pending_messages = getattr(game, '_pending_messages', None) or []
    game._pending_messages.append(
        f"⛏️ **{player.name}** dredges **{card.name}** "
        f"(milling {len(milled)} instead of drawing)")
    return card


def note_miracle_on_draw(game, player, card):
    """CR 702.94a: if this is the FIRST card its controller drew this turn and
    it has miracle, they may cast it for the miracle cost.

    Records (card, owner_index) on game._miracle_pending; the async drain
    (spells.resolve_pending_miracles) makes the cast-or-keep call, because
    draws are sync and casting is not — the same sync-gap bridge madness uses.
    Returns True when a miracle was recorded.
    """
    # "Whenever ... draws a card" watchers fire on EVERY draw, so they hook
    # ABOVE the miracle early-return below — the same placement lesson the
    # discard watchers needed relative to the madness return. This function is
    # called at all four draw-to-hand sites, which is what makes one hook here
    # complete coverage. Messages ride game._pending_messages because this
    # function's return value means "a miracle was recorded".
    from mtg.triggers import fire_draw_triggers
    _draw_msgs = fire_draw_triggers(game, player, card)
    if _draw_msgs:
        if getattr(game, '_pending_messages', None) is None:
            game._pending_messages = []
        game._pending_messages.extend(_draw_msgs)

    cost = parse_miracle(getattr(card, 'oracle_text', '') or '')
    if cost is None:
        return False
    # cards_drawn_this_turn has already been incremented for THIS draw, so
    # the first card of the turn is 1, not 0.
    if getattr(player, 'cards_drawn_this_turn', 0) != 1:
        return False
    card._miracle_cost = cost
    try:
        owner_idx = game.players.index(player)
    except ValueError:
        return False
    game._miracle_pending = getattr(game, '_miracle_pending', None) or []
    game._miracle_pending.append((card, owner_idx))
    print(f"[MIRACLE] {player.name} drew {card.name} as the first card this "
          f"turn (miracle {cost})")
    return True


def pay_jump_start_discard(game, player, card):
    """Pay jump-start's additional cost (CR 702.132a — "by discarding a card
    in addition to paying its other costs"). Returns the list of discarded
    cards so the caller's rollback can refund them; empty when the card has
    no jump-start or there was nothing to pitch.

    Routes through the madness choke point, so pitching a madness card to
    jump-start a spell exiles it and offers the madness cast — the same
    behavior every other discard in the engine has.
    """
    if not has_jump_start(getattr(card, 'oracle_text', '') or ''):
        return []
    pitchable = [c for c in player.hand if c is not card]
    if not pitchable:
        # CR 702.132a makes the discard an additional COST, and CR 601.2g
        # forbids casting a spell whose costs can't be paid. Returning [] here
        # let the cast proceed for free; None says "refuse it".
        print(f"[JUMP-START] {card.name}: no card to discard — cast refused "
              f"(CR 601.2g)")
        return None
    worst = max(pitchable, key=lambda c: (c.is_land(), -(c.cmc or 0)))
    player.hand.remove(worst)
    if madness_discard_to_exile(game, player, worst) is None:
        player.graveyard.append(worst)
    print(f"[JUMP-START] {card.name}: discarded {worst.name} as the "
          f"additional cost")
    return [worst]


# ---------------------------------------------------------------------------
# Aug 3, 2026 — COST MODIFICATION: affinity (CR 702.41) and converge
# (CR 702.100). Both live on the SPELL being cast rather than on some other
# permanent, which is why neither fits compute_cost_reduction (that scans the
# battlefield for permanents saying "spells you cast cost less").
# ---------------------------------------------------------------------------

# "Affinity for X" — the printed forms across all of Scryfall are a card type
# ("artifacts"), a supertype+type ("snow lands"), a subtype ("Equipment",
# "Elves", "Gates", "Foods"), or a basic land type ("Forests"). The reminder
# text always spells out the same rule: "This spell costs {1} less to cast for
# each <X> you control."
_AFFINITY_RE = re.compile(r'\baffinity for ([a-z][a-z\' ]*)', re.IGNORECASE)


def parse_affinity(oracle_text):
    """The "for X" phrase of a printed Affinity keyword, lowercased, or None.

    Uses the shared own-keyword guard, so a card that GRANTS affinity —
    "The next spell you cast this turn has affinity for artifacts" (Urza's
    +1) — does not report affinity for itself.
    """
    clause = _own_keyword_clause(oracle_text, 'affinity')
    if clause is None:
        return None
    match = re.match(r"\s*for\s+([a-z][a-z' ]*)", clause, re.IGNORECASE)
    return match.group(1).strip().lower() if match else None


def _affinity_matches(permanent, phrase: str) -> bool:
    """Does `permanent` count toward "affinity for <phrase>"?

    Matches the whole phrase against the type line, singularising the plural
    the keyword always prints ("snow lands" -> "snow land", "artifacts" ->
    "artifact"). Every word must appear, which is what makes the compound
    forms work: "snow lands" needs BOTH the snow supertype and the land type,
    so a snow artifact and a non-snow land each correctly fail.
    """
    type_line = (getattr(permanent, 'type_line', '') or '').lower()
    if not type_line:
        return False
    words = [w[:-1] if len(w) > 3 and w.endswith('s') else w
             for w in phrase.split()]
    return all(w in type_line for w in words if w)


def compute_affinity_reduction(player, card):
    """(amount, phrase) for a card with Affinity, else (0, None).

    CR 702.41a — "this spell costs {1} less to cast for each <X> you
    control". The caller must still clamp to the generic portion present
    (CR 601.2f); like every other reduction it can never eat a colored pip,
    which is what stops Icebreaker Kraken ({10}{U}{U}) from ever costing less
    than {U}{U}.
    """
    phrase = parse_affinity(getattr(card, 'oracle_text', '') or '')
    if not phrase:
        return 0, None
    battlefield = (player.active_battlefield()
                   if hasattr(player, 'active_battlefield')
                   else getattr(player, 'battlefield', []) or [])
    return sum(1 for p in battlefield
               if p is not card and _affinity_matches(p, phrase)), phrase


# Converge counts COLORS of mana spent (CR 702.100a). Colorless is not a
# color, so {C} and generic mana paid by a colorless source never count.
_REAL_COLORS = ('W', 'U', 'B', 'R', 'G')


def colors_spent_count(card) -> int:
    """How many distinct colors of mana were spent casting `card`.

    The mana engine already resolves each tapped source to ONE committed
    color (the June-10 OR-dual fix); this reads the set it recorded. Returns
    0 when nothing was recorded, which is the safe direction — a converge
    spell then does the least it can rather than inventing colors.
    """
    return len([c for c in (getattr(card, '_colors_spent', None) or ())
                if c in _REAL_COLORS])


# ---------------------------------------------------------------------------
# Aug 3, 2026 — SPLICE (CR 702.46), the last non-tail mechanic on the
# missing-mechanics backlog.
#
# Splice is NOT a cost adjustment, despite having been filed next to affinity
# and converge. It is a static ability that functions FROM YOUR HAND: as you
# cast a spell of the named subtype, you may reveal the splice card, pay its
# splice cost as an additional cost, and add its effects to that spell. The
# revealed card never leaves your hand (CR 702.46c) — that is what makes it
# unlike every other alternate/additional cost in this file, all of which
# read the card BEING CAST.
#
# The sweep over Scryfall bulk (33 splice cards, 97 Arcane cards) found three
# printed subtype phrases and one whole family the parser must DECLINE:
#
#   accept  "Splice onto Arcane {1}{R}"              — Glacial Ray, 27 cards
#   accept  "Splice onto instant or sorcery {2}{U}"  — Kamigawa: Neon Dynasty
#                                                      (note the LOWERCASE
#                                                      subtype phrase)
#   decline "Splice onto Arcane—Sacrifice two Mountains."  and the four other
#           em-dash non-mana forms (Torrent of Stone, Roar of Jukai, Reweave's
#           cousins). Same call as buyback's life/sacrifice forms: an unmodeled
#           cost paid for free is strictly worse than not splicing.
#   decline "Splice onto Anything {1}{R}" — falls out for free rather than
#           being special-cased: "anything" matches no type line, so the
#           subtype test fails and the card simply never splices.
#   decline Minamo's Meddling's rules text, "...each card with the same name
#           as a card spliced onto that spell" — `\bsplice\b` does not match
#           "spliced", so no line is even considered.
# ---------------------------------------------------------------------------

# The subtype phrase is letters and spaces only, and the cost must close the
# line. Both restrictions are what reject the em-dash forms: "Arcane—Exile
# four cards from your graveyard." has a character outside [A-Za-z ] and no
# trailing brace group, so it cannot match under any reading.
_SPLICE_RE = re.compile(r"^\s*onto\s+([A-Za-z][A-Za-z ]*?)\s*"
                        r"((?:\{[^}]+\})+)\s*$")


def parse_splice(oracle_text):
    """(subtype_phrase, mana_cost) for a card's own printed Splice, else None.

    The subtype phrase is returned lowercased and verbatim ("arcane",
    "instant or sorcery"); `splice_matches_spell` is what turns it into a
    test against a spell's type line.

    Uses the shared own-keyword guard for family consistency. No card in the
    pool currently GRANTS splice, but every other parser in this file learned
    the grant lesson the expensive way and a future set is one printing away.
    """
    clause = _own_keyword_clause(oracle_text, 'splice')
    if clause is None:
        return None
    match = _SPLICE_RE.match(clause)
    if not match:
        return None
    return match.group(1).strip().lower(), match.group(2).upper()


def splice_matches_spell(subtype_phrase: str, type_line: str) -> bool:
    """Can a card with "Splice onto <subtype_phrase>" splice onto this spell?

    The phrase is split on " or " so the two real forms need no special
    casing: "arcane" tests one token, "instant or sorcery" tests either. A
    phrase naming something no type line contains ("anything") matches
    nothing, which is the safe direction.
    """
    if not subtype_phrase or not type_line:
        return False
    lowered = type_line.lower()
    return any(part.strip() in lowered
               for part in subtype_phrase.lower().split(' or ')
               if part.strip())


def strip_splice_line(oracle_text: str) -> str:
    """`oracle_text` without its "Splice onto ..." line.

    Splice is a static ability that does nothing once the text has been
    copied onto the spell, so the spliced effects must resolve WITHOUT it —
    leaving it in hands the effect resolver a line whose reminder text
    describes casting, which is exactly the kind of text that has misfired
    generic patterns before (extort, the suspend reminder on Mox Tantalite).
    """
    if not oracle_text:
        return oracle_text or ""
    kept = [line for line in oracle_text.split('\n')
            if not re.sub(r'\([^)]*\)', '', line).strip().lower()
            .startswith('splice onto')]
    return '\n'.join(kept).strip()


def _splice_choices_are_makeable(game, spliced_text: str) -> bool:
    """CR 702.46b — you may not splice a card whose required choices can't
    be made.

    Narrow BY DESIGN, and the scope is stated exactly rather than overclaimed
    (an earlier draft of this docstring said the creature case was "the only
    shape that can actually arise", which is false — Soulless Revival wants a
    creature card in a GRAVEYARD, Wear Away an artifact or enchantment). The
    three zone-and-type shapes below are the ones real printed splice cards
    use; anything else is allowed through and, if it turns out to have no
    legal target, fizzles at resolution having charged its cost. That is a
    known gap, not a claim of completeness — full CR 702.46b enforcement needs
    the targeting engine, which is a larger seam than this mechanic.

    "Any target" is never restricted here: a player is always legal, so the
    only card the shipped decks can splice (Glacial Ray) never trips this.
    """
    lowered = (spliced_text or '').lower()

    def _battlefield(pred):
        return any(pred(c)
                   for p in getattr(game, 'players', []) or []
                   for c in getattr(p, 'battlefield', []) or [])

    if 'target creature card' in lowered and 'graveyard' in lowered:
        # Soulless Revival — the target lives in a GRAVEYARD, so scanning the
        # battlefield would answer a different question entirely.
        return any(c.is_creature(game)
                   for p in getattr(game, 'players', []) or []
                   for c in getattr(p, 'graveyard', []) or [])
    if 'target creature' in lowered:
        # is_creature(game) and not is_creature(): a devotion-gated god below
        # its threshold is NOT a creature (CR 207.4), and the bare call would
        # count it as one — the June-10 D4 class.
        return _battlefield(lambda c: c.is_creature(game))
    if 'target artifact or enchantment' in lowered:
        # Wear Away.
        return _battlefield(
            lambda c: 'artifact' in (getattr(c, 'type_line', '') or '').lower()
            or 'enchantment' in (getattr(c, 'type_line', '') or '').lower())
    return True


def parse_impending(oracle_text):
    """(time_counters, mana_cost) for a printed Impending, else None.

    CR 702.166a — "Impending N—[cost]": an ALTERNATIVE cost (it replaces the
    mana cost rather than adding to it, unlike kicker's family). Paying it
    makes the permanent enter with N time counters and NOT be a creature
    until the last is removed; one comes off at the beginning of each of its
    controller's end steps.

    The separator is an EM DASH on every printing ("Impending 4—{2}{R}{R}"),
    which is also how this is told apart from the ability word; a hyphen and
    an en dash are accepted too so a reprint's typography cannot silently
    turn the mechanic off.
    """
    clause = _own_keyword_clause(oracle_text, 'impending')
    if clause is None:
        return None
    match = re.match(r'\s*(\d+)\s*[—–-]\s*((?:\{[^}]+\})+)', clause)
    if not match:
        return None
    return int(match.group(1)), match.group(2).upper()


def splice_legal_target_exists(game, splice_card) -> bool:
    """Does a spliced card's own instruction still have a legal target?

    The resolution-time twin of the cast-time CR 702.46b check, sharing its
    one predicate rather than re-expressing it. CR 608.2b fails a spell to
    resolve only if ALL targets for EVERY instruction are illegal, and a
    spliced instruction is one of those instructions (CR 702.46a) — so this
    is what stops a spell whose PRINTED target went away from taking the
    spliced text (and the mana already paid for it) down with it.
    """
    return _splice_choices_are_makeable(
        game, strip_splice_line(getattr(splice_card, 'oracle_text', '') or ''))


def splice_candidates(game, player, spell_card):
    """Cards in `player`'s hand that may be spliced onto `spell_card`.

    Returns [(splice_card, cost_string)] cheapest-first (name breaks ties, so
    the order is fully deterministic). Affordability is the CALLER's call —
    it alone knows the running effective cost the splice cost is added to.

    The identity exclusion is the load-bearing line. `_compute_alt_costs`
    runs BEFORE the card leaves hand, so the spell being cast is still sitting
    in `player.hand`; without `c is not spell_card`, Through the Breach would
    splice onto ITSELF. It is deliberately an identity test and not a name
    test: with two copies in hand (any 4-of format) splicing copy B onto
    copy A is perfectly legal, and a name test would forbid it.
    """
    # CR 702.46a names the subtype of the SPELL, and for a split card the
    # spell is the HALF being cast. spell_face_for_gates is the canonical
    # answer ("Every CR 601.2c gate must evaluate the half").
    #
    # Honest scope: it covers split halves only. An ADVENTURE card's cached
    # type_line is already the creature face alone ("Creature — Giant", not a
    # combined string), so casting the adventure half reads the creature's
    # type line and an "instant or sorcery" splice would DECLINE where the
    # rules allow it. That is the safe direction, and no deck currently pairs
    # an "instant or sorcery" splice card with anything at all — all four are
    # absent from every deck JSON.
    #
    # Only the TYPE LINE comes from the face. The identity exclusion below
    # still compares against the ORIGINAL spell_card: the face is a freshly
    # built synthetic Card, so `c is not face` would be true for every card in
    # hand and self-splice would come straight back.
    type_line = getattr(spell_face_for_gates(spell_card), 'type_line', '') or ''
    out = []
    for c in list(getattr(player, 'hand', []) or []):
        if c is spell_card:
            continue
        parsed = parse_splice(getattr(c, 'oracle_text', '') or '')
        if not parsed:
            continue
        subtype, cost = parsed
        if not splice_matches_spell(subtype, type_line):
            continue
        if not _splice_choices_are_makeable(
                game, strip_splice_line(getattr(c, 'oracle_text', '') or '')):
            continue
        out.append((c, cost))
    out.sort(key=lambda pair: (cmc_of_cost_string(pair[1]),
                               getattr(pair[0], 'name', '')))
    return out


def spell_face_for_gates(card):
    """The card, or a synthetic Card for the split HALF being cast.

    `Card.oracle_text` on a split card is face 0 only, so any gate that reads
    it judges the wrong spell whenever the other half is being cast: Commit
    targets and Memory does not, so Memory was refused for "no valid targets"
    on Commit's requirement. Every CR 601.2c gate must evaluate the half.

    Shared by `_validate_cast` and BOTH executors' pre-cast targeting gates —
    fixing only the first left the executors judging face 0, which is the
    two-paths divergence this codebase keeps paying for.
    """
    index = getattr(card, 'cast_as_split_half', -1)
    texts = getattr(card, 'split_texts', None) or []
    if index is None or index < 0 or index >= len(texts):
        return card
    from mtg.models import Card
    names = getattr(card, 'split_names', None) or []
    types = getattr(card, 'split_types', None) or []
    costs = getattr(card, 'split_costs', None) or []
    return Card(
        name=names[index] if index < len(names) else card.name,
        mana_cost=costs[index] if index < len(costs) else card.mana_cost,
        type_line=types[index] if index < len(types) else card.type_line,
        oracle_text=texts[index],
    )


def unearthed_leaves_to_exile(card) -> bool:
    """CR 702.83a: an unearthed permanent is exiled "at the beginning of the
    next end step OR if it would leave the battlefield".

    The end-step half is a scheduled delayed trigger; this is the other half,
    and it is the one that matters in practice — an unearthed creature is a
    hasty attacker, so it usually dies in combat before the end step ever
    arrives. Without this it lands in the graveyard and can simply be
    unearthed AGAIN, which is exactly the recursion the printed exile clause
    exists to forbid.

    Death sites only, deliberately: bouncing your own unearthed creature is
    not a line anyone takes, and the zone-change replacement that would cover
    every departure is a bigger seam than this wave should open.
    """
    return bool(getattr(card, '_unearthed', False))


def exile_after_resolution_reason(card) -> str:
    """Why a spell cast FROM THE GRAVEYARD is exiled as it resolves, or "".

    Whether the card is exiled depends on which permission allowed the cast:

      flashback  (CR 702.34a)  exiles — native, or granted by Snapcaster Mage
      jump-start (CR 702.132a) exiles
      aftermath  (CR 702.127a) exiles
      escape     (CR 702.139)  does NOT — it has no exile clause at all, which
                               is the entire point: Cling to Dust escapes
                               again and again out of the same graveyard.

    Aug 3, 2026: before this, all three executors ran a blanket
    "if from_graveyard: graveyard → exile" after every graveyard cast, so an
    escaped instant was exiled after its first cast and the mechanic's
    recursion never worked. Escape is the ONLY behavior this changes —
    everything else keeps today's default of "exile", so a Snapcaster-granted
    flashback (which has no printed keyword to detect) still exiles.
    """
    oracle = getattr(card, 'oracle_text', '') or ''
    if parse_escape_cost(oracle):
        return ""
    if has_jump_start(oracle):
        return "jump-start"
    index = getattr(card, 'cast_as_split_half', -1)
    if index is not None and index >= 0 and aftermath_half_index(card) == index:
        return "aftermath"
    return "flashback"


def commander_declines_graveyard_redirect(card) -> bool:
    """CR 903.9a's command-zone redirect is a MAY, and autoplay always took
    it — which made escape commanders structurally unable to reach the
    graveyard they cast from (Aug 1 batch-12 reviewer: Kroxa was hardcast
    four times at tax 2/4/6/8 for one discard each while his flat escape
    cost sat unreachable). Escape commanders now decline the DEATH →
    graveyard redirect in the autoplay choice model. Redirects from
    exile / hand / library still happen (escape can't cast from those
    zones), and the countered-spell + legend-rule sites keep redirecting
    (conservative scope — noted at the sites)."""
    try:
        return parse_escape_cost(getattr(card, 'oracle_text', '') or '') is not None
    except (TypeError, AttributeError):
        return False


def command_zone_owner(game, card, fallback):
    """Resolve which player's command zone a commander belongs in.

    CR 903.9a: a commander leaving the battlefield goes to its OWNER's
    command zone — never the controller's. June 10 audit (C7): Animate Dead
    stole Tymna; when she died under the thief's control she landed in the
    THIEF's command zone and showed up in their castable list with command
    tax, permanently locking the owner out of his own commander. Falls back
    to the supplied player when owner_index is missing/invalid (un-stolen
    commanders: owner == controller, so behavior is unchanged).
    """
    idx = getattr(card, 'owner_index', None)
    try:
        if idx is not None and 0 <= int(idx) < len(game.players):
            return game.players[int(idx)]
    except (TypeError, ValueError):
        pass
    return fallback


def strip_combat_state(game, card):
    """Narrow combat-state strip for a permanent LEAVING the battlefield.

    Aug 7 batch audit (C-3): Korvold's attack trigger sacrificed a fellow
    declared attacker (Priest of Forgotten Gods); the object went to the
    graveyard with `.attacking=True` and the per-combat clear couldn't find
    it on the battlefield — the end-turn [COMBAT-SWEEP] net caught the flag,
    but the leak class is closed here at the leave chokepoints. Deliberately
    NOT `reset_battlefield_state` (that also wipes counters, which would
    break persist's CR 702.77b "no -1/-1 counters" gate on a dying creature
    and any dies trigger reading counters).
    """
    card.attacking = False
    card.attacking_player = None
    card.blocking = []
    card.blocked_by = []
    try:
        if getattr(game, 'attackers', None) and card.id in game.attackers:
            game.attackers.remove(card.id)
        if getattr(game, 'blockers', None) and card.id in game.blockers:
            del game.blockers[card.id]
    except (AttributeError, TypeError):
        pass


def route_dead_permanent(game, card, holder, to_exile=False):
    """Shared zone routing for a permanent DESTROYED/removed by a MASS
    handler — the class fix for the Aug 7 batch audit's G3-1 (CRITICAL).

    Mirrors the single-target destroy path's three routing rules, which the
    four mass handlers (destroy_all_creatures / destroy_all_permanents /
    destroy_by_power / exile-by-type callers use their own exile variant)
    all skipped with a bare `p.graveyard.append(...)`:
      1. CR 903.9a commander redirect to the OWNER's command zone (with the
         escape-commander decline, e.g. Kroxa) — live-confirmed
         game-deciding in game_1535060120164376726: Damnation put Korvold in
         the GRAVEYARD, and Rise of the Dark Realms then put the opponent's
         own commander onto the caster's battlefield for the lethal swing.
      2. CR 404.3: a destroyed card goes to its OWNER's graveyard, not the
         battlefield-holder's (stolen creatures were changing hands
         permanently).
      3. CR 702.83a: an unearthed creature leaving the battlefield is exiled.
    Also strips combat state (C-3). Returns the destination string for
    display: 'command_zone', 'exile', or 'graveyard'.
    """
    strip_combat_state(game, card)
    if (getattr(card, 'is_commander', False)
            and getattr(game, 'format', '') in ('commander', 'edh', 'brawl', 'oathbreaker')
            and not (not to_exile and commander_declines_graveyard_redirect(card))):
        # CR 903.9a applies to graveyard, exile, hand, and library moves
        # alike; the escape-commander decline only makes sense for the
        # graveyard destination (Kroxa wants to BE in the graveyard).
        _zone_owner = command_zone_owner(game, card, holder)
        if not hasattr(_zone_owner, 'command_zone'):
            _zone_owner.command_zone = []
        _zone_owner.command_zone.append(card)
        print(f"  [CR-903.9] Commander {card.name} redirected from "
              f"{'exile' if to_exile else 'graveyard'} → command zone "
              f"(owner={_zone_owner.name}, mass removal)")
        return 'command_zone'
    _dest_owner = owner_of(game, card, holder)
    if to_exile:
        _dest_owner.exile.append(card)
        return 'exile'
    if unearthed_leaves_to_exile(card):
        _dest_owner.exile.append(card)
        print(f"[UNEARTH] {card.name} destroyed → exiled (CR 702.83a, mass removal)")
        return 'exile'
    _dest_owner.graveyard.append(card)
    return 'graveyard'


def clamp_noop_reason(reason: str, max_len: int = 160) -> str:
    """Clamp Tier-3 no_action reasons for player display.

    June 10 audit: a Land Tax no_action came back as a full chain-of-thought
    paragraph (with a rules error in it — it counted the Land Tax enchantment
    itself as a land) and was posted verbatim to Discord. Long or
    multi-sentence reasons read as leaked model internals; show a neutral
    line instead. The caller's console print keeps the full text for audits.
    """
    r = (reason or "").strip()
    if not r:
        return r
    sentence_count = r.count(". ") + (1 if r.endswith(".") else 0)
    if len(r) > max_len or sentence_count >= 3:
        return "condition not met — no effect"
    return r


def burst_dedup_key(content: str) -> str:
    """Bucket key for the per-turn identical-message burst dedup (Layer 3,
    mtg/cog.py:_autoplay_send).

    Two normalizations:
    1. Strip trailing numeric parentheticals ("(life: 27)") so running-total
       variation doesn't defeat the dedup (May 23).
    2. Strip a trailing bolded card name ONLY for draw/discard/exile/reveal
       shapes ("Guardian Project - Rick draws **<varying>**", May 25 F8).
       June 10 audit (V19): the unrestricted strip made every
       "(sparkles) P cast **X**" line in a turn share one key, so every 3rd+
       DISTINCT cast was silently suppressed (44/139 games - creatures
       visibly "attacked out of nowhere"). Announcement shapes now keep
       their card-name keys.
    """
    import re as _re
    key = _re.sub(r"\s*\([^)]*\d[^)]*\)\s*$", "", content or "")
    if _re.search(r"\b(draws?|discards?|exiles?|reveals?)\b[^*]*\*\*[^*]+\*\*\s*$", key):
        key = _re.sub(r"\s*\*\*[^*]+\*\*\s*$", "", key)
    return key


def apnap_order_died(died_pairs, game):
    """Order simultaneous deaths NAP-first for immediate-mode dies-trigger
    resolution.

    CR 603.3b: the Active Player puts their triggers on the stack first
    (bottom), the Non-Active Player second (top); LIFO resolution means the
    NAP's triggers RESOLVE first. Immediate mode emulates that by scanning
    the NAP's dead creatures first. Python's sort is stable, so insertion
    order is preserved within each player's group (standing in for the
    controller's chosen relative order of their own triggers).

    June 10: extracted from the three drain sites (mtg/triggers.py dies
    scan, mtg/engine.py phase-transition drain + SBA-sibling drain) so the
    ordering is unit-testable - the autoplay matrix has never produced a
    both-sides simultaneous board wipe with dies-triggers on both sides
    (known-open coverage gap since May 30). Falls back to insertion order
    on any error, matching the previous inline behavior.
    """
    try:
        active_idx = game.active_player_index
        return sorted(
            died_pairs,
            key=lambda pair: 0 if game.players.index(pair[1]) != active_idx else 1,
        )
    except (ValueError, AttributeError, TypeError, IndexError):
        # Player not in game.players / malformed pair — insertion order.
        return list(died_pairs)


# --------------------------------------------------------------------------- #
# Static cost reduction (CR 601.2f)                                            #
# --------------------------------------------------------------------------- #
# "Black spells you cast cost {1} less to cast." — implemented July 26, 2026.
# Before that, `ManaCost.cost_reductions` was declared in rules/mana.py and
# NEVER written by anything, so every reducer was a blank card. Nine of them
# sit in the test decks, including Baral, Chief of Compliance — a COMMANDER
# whose entire defining ability was a no-op in the deck named after him.
#
# CR 601.2f: cost reduction applies to the TOTAL cost but can only reduce the
# GENERIC portion — colored pips are never reducible. Callers must cap the
# returned amount at the generic actually available (see _compute_alt_costs).

_COST_REDUCTION_RE = re.compile(
    r'(?P<restrict>[^.\n]*?)\bspells?\s+you\s+cast\b'
    r'(?P<zone>[^.\n]*?)'
    r'\bcosts?\s*\{(?P<amt>\d+)\}\s*less\s+to\s+cast',
    re.IGNORECASE,
)

_REDUCTION_COLORS = {
    'white': 'W', 'blue': 'U', 'black': 'B', 'red': 'R', 'green': 'G',
}
# Card TYPES (CR 205.2a). Anything left over in the restriction clause is
# treated as a subtype — that is how Danitha's "Aura and Equipment spells"
# works, since Aura and Equipment are subtypes, not types.
_REDUCTION_TYPES = {
    'creature', 'instant', 'sorcery', 'artifact', 'enchantment',
    'planeswalker', 'land', 'battle', 'kindred', 'tribal',
}
# Words that appear in the restriction clause but carry no restriction.
_REDUCTION_NOISE = {
    'and', 'or', 'the', 'a', 'an', 'your', 'you', 'this', 'these', 'other',
    'spell', 'spells', 'cast', '',
}


def spell_colors_from_cost(mana_cost: str) -> set:
    """The spell's colors, per CR 202.2 — derived from its mana cost.

    Deliberately NOT `card.color_identity`: identity also picks up colors
    from oracle text (an activated ability's {B}), which would make a
    colorless artifact with a black activation count as a "Black spell".
    Hybrid ({W/U}) and Phyrexian ({W/P}) symbols each contribute their
    color, which is correct — such a spell IS both colors.
    """
    out = set()
    for sym in re.findall(r'\{([^}]+)\}', mana_cost or ''):
        for ch in sym.upper():
            if ch in ('W', 'U', 'B', 'R', 'G'):
                out.add(ch)
    return out


def _restriction_applies(restrict: str, card) -> bool:
    """Does a parsed restriction clause ("Black creature", "Noncreature") match?

    Within a category the match is OR ("Instant and sorcery" = either);
    across categories it is AND ("Black creature" = black AND a creature).

    NEGATION is handled explicitly. `non`-prefixed words are the whole point
    of the tax family ("Noncreature spells cost {1} more"), and a naive
    substring test gets them exactly backwards — 'creature' is a substring of
    'noncreature'. That is the documented Woodfall Primus trap from the
    July 24 audit, and it would make Thalia tax precisely the spells she is
    supposed to leave alone.
    """
    tokens = [t for t in re.split(r'[^a-z]+', (restrict or '').lower())
              if t and t not in _REDUCTION_NOISE]
    if not tokens:
        return True  # unrestricted: "Spells cost {1} more/less"

    type_line = (getattr(card, 'type_line', '') or '').lower()
    card_colors = spell_colors_from_cost(getattr(card, 'mana_cost', ''))

    want_colors, want_types, want_subtypes = set(), set(), set()
    for t in tokens:
        negated = False
        base = t
        if t.startswith('non') and len(t) > 3:
            stripped = t[3:]
            if stripped in _REDUCTION_TYPES or stripped in _REDUCTION_COLORS:
                negated, base = True, stripped
        if negated:
            # A negated clause is a hard veto, evaluated immediately: the
            # spell must NOT be that type/color for the effect to apply.
            if base in _REDUCTION_COLORS:
                if _REDUCTION_COLORS[base] in card_colors:
                    return False
            elif base in type_line:
                return False
            continue
        if base in _REDUCTION_COLORS:
            want_colors.add(_REDUCTION_COLORS[base])
        elif base in _REDUCTION_TYPES:
            want_types.add(base)
        else:
            want_subtypes.add(base)

    if want_colors and not (card_colors & want_colors):
        return False
    if want_types and not any(t in type_line for t in want_types):
        return False
    if want_subtypes and not any(s in type_line for s in want_subtypes):
        return False
    return True


def _zone_ok(zone: str, from_graveyard: bool) -> bool:
    """Zone qualifier gate. Unmodelled qualifiers REFUSE rather than over-apply."""
    zone = (zone or '').lower()
    if 'from your graveyard' in zone:
        return bool(from_graveyard)
    # "from exile", "from anywhere but your hand", ... — not modelled.
    return 'from' not in zone


def _reduction_applies(restrict: str, zone: str, card, from_graveyard: bool) -> bool:
    """Does a parsed reduction clause apply to `card`?"""
    if not _zone_ok(zone, from_graveyard):
        return False
    return _restriction_applies(restrict, card)


def compute_cost_reduction(player, card, *, from_graveyard: bool = False):
    """Total generic cost reduction for `card` from `player`'s battlefield.

    Returns (amount, [source names]). The amount is UNCAPPED — the caller
    must clamp it to the generic portion actually present (CR 601.2f), since
    a reduction can never eat a colored pip.

    Only the controller's own permanents are scanned: every printed reducer
    of this shape says "you cast".
    """
    total = 0
    sources = []
    for perm in (player.active_battlefield()
                 if hasattr(player, 'active_battlefield') else player.battlefield):
        if perm is card:
            continue
        oracle = getattr(perm, 'oracle_text', '') or ''
        if 'less to cast' not in oracle.lower():
            continue
        for m in _COST_REDUCTION_RE.finditer(oracle):
            if _reduction_applies(m.group('restrict'), m.group('zone'),
                                  card, from_graveyard):
                try:
                    amt = int(m.group('amt'))
                except (TypeError, ValueError):
                    continue
                total += amt
                sources.append(perm.name)
    return total, sources


# --------------------------------------------------------------------------- #
# Static cost INCREASES — "spells cost {N} more to cast" (CR 601.2f)           #
# --------------------------------------------------------------------------- #
# Thalia, Sphere of Resistance, Thorn of Amethyst, Grand Arbiter, Vryn
# Wingmare, Archon of Emeria, Lodestone Golem, Elspeth Conquers Death II...
#
# Scope differs from reductions in a way that matters: reducers all say "you
# cast" (controller-only), but taxes are frequently SYMMETRIC ("Noncreature
# spells cost {1} more to cast" — Thalia taxes her own controller too) or
# explicitly opponent-facing ("Spells your opponents cast cost {1} more").
# So this scans EVERY battlefield and resolves the scope per clause, relative
# to the permanent's own controller.
_COST_INCREASE_RE = re.compile(
    r'(?P<restrict>[^.\n]*?)\bspells?\b(?P<scope>[^.\n]*?)'
    r'\bcosts?\s*\{(?P<amt>\d+)\}\s*more\s+to\s+cast',
    re.IGNORECASE,
)


def _increase_scope_matches(scope: str, restrict: str, source_controller,
                            caster) -> bool:
    """Whose spells does this clause tax?

    "your opponents cast" -> only casters who are NOT the source's controller
    "you cast"            -> only the source's controller
    (unqualified)         -> everyone, including the source's controller
    """
    blob = f"{restrict or ''} {scope or ''}".lower()
    if 'opponent' in blob:
        return caster is not source_controller
    if re.search(r'\byou\s+cast\b', blob):
        return caster is source_controller
    return True


def compute_cost_increase(game, player, card):
    """Total generic cost increase for `player` casting `card`.

    Returns (amount, [source names]). Scans every player's battlefield, since
    taxes are commonly symmetric or opponent-facing.
    """
    total = 0
    sources = []
    players = getattr(game, 'players', None) or [player]
    for owner in players:
        bf = (owner.active_battlefield()
              if hasattr(owner, 'active_battlefield') else owner.battlefield)
        for perm in bf:
            if perm is card:
                continue
            oracle = getattr(perm, 'oracle_text', '') or ''
            # Saga chapter text is not a static ability of the permanent.
            if 'saga' in (getattr(perm, 'type_line', '') or '').lower():
                continue
            if 'more to cast' not in oracle.lower():
                continue
            for m in _COST_INCREASE_RE.finditer(oracle):
                scope, restrict = m.group('scope'), m.group('restrict')
                # A trailing "your opponents"/"you" lives in `scope`; keep it
                # out of the type/color restriction parsing.
                if not _increase_scope_matches(scope, restrict, owner, player):
                    continue
                if not _restriction_applies(
                        re.sub(r'\b(your\s+opponents?|you)\b', '', restrict or '',
                               flags=re.IGNORECASE), card):
                    continue
                try:
                    total += int(m.group('amt'))
                except (TypeError, ValueError):
                    continue
                sources.append(perm.name)

    # Chapter-created taxes persist independently of the Saga that created
    # them and expire as the source controller's next turn begins.
    _effects = getattr(game, '_temporary_cost_increases', []) or []
    _active_effects = []
    for _effect in _effects:
        try:
            _expires = int(_effect.get('expires_turn', 0))
        except (TypeError, ValueError):
            continue
        if game.turn_number >= _expires:
            continue
        _active_effects.append(_effect)
        if player.name == _effect.get('controller'):
            continue
        if (_effect.get('restriction') == 'noncreature'
                and card.is_creature(game=game)):
            continue
        try:
            _amount = int(_effect.get('amount', 0))
        except (TypeError, ValueError):
            continue
        if _amount <= 0:
            continue
        total += _amount
        sources.append(_effect.get('source', 'temporary effect'))
    game._temporary_cost_increases = _active_effects
    return total, sources


def damage_source_colors(game, source_card=None, source_name: str = "",
                         source_id: str = ""):
    """Colors of a damage SOURCE, for color-gated replacement effects.

    Aug 2 batch-14 audit: Torbran, Thane of Red Fell ("If a RED source you
    control would deal damage to an opponent or a permanent an opponent
    controls, it deals that much damage plus 2 instead") was entirely
    unimplemented — GameEvent carried no notion of the source's color, so
    every life total in the cube game ran 2 low from turn 15 on.

    Colors come from the mana cost (CR 202.2), never from color_identity —
    identity absorbs colors from oracle text, which would make a colorless
    artifact with a {B} activation a "black source". Returns None when the
    source can't be resolved, and a color-gated effect must DECLINE on None
    rather than guess: an unpopulated call site can never silently apply
    the wrong bonus.

    A live card object is preferred. Otherwise the source is searched for by
    id then by name across the zones a damage source can actually occupy: the
    battlefield (a permanent), the stack (a burn spell mid-cast), and the
    graveyard/exile (a burn spell whose damage applies AFTER it has left the
    stack — `_dispatch_resolution` pops the entry before running the effect,
    so this is the common case for Chain Lightning and friends).
    """
    if source_card is not None:
        cost = getattr(source_card, 'mana_cost', '') or ''
        return spell_colors_from_cost(cost)
    if game is None:
        return None
    if not source_name and not source_id:
        return None

    def _match(c):
        if source_id and getattr(c, 'id', None) == source_id:
            return True
        return bool(source_name) and names_match(getattr(c, 'name', ''),
                                                 source_name)

    for p in getattr(game, 'players', []) or []:
        for c in getattr(p, 'battlefield', []) or []:
            if _match(c):
                return spell_colors_from_cost(getattr(c, 'mana_cost', '') or '')
    for entry in getattr(game, 'stack', []) or []:
        c = getattr(entry, 'card', None)
        if c is not None and _match(c):
            return spell_colors_from_cost(getattr(c, 'mana_cost', '') or '')
    for p in getattr(game, 'players', []) or []:
        for zone in ('graveyard', 'exile'):
            for c in getattr(p, zone, []) or []:
                if _match(c):
                    return spell_colors_from_cost(
                        getattr(c, 'mana_cost', '') or '')
    return None


# ---------------------------------------------------------------------------
# Aug 2, 2026 — KEYWORD ATTACK TRIGGERS (CR 702).
#
# A keyword ability states its trigger in REMINDER text, or on a bare keyword
# line with no reminder at all: Emrakul, the Aeons Torn's whole annihilator
# clause is the tail of "Flying, protection from spells that are one or more
# colors, annihilator 6". The paragraph-shape detector
# (_is_self_attack_trigger_paragraph) requires a paragraph that STARTS with
# "whenever", so none of these were reachable — Emrakul attacked and the
# defending player sacrificed nothing, in the deck built around casting her.
#
# Parsed from the keyword LINE rather than the reminder, so a card that omits
# reminder text (older printings, or a combined keyword line) still works.
# ---------------------------------------------------------------------------

_KEYWORD_LINE_RE = re.compile(r'^[^.\n]*$')


def _keyword_line_tokens(oracle_text: str):
    """Comma-separated tokens from lines that are keyword lines.

    A keyword line has no sentence punctuation outside reminder text — that
    is what separates "Flying, protection from X, annihilator 6" from a real
    sentence that happens to contain a comma.
    """
    for raw in (oracle_text or '').split('\n'):
        line = re.sub(r'\([^)]*\)', '', raw).strip().rstrip('.')
        if not line or '.' in line:
            continue
        low = line.lower()
        # A GRANT line ("Other creatures you control have flying, melee")
        # tokenizes to a bare "melee" and would hand the trigger to the
        # SOURCE, which merely grants it — the July-21 Yidris cascade-grant
        # class, found by mutation-testing this parser. A keyword line never
        # contains grant language.
        if re.search(r'\b(have|has|gains?|gain)\b', low):
            continue
        for tok in line.split(','):
            tok = tok.strip().lower()
            if tok:
                yield tok


_SPEND_RESTRICTION_FAMILIES = (
    # Aug 8, 2026 (queue R4): the "Spend this mana only ..." clause family,
    # swept against the full Scryfall bulk BEFORE these regexes were
    # written (the alt-cost wave discipline): ~180 cards carry the phrase
    # (oracle_text scan; ~188 counting card faces — the bulk drifts);
    # the three period-ANCHORED families below classify ~37 of them —
    # including all three inventory cards (Sarkhan, Fireblood → dragon;
    # Jaya Ballard → instant/sorcery; Castle Garenbrig → creature, whose
    # "or activate abilities of creatures" half is deliberately
    # under-permitted: our activation payments carry no spending context,
    # so ability spends HOLD, the safe direction). The anchors matter:
    # unanchored, "creature spells OF THE CHOSEN TYPE" (Unclaimed
    # Territory) would classify as plain creature_spell and OVER-permit.
    # The remaining 143 unmodeled variants return an 'unmodeled:' key that
    # _restricted_mana_allows never matches — the mana is HELD, per the
    # July-26 cost-reduction precedent (never grant a benefit you can't
    # gate).
    (re.compile(r'spend this mana only to cast dragon spells\.'),
     'dragon_spell'),
    (re.compile(r'spend this mana only to cast (?:an )?instant '
                r'(?:or|and) sorcery spells?\.'),
     'instant_sorcery_spell'),
    (re.compile(r'spend this mana only to cast (?:a )?creature spells?'
                r'(?: or activate abilities of creatures)?\.'),
     'creature_spell'),
)


def parse_mana_spend_restriction(text):
    """Parse a printed "Spend this mana only ..." clause to a predicate key.

    Returns None when the text carries no such clause (ordinary mana), a
    known predicate key ('dragon_spell' / 'instant_sorcery_spell' /
    'creature_spell') for the modeled families, or an 'unmodeled:...' key
    for any other variant — which Player._restricted_mana_allows never
    matches, so that mana is added RESTRICTED and held unspent (the safe
    direction; an unmodeled restriction must never become unrestricted
    mana, which is exactly the Jaya Ballard / Castle Garenbrig producer
    leak this helper closes).
    """
    low = (text or '').lower()
    if 'spend this mana only' not in low:
        return None
    for pat, key in _SPEND_RESTRICTION_FAMILIES:
        if pat.search(low):
            return key
    m = re.search(r'spend this mana only[^.\n]*', low)
    return 'unmodeled:' + (m.group(0)[:80] if m else 'unknown')


def has_city_blessing(game, player) -> bool:
    """Ascend / the city's blessing (CR 702.131) — Aug 8 batch audit (#2).

    Ten or more permanents awards the city's blessing for the REST OF THE
    GAME (CR 702.131c-d): sticky once earned, even if the permanent count
    later drops. Compute-on-read with a sticky award, and the award is
    CR-correct by construction: every read site lives inside an Ascend
    card's OWN condition check (Wayward Swordtooth's combat gate in
    Card.can_attack/can_block; Tendershoot Dryad's anthem condition in
    GameState._static_condition_met), so the award can never fire without
    an Ascend source on the battlefield — which is what CR 702.131a
    requires. The residual miss (a player momentarily at ten permanents
    between reads, dropping below before any Ascend card asks) is an
    undercount, the safe direction.

    Before this existed the mechanic was entirely unimplemented: in
    game_1535486721779568700 Wayward Swordtooth blocked and killed Jorn at
    six permanents and attacked on three turns, and Tendershoot Dryad's
    anthem was permanently OFF (its reminder text routed into the generic
    "control N or more <type>" static-condition regex, which counts
    permanents whose TYPE LINE contains the word "permanent" — never true).
    """
    if getattr(player, 'city_blessing', False):
        return True
    if len(getattr(player, 'battlefield', []) or []) >= 10:
        player.city_blessing = True
        print(f"[ASCEND] {player.name} gets the city's blessing "
              f"(ten or more permanents — permanent for the rest of the game)")
        return True
    return False


def parse_attack_keywords(oracle_text: str) -> dict:
    """Keyword abilities that trigger on attack. Returns {name: value}.

    annihilator -> int N (CR 702.85), the others -> True.
    """
    out = {}
    text = oracle_text or ''
    for tok in _keyword_line_tokens(text):
        m = re.match(r'annihilator\s+(\d+)$', tok)
        if m:
            out['annihilator'] = int(m.group(1))
        elif tok == 'battle cry':
            out['battle_cry'] = True
        elif tok == 'melee':
            out['melee'] = True
        elif tok == 'mentor':
            out['mentor'] = True
        elif tok == 'exalted':
            out['exalted'] = True
    # (A second, line-anchored detection pass lived here for the ability-word
    # form. Mutation testing showed it was DEAD CODE: all four of these are
    # keyword ABILITIES, always printed as a bare keyword line or mid-line
    # among other keywords, so the tokenizer above already catches every real
    # printing — and the redundancy made a mutant that deleted one path
    # survive. Removed rather than kept as decorative defence.)
    return out


# ---------------------------------------------------------------------------
# Aug 2, 2026 — ABILITY-WORD CONDITIONS (CR 207.2c).
#
# Delirium / metalcraft / morbid / coven / threshold gate an effect on a board
# or graveyard state. None of them existed, so every card carrying one used
# its WEAK half forever: Tragic Slip was -1/-1 rather than -13/-13, Unholy
# Heat dealt 2 instead of 6, Dragon's Rage Channeler never grew or flew.
#
# Predicates only — the consumers (templates, static P/T) read them, so a
# card that gains a new condition gets the check for free.
# ---------------------------------------------------------------------------

_CARD_TYPES = ("artifact", "creature", "enchantment", "instant", "land",
               "planeswalker", "sorcery", "battle", "kindred", "tribal")


def graveyard_card_types(player) -> set:
    """Distinct CARD TYPES among cards in a player's graveyard (CR 205.2a)."""
    found = set()
    for c in (getattr(player, 'graveyard', []) or []):
        tl = (getattr(c, 'type_line', '') or '').lower()
        for t in _CARD_TYPES:
            if t in tl:
                found.add(t)
    return found


def has_delirium(player) -> bool:
    """Four or more card types among cards in your graveyard (CR 702.x)."""
    return len(graveyard_card_types(player)) >= 4


def has_threshold(player) -> bool:
    """Seven or more cards in your graveyard."""
    return len(getattr(player, 'graveyard', []) or []) >= 7


def has_metalcraft(player) -> bool:
    """You control three or more artifacts."""
    return sum(
        1 for c in (getattr(player, 'battlefield', []) or [])
        if 'artifact' in (getattr(c, 'type_line', '') or '').lower()
    ) >= 3


# "You may cast <types> spells from the top of your library" (Augur of
# Autumn's coven half, Vizier of the Menagerie, Elven Chorus, ...).
#
# The phrase must be CONTIGUOUS. Every cascade card in the game contains both
# "from the top of your library" (in its exile clause) and "You may cast" (in
# its free-cast clause), so a two-substring test grants library casting to 32
# cards that have no such ability. Requiring "cast ... spells from the top of
# your library" as one phrase matches 27 cards across all of Scryfall and
# ZERO cascade cards (swept 2026-08-03).
LIBRARY_TOP_CAST_RE = re.compile(
    r'you may cast ([a-z, \-]*?)spells? from the top of your library',
    re.IGNORECASE)

# Grant clauses this cannot honour, and therefore declines outright rather
# than granting a free version of (the `damage_source_colors` convention —
# an unmodelled rider is a reason to decline, not to guess).
_LIBRARY_TOP_UNMODELED = (
    'by removing',          # Falco Spara — additional cost
    'by sacrificing',       # Into the Pit — additional cost
    'in addition to paying',
    'once each turn',       # Cemetery Illuminator, Johann — per-turn limit
)

# Class levels (CR 716.2) are not modelled anywhere in this engine, so a
# level-gated grant would read as unconditional and active from the moment
# the Class hits the battlefield. Ranger Class puts "You may cast creature
# spells from the top of your library" under "{3}{G}: Level 3" — a separate
# LINE, so neither the ability-word stripper nor the "as long as" check can
# see the gate. A Class is therefore declined outright: the condition is
# real and we cannot evaluate it.
_CLASS_LEVEL_TYPE = 'class'


def library_top_cast_types(player, game=None) -> set:
    """Card types this player may cast off the TOP of their library.

    v1 recognises the `creature` grant only — the family the live inventory
    needs (Augur of Autumn) — while parsing the type phrase generically so
    widening to the subtype grants (Goblin, Dragon, Merfolk...) is a change
    to one comparison rather than a new parser.

    A CONDITIONAL grant is honoured only when the condition is one we model.
    Augur's is coven, which `has_coven` computes; anything else (Summoning
    Materia's "as long as this Equipment is attached to a creature") declines,
    so an unmodelled condition can never read as permanently satisfied.
    """
    types = set()
    for perm in (getattr(player, 'battlefield', []) or []):
        if getattr(perm, '_phased_out', False):
            continue
        if _CLASS_LEVEL_TYPE in (getattr(perm, 'type_line', '') or '').lower():
            continue  # level-gated (CR 716.2) — see _CLASS_LEVEL_TYPE
        for line in ((getattr(perm, 'oracle_text', '') or '')
                     .split('\n')):
            m = LIBRARY_TOP_CAST_RE.search(line)
            if not m:
                continue
            low = line.lower()
            if any(marker in low for marker in _LIBRARY_TOP_UNMODELED):
                continue
            # Ability words ("Coven — ", "Solved — ") are flavour, CR 207.2c.
            body = re.sub(r'^\s*[A-Za-z\' ]+\s*[—–-]\s*', '', line).strip()
            if body.lower().startswith('as long as'):
                if 'creatures with different powers' in low:
                    if not has_coven(player, game):
                        continue
                else:
                    continue  # condition we do not model — decline
            phrase = (m.group(1) or '').strip().lower()
            if phrase == 'creature':
                types.add('creature')
    return types


# The Vivid MANA ability (Bloom Tender, Faeburrow Elder). Matched on the
# phrase rather than a card name — the `_all_lands_are_all_basic_types`
# convention, since a name substring is how the Coldsteel-Heart-vs-Painter's-
# Servant misfire happened.
#
# BOTH halves are required, ON THE SAME LINE. "for each color among permanents
# you control" alone is a COUNTING phrase that nine cards share, and seven of
# them have no mana ability at all: Soul of Ravnica and Mondo Gecko draw cards
# with it, Conqueror's Flail pumps with it, Wildvine Pummeler and Rime Chill
# reduce a cost with it. The first version of this predicate tested the count
# alone and turned an EQUIPMENT into a mana source producing one mana of every
# colour — mana from nothing (CR 106.1), and an underpaid cast (CR 601.2g).
# Same-line scoping is what makes it the two cards this exists for; Chromatic
# Orrery is the case that proves per-line matters, since it has a real mana
# ability AND the counting phrase, on different lines.
VIVID_COUNT_PHRASE = 'for each color among permanents you control'
VIVID_ADD_PHRASE = 'add one mana of that color'


def is_vivid_mana_line(oracle_text: str) -> bool:
    """Does this card have the Vivid MANA ability (not merely the count)?"""
    for line in (oracle_text or '').split('\n'):
        low = line.lower()
        if VIVID_COUNT_PHRASE in low and VIVID_ADD_PHRASE in low:
            return True
    return False


def colors_among_permanents(player) -> set:
    """The distinct colors among permanents this player controls (CR 202.2).

    Never `color_identity` — identity also absorbs colors from oracle text,
    which would make a colorless artifact with a {B} activated ability count
    as a black permanent (the same reason `spell_colors_from_cost` exists).

    Which of the two branches runs, stated accurately because it is easy to
    get backwards: `deck_loader` stamps `card.colors` from Scryfall on EVERY
    loaded card, so in production the `colors` branch handles essentially
    everything and the mana-cost fallback only catches permanents Scryfall
    calls colorless. Scryfall's `colors` IS the CR 202.2 colour including
    colour indicators, so it is strictly better than the cost where the two
    differ (Dryad Arbor is green with no mana cost). The fallback is what
    tests and hand-built Cards hit, and what covers a token created without
    an explicit colour list.

    Consequence worth stating because it looks like a bug and is not: BASIC
    LANDS ARE COLORLESS and contribute nothing. Bloom Tender alongside four
    basics sees only its own {1}{G} and taps for a single {G} — which is the
    printed, correct behavior.

    Painter's Servant-style color-ADDING effects (rules/layers.py Layer 5)
    are not consulted; there is no effective-colors accessor reachable from
    Player, and over-counting here would over-advertise mana. Under-counting
    is the safe direction (see `one_tap_mana_total`).
    """
    out = set()
    for perm in (getattr(player, 'battlefield', []) or []):
        if getattr(perm, '_phased_out', False):
            continue
        tok_colors = getattr(perm, 'colors', None)
        if tok_colors:
            out.update(str(c).upper() for c in tok_colors
                       if str(c).upper() in ('W', 'U', 'B', 'R', 'G'))
            continue
        out |= spell_colors_from_cost(getattr(perm, 'mana_cost', '') or '')
    return out


def has_morbid(game) -> bool:
    """A creature died this turn.

    Reads the per-turn flag stamped by the CREATURE_DIED accumulator (the
    single choke point every death path reaches) and cleared at turn
    advance — the wave-scoped `_recently_died` list is reset mid-turn and
    cannot answer this question.
    """
    return bool(getattr(game, '_creature_died_this_turn', False))


def has_coven(player, game=None) -> bool:
    """You control three or more creatures with DIFFERENT powers.

    CR 702.26b: a phased-out permanent is treated as though it does not
    exist, so it cannot contribute a power. The skip was missing while this
    predicate had no consumer; the coven grant made it load-bearing.
    """
    powers = set()
    for c in (getattr(player, 'battlefield', []) or []):
        if getattr(c, '_phased_out', False):
            continue
        try:
            if not c.is_creature(game=game) if game is not None else not c.is_creature():
                continue
        except TypeError:
            if not c.is_creature():
                continue
        try:
            powers.add(int(c.get_effective_power(game)) if game is not None
                       else int(c.power or 0))
        except (AttributeError, TypeError, ValueError):
            continue
    return len(powers) >= 3


def activated_ability_restriction_failure(game, player, ability_text: str):
    """Return a pre-cost failure reason for supported printed restrictions.

    This deliberately starts with the restriction used by Inventors' Fair.
    Unknown ``Activate only if`` clauses are left to existing card-specific
    handlers rather than guessed at here.
    """
    text = (ability_text or '').lower()
    match = re.search(
        r'activate only if you control (a|an|one|two|three|four|five|six|'
        r'seven|eight|nine|ten|\d+)(?: or more)? artifacts?', text)
    if not match:
        return None
    raw = match.group(1)
    needed = int(raw) if raw.isdigit() else (_NUMBER_WORDS.get(raw) or 1)
    controlled = sum(
        1 for permanent in (getattr(player, 'battlefield', []) or [])
        if not getattr(permanent, '_phased_out', False)
        and 'artifact' in (getattr(permanent, 'type_line', '') or '').lower()
    )
    if controlled < needed:
        return (f"requires {needed} artifacts; {player.name} controls "
                f"only {controlled}")
    return None


def is_castable_from_exile(game, player, card) -> bool:
    """Whether this exact exiled object has a live cast permission."""
    if card.id in (getattr(player, 'playable_from_exile', None) or []):
        return True
    if getattr(card, '_adventure_exiled', False):
        return True
    # Aug 7 queue item Q3: Draugr Necromancer's cast permission — stamped by
    # the death redirect, valid while THE STAMPING Draugr remains on this
    # player's battlefield (adversarial review #10: a NEW Draugr must not
    # revive a lapsed permission — CR 607 linked abilities; #5: a
    # phased-out Draugr grants nothing, CR 702.26 — same live-computation
    # convention as the other statics). Lands excluded: the permission is
    # "cast", and lands aren't cast.
    if (getattr(card, '_castable_by_player', None) == getattr(player, 'name', None)
            and not card.is_land()):
        _stamp_id = getattr(card, '_draugr_source_id', None)
        for c in (getattr(player, 'battlefield', None) or []):
            if getattr(c, '_phased_out', False):
                continue
            if c.name != 'Draugr Necromancer':
                continue
            if _stamp_id is None or c.id == _stamp_id:
                return True
    if getattr(card, '_foretold', False):
        return getattr(card, '_foretold_turn', None) != game.turn_number
    record = (getattr(game, 'conditional_exile_casts', None) or {}).get(card.id)
    if not record or card.is_land():
        return False
    try:
        controller_index = game.players.index(player)
    except ValueError:
        return False
    return (int(record.get('controller_index', -1)) == controller_index
            and int(record.get('expires_turn', -1)) == game.turn_number)
