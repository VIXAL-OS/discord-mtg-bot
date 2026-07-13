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
            if len(sanitized) > 72:
                return f"⚡ **{card_name}** activates {bracket} ability: _{sanitized[:69]}…_"
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
