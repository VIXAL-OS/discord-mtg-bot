"""
LLM adapter for using alternative providers (DeepSeek, OpenRouter) as drop-in
replacements for the Anthropic client. Duck-types the Anthropic response
interface so that ClaudePlayer, RulesEngine, and EffectExecutor work unchanged.

The MTG engine's API usage is uniform — every call is:
    response = self.client.messages.create(
        model=..., max_tokens=..., messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text
    usage = response.usage.input_tokens, response.usage.output_tokens

This adapter translates OpenAI-compatible responses to match that shape exactly.

May 18 audit also added streaming support via `messages.stream()`. The crashed
game's 28-min strategist hang showed that non-streaming has no kill switch —
once a request is in flight, the server can think for arbitrarily long and we
can't tell it to stop. With streaming, the bot can monitor inter-chunk delays
and close the socket if no token arrives in N seconds, propagating cancel
back to the server. Used by the strategist; actor calls stay non-streaming.

Usage:
    from rules.llm_adapter import create_deepseek_adapter, create_openrouter_adapter

    # DeepSeek (reads DEEPSEEK_API_KEY from env)
    adapter = create_deepseek_adapter()

    # OpenRouter (reads OPENROUTER_API_KEY from env, specify model)
    adapter = create_openrouter_adapter("openrouter/optimus-alpha")

    # Streaming (for the strategist's deadman-timer guard)
    async with adapter.messages.stream(messages=[...], system="...") as stream:
        async for chunk in stream.text_chunks():
            ...  # accumulate
        final_text = stream.full_text
        final_usage = stream.usage
"""

import asyncio
import os
import time


# ---------------------------------------------------------------------------
# Response shim classes — make OpenAI responses look like Anthropic responses
# ---------------------------------------------------------------------------

class _ContentBlock:
    """Mimics anthropic.types.ContentBlock with a .text attribute."""
    __slots__ = ('text',)

    def __init__(self, text: str):
        self.text = text


class _Usage:
    """Mimics anthropic.types.Usage with .input_tokens and .output_tokens."""
    __slots__ = ('input_tokens', 'output_tokens')

    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.input_tokens = prompt_tokens
        self.output_tokens = completion_tokens


class _AdaptedResponse:
    """Wraps an OpenAI ChatCompletion response to look like an Anthropic Message.

    After wrapping:
        response.content[0].text       -> the generated text
        response.usage.input_tokens    -> prompt tokens
        response.usage.output_tokens   -> completion tokens
        response.reasoning_content     -> separated reasoning trace (V4-Pro)
    """
    __slots__ = ('content', 'usage', 'reasoning_content')

    def __init__(self, openai_response):
        msg = openai_response.choices[0].message
        # DeepSeek V4-Pro (and other reasoning-capable OpenAI-compatible models)
        # return the prose answer in `message.content` and the chain-of-thought
        # in `message.reasoning_content` — two separate fields. When V4-Pro is
        # truncated mid-answer at the caller's timeout, `.content` is empty
        # but `.reasoning_content` may have useful text. Prefer `.content`;
        # fall back to `.reasoning_content` so a truncated answer at least
        # surfaces the reasoning rather than an empty string.
        content = (getattr(msg, 'content', None) or "").strip()
        reasoning = (getattr(msg, 'reasoning_content', None) or "").strip()
        text = content or reasoning
        self.content = [_ContentBlock(text)]
        self.reasoning_content = reasoning
        self.usage = _Usage(
            prompt_tokens=openai_response.usage.prompt_tokens,
            completion_tokens=openai_response.usage.completion_tokens,
        )


class _StreamingResponse:
    """Async-iterable wrapper around an OpenAI streaming completion.

    May 18 audit: added so the strategist call can monitor inter-chunk delays
    and abort if the server stops producing tokens. The crashed game's
    28-min hang showed that without streaming there's no kill switch — once
    a non-streaming request is in flight, the server can think for an
    arbitrary time and we can't cancel from our side. With streaming we can
    close the socket on a deadman timeout, which propagates back to the
    server (HTTP request cancel) and frees the GPU.

    Usage:
        async with adapter.messages.stream(messages=...) as stream:
            async for chunk in stream.text_chunks():
                # chunk is the most recent text delta (may be empty if a
                # reasoning-only chunk arrived).
                ...
            # After iteration completes (or breaks), the stream is closed.
            final_text = stream.full_text
            final_reasoning = stream.full_reasoning
            usage = stream.usage  # may be None if the server didn't emit
                                  # a final usage chunk (V4-Pro typically does).

    The async iterator yields per-chunk text deltas. Callers can also
    `await stream.next_chunk(timeout=N)` for explicit deadman-timer control.
    """

    def __init__(self, openai_stream, log_tag: str = "", namespace=None):
        self._openai_stream = openai_stream
        self._text_parts: list = []
        self._reasoning_parts: list = []
        self._final_usage = None
        self._log_tag = log_tag
        self._closed = False
        self._last_chunk_time = time.monotonic()
        # June 10 audit (V30): mid-stream exception, recorded so callers can
        # distinguish "stream exhausted" from "stream died" (usage will be
        # None either way; the silent None previously cascaded into an
        # AttributeError that discarded the memo 14×/batch).
        self.stream_error = None
        # Back-reference so we can post token totals to the namespace's
        # cumulative counters once the stream emits its final-usage chunk.
        # None on tests / standalone use — accounting just gets skipped.
        self._namespace = namespace
        self._usage_posted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False  # don't swallow exceptions

    async def close(self):
        """Close the underlying HTTP stream. Idempotent.

        This sends an HTTP request-cancel to the server, freeing its
        compute. Safe to call from a finally block or a deadman timer.
        """
        if self._closed:
            return
        self._closed = True
        # Post token totals to the namespace IF the server emitted a
        # final-usage chunk. Stream may close early (deadman timeout,
        # caller break) without one — in which case we skip accounting
        # rather than guess. Better to under-count than misrepresent.
        if self._final_usage is not None and not self._usage_posted and self._namespace is not None:
            try:
                pt = getattr(self._final_usage, 'prompt_tokens', 0) or 0
                ct = getattr(self._final_usage, 'completion_tokens', 0) or 0
                self._namespace._total_prompt_tokens += pt
                self._namespace._total_completion_tokens += ct
                # Cache stats if exposed (DeepSeek's prompt_cache_*).
                ch = getattr(self._final_usage, 'prompt_cache_hit_tokens', None)
                cm = getattr(self._final_usage, 'prompt_cache_miss_tokens', None)
                if ch is not None or cm is not None:
                    if not hasattr(self._namespace, '_total_cache_hit_tokens'):
                        self._namespace._total_cache_hit_tokens = 0
                        self._namespace._total_cache_miss_tokens = 0
                    self._namespace._total_cache_hit_tokens += ch or 0
                    self._namespace._total_cache_miss_tokens += cm or 0
                self._usage_posted = True
            except Exception as e:
                print(f"[{self._log_tag}] Stream usage-post error: {e}")
        try:
            # OpenAI SDK's Stream object has a .close() method that closes
            # the HTTP connection. Run in a thread because it may block.
            await asyncio.to_thread(self._openai_stream.close)
        except Exception as e:
            print(f"[{self._log_tag}] Stream close error: {e}")

    async def text_chunks(self):
        """Async generator yielding text deltas as they arrive.

        Each yielded value is a string (may be empty if a reasoning-only
        chunk arrived). Reasoning content is silently accumulated in
        `self._reasoning_parts` but not yielded — callers that want the
        reasoning can read `.full_reasoning` after the stream completes.

        Final-usage chunks (DeepSeek emits one at the end) are captured
        into `self._final_usage` and not yielded.
        """
        try:
            while True:
                # `next()` on an OpenAI Stream blocks until the next chunk
                # arrives or the stream ends. Wrap in to_thread so the
                # event loop can do other work (and so we can race it
                # against asyncio.wait_for for a deadman timer).
                chunk = await asyncio.to_thread(self._safe_next)
                if chunk is None:
                    # Sentinel: stream exhausted.
                    return
                self._last_chunk_time = time.monotonic()
                # OpenAI chunk format: chunk.choices[0].delta has .content
                # and optionally .reasoning_content. Final-usage chunks
                # have empty choices and a `.usage` attribute populated.
                if hasattr(chunk, 'usage') and chunk.usage is not None:
                    self._final_usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                reasoning = getattr(delta, 'reasoning_content', None) or ''
                if reasoning:
                    self._reasoning_parts.append(reasoning)
                text = getattr(delta, 'content', None) or ''
                if text:
                    self._text_parts.append(text)
                    yield text
                # Reasoning-only chunk: still yield an empty string so a
                # deadman watcher iterating this generator sees a heartbeat.
                elif reasoning:
                    yield ''
        finally:
            # Ensure the underlying stream is closed even on early break /
            # exception from the caller.
            await self.close()

    def _safe_next(self):
        """Pull one chunk from the underlying OpenAI stream.

        Returns None when the stream is exhausted (StopIteration → None
        translation). Lets `await asyncio.to_thread(self._safe_next)` work
        cleanly without raising across the thread boundary.
        """
        try:
            return next(iter(self._openai_stream))
        except StopIteration:
            return None
        except Exception as e:
            # An error mid-stream — log, RECORD (June 10, V30), and signal
            # exhaustion so the caller can decide what to do with the partial
            # text accumulated so far.
            print(f"[{self._log_tag}] Stream error: {e}")
            self.stream_error = e
            return None

    @property
    def full_text(self) -> str:
        return ''.join(self._text_parts)

    @property
    def full_reasoning(self) -> str:
        return ''.join(self._reasoning_parts)

    @property
    def usage(self):
        """Anthropic-shaped usage object, or None if the server didn't emit one."""
        if self._final_usage is None:
            return None
        return _Usage(
            prompt_tokens=getattr(self._final_usage, 'prompt_tokens', 0),
            completion_tokens=getattr(self._final_usage, 'completion_tokens', 0),
        )

    @property
    def seconds_since_last_chunk(self) -> float:
        """How long ago we received the most recent chunk.

        Useful for an external deadman-watcher task that monitors stream
        health and calls `.close()` when this exceeds a threshold.
        """
        return time.monotonic() - self._last_chunk_time


# ---------------------------------------------------------------------------
# Messages namespace — duck-types anthropic.Anthropic().messages
# ---------------------------------------------------------------------------

class _MessagesNamespace:
    """Duck-types anthropic.Anthropic().messages with a .create() method.

    Translates Anthropic-style messages.create() calls to OpenAI format.
    The message format is identical for simple single-turn prompts (which is
    all the MTG engine uses), so no message translation is needed.
    """

    def __init__(self, openai_client, default_model: str = "deepseek-v4-flash",
                 log_tag: str = "DEEPSEEK",
                 thinking_enabled: bool = None,
                 reasoning_effort: str = None):
        self._client = openai_client
        self._default_model = default_model
        self._log_tag = log_tag
        # DeepSeek V4: explicit thinking-mode control. When None, the server
        # default applies (V4-Flash and V4-Pro both default to thinking
        # enabled). The actor explicitly sets False to keep JSON output fast;
        # the strategist leaves it None and relies on V4-Pro's default
        # plus reasoning_effort="high".
        self._thinking_enabled = thinking_enabled
        self._reasoning_effort = reasoning_effort
        self._call_count = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        # May 16 audit: per-purpose call counter. Each call site can pass
        # `purpose="plan_turn"` etc. to bucket itself. Lets post-batch grep
        # of `[CALL-BREAKDOWN]` show which sites dominate the spend (used to
        # investigate the "hidden 10x call multiplier" — visible ~330/game
        # vs reported ~1907/game).
        self._purpose_counts: dict = {}

    def create(self, *, model: str = None, max_tokens: int = 1024,
               messages: list = None, **kwargs) -> _AdaptedResponse:
        """Translate an Anthropic-style API call to OpenAI format.

        Translates Anthropic's separate 'system' kwarg to an OpenAI system
        message (first in the messages list). Forces JSON output mode by
        default; pass json_mode=False for free-text calls (e.g. strategist
        memos, rulings that return prose rather than structured JSON).
        """
        # json_mode=False is used by the strategist (returns prose) and any
        # other call that intentionally produces non-JSON output.
        json_mode = kwargs.pop('json_mode', True)
        # May 16 audit: callers can label what they're asking for so the
        # per-purpose counter can break down where calls are coming from.
        # Unknown bucket is "uncategorized" — chase those down to label them.
        purpose = kwargs.pop('purpose', 'uncategorized')
        # July 20: per-call reasoning_effort override (adaptive strategist
        # degrade after repeated deadman fires) — beats the adapter default.
        effort_override = kwargs.pop('reasoning_effort', None)

        try:
            # Translate Anthropic 'system' kwarg → OpenAI system message
            api_messages = list(messages or [])
            system_text = kwargs.get('system', '')
            if system_text:
                api_messages.insert(0, {"role": "system", "content": system_text})

            create_kwargs = dict(
                model=self._default_model,
                max_tokens=max_tokens,
                messages=api_messages,
            )

            # V4-style thinking-mode toggle (when set explicitly on adapter).
            # Goes via `extra_body` because the OpenAI SDK doesn't know about
            # DeepSeek's thinking parameter; extra_body forwards untouched.
            if self._thinking_enabled is not None:
                extra_body = create_kwargs.setdefault('extra_body', {})
                extra_body['thinking'] = {
                    "type": "enabled" if self._thinking_enabled else "disabled"
                }
            if effort_override is not None:
                create_kwargs['reasoning_effort'] = effort_override
            elif self._reasoning_effort is not None:
                create_kwargs['reasoning_effort'] = self._reasoning_effort

            if json_mode:
                # JSON mode requires "json" to appear somewhere in messages.
                # Not all call paths include it, so inject if missing.
                has_json_word = any(
                    'json' in (m.get('content', '') or '').lower()
                    for m in api_messages
                )
                if not has_json_word:
                    for m in reversed(api_messages):
                        if m.get('role') == 'user':
                            m['content'] = (m['content'] or '') + '\nRespond with a JSON object.'
                            break
                    else:
                        api_messages.append({"role": "user", "content": "Respond with a JSON object."})
                create_kwargs['response_format'] = {"type": "json_object"}

            try:
                openai_response = self._client.chat.completions.create(**create_kwargs)
            except Exception as json_err:
                # Fall back without response_format if JSON mode truly unsupported
                err_str = str(json_err).lower()
                if json_mode and ('response_format' in err_str or 'json_object' in err_str):
                    print(f"[{self._log_tag}] ⚠️ JSON mode rejected by API — falling back to free text. "
                          f"Parse failures may increase. Error: {str(json_err)[:100]}")
                    del create_kwargs['response_format']
                    # Reinforce JSON instruction since we lost the format constraint
                    for m in reversed(api_messages):
                        if m.get('role') == 'user':
                            m['content'] = (m['content'] or '') + '\n\nCRITICAL: Output ONLY a JSON object. No other text.'
                            break
                    openai_response = self._client.chat.completions.create(**create_kwargs)
                else:
                    raise

            self._call_count += 1
            self._total_prompt_tokens += openai_response.usage.prompt_tokens
            self._total_completion_tokens += openai_response.usage.completion_tokens
            self._purpose_counts[purpose] = self._purpose_counts.get(purpose, 0) + 1
            # Periodic breakdown so you can grep [CALL-BREAKDOWN] post-batch.
            if self._call_count % 200 == 0:
                breakdown = ", ".join(
                    f"{k}={v}" for k, v in sorted(
                        self._purpose_counts.items(), key=lambda x: -x[1]
                    )[:10]
                )
                print(f"[CALL-BREAKDOWN] [{self._log_tag}] call#{self._call_count}: {breakdown}")

            # Track API-side prompt cache hits when the provider exposes them
            # (DeepSeek's `prompt_cache_hit_tokens` field). This tells us how
            # well the provider's automatic prefix caching is working — the
            # local _state_fingerprint cache only avoids Python work, not
            # token cost. If hit_ratio stays low across a batch, it means the
            # prompt prefix is changing too often and a structural rewrite
            # is needed (move volatile state to the end).
            try:
                usage = openai_response.usage
                cache_hit = getattr(usage, 'prompt_cache_hit_tokens', None)
                cache_miss = getattr(usage, 'prompt_cache_miss_tokens', None)
                if cache_hit is not None or cache_miss is not None:
                    if not hasattr(self, '_total_cache_hit_tokens'):
                        self._total_cache_hit_tokens = 0
                        self._total_cache_miss_tokens = 0
                    self._total_cache_hit_tokens += cache_hit or 0
                    self._total_cache_miss_tokens += cache_miss or 0
                    # Per-call log for at least the first few + every 50th call
                    if self._call_count <= 3 or self._call_count % 50 == 0:
                        total_prompt = (cache_hit or 0) + (cache_miss or 0)
                        ratio = (cache_hit or 0) / max(total_prompt, 1)
                        print(f"[{self._log_tag}] API cache: hit={cache_hit} miss={cache_miss} ratio={ratio:.0%} (call #{self._call_count})")
            except Exception:
                pass  # Provider doesn't expose cache fields — silently skip

            return _AdaptedResponse(openai_response)

        except Exception as e:
            print(f"[{self._log_tag}] API error: {e}")
            raise  # Let the caller's existing error handling deal with it

    def stream(self, *, model: str = None, max_tokens: int = 1024,
               messages: list = None, **kwargs) -> _StreamingResponse:
        """Open a streaming completion. Returns a _StreamingResponse.

        Same interface as `create()` but the underlying request is sent
        with `stream=True`. Caller iterates `stream.text_chunks()` to
        receive deltas as they arrive. Suitable for long-latency calls
        where we want a deadman timer (the strategist; possibly the
        future Discord Activity frontend's live-thinking display).

        Usage:
            async with ns.stream(messages=..., system="...") as stream:
                async for chunk in stream.text_chunks():
                    ...
                text = stream.full_text

        Stats: streaming calls are counted in the per-purpose counter,
        but token totals are only added when the server emits a final
        usage chunk (DeepSeek does; some providers don't). On providers
        without final-usage, the streaming call's tokens are NOT counted
        in `_total_prompt_tokens` — caller can read `stream.usage`
        directly if precise accounting matters.
        """
        json_mode = kwargs.pop('json_mode', False)  # strategist defaults free-text
        purpose = kwargs.pop('purpose', 'stream-uncategorized')
        # July 20: per-call reasoning_effort override (see create()).
        effort_override = kwargs.pop('reasoning_effort', None)

        api_messages = list(messages or [])
        system_text = kwargs.get('system', '')
        if system_text:
            api_messages.insert(0, {"role": "system", "content": system_text})

        create_kwargs = dict(
            model=self._default_model,
            max_tokens=max_tokens,
            messages=api_messages,
            stream=True,
            # Per OpenAI streaming docs, asking for usage in the stream
            # makes the server emit a final-usage chunk at the end. Without
            # this, we get no token-count accounting on streaming calls.
            stream_options={"include_usage": True},
        )

        if self._thinking_enabled is not None:
            extra_body = create_kwargs.setdefault('extra_body', {})
            extra_body['thinking'] = {
                "type": "enabled" if self._thinking_enabled else "disabled"
            }
        if effort_override is not None:
            create_kwargs['reasoning_effort'] = effort_override
        elif self._reasoning_effort is not None:
            create_kwargs['reasoning_effort'] = self._reasoning_effort

        if json_mode:
            create_kwargs['response_format'] = {"type": "json_object"}

        try:
            openai_stream = self._client.chat.completions.create(**create_kwargs)
        except Exception as e:
            print(f"[{self._log_tag}] Stream open error: {e}")
            raise

        # Bookkeeping: count the call now (token totals get added later when
        # the final-usage chunk arrives, in _StreamingResponse.close()).
        self._call_count += 1
        self._purpose_counts[purpose] = self._purpose_counts.get(purpose, 0) + 1

        return _StreamingResponse(openai_stream, log_tag=self._log_tag,
                                   namespace=self)


# ---------------------------------------------------------------------------
# Main adapter class
# ---------------------------------------------------------------------------

class OpenAICompatibleAdapter:
    """Duck-types as anthropic.Anthropic for the MTG engine.

    Provides self.messages.create() that translates to OpenAI-compatible format
    and returns Anthropic-shaped responses. Works with DeepSeek, OpenRouter,
    and any other OpenAI-compatible API. ClaudePlayer, RulesEngine, and
    EffectExecutor can use this as a drop-in replacement with zero code changes.
    """

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-v4-flash", log_tag: str = "DEEPSEEK",
                 extra_headers: dict = None,
                 thinking_enabled: bool = None,
                 reasoning_effort: str = None):
        from openai import OpenAI
        client_kwargs = dict(api_key=api_key, base_url=base_url)
        if extra_headers:
            client_kwargs['default_headers'] = extra_headers
        self._openai_client = OpenAI(**client_kwargs)
        self.messages = _MessagesNamespace(self._openai_client,
                                           default_model=model,
                                           log_tag=log_tag,
                                           thinking_enabled=thinking_enabled,
                                           reasoning_effort=reasoning_effort)
        self._model = model
        self._log_tag = log_tag
        # Build a compact init log line summarizing thinking/reasoning config
        extras = []
        if thinking_enabled is False:
            extras.append("thinking=disabled")
        elif thinking_enabled is True:
            extras.append("thinking=enabled")
        if reasoning_effort:
            extras.append(f"reasoning_effort={reasoning_effort}")
        extras_str = f", {', '.join(extras)}" if extras else ""
        print(f"[{log_tag}] Adapter initialized (model={model}, base_url={base_url}{extras_str})")

    def get_stats(self) -> dict:
        """Return cumulative usage statistics for this adapter session."""
        ns = self.messages
        return {
            "calls": ns._call_count,
            "prompt_tokens": ns._total_prompt_tokens,
            "completion_tokens": ns._total_completion_tokens,
            "model": self._model,
            # May 17 audit: surface cache stats so STATS-CUMULATIVE can report
            # real hit rate. Previously these counters were accumulated but
            # never read by any caller — dead instrumentation.
            "cache_hit_tokens": getattr(ns, '_total_cache_hit_tokens', 0),
            "cache_miss_tokens": getattr(ns, '_total_cache_miss_tokens', 0),
            # Per-purpose call counts (plan_turn, decide_response, strategist,
            # decide_action, decide_mulligan, etc.). Surfaced so the autoplay
            # game-end path can emit one [CALL-BREAKDOWN] line per game,
            # not just every 200 calls (60% of games never crossed that
            # threshold in the May 16 batch).
            "purpose_counts": dict(getattr(ns, '_purpose_counts', {}) or {}),
        }


# Backwards compatibility alias
DeepseekAdapter = OpenAICompatibleAdapter


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def create_deepseek_adapter(api_key: str = None) -> 'OpenAICompatibleAdapter | None':
    """Create an adapter for DeepSeek (V4-Flash, non-thinking) if available.

    Defaults to deepseek-v4-flash with thinking mode explicitly DISABLED —
    this is the Actor in the parallel CoT split: 3-5 fast JSON action plans
    per turn. For the Strategist (deep reasoning, once per turn), use
    create_deepseek_reasoner_adapter() instead.

    V4-Flash defaults to thinking mode ENABLED on the server side, so we MUST
    set thinking_enabled=False here or the actor will silently get expensive
    chain-of-thought tokens we don't want.

    The legacy aliases `deepseek-chat` and `deepseek-reasoner` are deprecated
    on July 24, 2026 — V4-Flash + V4-Pro are the canonical model strings now.

    Returns None (not an error) if:
    - No API key provided and DEEPSEEK_API_KEY env var not set
    - openai package not installed
    - Any other initialization failure

    This makes it safe to call unconditionally at startup.
    """
    key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return None
    try:
        return OpenAICompatibleAdapter(
            api_key=key,
            model="deepseek-v4-flash",
            log_tag="DEEPSEEK",
            thinking_enabled=False,  # actor: fast JSON, no chain-of-thought
        )
    except ImportError:
        print("[DEEPSEEK] openai package not installed. Run: pip install openai>=1.40.0")
        return None
    except Exception as e:
        print(f"[DEEPSEEK] Failed to create adapter: {e}")
        return None


def create_openrouter_adapter(model: str = "openrouter/optimus-alpha",
                               api_key: str = None) -> 'OpenAICompatibleAdapter | None':
    """Create an adapter for OpenRouter if OPENROUTER_API_KEY is available.

    OpenRouter hosts many models (DeepSeek, stealth models, etc.) behind a
    single OpenAI-compatible API. Model names use provider/model format:
        openrouter/optimus-alpha, openrouter/quasar-alpha, etc.

    Returns None if no API key is available or initialization fails.
    """
    key = api_key or os.getenv("OPENROUTER_API_KEY")
    if not key:
        return None
    try:
        # Short model name for the log tag (e.g. "optimus-alpha" from "openrouter/optimus-alpha")
        short_name = model.split("/")[-1] if "/" in model else model
        return OpenAICompatibleAdapter(
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            model=model,
            log_tag=f"OPENROUTER:{short_name}",
            extra_headers={
                "HTTP-Referer": "https://github.com/VIXAL-OS/discord-mtg-bot",
                "X-Title": "Discord MTG Bot",
            },
        )
    except ImportError:
        print("[OPENROUTER] openai package not installed. Run: pip install openai>=1.40.0")
        return None
    except Exception as e:
        print(f"[OPENROUTER] Failed to create adapter: {e}")
        return None


def create_deepseek_reasoner_adapter(api_key: str = None) -> 'OpenAICompatibleAdapter | None':
    """Create a DeepSeek adapter for the Strategist role.

    Aug 2, 2026: V4-FLASH (0731 build) in THINKING mode — the approved
    A/B replacing V4-Pro (see the inline comment below for rationale and the
    full revert path).

    Intended for the Strategist in the parallel CoT split: deep reasoning
    fires once per turn, output is a free-text strategy memo (not JSON),
    so callers should pass json_mode=False.

    Function name kept as `_reasoner_adapter` for backward compatibility with
    existing call sites (mtg.cog._deepseek_reasoner_adapter, mtg.autoplay
    swap block). The role is "deep-reasoning strategist" regardless of which
    model backs it.

    Falls back gracefully to None if DEEPSEEK_API_KEY is not set.
    """
    key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        return None
    try:
        return OpenAICompatibleAdapter(
            api_key=key,
            # Aug 2, 2026 (approved A/B): strategist moved from
            # deepseek-v4-pro (reasoning_effort=medium since May 23) to
            # V4-FLASH THINKING MODE. Rationale: the strategist's chronic
            # instability — density nukes, deadman/hard-cap fires,
            # scaffolding leaks — has been V4-Pro reasoning all along, and
            # the 0731 Flash re-post-train reports agent scores PASSING
            # V4-Pro-Preview at ~6x lower cost. thinking_enabled=True is
            # REQUIRED (flash defaults per-endpoint; explicit beats
            # assumption — the cube-draft 0-token lesson in reverse).
            # reasoning_effort is a PRO knob — flash doesn't take it (the
            # per-game degrade in claude_player is model-gated to match).
            # REVERT PATH: model back to "deepseek-v4-pro", drop
            # thinking_enabled, restore reasoning_effort="medium", restore
            # the Pro STRAT_* rates in mtg/autoplay.py, and the swap-block
            # strings there — five edits, all tagged "Aug 2" + "A/B".
            model="deepseek-v4-flash",
            log_tag="DEEPSEEK:REASONER",
            thinking_enabled=True,
        )
    except ImportError:
        print("[DEEPSEEK:REASONER] openai package not installed. Run: pip install openai>=1.40.0")
        return None
    except Exception as e:
        print(f"[DEEPSEEK:REASONER] Failed to create adapter: {e}")
        return None
