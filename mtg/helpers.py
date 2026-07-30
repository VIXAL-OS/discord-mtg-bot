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
    return True


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
    return total, sources
