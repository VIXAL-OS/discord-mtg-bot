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
        usage = getattr(openai_response, 'usage', None)
        self.usage = _Usage(
            prompt_tokens=getattr(usage, 'prompt_tokens', 0) or 0,
            completion_tokens=getattr(usage, 'completion_tokens', 0) or 0,
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
            _usage = getattr(openai_response, 'usage', None)
            self._total_prompt_tokens += (
                getattr(_usage, 'prompt_tokens', 0) or 0)
            self._total_completion_tokens += (
                getattr(_usage, 'completion_tokens', 0) or 0)
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
                 reasoning_effort: str = None,
                 request_timeout: float = 90.0,
                 max_retries: int = 1,
                 rate_key: str = None):
        from openai import OpenAI
        # Aug 3, 2026 — THE HANG FIX. This client was constructed with no
        # timeout at all, so it inherited the openai SDK's default: 600s per
        # request with 2 retries, i.e. up to THIRTY MINUTES before a single
        # call gives up. On Aug 3 that parked an entire 25-game batch: every
        # game froze mid-`[PLAN] Planning main1` awaiting a DeepSeek actor
        # call that never returned, and 53 minutes later not one game had
        # finished a turn.
        #
        # The strategist has had a deadman since May (60s no-chunk / 120s
        # hard cap) precisely because a 28-minute hang killed a game. The
        # ACTOR never got one — it is non-streaming, so there was nothing to
        # watch inter-chunk — and the actor is the call every game makes
        # several times a turn. This bounds it at the transport instead,
        # which covers every call site at once rather than needing a wrapper
        # around each of the ~10 `asyncio.to_thread` invocations.
        #
        # Timing out is SAFE here: every call site already handles adapter
        # exceptions (that is how 503s degrade today), so a timeout surfaces
        # as the same recoverable error and the game moves on instead of
        # blocking forever. Worst case per logical call is
        # request_timeout * (max_retries + 1).
        client_kwargs = dict(api_key=api_key, base_url=base_url,
                             timeout=request_timeout, max_retries=max_retries)
        if extra_headers:
            client_kwargs['default_headers'] = extra_headers
        self._openai_client = OpenAI(**client_kwargs)
        self._request_timeout = request_timeout
        self._max_retries = max_retries
        self.messages = _MessagesNamespace(self._openai_client,
                                           default_model=model,
                                           log_tag=log_tag,
                                           thinking_enabled=thinking_enabled,
                                           reasoning_effort=reasoning_effort)
        self._model = model
        self._log_tag = log_tag
        # Aug 4: the model STRING is not a unique price key. DashScope
        # RESELLS deepseek-v4-flash under the identical name at its own
        # rates (notably a 10x worse cache rate), so pricing by model alone
        # would bill an Alibaba-hosted DeepSeek at DeepSeek's own prices.
        # rate_key lets a factory disambiguate; it defaults to the model, so
        # every existing adapter is unchanged.
        self.rate_key = rate_key or model
        # Build a compact init log line summarizing thinking/reasoning config
        extras = []
        if thinking_enabled is False:
            extras.append("thinking=disabled")
        elif thinking_enabled is True:
            extras.append("thinking=enabled")
        if reasoning_effort:
            extras.append(f"reasoning_effort={reasoning_effort}")
        extras.append(f"timeout={request_timeout:g}s x{max_retries + 1}")
        extras_str = f", {', '.join(extras)}" if extras else ""
        print(f"[{log_tag}] Adapter initialized (model={model}, base_url={base_url}{extras_str})")

    @property
    def model(self) -> str:
        """Public alias for the configured model.

        Aug 4: the class only ever exposed `_model`, and the autoplay swap
        block read `getattr(adapter, 'model', 'deepseek-v4-flash')` — so it
        silently received the FALLBACK for every adapter ever built. A batch
        pinned to Qwen therefore ran with claude_ai.model set to
        'deepseek-v4-flash': the right adapter, the wrong model string, which
        DashScope happily served because it hosts that model too. The pin
        said qwen and the games ran DeepSeek.

        The misleading default is gone from the call sites; this property
        exists so `adapter.model` is simply correct for anyone who reaches
        for the obvious name.
        """
        return self._model

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
            # Longer than the actor's 90s: a thinking-mode memo legitimately
            # runs long, and this role already has a streaming deadman (60s
            # no-chunk / 120s hard cap) as its primary bound. The transport
            # cap is the backstop for what the deadman CANNOT see — a request
            # that never opens a stream at all, which is exactly how the Aug 3
            # hang presented.
            request_timeout=180.0,
        )
    except ImportError:
        print("[DEEPSEEK:REASONER] openai package not installed. Run: pip install openai>=1.40.0")
        return None
    except Exception as e:
        print(f"[DEEPSEEK:REASONER] Failed to create adapter: {e}")
        return None


# ===========================================================================
# ALIBABA / DASHSCOPE (Qwen) — Aug 3, 2026
# ===========================================================================
# Added so a batch is not hostage to one provider's congestion. DeepSeek has
# ANNOUNCED (not yet live) peak-hour pricing for Beijing 09:00-12:00 and
# 14:00-18:00; those windows are 21:00-00:00 and 02:00-06:00 EDT, and an
# overnight batch straddles both. The Aug 3 batch launched at Beijing 11:13
# and took a burst of HTTP 503 "service is too busy" plus a latency tail of
# 200-365s per call against a healthy median of ~8s.
#
# DashScope is OpenAI-compatible, so this is the same OpenAICompatibleAdapter
# with a different base_url — and unlike the OpenRouter route it preserves the
# actor/strategist split, because we hold two independent model slugs.

def _dashscope_base_url() -> str:
    """Resolve the DashScope endpoint for the configured region.

    Regions are NOT interchangeable: "API Keys are independent across regions
    and cannot be used across regions" — a Frankfurt key against the
    Singapore host returns 401 with a message that reads like a bad key,
    which is a genuinely misleading error and cost an hour on Aug 4.

    Frankfurt and Tokyo additionally embed a WORKSPACE ID in the hostname,
    a different shape from Singapore/Beijing. Set DASHSCOPE_WORKSPACE_ID for
    those; the console shows it next to the API key.

        DASHSCOPE_REGION=intl          -> Singapore  (default)
        DASHSCOPE_REGION=cn            -> Beijing
        DASHSCOPE_REGION=eu-central-1  -> Frankfurt  (needs workspace id)
        DASHSCOPE_REGION=ap-northeast-1-> Tokyo      (needs workspace id)
        DASHSCOPE_BASE_URL=...         -> full override, wins over all of it
    """
    explicit = os.getenv("DASHSCOPE_BASE_URL", "").strip()
    if explicit:
        return explicit
    region = os.getenv("DASHSCOPE_REGION", "intl").strip().lower()
    workspace = os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip()
    if region in ("intl", "singapore", "ap-southeast-1"):
        return "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    if region in ("cn", "beijing", "cn-beijing"):
        return "https://dashscope.aliyuncs.com/compatible-mode/v1"
    if region in ("eu-central-1", "frankfurt", "ap-northeast-1", "tokyo",
                  "us-east-1", "us-virginia", "virginia"):
        canon = {"frankfurt": "eu-central-1",
                 "tokyo": "ap-northeast-1",
                 "us-virginia": "us-east-1",
                 "virginia": "us-east-1"}.get(region, region)
        if not workspace:
            print(f"[DASHSCOPE] region={canon} needs DASHSCOPE_WORKSPACE_ID "
                  f"(the hostname embeds it) — falling back to Singapore, "
                  f"which will 401 if the key is not a Singapore key")
            return "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        return f"https://{workspace}.{canon}.maas.aliyuncs.com/compatible-mode/v1"
    print(f"[DASHSCOPE] unknown DASHSCOPE_REGION={region!r} — using Singapore")
    return "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


DASHSCOPE_BASE_URL = _dashscope_base_url()

# Confirmed 2026-08-04 against Alibaba's per-model doc pages. qwen3.7-flash
# is real, callable, and available in Beijing / Singapore / Frankfurt / Tokyo
# / Hong Kong, with function calling, structured outputs and context caching
# all supported — which is the whole feature set this workload needs.
#
# Alibaba publishes DATED snapshot ids too (qwen3.7-flash-2026-07-15). The
# undated alias is deliberate: it tracks the current build the way
# `deepseek-v4-flash` does, and pinning a date is what to do if a build
# regresses, not before.
#
# Also worth knowing, from the same model list: DashScope HOSTS
# `deepseek-v4-flash` and `deepseek-v4-pro` itself. If DeepSeek's own API is
# congested, that is a route to the identical model on different
# infrastructure — zero quality risk, unlike switching model families. Not
# wired here (it needs its own rate entries, since Alibaba's resale price is
# not DeepSeek's), but it is the lowest-risk failover available.
QWEN_ACTOR_MODEL = os.getenv("QWEN_ACTOR_MODEL", "qwen3.7-flash")
# Aug 7, 2026 A/B (maintainer-approved): strategist default Plus → FLASH
# thinking mode — the exact mirror of the Aug 2 DeepSeek A/B (V4-Pro →
# V4-Flash thinking, which saved 61% with quality holding). The Plus
# strategist's thinking output was the entire Qwen cost premium in the
# Aug 7 batch ($1.4268 of $1.7498 — ~19x the DS strategist per game).
# Verdict criteria on the next Qwen batch: `Win condition:` compliance
# >=95%, nukes near zero, stale-30s waits not worse, strat cost/game
# ~$0.0285 → ~$0.003, memo quality eyeball in 2-3 games. REVERT PATH:
# QWEN_STRATEGIST_MODEL=qwen3.7-plus (env, no code change) or flip this
# default back.
QWEN_STRATEGIST_MODEL = os.getenv("QWEN_STRATEGIST_MODEL", "qwen3.7-flash")


def create_dashscope_adapter(model: str, api_key: str = None,
                             log_tag: str = None,
                             thinking_enabled: bool = None,
                             request_timeout: float = 90.0,
                             rate_key: str = None):
    """Adapter for Alibaba Model Studio (DashScope), OpenAI-compatible.

    Mirrors create_deepseek_adapter: returns None rather than raising when
    the key or the openai package is missing, so callers can invoke it
    unconditionally at startup and degrade to whatever else is configured.
    """
    key = api_key or os.getenv("DASHSCOPE_API_KEY") or os.getenv("ALIBABA_API_KEY")
    if not key:
        return None
    try:
        return OpenAICompatibleAdapter(
            api_key=key,
            base_url=DASHSCOPE_BASE_URL,
            model=model,
            log_tag=log_tag or f"DASHSCOPE:{model}",
            thinking_enabled=thinking_enabled,
            request_timeout=request_timeout,
            rate_key=rate_key,
        )
    except ImportError:
        print("[DASHSCOPE] openai package not installed. Run: pip install openai>=1.40.0")
        return None
    except Exception as e:
        print(f"[DASHSCOPE] Failed to create adapter: {e}")
        return None


def create_qwen_actor_adapter(api_key: str = None):
    """Actor role on Qwen: fast structured JSON, thinking OFF.

    Mirrors the DeepSeek actor exactly — thinking is disabled EXPLICITLY
    rather than left to the endpoint default, which is the lesson from
    V4-Flash defaulting to thinking-enabled server-side and silently
    billing chain-of-thought.
    """
    return create_dashscope_adapter(
        QWEN_ACTOR_MODEL, api_key=api_key,
        log_tag="QWEN:ACTOR", thinking_enabled=False)


def create_qwen_strategist_adapter(api_key: str = None):
    """Strategist role on Qwen: one deep-reasoning memo per turn.

    Aug 7, 2026: defaults to FLASH thinking mode (see the A/B note at
    QWEN_STRATEGIST_MODEL above) — the DeepSeek-arrangement mirror. The
    thinking toggle rides the same adapter mechanism the Plus strategist
    already used; the strategist billing routes by the reported model
    string, so the flash rates apply automatically.
    """
    return create_dashscope_adapter(
        QWEN_STRATEGIST_MODEL, api_key=api_key,
        log_tag="QWEN:STRATEGIST", thinking_enabled=True,
        request_timeout=180.0)


# ===========================================================================
# PROVIDER RATE TABLE — the single source of truth for token pricing
# ===========================================================================
# Before this existed the rates were LOCAL constants inside the [STATS-GAME]
# cost block in mtg/autoplay.py, hardcoded per ROLE (ACTOR_*/STRAT_*). That
# silently assumed the provider, so the moment a batch runs on anything but
# DeepSeek every cost figure it prints is wrong. Keying on the model string
# instead means a provider swap reprices itself.
#
# Values are US DOLLARS PER MILLION TOKENS: (cache_hit_in, cache_miss_in, out).
# The cache_hit column is the one that decides this workload's economics —
# 66-84% of input tokens are hits here, so blended input is far below the
# headline miss rate.
MODEL_RATES = {
    # DeepSeek — verified against the account's own usage export (reproduced
    # a known monthly bill to the cent); the April 2026 price cut put the hit
    # rate at 98% off, which is unusually aggressive (most providers do 90%).
    #
    # ⚠️ PENDING INCREASE (Aug 7, 2026): DeepSeek emailed that "a significant
    # increase" to API pricing is coming "in the near future" — no numbers,
    # no date. These DeepSeek rows are correct only until that lands. When
    # the official notice arrives: update these rows SAME DAY (from the
    # official pricing page, never the email or an aggregator — the Aug 4
    # source lesson), reconcile one bill, and drop this warning. Until then
    # every [STATS-*] line on a DeepSeek batch after the change silently
    # under-reports.
    "deepseek-v4-flash": (0.0028, 0.14, 0.28),
    "deepseek-v4-pro": (0.0036, 0.435, 0.87),

    # Qwen via DashScope — from Alibaba's PER-MODEL doc pages, which is the
    # only source that has held up. Both an aggregator listing and a
    # summarised read of the consolidated pricing table gave wrong answers
    # tonight, the latter reporting qwen3.7-flash as absent when it has its
    # own page with a full billing breakdown. If a Qwen rate needs checking,
    # go to help/en/model-studio/<model-name>, not a table or a third party.
    #
    # qwen3.7-flash, Input<=32k tier (verified 2026-08-04). This workload's
    # prompts are ~10K, so the lowest tier is the one that applies; the
    # tiers step up above 32K and again above 256K.
    #   input 0.028 | output 0.11 | IMPLICIT cache 0.006 | explicit read 0.003
    # The implicit-cache rate is the one modelled: it needs no cache
    # management, exactly like DeepSeek's. (Explicit caching is cheaper still
    # at 0.003 but bills 0.034 for creation and has to be managed, so it is
    # only worth it if a stable prefix is reused hard — which this workload's
    # STABLE STRATEGY REFERENCE block arguably is. Worth revisiting.)
    "qwen3.7-flash": (0.006, 0.028, 0.11),
    # Plus: input corroborated by two independent sources (the promo page's
    # "List Price $0.276" and the pricing table). Output from the table only.
    "qwen3.7-plus": (0.0276, 0.276, 1.101),
    "qwen3.7-max": (0.165, 1.65, 4.951),

    # DeepSeek RESOLD BY DASHSCOPE — provider-scoped keys, because the model
    # strings are identical to DeepSeek's own and would otherwise be priced
    # at DeepSeek's rates. Verified 2026-08-04 from the Model Studio
    # per-model page (deepseek-v4-flash: in 0.138 / out 0.275 / implicit
    # cache 0.028; regions Beijing, Frankfurt, US-Virginia, Tokyo).
    #
    # The list prices essentially match DeepSeek's own, but the CACHE rate is
    # 0.028 against DeepSeek's 0.0028 — 10x worse — so blended input runs
    # ~1.45x DeepSeek direct. That is the honest price of this failover, and
    # it buys the thing no model swap can: the IDENTICAL model on different
    # infrastructure, so a congested-DeepSeek batch keeps its exact play
    # quality instead of trading it for availability.
    "dashscope:deepseek-v4-flash": (0.028, 0.138, 0.275),
    "dashscope:deepseek-v4-pro": (0.028, 0.138, 0.275),

    # Family catch-alls, so an unrecognised member still prices as its family
    # rather than falling to the unknown-model rate. They also make the
    # longest-first match below load-bearing rather than merely defensive,
    # since every specific slug nests inside one of these.
    #
    # Both sit at the QUALITY tier of their family (Plus / V4-Pro), not the
    # absolute top. Stated plainly because it is a compromise rather than a
    # rule: an unrecognised Flash-class model over-reports ~1.7x, which is
    # the safe direction, but an unrecognised MAX-class model would
    # UNDER-report ~6x. Pricing the catch-all at Max instead would make every
    # unknown Qwen read ~10x high, and a cost line nobody believes is worth
    # no more than one that is wrong. The mitigation is the real one: add the
    # slug to the table when a new tier starts being used.
    "qwen": (0.0276, 0.276, 1.101),
    "deepseek": (0.0036, 0.435, 0.87),
}

# What to charge a model we have no entry for. DeepSeek V4-Flash's MISS rate
# is used for all three columns: it is the provider we actually run, and
# pricing an unknown model at the no-discount rate over-reports rather than
# under-reports. A cost line that reads high is a prompt to add a rate entry;
# one that reads low is a silent lie.
_UNKNOWN_MODEL_RATE = (0.14, 0.14, 0.28)


def rates_for_model(model: str):
    """(hit, miss, out) dollars-per-token for a model string.

    Matching is substring-based and LONGEST-FIRST, because the slugs nest:
    'qwen3.7-flash' contains 'qwen3.7', and a shortest-first match would
    price Plus at Flash rates. Returns per-TOKEN rates (already divided by
    a million) so call sites can multiply directly.
    """
    m = (model or "").lower()
    # Provider-scoped keys ("dashscope:deepseek-v4-flash") are checked first
    # and EXACTLY, so a resold model is never priced at its origin's rates
    # by the substring pass below.
    if m in MODEL_RATES:
        hit, miss, out = MODEL_RATES[m]
        return (hit / 1_000_000, miss / 1_000_000, out / 1_000_000)
    for key in sorted(MODEL_RATES, key=len, reverse=True):
        if key in m:
            hit, miss, out = MODEL_RATES[key]
            return (hit / 1_000_000, miss / 1_000_000, out / 1_000_000)
    hit, miss, out = _UNKNOWN_MODEL_RATE
    return (hit / 1_000_000, miss / 1_000_000, out / 1_000_000)


# ===========================================================================
# PROVIDER LATENCY PROBE — Aug 3, 2026
# ===========================================================================

async def probe_adapter_latency(adapter, samples: int = 2,
                                timeout: float = 25.0) -> dict:
    """Time a few trivial completions against one adapter.

    `messages.create` is SYNCHRONOUS (callers wrap it in a thread), so each
    sample goes through asyncio.to_thread and the whole probe stays awaitable.

    ANY error disqualifies the provider. That is deliberate rather than
    lenient: the failure this exists to dodge is HTTP 503 "service is too
    busy", so an erroring provider is precisely the one not to pick — there
    is no point averaging a 503 into a latency figure.

    Also doubles as a slug validity check. An unknown model returns 4xx here
    and the provider is skipped, which is what keeps the VERIFY-flagged Qwen
    slugs from being able to kill a batch mid-run.
    """
    if adapter is None:
        return {"ok": False, "median_ms": None, "errors": 0,
                "detail": "not configured"}
    lat = []
    errors = 0
    last_err = ""
    for _ in range(max(1, samples)):
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: adapter.messages.create(
                        max_tokens=4,
                        messages=[{"role": "user", "content": "ping"}])),
                timeout=timeout)
            lat.append((time.monotonic() - t0) * 1000.0)
        except Exception as e:
            errors += 1
            last_err = str(e)[:160]
    if not lat:
        return {"ok": False, "median_ms": None, "errors": errors,
                "detail": last_err or "all probes failed"}
    lat.sort()
    return {"ok": errors == 0, "median_ms": lat[len(lat) // 2],
            "errors": errors, "detail": last_err}


# ---------------------------------------------------------------------------
# DeepSeek peak / off-peak billing window (announced Aug 22, 2026)
# ---------------------------------------------------------------------------
# Beijing weekday peak windows. Weekends are exempt entirely as of Aug 23.
_DS_PEAK_WINDOWS_BEIJING = ((9, 12), (14, 18))

# The one unknown, deliberately isolated. DeepSeek has published the WINDOWS
# but never the peak RATES -- its Aug 6 notice said "a significant increase"
# with no figure and no effective date. 1.0 == today's behaviour, so nothing
# changes until a real number is known. Set MTG_DS_PEAK_MULTIPLIER once the
# official pricing page carries it, and update MODEL_RATES in the same commit.
DEEPSEEK_PEAK_MULTIPLIER = float(
    os.getenv("MTG_DS_PEAK_MULTIPLIER", "1.0"))


def deepseek_pricing_window(now_utc=None) -> str:
    """'peak' or 'off_peak' for DeepSeek DIRECT billing, right now.

    Weekend is decided in BEIJING time, not local: a Friday evening in the US
    is already Saturday in Beijing and therefore off-peak, which is exactly
    the case an overnight batch hits.
    """
    import datetime as _dt

    now = now_utc or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    beijing = now.astimezone(_dt.timezone(_dt.timedelta(hours=8)))
    if beijing.weekday() >= 5:          # Sat/Sun in Beijing
        return "off_peak"
    hour = beijing.hour
    for start, end in _DS_PEAK_WINDOWS_BEIJING:
        if start <= hour < end:
            return "peak"
    return "off_peak"


def _is_direct_deepseek(model: str) -> bool:
    """Direct DeepSeek only.

    The surcharge is DeepSeek's own billing. DashScope resells the same model
    names at Alibaba's rates, and those adapters carry a `dashscope:` rate_key
    precisely so the two price separately.
    """
    return "deepseek" in (model or "").lower() and not (
        model or "").lower().startswith("dashscope:")


def provider_cost_score(adapter) -> float:
    """Heuristic $-per-token-mix score for ORDERING providers by price.

    Aug 7, 2026 (maintainer-approved, prompted by DeepSeek's announced
    "significant" price increase): the probe used to optimise latency only,
    which was the right call when DeepSeek was unambiguously the cheap
    default. This scores the provider's ACTOR model (the high-volume role —
    the same role the probe measures) as
        blended_input * 20 + output * 1
    per token: 75%-hit-share blended input, 20 input tokens per output
    token (the Aug-7 batch measured ~34:1 for the DS actor and ~10:1 for
    the thinking-inflated Qwen side — 20:1 sits between and the ORDERING is
    insensitive to the exact weight). This is a ranking heuristic, NOT a
    bill prediction; MODEL_RATES stays the single pricing truth, so a
    repricing (the pending DeepSeek increase) reorders selection by itself
    the day the table is updated. Reads rate_key first so the
    DashScope-resold DeepSeek prices at its resale rates.
    """
    model = getattr(adapter, "rate_key", None) or getattr(adapter, "model", "")
    hit, miss, out = rates_for_model(model)
    # rates_for_model returns per-TOKEN dollars; score in $-per-MILLION-mix
    # units so the probe log line is legible (qwen ~0.34, DS ~1.02,
    # DS-via-Alibaba ~1.39 at today's table).
    blended_in = 0.75 * hit + 0.25 * miss
    score = (blended_in * 20.0 + out) * 1_000_000
    # Aug 22, 2026: DeepSeek bills a weekday peak surcharge. The multiplier is
    # 1.0 until the real figure is published, so this is structure rather than
    # a guess -- but the window is real, and the day a number lands the
    # ordering corrects itself without touching this function.
    if _is_direct_deepseek(model) and deepseek_pricing_window() == "peak":
        score *= DEEPSEEK_PEAK_MULTIPLIER
    return score


async def choose_fastest_provider(candidates: list, samples: int = 2,
                                  timeout: float = 25.0):
    """Pick the CHEAPEST responsive provider within a latency band.

    `candidates` is [(name, probe_adapter), ...] — probe the ACTOR adapter of
    each provider, since that is the high-volume role whose latency dominates
    a batch.

    Selection (Aug 7, 2026 — cost-aware upgrade, maintainer-approved):
      1. Any probe ERROR disqualifies outright, exactly as before — the
         failure being dodged is 503, and a cheap-but-erroring provider is
         precisely the one not to pick.
      2. Healthy providers within the latency BAND of the fastest —
         median <= fastest * MTG_PROBE_BAND (default 1.5) + 250ms — are
         eligible; the cheapest eligible (provider_cost_score) wins, ties
         going to the faster one. A provider outside the band loses no
         matter how cheap: the Aug-3/Aug-7 Qwen probe spikes (7.4-7.6s vs a
         ~1s DS median) must still route away.
      3. MTG_PROBE_BAND=0 (or "off") restores pure latency selection — the
         documented revert path, no code change needed.

    Returns (winner_name_or_None, results_by_name). None means nothing was
    usable and the caller should keep whatever default it already had; it
    never means "pick arbitrarily".

    All probes run CONCURRENTLY, so the pre-flight costs one round-trip of
    wall clock rather than one per provider, and a few tokens in total.
    """
    named = [(n, a) for n, a in candidates if a is not None]
    if not named:
        return None, {}
    results = await asyncio.gather(
        *[probe_adapter_latency(a, samples, timeout) for _, a in named])
    by_name = {n: r for (n, _), r in zip(named, results)}
    adapters_by_name = {n: a for n, a in named}
    healthy = {n: r for n, r in by_name.items() if r["ok"]}
    for n, r in by_name.items():
        if r["median_ms"] is None:
            print(f"[PROVIDER-PROBE] {n}: UNUSABLE — {r['detail']}")
        elif r["ok"]:
            _ad = adapters_by_name[n]
            _mdl = getattr(_ad, "rate_key", None) or getattr(_ad, "model", "")
            _win = (" %s" % deepseek_pricing_window()
                    if _is_direct_deepseek(_mdl) else "")
            print(f"[PROVIDER-PROBE] {n}: median {r['median_ms']:.0f}ms "
                  f"(cost score {provider_cost_score(_ad):.3f}{_win})")
        else:
            print(f"[PROVIDER-PROBE] {n}: median {r['median_ms']:.0f}ms "
                  f"but {r['errors']} error(s) — disqualified: {r['detail']}")
    if not healthy:
        return None, by_name
    fastest = min(healthy, key=lambda n: healthy[n]["median_ms"])
    _band_raw = os.getenv("MTG_PROBE_BAND", "1.5").strip().lower()
    try:
        _band = 0.0 if _band_raw in ("0", "off", "") else float(_band_raw)
    except ValueError:
        _band = 1.5
    if _band <= 0 or len(healthy) == 1:
        return fastest, by_name
    _cutoff = healthy[fastest]["median_ms"] * _band + 250.0
    eligible = {n: r for n, r in healthy.items() if r["median_ms"] <= _cutoff}
    winner = min(
        eligible,
        key=lambda n: (provider_cost_score(adapters_by_name[n]),
                       eligible[n]["median_ms"]))
    if winner != fastest:
        print(f"[PROVIDER-PROBE] cost-aware: {winner} over {fastest} "
              f"(within {_cutoff:.0f}ms band, cheaper score)")
    return winner, by_name


def create_dashscope_deepseek_actor_adapter(api_key: str = None):
    """DeepSeek V4-Flash, served by Alibaba rather than DeepSeek.

    The lowest-risk failover there is: when DeepSeek's own API is congested
    this is the SAME MODEL on different infrastructure, so nothing about play
    quality changes — unlike switching to Qwen, which is a real A/B. Costs
    ~1.45x DeepSeek direct on blended input (the cache rate is 10x worse).

    Availability note: NOT offered in Singapore. Regions are Beijing,
    Frankfurt, US-Virginia and Tokyo, so this needs DASHSCOPE_REGION set to
    one of those — on the default Singapore endpoint the call will simply
    fail, and the pre-flight probe will skip it rather than break a batch.
    """
    return create_dashscope_adapter(
        "deepseek-v4-flash", api_key=api_key,
        log_tag="DASHSCOPE:DEEPSEEK-ACTOR", thinking_enabled=False,
        rate_key="dashscope:deepseek-v4-flash")


def create_dashscope_deepseek_strategist_adapter(api_key: str = None):
    """Strategist twin of the above — flash in THINKING mode, mirroring the
    Aug 2 A/B that this repo already validated on DeepSeek direct."""
    return create_dashscope_adapter(
        "deepseek-v4-flash", api_key=api_key,
        log_tag="DASHSCOPE:DEEPSEEK-STRATEGIST", thinking_enabled=True,
        rate_key="dashscope:deepseek-v4-flash", request_timeout=180.0)
