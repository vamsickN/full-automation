"""Anthropic Claude client routed through the derouter proxy.

Used for:
  - generating image prompts from an uploaded reference video (vision)
  - generating a script / shot list from a brief
  - planning a video edit from generated frames + audio (vision)
  - analysing a single scene image

All Claude features (vision, streaming, tools) are available via the official
SDK — we just point ``base_url`` at the derouter proxy.
"""
import base64
import json
import math
import re
import sys
from typing import List, Optional

import random
import threading
import time

import anthropic
from anthropic import (
    APIError,
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
)
try:
    from anthropic import RateLimitError
except Exception:                       # very old SDKs
    class RateLimitError(Exception):
        pass
import requests

import config

# --- Claude rate-limit handling --------------------------------------------
# The key enforces a CONCURRENT-request cap per model (e.g. "limit: 2 in-flight")
# plus the usual tokens-per-minute limits. We (a) cap how many Claude requests
# run at once process-wide so we don't trip the concurrent limit, and (b) retry
# 429 / 5xx / connection errors with backoff, honouring the server's
# `retry_after_sec` / Retry-After hint. Tunable via env.
CLAUDE_MAX_CONCURRENCY = max(1, int(float(config._get("CLAUDE_MAX_CONCURRENCY", "1"))))
CLAUDE_MAX_RETRIES = max(0, int(float(config._get("CLAUDE_MAX_RETRIES", "6"))))
CLAUDE_BACKOFF_BASE_MS = max(100, int(float(config._get("CLAUDE_BACKOFF_BASE_MS", "1500"))))
CLAUDE_BACKOFF_MAX_MS = max(1000, int(float(config._get("CLAUDE_BACKOFF_MAX_MS", "30000"))))

_CLAUDE_SEM = threading.Semaphore(CLAUDE_MAX_CONCURRENCY)


def _retry_after_seconds(e, default):
    """Pull a retry hint from a RateLimitError: Retry-After header or the body's
    retry_after_sec. Falls back to ``default``."""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            ra = resp.headers.get("retry-after")
            if ra:
                return float(ra)
        except Exception:
            pass
    try:
        body = getattr(e, "body", None) or {}
        err = body.get("error") if isinstance(body, dict) else {}
        if isinstance(err, dict) and err.get("retry_after_sec"):
            return float(err["retry_after_sec"])
    except Exception:
        pass
    low = str(e).lower()
    m = re.search(r"retry_after_sec[\"'\s:=]+(\d+(?:\.\d+)?)", low)
    if m:
        return float(m.group(1))
    return default


def _log(msg):
    print(f"[claude] {msg}", file=sys.stderr, flush=True)


class ClaudeClient:
    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        self.api_key = api_key or config.CLAUDE_API_KEY
        self.base_url = (base_url or config.CLAUDE_BASE_URL).rstrip("/")
        self.model = model or config.CLAUDE_MODEL
        # AgentRouter requires "Authorization: Bearer <key>" rather than "x-api-key".
        # Send both so the proxy can honour whichever it prefers without breaking
        # other providers that only read x-api-key.
        extra_headers: dict = {}
        if "agentrouter" in self.base_url.lower():
            extra_headers["Authorization"] = f"Bearer {self.api_key or 'unset'}"
        # timeout: proxies (9Router's Claude OAuth route especially) sometimes
        # stall without answering; the SDK default of 600s combined with its
        # 2 internal retries meant a single call could hang ~30 minutes with
        # no log output. Fail at 240s, disable SDK-internal retries, and let
        # _msg's own 502/timeout backoff loop (which logs every attempt) own
        # the retrying instead.
        self._sdk = anthropic.Anthropic(
            api_key=self.api_key or "unset",
            base_url=self.base_url,
            timeout=240.0,
            max_retries=0,
            **({"default_headers": extra_headers} if extra_headers else {}),
        )

    def _require_key(self):
        if not self.api_key:
            raise RuntimeError(
                "No Claude API key set. Add CLAUDE_API_KEY to your .env or paste a "
                "key in the Settings panel."
            )

    # ------------------------------------------------------------------ #
    #  Connectivity check
    # ------------------------------------------------------------------ #
    def ping(self):
        """Real auth check via a 1-token messages.create.

        derouter serves ``GET /v1/models`` WITHOUT auth, so a models list alone
        reports "ok" even when the key is invalid. We do a tiny round-trip
        through the Messages API instead — the only thing that proves the key
        works. The (unauthenticated) models list is still fetched for the UI.
        """
        if not self.api_key:
            return {"ok": False, "error": "no api key set"}

        ids = self._list_models_best_effort()

        if self._is_openai_model():
            # Non-Claude model (e.g. the user's ChatGPT account via 9Router):
            # test over the OpenAI protocol, which is what _msg will use.
            try:
                # generous cap: gpt-5 reasoning models spend tokens thinking
                # before emitting text, and an empty reply would read as a fail
                self._msg_openai([{"type": "text", "text": "ping"}],
                                 max_tokens=2000)
                return {"ok": True, "models": ids,
                        "configured_model": self.model,
                        "base_url": self.base_url}
            except Exception as e:
                return {"ok": False, "error": str(e), "models": ids,
                        "base_url": self.base_url}

        try:
            self._sdk.messages.create(
                model=self.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
        except AuthenticationError as e:
            return {"ok": False, "error": f"auth rejected — {self._format_err(e)}",
                    "models": ids, "base_url": self.base_url}
        except (APIConnectionError, APITimeoutError) as e:
            return {"ok": False, "error": f"connection failed — {self._format_err(e)}",
                    "base_url": self.base_url}
        except APIError as e:
            return {"ok": False, "error": f"API error — {self._format_err(e)}",
                    "models": ids, "base_url": self.base_url}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "base_url": self.base_url}

        return {"ok": True, "models": ids, "configured_model": self.model,
                "base_url": self.base_url}

    def _list_models_best_effort(self):
        """Fetch the model id list for the UI; never raises."""
        try:
            r = requests.get(
                f"{self.base_url}/v1/models",
                headers={"x-api-key": self.api_key,
                         "anthropic-version": "2023-06-01"},
                timeout=20,
            )
            if r.status_code < 400:
                data = r.json()
                return [m.get("id") for m in (data.get("data") or [])][:30]
        except Exception:
            pass
        return []

    @staticmethod
    def _format_err(e):
        klass = type(e).__name__
        msg = str(e) or "no message"
        body = None
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.text[:600]
            except Exception:
                body = None
        if body:
            return f"{klass}: {msg} | response: {body}"
        return f"{klass}: {msg}"

    # ------------------------------------------------------------------ #
    #  Low-level
    # ------------------------------------------------------------------ #
    def _is_openai_model(self):
        """True when the configured model is NOT a Claude model (e.g. cx/gpt-5.5
        — the user's ChatGPT/Codex account via 9Router). Those models reject the
        Anthropic protocol's max_tokens translation, so we speak the OpenAI chat
        protocol directly instead."""
        return bool(self.model) and "claude" not in self.model.lower()

    def _fallback_chat_model(self):
        """Emergency fallback for flaky local-router Claude routes: a non-Claude
        model on the same router's OpenAI endpoint (the user's ChatGPT account).
        Only active for local 9Router-style base URLs, where both accounts share
        one API key."""
        if self._is_openai_model():
            return None
        base = (self.base_url or "").lower()
        if "localhost" in base or "127.0.0.1" in base:
            return getattr(config, "CLAUDE_FALLBACK_MODEL", "") or None
        return None

    def _msg_openai(self, content_blocks, system: Optional[str] = None,
                    max_tokens: int = 4096, model: str = None):
        """OpenAI-protocol twin of _msg for non-Claude models behind an
        OpenAI-compatible router (9Router /v1). Accepts the same Anthropic-style
        content blocks (text + base64 images) and returns plain text."""
        self._require_key()
        model = model or self.model
        parts = []
        for b in content_blocks:
            t = b.get("type")
            if t == "text":
                parts.append({"type": "text", "text": b.get("text", "")})
            elif t == "image":
                src = b.get("source") or {}
                parts.append({"type": "image_url", "image_url": {
                    "url": f"data:{src.get('media_type', 'image/jpeg')};"
                           f"base64,{src.get('data', '')}"}})
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": parts})
        base = self.base_url.rstrip("/")
        # Most OpenAI-compatible routers expose /v1/chat/completions. Google's
        # Gemini OpenAI-compat endpoint already ends in /openai (no /v1), and
        # some bases are already versioned (/v1, /v1beta/openai). Only append
        # /v1 when the base has no recognisable OpenAI path suffix.
        if not (base.endswith("/v1") or base.endswith("/openai")
                or base.endswith("/v1beta") or "/openai" in base):
            base += "/v1"
        _log(f"chat.completions model={model} "
             f"max_completion_tokens={max_tokens} base={base}")
        last_err = None
        for attempt in range(1, CLAUDE_MAX_RETRIES + 2):
            try:
                with _CLAUDE_SEM:
                    r = requests.post(
                        f"{base}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}",
                                 "Content-Type": "application/json"},
                        json={"model": model,
                              "max_completion_tokens": max_tokens,
                              "messages": messages},
                        timeout=300)
                if r.status_code == 429 or r.status_code >= 500:
                    last_err = f"[{r.status_code}] {r.text[:300]}"
                    _log(f"chat {r.status_code} (attempt {attempt}) — backing off")
                    time.sleep(min(20, 2 ** attempt))
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(
                        f"chat API error [{r.status_code}]: {r.text[:400]}")
                # Some routers (e.g. 9Router for mimo models) return SSE
                # streaming instead of plain JSON even on non-streaming
                # requests. Handle both: plain JSON and SSE data: lines.
                raw = r.text.strip()
                if raw.startswith("data:"):
                    # SSE streaming response — parse the LAST complete chunk
                    # (which contains the final content).
                    import re as _re
                    chunks = _re.findall(r"^data: (\{.*\})$", raw, _re.MULTILINE)
                    if chunks:
                        data = json.loads(chunks[-1])
                    else:
                        raise RuntimeError(
                            f"chat API returned unparseable SSE (model={model}): "
                            f"{raw[:200]}")
                else:
                    data = r.json()
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                text = (msg.get("content") or "").strip()
                # "Thinking" models (e.g. mimo-v2.5-pro) put the actual
                # response in reasoning_content when content is empty.
                if not text:
                    text = (msg.get("reasoning_content") or "").strip()
                if not text:
                    raise RuntimeError(
                        f"chat API returned no text (model={model})")
                return text
            except RuntimeError:
                raise
            except Exception as e:
                last_err = str(e)
                _log(f"chat connection error (attempt {attempt}): {e}")
                time.sleep(min(20, 2 ** attempt))
        raise RuntimeError(f"could not reach chat API at {base} — {last_err}")

    def _msg(self, content_blocks, system: Optional[str] = None, max_tokens: int = 4096,
             stream: bool = False):
        self._require_key()
        if self._is_openai_model():
            return self._msg_openai(content_blocks, system=system,
                                    max_tokens=max_tokens)
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content_blocks}],
        }
        if system:
            kwargs["system"] = system

        # IMPORTANT: default to a NON-streaming messages.create.
        #
        # The derouter proxy does not emit a spec-compliant SSE stream for many
        # calls (especially vision): the Anthropic SDK's streaming helper then
        # finishes with no final-message snapshot and raises a bare
        # AssertionError (empty message), which surfaced to the user as
        # "analysis failed:" / "edit planning failed:" with nothing after the
        # colon. The plain messages.create path works fine against derouter, so
        # we use it for everything by default.
        #
        # Only very long *text* generations (the full paced script) risk idling
        # past the proxy's ~100s edge timeout; those opt into streaming via
        # stream=True and we fall back to non-streaming if the stream yields no
        # message.
        mode = "messages.stream" if stream else "messages.create"
        _log(f"{mode} model={self.model} max_tokens={max_tokens} "
             f"blocks={len(content_blocks)} base={self.base_url}")

        def _backoff(attempt):
            raw = CLAUDE_BACKOFF_BASE_MS * (2 ** (attempt - 1)) + \
                random.uniform(0, CLAUDE_BACKOFF_BASE_MS)
            return min(raw, CLAUDE_BACKOFF_MAX_MS) / 1000.0

        # Cap concurrent Claude calls (avoids the "limit: 2 in-flight" 429) and
        # retry 429 / 5xx / connection errors with backoff, honouring the
        # server's retry-after hint. The semaphore is released between attempts
        # so a backoff sleep never holds a slot.
        #
        # 9Router's Claude OAuth route is intermittently flaky (silent stalls /
        # "fetch connect timeout" 502 storms). When it keeps failing, reroute
        # the SAME call to the user's ChatGPT account on the router's OpenAI
        # endpoint instead of erroring out — GPT-5 handles the vision + JSON
        # work well enough to keep the pipeline moving.
        fb_model = self._fallback_chat_model()

        def _try_fallback(orig_err):
            _log(f"anthropic route failing — falling back to {fb_model} "
                 f"via the OpenAI endpoint")
            try:
                return self._msg_openai(content_blocks, system=system,
                                        max_tokens=max_tokens, model=fb_model)
            except Exception as fe:
                raise RuntimeError(
                    f"{orig_err} | fallback {fb_model} also failed: {fe}")

        # 429 model cascade: when the primary model is rate-limited, try
        # progressively lighter models on the same router before giving up.
        # This prevents "silent placeholder topics" / "keeps loading" when
        # the user's chosen model is throttled.
        _RATE_LIMIT_CASCADE = ["cc/claude-sonnet-4-6", "mimo/mimo-v2.5-pro"]

        def _try_cascade(orig_err):
            """Try lighter models on the same base_url. Returns result or raises."""
            for cm in _RATE_LIMIT_CASCADE:
                if cm == self.model:
                    continue  # skip the model that just failed
                _log(f"429 cascade — trying {cm}")
                if "claude" in cm.lower():
                    # Use the Anthropic SDK with the lighter model
                    try:
                        ckwargs = dict(kwargs)
                        ckwargs["model"] = cm
                        with _CLAUDE_SEM:
                            resp2 = self._sdk.messages.create(**ckwargs)
                        out2 = []
                        for block in resp2.content:
                            btype = getattr(block, "type", None)
                            if btype == "text":
                                out2.append(block.text)
                            elif btype == "tool_use":
                                inp = getattr(block, "input", None)
                                if inp:
                                    out2.append(json.dumps(inp))
                        _log(f"429 cascade succeeded with {cm}")
                        return "\n".join(out2).strip()
                    except Exception as ce:
                        _log(f"429 cascade {cm} failed: {ce}")
                        continue
                else:
                    # Use the OpenAI path for non-Claude models
                    try:
                        result = self._msg_openai(content_blocks, system=system,
                                                   max_tokens=max_tokens, model=cm)
                        _log(f"429 cascade succeeded with {cm}")
                        return result
                    except Exception as ce:
                        _log(f"429 cascade {cm} failed: {ce}")
                        continue
            raise RuntimeError(
                f"{orig_err} — all cascade models also rate-limited. "
                f"Tried: {[self.model] + _RATE_LIMIT_CASCADE}. "
                f"Wait for quota reset or switch model in Settings.")

        attempt = 0
        while True:
            try:
                with _CLAUDE_SEM:
                    if stream:
                        try:
                            with self._sdk.messages.stream(**kwargs) as s:
                                resp = s.get_final_message()
                        except Exception:
                            # Any stream failure (AssertionError, AttributeError,
                            # APIError, StopIteration from non-standard SSE, etc.)
                            # — fall back to a plain non-streaming create.
                            _log("stream produced no final message; retrying non-streamed")
                            resp = self._sdk.messages.create(**kwargs)
                    else:
                        resp = self._sdk.messages.create(**kwargs)
                break
            except AuthenticationError as e:
                raise RuntimeError(
                    f"Claude API rejected the key — {self._format_err(e)}")
            except RateLimitError as e:
                attempt += 1
                if attempt > CLAUDE_MAX_RETRIES:
                    # Primary model is rate-limited — try the cascade of
                    # lighter models before giving up entirely.
                    _emsg = (f"Claude API rate limit — {self.model} gave up "
                             f"after {attempt-1} retries: {self._format_err(e)}")
                    return _try_cascade(_emsg)
                wait = _retry_after_seconds(e, default=5.0) + random.uniform(0, 1.0)
                _log(f"429 rate_limit (attempt {attempt}) — waiting {wait:.1f}s "
                     f"then retrying")
                time.sleep(wait)
            except (APIConnectionError, APITimeoutError) as e:
                attempt += 1
                # Retry the Claude route a couple times before bailing to the
                # fallback — a single transient stall shouldn't abandon a model
                # that normally works. Only reroute to the GPT fallback after
                # the Claude route has genuinely failed repeatedly.
                if fb_model and attempt >= 3:
                    return _try_fallback(f"Claude route timed out at "
                                         f"{self.base_url}")
                if attempt > CLAUDE_MAX_RETRIES:
                    raise RuntimeError(
                        f"could not reach Claude API at {self.base_url} — "
                        f"{self._format_err(e)}")
                wait = _backoff(attempt)
                _log(f"connection/timeout (attempt {attempt}) — backing off "
                     f"{wait:.1f}s")
                time.sleep(wait)
            except APIError as e:
                status = getattr(e, "status_code", None)
                if status and 500 <= int(status) < 600:
                    attempt += 1
                    if fb_model and attempt >= 2:
                        return _try_fallback(
                            f"Claude route returning {status} at {self.base_url}")
                    if attempt > CLAUDE_MAX_RETRIES:
                        raise RuntimeError(
                            f"Claude API server error — gave up after "
                            f"{attempt-1} retries: {self._format_err(e)}")
                    wait = _backoff(attempt)
                    _log(f"server {status} (attempt {attempt}) — backing off "
                         f"{wait:.1f}s")
                    time.sleep(wait)
                else:
                    raise RuntimeError(f"Claude API error — {self._format_err(e)}")

        # Collect text from all text blocks.
        out = []
        tool_inputs = []
        thinking_out = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                out.append(block.text)
            elif btype == "tool_use":
                # Some proxies (e.g. agentrouter) and "thinking" models return
                # the structured answer as a TOOL CALL rather than a text block
                # (stop_reason=tool_use, no text). The tool input IS the JSON we
                # asked for — capture it so we can serialize it as the response.
                inp = getattr(block, "input", None)
                if inp:
                    tool_inputs.append(inp)
            elif btype == "thinking":
                t = getattr(block, "thinking", None)
                if t:
                    thinking_out.append(t)
        text = "\n".join(out).strip()
        stop = getattr(resp, "stop_reason", None)
        # If the model ran out of room, the JSON is truncated → parsing fails
        # downstream with a confusing error. Surface the real cause instead.
        if stop == "max_tokens":
            _log(f"WARNING stop_reason=max_tokens (output hit the {max_tokens} "
                 f"cap; response likely truncated)")
        if not text and tool_inputs:
            # Serialize the tool-call payload as JSON text; downstream callers
            # run extract_json() on it, so a JSON object/array parses cleanly.
            payload = tool_inputs[0] if len(tool_inputs) == 1 else tool_inputs
            try:
                text = json.dumps(payload)
            except (TypeError, ValueError):
                text = str(payload)
            _log(f"recovered answer from tool_use block "
                 f"(stop={stop}, model={self.model})")
        if not text:
            block_types = [getattr(b, "type", "?") for b in (resp.content or [])]
            _log(f"WARNING: empty text — model={self.model} base={self.base_url} "
                 f"stop={stop} n_blocks={len(resp.content or [])} types={block_types}")
            # Last resort: a thinking-only response sometimes carries the JSON in
            # the reasoning text (a fenced block). Hand it to extract_json rather
            # than failing outright.
            if thinking_out:
                _log("no text/tool_use — falling back to thinking-block content")
                return "\n".join(thinking_out).strip()
            raise RuntimeError(
                f"No text returned by the model at {self.base_url} "
                f"(stop_reason={stop}, blocks={block_types}). "
                "Check that your API key is valid and the provider supports this request type."
            )
        return text

    @staticmethod
    def _sniff_media_type(b: bytes) -> str:
        """Detect the real image format from its magic bytes.

        Vision calls were failing with HTTP 400 from derouter because the
        downsizer re-encodes frames as JPEG but every block was hardcoded as
        ``image/png`` — Anthropic rejects a media_type that doesn't match the
        actual bytes. Sniff it instead of trusting a fixed label.
        """
        if b[:3] == b"\xff\xd8\xff":
            return "image/jpeg"
        if b[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            return "image/webp"
        if b[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        return "image/png"

    @classmethod
    def _image_block(cls, image_bytes: bytes, media_type: str = None):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type or cls._sniff_media_type(image_bytes),
                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
            },
        }

    # ------------------------------------------------------------------ #
    #  Public helpers
    # ------------------------------------------------------------------ #
    def chat_text(self, prompt: str, system: Optional[str] = None, max_tokens: int = 4096,
                  stream: bool = False):
        """Plain text round-trip."""
        return self._msg([{"type": "text", "text": prompt}], system=system,
                         max_tokens=max_tokens, stream=stream)

    def vision_describe(self, images: List[bytes], instruction: str,
                        system: Optional[str] = None, max_tokens: int = 4096):
        """Send images + instruction, return text."""
        blocks = []
        for img in images[:20]:  # cap to keep token usage sane
            blocks.append(self._image_block(img))
        blocks.append({"type": "text", "text": instruction})
        return self._msg(blocks, system=system, max_tokens=max_tokens)

    # ------------------------------------------------------------------ #
    #  Higher-level tasks
    # ------------------------------------------------------------------ #
    def generate_script(self, title: str = "", description: str = "",
                        total_duration: float = 60.0, pacing_seconds: float = 1.0,
                        num_characters: int = 0, style_notes: str = "",
                        master_prompt: str = "", brief: str = "",
                        dialogue: bool = False, dynamic: bool = False):
        """Title + description -> full VO script + paced scene list + packed
        per-character sheet prompts.

        ``pacing_seconds`` is how long each image is on screen, so the number of
        scenes (= number of images) is round(total_duration / pacing_seconds).
        The VO is written to be readable in roughly ``total_duration`` seconds.

        Returns the raw model text (STRICT JSON). Caller runs extract_json().
        """
        # Back-compat: an old-style call may pass only `brief`.
        if brief and not description:
            description = brief

        scene_count = max(1, min(400, round((total_duration or 1) /
                                            max(0.1, pacing_seconds or 1))))
        # Target word count per scene so TTS timing matches the requested pacing.
        # Natural speech ≈ 2.5 words/second.
        words_per_scene = max(3, round(pacing_seconds * 2.5))

        # Character-count rule for the system prompt. Negative means AUTO:
        # let the writer decide how many recurring characters the story needs.
        if num_characters > 0:
            char_rule = (
                f"- The 'characters' array MUST have EXACTLY {num_characters} "
                "entries — no more, no fewer. Count before you output.\n"
            )
        elif num_characters == 0:
            char_rule = (
                "- This is a pure-narration video. The 'characters' array MUST "
                "be an empty list [].\n"
            )
        else:
            char_rule = (
                "- AUTO-CAST: decide how many recurring characters this script "
                "actually needs from the story and requested duration. Use 0 only "
                "for pure documentary/object-only narration; use 1 relatable lead "
                "for most explainers; use 2-3 only when interactions or contrast "
                "matter. Avoid bloated casts. Every recurring person/animal/mascot "
                "named in scenes MUST appear in characters[] with a rich sheet_prompt.\n"
            )
        
        # Character sheet prompts need to be style-anchored; otherwise generated
        # sheets drift and scene renders inherit the wrong design.
        char_sheet_rule = (
            "- Each character sheet_prompt must be a production-ready character "
            "design paragraph: exact body shape, face/head shape, clothing, colors, "
            "silhouette, expressions, and how that design matches the reference "
            "video art style. Do NOT describe generic realistic people.\n"
        )

        system = (
            "You are a film director, narrator and screenwriter for a "
            "continuity-driven visual series produced as a sequence of still "
            "images shown in time with a voice-over. Return STRICT JSON ONLY "
            "(no prose, no markdown fences) shaped EXACTLY as:\n"
            "{\n"
            '  "title": str,\n'
            '  "logline": str,\n'
            '  "voiceover": str,            // the full narration script, plain text\n'
            f'  "pacing_seconds": {pacing_seconds:g},   // FIXED — do not change\n'
            f'  "total_duration": {total_duration:.0f},  // FIXED — do not change\n'
            f'  "scene_count": {scene_count},             // FIXED — do not change\n'
            '  "characters": [ { "name": str, "sheet_prompt": str } ],\n'
            '  "scenes": [ { "n": int, "heading": str, "action": str, "vo": str, "prompt": str, "shot_relation": "cut"|"continue" } ]\n'
            "}\n"
            "RULES:\n"
            f"- scenes[] MUST have EXACTLY {scene_count} elements — count them "
            "before you output. Stop adding scenes once you reach that number.\n"
            '- Each scene "shot_relation" tells the renderer how this shot relates '
            "to the PREVIOUS one:\n"
            '    "continue" = SAME continuous moment as the previous scene — a '
            "micro-cut: push-in/pull-out, reverse angle, a tighter close-up, or a "
            "small reaction on the SAME subject in the SAME place/instant. The "
            "renderer will reuse the previous frame so the look stays locked.\n"
            '    "cut" = a NEW beat — different location, time jump, different '
            "subject/action, or any fresh scene. The renderer composes it freshly.\n"
            '  Default to "cut". Only use "continue" when this scene is genuinely '
            "the same moment as the one right before it. The FIRST scene is always "
            '"cut".\n'
            f"- Each scene 'vo' should be approximately {words_per_scene} words "
            f"({pacing_seconds:g}s of speech at natural pace). "
            "Exception: the first 3-5 hook scenes may use shorter 4-8 word bursts.\n"
            f"- The total narration must read aloud in ~{total_duration:.0f} seconds.\n"
            + char_rule + char_sheet_rule +
            '- Each scene "vo" is the slice of narration spoken while that image is '
            'on screen; concatenated in order they equal the full "voiceover".\n'
            '- Each scene "prompt" is a self-contained image-generation prompt. '
            "Start EVERY prompt with the STYLE NOTES prefix (if provided) then add "
            "the scene subject, action, framing, and mood. Name any recurring "
            "character by their exact short name so a reference sheet can be "
            "auto-attached.\n"
            '- Each character "sheet_prompt" is ONE rich paragraph describing that '
            "character's canonical look (face, hair, build, outfit, colors, vibe) — "
            "written so it can be sent straight to a character-sheet generator. Use "
            "the exact same names as in the scene prompts.\n"
            "- HOOK IS EVERYTHING. The first 3-5 scenes MUST be a rapid-fire, "
            "scroll-stopping opening — shocking question, wild claim, or visceral "
            "image. Short punchy VO (4-8 words). After the hook, settle into "
            f"normal ~{words_per_scene}-word VO lines per scene.\n"
            "- STYLE BAN: NEVER write doodle, hand-drawn, hand-sketched, "
            "whiteboard, marker-sketch, pencil-sketch, crayon, scribble, or any "
            "hand-crafted art style in any scene prompt or character sheet_prompt. "
            "If the STYLE NOTES contain any of these, IGNORE them and use "
            "stick man / stick figure style (clean line-art humans, round heads, "
            "minimal detail) instead."
        )
        if pacing_seconds < 2.0:
            system += (
                f"\n\nFAST-CUT MODE ({pacing_seconds:g}s/frame — {scene_count} frames total):\n"
                f"- This is a rapid-fire montage. You MUST write ALL {scene_count} scenes.\n"
                "- Each frame is a completely DIFFERENT visual moment — new angle, new action, "
                "new environment, or new detail. Never repeat the same image description.\n"
                "- Think: establish → close-up → reaction → cutaway → wide → detail → payoff. "
                "Cycle through these beats so every consecutive frame feels like a real cut.\n"
                f"- Before you output, count your scenes[]. If it is not EXACTLY {scene_count}, "
                "add more unique scenes until it is. Do NOT stop early.\n"
                f"- ~{words_per_scene} words of VO per scene (3 words is fine for fast cuts).\n"
            )
        if dynamic:
            system += (
                "\n\nDYNAMIC / HIGH-RETENTION MODE (avoid a static slideshow):\n"
                "- Every scene image \"prompt\" MUST show the MAIN CHARACTER actively "
                "REACTING to that exact beat — pick the fitting one: scared, confused, "
                "coughing, choking, sweating, running, shielding their face, stumbling/"
                "falling back from heat, bracing for impact, looking up at the sky, "
                "pointing, jaw-dropped. Never just standing and watching.\n"
                "- VARY THE BACKGROUND every scene and ESCALATE the danger over time "
                "(calm/confused -> toxic sky -> extreme heat -> lava oceans -> meteor "
                "storm -> no oxygen/black cracked ground -> payoff). Pull from: lava "
                "ocean, smoky toxic sky, meteor storm, cracked black ground, volcano "
                "silhouettes, burning horizon, ash clouds, glowing molten surface.\n"
                "- Add a CAMERA/COMPOSITION cue to each prompt (extreme close-up on the "
                "face, low dramatic angle, wide establishing, over-the-shoulder, dutch "
                "tilt) so consecutive frames feel cut, not repeated.\n"
                "- Keep the SAME simple art style the whole way through.\n"
                "- The first scene must be a strong HOOK and the last a punchy PAYOFF.\n"
            )
        if dialogue:
            system += (
                "\n\nDIALOGUE MODE:\n"
                '- Write each scene "vo" with speaker tags: [SPEAKER NAME]: Their line here.\n'
                "- Use [NARRATOR]: for narration lines.\n"
                "- Use the character's exact name in brackets for dialogue lines.\n"
                '- The full "voiceover" must also use these speaker tags.\n'
                "- Example: \"[NARRATOR]: The city sleeps.\\n[MAYA]: I can't stay here.\""
            )
        bits = []
        if title.strip():
            bits.append(f"TITLE:\n{title.strip()}")
        if description.strip():
            bits.append(f"DESCRIPTION / HOW IT SHOULD BE:\n{description.strip()}")
        bits.append(f"Target total duration: {total_duration:.0f} seconds")
        bits.append(
            f"Pacing: {pacing_seconds:g}s per image  =>  EXACTLY {scene_count} scenes  "
            f"(~{words_per_scene} words per 'vo', body scenes; 4-8 words for hook scenes)"
        )
        if num_characters > 0:
            bits.append(
                f"Characters: EXACTLY {num_characters} recurring character(s) in the "
                "'characters' array."
            )
        elif num_characters == 0:
            bits.append("Characters: NONE — 'characters' must be an empty list [].")
        else:
            bits.append("Characters: define as many as the story naturally needs.")
        if style_notes.strip():
            bits.append(f"STYLE NOTES:\n{style_notes.strip()}")
        if master_prompt.strip():
            bits.append(f"WORLD / STYLE BIBLE:\n{master_prompt.strip()}")
        tok = min(64000, max(16000, scene_count * 90))
        return self.chat_text("\n\n".join(bits), system=system, max_tokens=tok,
                              stream=True)

    def prompts_from_video_frames(self, frames: List[bytes], count: int = 8,
                                  style_hint: str = "", master_prompt: str = ""):
        """Given sampled frames of a reference video, produce ``count`` image
        prompts that recreate that look as a continuous series."""
        system = (
            "You analyse a reference video (sampled as still frames) and write "
            "image-generation prompts that recreate its world, palette, lighting "
            "and cinematography across a continuous series. Return STRICT JSON only:\n"
            '{ "style_summary": str, "prompts": [str, ...] }\n'
            "Each prompt is a self-contained visual description (subject, framing, "
            "lighting, mood, palette) — no character names unless they obviously "
            "recur. Do NOT include any preamble."
        )
        instr_bits = [
            f"Write {count} image prompts that form a coherent series in the SAME "
            "world, style, palette and lighting language as these reference frames."
        ]
        if style_hint.strip():
            instr_bits.append(f"User style hint: {style_hint.strip()}")
        if master_prompt.strip():
            instr_bits.append(f"Existing world bible: {master_prompt.strip()}")
        return self.vision_describe(frames, "\n\n".join(instr_bits), system=system,
                                    max_tokens=6000)

    def suggest_from_reference(self, frames: List[bytes], transcript: str = "",
                               source_title: str = "", source_channel: str = "",
                               nudge: str = "", master_prompt: str = "",
                               n_suggestions: int = 10):
        """Analyse a reference YouTube video (sampled frames + transcript) and
        return STRICT JSON with a style read + N ready-to-produce video ideas in
        the same vein.

        Shape:
          { "style_summary": str, "speech_style": str, "topic": str,
            "suggestions": [ {
                "title": str, "logline": str,
                "num_characters": int,
                "characters": [ { "name": str, "sheet_prompt": str } ],
                "voiceover_style": str,
                "image_prompt_style": str,
                "pacing_seconds": number, "total_duration": number,
                "scene_count": int
            } ] }
        """
        system = (
            "You are a viral short-form video strategist and film director. You "
            "are shown sampled frames from a reference video plus its transcript. "
            "Analyse its VISUAL STYLE (palette, lighting, framing, editing rhythm), "
            "its TOPIC, and its WAY OF SPEAKING (tone, pacing, vocabulary, hooks). "
            "Then invent fresh video ideas in the SAME spirit — same energy, format "
            "and audience — that a creator could produce as a sequence of generated "
            "still images with a voice-over.\n"
            "Return STRICT JSON ONLY (no prose, no markdown fences):\n"
            "{\n"
            '  "style_summary": str,   // 2-3 sentences on the visual look\n'
            '  "speech_style": str,    // how the narration talks (tone/pacing/hooks)\n'
            '  "topic": str,           // what the source is about\n'
            '  "suggestions": [ {\n'
            '     "title": str,\n'
            '     "logline": str,                 // one punchy sentence\n'
            '     "hook": str,                    // the first-3-seconds opening line/visual\n'
            '     "distinct_angle": str,          // how this DIFFERS from the source (fresh, NOT a re-upload)\n'
            '     "virality_score": int,          // 1-100 predicted virality\n'
            '     "virality_reason": str,         // why it could pop (hook, format, timeliness, emotion)\n'
            '     "num_characters": int,          // 0-8 recurring characters\n'
            '     "characters": [ {"name": str, "sheet_prompt": str} ],\n'
            '     "voiceover_style": str,          // how this VO should sound\n'
            '     "image_prompt_style": str,       // 40-60 word visual style spec — see rules\n'
            '     "pacing_seconds": number,        // seconds each image is on screen\n'
            '     "total_duration": number,        // seconds\n'
            '     "scene_count": int               // = round(total_duration/pacing)\n'
            "  } ]\n"
            "}\n"
            f"RULES:\n- Produce EXACTLY {n_suggestions} suggestions, varied but all "
            "true to the reference's style and audience.\n"
            "- image_prompt_style MUST be a DETAILED 40-60 word visual style specification "
            "that fully defines the look for an image-generation model. Cover ALL six: "
            "(1) rendering technique — flat 2D vector / photorealistic 3D CGI / cel-shaded "
            "animation / stop-motion / etc., "
            "(2) colour palette — name 4-6 specific colours or ranges (e.g. 'burnt orange, "
            "deep teal, off-white, dark charcoal'), "
            "(3) line style — e.g. 'bold 3px black outlines' / 'no outlines' / 'thin grey strokes', "
            "(4) lighting — e.g. 'soft warm ambient', 'hard dramatic side-light', 'flat even', "
            "(5) texture/finish — e.g. 'smooth clean', 'film grain', 'painterly', 'cel shading', "
            "(6) character style — proportions, face style, level of detail. "
            "This string is prepended to EVERY image prompt so it must define the whole look alone. "
            "Example: 'flat 2D vector animation, bold black outlines, warm palette of burnt orange "
            "#E05C00 and cream, minimal shading, simple geometric backgrounds, expressive "
            "characters with large round heads, smooth finish, soft even lighting'.\n"
            "- Each idea must be a FRESH, DISTINCT concept — same niche/energy as the "
            "source but NOT a copy or re-upload of it. distinct_angle states plainly "
            "what makes it new.\n"
            "- Score virality_score honestly (1-100): reward a strong hook, an "
            "emotional or surprising payoff, a repeatable format, broad relatability "
            "and timeliness. ORDER the suggestions from HIGHEST to LOWEST "
            "virality_score (most viral first).\n"
            "- characters[].sheet_prompt is ONE rich paragraph of that character's "
            "canonical look, ready for a character-sheet generator. If num_characters "
            "is 0, characters is an empty list.\n"
            "- Keep pacing_seconds, total_duration and scene_count internally "
            "consistent (scene_count ≈ total_duration / pacing_seconds).\n"
            "- STYLE BAN: NEVER suggest doodle, hand-drawn, hand-sketched, "
            "whiteboard, marker-sketch, pencil-sketch, crayon, or scribble in "
            "image_prompt_style. Use stick man / stick figure style instead if "
            "the reference uses any hand-crafted art style."
        )
        bits = []
        if source_title:
            bits.append(f"SOURCE TITLE: {source_title}")
        if source_channel:
            bits.append(f"SOURCE CHANNEL: {source_channel}")
        if transcript.strip():
            bits.append("TRANSCRIPT (topic + speaking style):\n" + transcript.strip())
        else:
            bits.append("No transcript available — infer topic/voice from the frames.")
        if nudge.strip():
            bits.append(f"CREATOR'S EXTRA DIRECTION: {nudge.strip()}")
        if master_prompt.strip():
            bits.append(f"EXISTING WORLD / STYLE BIBLE: {master_prompt.strip()}")
        bits.append(f"Now analyse the look and write EXACTLY {n_suggestions} "
                    "suggestions. JSON only.")
        # Frames first (vision), then the instruction text.
        return self.vision_describe(frames, "\n\n".join(bits), system=system,
                                    max_tokens=8000)

    def suggest_from_references(self, frames: List[bytes], sources: List[dict],
                                nudge: str = "", master_prompt: str = "",
                                n_suggestions: int = 10):
        """Analyse MANY reference YouTube videos at once and return a deep,
        4-axis read of what they have in common plus N ready-to-produce video
        ideas in that combined vein.

        ``frames`` is a FLAT list of sampled stills pooled from every video.
        ``sources`` is per-video metadata: [{title, channel, transcript}, ...].

        Returns the raw model text (STRICT JSON). Caller runs extract_json().
        Shape:
          { "art_style": str, "pacing": str, "speech_style": str,
            "storytelling": str, "sources_summary": str,
            "suggestions": [ {
                "title": str, "logline": str, "num_characters": int,
                "characters": [ {"name": str, "sheet_prompt": str} ],
                "voiceover_style": str, "image_prompt_style": str,
                "pacing_seconds": number, "total_duration": number,
                "scene_count": int
            } ] }
        """
        system = (
            "You are a viral short-form video strategist and film director. You "
            "are shown sampled frames pooled from SEVERAL reference videos plus "
            "their transcripts. Study what they share and deconstruct it along "
            "FOUR axes:\n"
            "  1. ART STYLE  — palette, lighting, framing, texture, the visual look\n"
            "  2. PACING     — shot length, editing rhythm, energy, how fast it moves\n"
            "  3. WAY OF SPEAKING — narration tone, vocabulary, hooks, cadence\n"
            "  4. STORYTELLING — structure, how a hook opens, how tension builds, payoff\n"
            "Then invent fresh video ideas that fuse those four qualities — same "
            "spirit, format and audience — producible as a sequence of generated "
            "still images with a voice-over.\n"
            "Return STRICT JSON ONLY (no prose, no markdown fences):\n"
            "{\n"
            '  "art_style": str,        // 2-3 sentences on the shared visual look\n'
            '  "pacing": str,           // how fast/slow it cuts and why it works\n'
            '  "speech_style": str,     // how the narration talks (tone/hooks/cadence)\n'
            '  "storytelling": str,     // shared narrative structure & hook strategy\n'
            '  "sources_summary": str,  // 1-2 sentences on what these videos are about\n'
            '  "suggestions": [ {\n'
            '     "title": str,\n'
            '     "logline": str,                 // one punchy sentence\n'
            '     "hook": str,                    // the first-3-seconds opening line/visual\n'
            '     "distinct_angle": str,          // how this DIFFERS from the sources (fresh, NOT a re-upload)\n'
            '     "num_characters": int,          // 0-8 recurring characters\n'
            '     "characters": [ {"name": str, "sheet_prompt": str} ],\n'
            '     "voiceover_style": str,          // how this VO should sound\n'
            '     "image_prompt_style": str,       // 40-60 word visual style spec — see rules\n'
            '     "pacing_seconds": number,        // seconds each image is on screen\n'
            '     "total_duration": number,        // seconds\n'
            '     "scene_count": int,              // = round(total_duration/pacing)\n'
            '     "virality_score": int,           // 1-100 predicted virality\n'
            '     "virality_reason": str           // why it could pop (hook, format, timeliness, emotion)\n'
            "  } ]\n"
            "}\n"
            f"RULES:\n- Produce EXACTLY {n_suggestions} suggestions, varied but all "
            "true to the references' combined style and audience.\n"
            "- Each idea must be a FRESH, DISTINCT concept — same niche/energy as the "
            "sources but NOT a copy or re-upload of them. distinct_angle states plainly "
            "what makes it new, and hook is the scroll-stopping first 3 seconds.\n"
            "- Score virality_score honestly (1-100): reward a strong hook, a "
            "proven format, emotional pull and timeliness; punish generic ideas. "
            f"Return the {n_suggestions} suggestions SORTED by virality_score, most "
            "viral FIRST.\n"
            "- image_prompt_style MUST be a DETAILED 40-60 word visual style specification "
            "covering: (1) rendering technique, (2) 4-6 specific colours, (3) line style, "
            "(4) lighting, (5) texture/finish, (6) character proportions and face style. "
            "Example: 'photorealistic 3D CGI, warm golden side-lighting, shallow depth of "
            "field, cinematic teal-and-orange grade, subtle film grain, realistic human "
            "proportions with expressive faces, smooth high-detail textures'.\n"
            "- characters[].sheet_prompt is ONE rich paragraph of that character's "
            "canonical look, ready for a character-sheet generator. If num_characters "
            "is 0, characters is an empty list.\n"
            "- Keep pacing_seconds, total_duration and scene_count internally "
            "consistent (scene_count ≈ total_duration / pacing_seconds).\n"
            "- STYLE BAN: NEVER suggest doodle, hand-drawn, hand-sketched, "
            "whiteboard, marker-sketch, pencil-sketch, crayon, or scribble in "
            "image_prompt_style. Use stick man / stick figure style instead if "
            "the reference uses any hand-crafted art style."
        )
        bits = [f"You are analysing {len(sources)} reference video(s)."]
        for i, src in enumerate(sources, 1):
            head = f"--- SOURCE {i}"
            if src.get("title"):
                head += f": {src['title']}"
            if src.get("channel"):
                head += f"  (channel: {src['channel']})"
            bits.append(head)
            tr = (src.get("transcript") or "").strip()
            bits.append("TRANSCRIPT:\n" + tr if tr else
                        "No transcript — infer topic/voice from the frames.")
        if nudge.strip():
            bits.append(f"CREATOR'S EXTRA DIRECTION: {nudge.strip()}")
        if master_prompt.strip():
            bits.append(f"EXISTING WORLD / STYLE BIBLE: {master_prompt.strip()}")
        bits.append(
            "Now deconstruct the shared ART STYLE, PACING, WAY OF SPEAKING and "
            f"STORYTELLING, then write EXACTLY {n_suggestions} suggestions. JSON only.")
        # Frames first (vision), then the instruction text.
        return self.vision_describe(frames, "\n\n".join(bits), system=system,
                                    max_tokens=8000)

    # ----- Wave 3: script editing helpers -----
    def regen_scene(self, n, heading, action, vo, prompt, direction="", master_prompt=""):
        system = ("Rewrite ONE scene of a visual voice-over script. Return STRICT "
                  'JSON ONLY: {"heading":str,"action":str,"vo":str,"prompt":str}. '
                  "Keep it consistent with the rest of the show; the image prompt "
                  "stays a self-contained visual description.")
        bits = [f"SCENE {n}", f"heading: {heading}", f"action: {action}",
                f"vo: {vo}", f"image prompt: {prompt}"]
        if direction.strip():
            bits.append(f"DIRECTION: {direction.strip()}")
        if master_prompt.strip():
            bits.append(f"WORLD / STYLE: {master_prompt.strip()}")
        bits.append("Rewrite this scene. JSON only.")
        return self.chat_text("\n".join(bits), system=system, max_tokens=1500)

    def translate_script(self, voiceover, scene_vos, lang):
        system = ("Translate a voice-over script for narration. Return STRICT JSON "
                  'ONLY: {"voiceover":str,"scenes":[{"n":int,"vo":str}]}. Translate '
                  "naturally; keep proper names; do not add commentary.")
        payload = {"voiceover": voiceover,
                   "scenes": [{"n": i + 1, "vo": v} for i, v in enumerate(scene_vos)]}
        return self.chat_text(
            f"Target language: {lang}\n\nTranslate this script:\n" +
            json.dumps(payload, ensure_ascii=False),
            system=system, max_tokens=8000)

    def rewrite_tone(self, voiceover, scenes, tone, direction="", master_prompt=""):
        system = ("Rewrite a voice-over script in a new TONE, keeping the SAME scene "
                  "count, numbering, headings positions and image prompts. Return "
                  'STRICT JSON ONLY: {"voiceover":str,"scenes":[{"n":int,"heading":str,'
                  '"action":str,"vo":str}]}.')
        bits = [f"NEW TONE: {tone}"]
        if direction.strip():
            bits.append(f"EXTRA DIRECTION: {direction.strip()}")
        if master_prompt.strip():
            bits.append(f"WORLD / STYLE: {master_prompt.strip()}")
        bits.append("Rewrite (keep scene numbers; do NOT change image prompts):\n" +
                    json.dumps({"voiceover": voiceover, "scenes": scenes}, ensure_ascii=False))
        return self.chat_text("\n\n".join(bits), system=system, max_tokens=8000)

    def hooks(self, title, description, n=6):
        system = ('Write punchy opening hooks for a short video. Return STRICT JSON '
                  'ONLY: {"hooks":[str, ...]}. Each hook is ONE scroll-stopping sentence.')
        return self.chat_text(
            f"Title: {title}\nAbout: {description}\nWrite {n} distinct hooks. JSON only.",
            system=system, max_tokens=1500)

    def outline(self, title, description, beats=6):
        system = ('Outline a short video as a beat sheet. Return STRICT JSON ONLY: '
                  '{"beats":[{"title":str,"summary":str}]}.')
        return self.chat_text(
            f"Title: {title}\nAbout: {description}\nProduce {beats} beats that build "
            "a satisfying arc. JSON only.", system=system, max_tokens=2000)

    def character_consistency(self, sheet_img, frames, name):
        system = ("You check visual character consistency. The FIRST image is the "
                  "canonical reference sheet; the rest are story frames in order. Flag "
                  "where the character drifts (face, hair, outfit, colours). Return "
                  'STRICT JSON ONLY: {"summary":str,"issues":[{"frame":int,"note":str}]}. '
                  "`frame` is the 1-based position among the story frames shown.")
        return self.vision_describe(
            [sheet_img] + list(frames),
            f"Character: {name}. Compare every story frame to the canonical sheet and "
            "report drift. JSON only.", system=system, max_tokens=2000)

    # ----- Wave 4b: audio-sync edit planning -----
    def edit_holds(self, vo_lines: list, total_duration: float) -> list:
        """Analyse voice-over narration per frame and return optimal hold_seconds
        so image cuts lock to the natural rhythm of the speech.

        ``vo_lines`` is a list of VO strings, one per frame (empty string if a
        frame has no matching VO line).  Returns a list of floats the same
        length as ``vo_lines``; the sum is normalised to ``total_duration``.

        Returns ONLY a JSON array — no prose — so it is cheap and fast.
        """
        n = len(vo_lines)
        if n == 0:
            return []
        lines_text = "\n".join(
            f"{i+1}. {ln.strip() or '(no line)'}"
            for i, ln in enumerate(vo_lines)
        )
        system = (
            "You are a video editor. Given voice-over narration lines (one per "
            "image frame) and the total audio duration, calculate the optimal "
            "hold_seconds for each frame so cuts feel natural and match the "
            "narration energy.\n"
            "RULES:\n"
            "- Hook frames (first 3-4): cap at 0.8s — very fast, scroll-stopping.\n"
            "- Punchy lines ≤6 words, questions, exclamations: 0.6–1.2s.\n"
            "- Medium lines 7–12 words: 1.0–2.0s.\n"
            "- Long detailed lines >12 words: 1.5–3.5s.\n"
            "- Frames with no VO: 0.7s (micro-cut filler).\n"
            "- The sum of all values MUST equal the total duration exactly.\n"
            "Return ONLY a compact JSON array of numbers — no keys, no prose:\n"
            "  [0.8, 0.7, 1.4, 2.1, ...]"
        )
        prompt = (
            f"Total audio duration: {total_duration:.2f}s\n"
            f"Number of frames: {n}\n\n"
            f"VO lines per frame:\n{lines_text}\n\n"
            f"Return a JSON array of exactly {n} hold_seconds floats. JSON only."
        )
        raw = self.chat_text(prompt, system=system, max_tokens=max(512, n * 12))
        # parse — accept a bare array or {"holds":[...]} wrapper
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
        start = raw.find("[")
        if start >= 0:
            raw = raw[start:]
            end = raw.rfind("]")
            if end >= 0:
                raw = raw[:end + 1]
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            for k in ("holds", "hold_seconds", "durations"):
                if k in parsed:
                    parsed = parsed[k]
                    break
        holds = []
        for v in parsed:
            try:
                holds.append(max(0.4, float(v)))
            except (TypeError, ValueError):
                holds.append(0.4)  # model returned null/non-numeric; use floor
        if len(holds) != n:
            raise ValueError(f"expected {n} holds, got {len(holds)}")
        # Normalise sum to exactly total_duration.
        total = sum(holds) or 1.0
        return [round(h * total_duration / total, 3) for h in holds]

    def generate_missing_scenes(self, existing_scenes: list, needed: int,
                               voiceover: str = "", style_notes: str = "",
                               master_prompt: str = "") -> str:
        """Generate `needed` UNIQUE additional scenes to reach a target count.

        Unlike the mechanical split (which repeats prompts with camera cues),
        this asks Claude to write truly new visual moments so every frame in a
        fast-cut video looks different.  Returns raw JSON text:
          {"scenes": [{n, heading, action, vo, prompt}, ...]}
        """
        total = len(existing_scenes) + needed
        words = voiceover.split() if voiceover else []
        chunk_sz = max(1, math.ceil(len(words) / total)) if words else 0
        vo_slots = []
        for i in range(len(existing_scenes), total):
            start = i * chunk_sz
            vo_slots.append((i + 1, " ".join(words[start:start + chunk_sz])))

        style_prefix = f"Start EVERY prompt with: \"{style_notes.strip()}\"\n" \
                       if style_notes.strip() else ""
        system = (
            f"You are extending a video script. {len(existing_scenes)} scenes exist; "
            f"add EXACTLY {needed} MORE (n={len(existing_scenes)+1}..{total}).\n"
            "Return STRICT JSON ONLY:\n"
            '{{"scenes": [{{"n": int, "heading": str, "action": str, '
            '"vo": str, "prompt": str}}, ...]}}\n'
            f"RULES:\n"
            f"- Generate EXACTLY {needed} scene objects — count before output.\n"
            "- Every 'prompt' is a DIFFERENT visual moment: vary location, character "
            "pose, camera angle (close-up / wide / low / high / reaction).\n"
            f"{style_prefix}"
            "- STYLE BAN: no doodle, hand-drawn, whiteboard, scribble.\n"
        )
        bits = []
        if existing_scenes:
            snips = "\n".join(
                f"  {s.get('n',i+1)}. {(s.get('prompt') or '')[:70]}"
                for i, s in enumerate(existing_scenes[:6])
            )
            bits.append(f"Existing scenes (DO NOT repeat these moments):\n{snips}")
        if voiceover:
            bits.append(f"Full voiceover:\n{voiceover[:600]}")
        bits.append(
            "VO slices for the NEW scenes:\n" +
            "\n".join(f"  Scene {n}: \"{vo}\"" for n, vo in vo_slots)
        )
        if style_notes.strip():
            bits.append(f"STYLE: {style_notes.strip()}")
        if master_prompt.strip():
            bits.append(f"WORLD BIBLE: {master_prompt.strip()[:200]}")
        bits.append(f"Write exactly {needed} NEW scenes. JSON only.")
        tok = max(2000, needed * 160)
        return self.chat_text("\n\n".join(bits), system=system, max_tokens=tok)

    # ----- Wave 5: YT growth helpers -----
    def scenes_from_transcript(self, segments: list, style_notes: str = "",
                                master_prompt: str = "", dynamic: bool = True) -> str:
        """Audio->Video: write ONE visual scene per transcript segment.

        ``segments`` is a list of ``{"text": str, "start": float, "end": float}``
        taken straight from the user's transcribed audio. The narration is FIXED
        (it is the user's own audio) — Claude must NOT rewrite the words; it only
        invents the matching VISUAL for each segment so the images illustrate
        exactly what is being said at that moment. Fast micro-cut, high-retention
        pacing with continuity across shots.

        Returns STRICT JSON text:
          {"characters":[{name, sheet_prompt}],
           "scenes":[{n, vo, prompt, shot_relation}]}
        The vo of scene i is segments[i]["text"] verbatim; prompt is the image.
        """
        n = len(segments)
        seg_lines = "\n".join(
            f'{i+1}. [{s.get("start",0):.1f}-{s.get("end",0):.1f}s] "{(s.get("text") or "").strip()}"'
            for i, s in enumerate(segments)
        )
        style_prefix = (f'Start EVERY scene prompt with this style: "{style_notes.strip()}"\n'
                        if style_notes.strip() else "")
        dyn = ("- HIGH-RETENTION: every frame shows the subject ACTIVELY doing/"
               "reacting to that exact line — motion, emotion, a fresh angle. "
               "Never a static talking-head repeat.\n") if dynamic else ""
        system = (
            "You are a music-video / explainer director. You are given the EXACT "
            "transcript of a piece of audio, split into timed segments. Write ONE "
            "image scene per segment that VISUALLY illustrates that line. Return "
            "STRICT JSON ONLY (no prose, no fences) shaped EXACTLY as:\n"
            '{\n'
            '  "characters": [ { "name": str, "sheet_prompt": str } ],\n'
            '  "scenes": [ { "n": int, "vo": str, "prompt": str, "shot_relation": "cut"|"continue" } ]\n'
            "}\n"
            "RULES:\n"
            f"- scenes[] MUST have EXACTLY {n} elements (n=1..{n}), one per segment, "
            "IN ORDER. Count before you output.\n"
            "- Each scene 'vo' MUST be the segment's transcript text VERBATIM — do "
            "NOT paraphrase, shorten or rewrite the words. The audio is fixed.\n"
            "- Each scene 'prompt' is a self-contained image-generation prompt that "
            "illustrates that line: subject, action, framing, mood. Make consecutive "
            "frames DIFFERENT visual moments (close-up -> wide -> reaction -> cutaway "
            "-> detail) so it reads like a fast, snappy cut — never repeat a shot.\n"
            '- "shot_relation": "continue" only when this line is the SAME continuous '
            'moment as the previous (a micro-cut: push-in / reverse / reaction); '
            'otherwise "cut". The first scene is always "cut".\n'
            f"{style_prefix}"
            f"{dyn}"
            "- AUTO-CAST: add to characters[] only the recurring people/mascots the "
            "visuals actually need (0 for pure object/scenery audio, 1 lead for most, "
            "2-3 max). Every named recurring character MUST have a rich sheet_prompt "
            "describing their canonical look in the target art style. Name them in the "
            "scene prompts so their sheet can be auto-attached.\n"
            "- STYLE BAN: never write doodle, whiteboard, scribble, pencil-sketch.\n"
        )
        bits = [f"Transcript segments ({n} total) — write one visual scene each:\n{seg_lines}"]
        if style_notes.strip():
            bits.append(f"ART STYLE to match (from the sample video): {style_notes.strip()}")
        if master_prompt.strip():
            bits.append(f"WORLD BIBLE: {master_prompt.strip()[:300]}")
        bits.append(f"Write exactly {n} scenes, vo verbatim from each segment. JSON only.")
        tok = max(3000, n * 170)
        return self.chat_text("\n\n".join(bits), system=system, max_tokens=tok)

    def seo(self, title, description, n=5):
        system = ('YouTube SEO assistant. Return STRICT JSON ONLY: '
                  '{"titles":[str,...],"description":str,"tags":[str,...]}. '
                  "Titles are click-worthy but honest; description is 2-3 short "
                  "paragraphs that open with a hook and include a soft CTA; tags are "
                  "lowercase keywords.")
        return self.chat_text(
            f"Topic: {title}\nDetails: {description}\nGive {n} title options, a "
            "description, and ~15 tags. JSON only.", system=system, max_tokens=2000)

    def gaps(self, channel_title, titles, n=10):
        system = ("You find content gaps for a YouTube channel. Given its recent "
                  "video titles, propose topics it has NOT covered but its audience "
                  'would love. Return STRICT JSON ONLY: {"gaps":[{"title":str,"why":str}]}.')
        body = "\n".join("- " + t for t in titles[:60] if t)
        return self.chat_text(
            f"Channel: {channel_title}\nRecent titles:\n{body}\n\nPropose {n} fresh "
            "gap topics. JSON only.", system=system, max_tokens=2500)

    def trend_angles(self, niche, sample_titles, n=10):
        system = ("You distill winning content angles in a niche from example popular "
                  'video titles. Return STRICT JSON ONLY: {"angles":[{"title":str,"why":str}]}.')
        body = "\n".join("- " + t for t in sample_titles[:50] if t)
        return self.chat_text(
            f"Niche: {niche}\nPopular titles right now:\n{body}\n\nDistill {n} strong "
            "angles to make videos about. JSON only.", system=system, max_tokens=2500)

    def analyse_scene(self, image: bytes, question: str = ""):
        instr = question.strip() or (
            "Describe this scene as a cinematographer would: subject, framing, "
            "lighting, palette, mood, lens, and what's happening. Be concise (4-6 "
            "lines)."
        )
        return self.vision_describe([image], instr,
                                    system="You are an expert cinematographer.",
                                    max_tokens=1200)

    # Anthropic caps a single request at ~20 images. To let the editor "see"
    # an arbitrarily long sequence (the user had 59 frames but only the first
    # 20 were ever sent), we send the frames in batches of this size and merge
    # the per-batch decisions into one edit decision list.
    _FRAME_BATCH = 18

    def plan_edit(self, frames: List[bytes], audio_duration: float,
                  user_brief: str = "", master_prompt: str = "",
                  vo_lines: list = None):
        """Look at EVERY frame (chunked) + know the audio length, produce an EDL.

        Returns a dict:
          { "total_duration": float,
            "transition": "cut"|"fade"|"crossfade",
            "shots": [ { "index": int (1-based, GLOBAL into the full list),
                         "duration": float (seconds), "note": str } ],
            "rationale": str }
        """
        n = len(frames)
        if n == 0:
            raise RuntimeError("plan_edit needs at least one frame")

        # Per-batch the model only knows its slice of the audio; give each batch
        # a proportional share of the total so the merged durations are sane.
        batches = [(i, frames[i:i + self._FRAME_BATCH])
                   for i in range(0, n, self._FRAME_BATCH)]
        all_shots = []
        transition = None
        rationales = []
        vo_lines = vo_lines or []
        # Per-frame VO line length (word count) drives proportional time: a
        # batch covering longer narration should fill a bigger slice of audio,
        # and within a batch each shot's hold is tied to its line length.
        def _line_len(i):
            line = (vo_lines[i] or "").strip() if i < len(vo_lines) else ""
            return len(line.split())
        vo_weights = [_line_len(i) for i in range(n)]
        total_vo = sum(vo_weights)
        for offset, batch in batches:
            # Weight this batch's audio share by the VO words spoken over it when
            # we have narration; otherwise fall back to an even split by frames.
            batch_vo = sum(vo_weights[offset:offset + len(batch)])
            if total_vo > 0:
                share = audio_duration * (batch_vo / total_vo) if batch_vo else \
                    audio_duration * (len(batch) / n)
            else:
                share = audio_duration * (len(batch) / n) if n else audio_duration
            lo, hi = offset + 1, offset + len(batch)
            # Give Claude the narration spoken over this batch so the cut can
            # match imagery to WHAT IS BEING SAID, not just fill the runtime.
            narration = "\n".join(
                f'  frame #{offset + j + 1}: "{(vo_lines[offset + j] or "").strip()}"'
                for j in range(len(batch))
                if offset + j < len(vo_lines) and (vo_lines[offset + j] or "").strip())
            data = self._plan_edit_batch(
                batch, share, lo, hi, n, user_brief, master_prompt,
                narration=narration)
            transition = transition or data.get("transition")
            if data.get("rationale"):
                rationales.append(str(data["rationale"]))
            for sh in (data.get("shots") or []):
                try:
                    local = int(sh.get("index", 0))
                except Exception:
                    continue
                # Model is told to use GLOBAL indices (lo..hi); accept those, but
                # tolerate a model that slipped into 1..len(batch) local form.
                if lo <= local <= hi:
                    g = local
                elif 1 <= local <= len(batch):
                    g = offset + local
                else:
                    continue
                all_shots.append({
                    "index": g,
                    "duration": max(0.2, float(sh.get("duration") or 1.0)),
                    "note": sh.get("note", ""),
                })

        if not all_shots:
            raise RuntimeError("Claude returned no usable shots across batches")

        # Normalise durations so the whole cut equals the audio length exactly.
        self._normalise_durations(all_shots, audio_duration)
        return {
            "total_duration": round(audio_duration, 2),
            "transition": transition or "cut",
            "shots": all_shots,
            "rationale": " ".join(rationales)[:1500],
            "frames_seen": n,
            "batches": len(batches),
        }

    def _plan_edit_batch(self, frames, share_duration, lo, hi, total,
                         user_brief, master_prompt, narration=""):
        system = (
            "You are a film editor assembling ONE continuous cut. You receive a "
            "BATCH of generated frames from a longer sequence, plus the slice of "
            "audio time this batch should fill. Decide how long each frame is on "
            "screen, in what order, and the transition style. Return STRICT JSON "
            "ONLY (no markdown):\n"
            '{ "transition": "cut"|"fade"|"crossfade", '
            '"shots": [{"index": int, "duration": float, "note": str}], '
            '"rationale": str }\n'
            "Rules: index is the GLOBAL 1-based frame number shown below — use "
            "those numbers exactly. Output EXACTLY ONE shot per frame in this "
            "batch (shots count MUST equal the number of frames; do not omit or "
            "repeat frames). The sum of all durations MUST equal the batch audio "
            "share given below. Tie each shot's duration to how long its matched "
            "narration line takes to speak — a longer line gets a longer hold, a "
            "short line a shorter one. Keep shots between "
            "0.4 and 8 seconds unless the brief says otherwise."
        )
        instr = [
            f"This batch contains frames {lo}..{hi} of {total} total "
            f"(use ONLY indices {lo}..{hi}).",
            f"Return EXACTLY {hi - lo + 1} shots — one per frame.",
            f"The durations MUST sum to exactly {share_duration:.2f} seconds "
            f"(the audio share for this batch).",
        ]
        if (narration or "").strip():
            instr.append(
                "NARRATION SPOKEN OVER THESE FRAMES (match each image to the "
                "words being said — hold a frame while its line is spoken, cut "
                "on sentence beats):\n" + narration.strip())
        if user_brief.strip():
            instr.append(f"USER EDIT DIRECTION:\n{user_brief.strip()}")
        if master_prompt.strip():
            instr.append(f"WORLD / STYLE BIBLE:\n{master_prompt.strip()}")
        instr.append("Now design this batch. JSON only.")
        raw = self.vision_describe(frames, "\n\n".join(instr), system=system,
                                   max_tokens=3000)
        try:
            return extract_json(raw)
        except Exception:
            return {"shots": []}

    def plan_edit_within_budget(self, frames: List[bytes], scene_durations: List[float],
                                user_brief: str = "", master_prompt: str = ""):
        """Scene-locked edit: each frame's duration is FIXED to its measured
        narration length. Claude may only REORDER or DROP scenes for better flow
        — never change a duration. Chunk-aware (global 1-based indices).

        Returns a dict:
          { "transition": ..., "order": [int, ...]   # global indices, in play order
            "notes": {index: str}, "rationale": str }
        """
        n = len(frames)
        if n == 0:
            raise RuntimeError("plan_edit_within_budget needs at least one frame")

        batches = [(i, frames[i:i + self._FRAME_BATCH])
                   for i in range(0, n, self._FRAME_BATCH)]
        # When everything fits one batch, ask for a single global ordering.
        keep_per_batch = []
        transition = None
        notes = {}
        rationales = []
        for offset, batch in batches:
            lo, hi = offset + 1, offset + len(batch)
            durs = {offset + j + 1: round(scene_durations[offset + j], 2)
                    for j in range(len(batch))
                    if offset + j < len(scene_durations)}
            data = self._plan_synced_batch(batch, durs, lo, hi, n,
                                            user_brief, master_prompt)
            transition = transition or data.get("transition")
            if data.get("rationale"):
                rationales.append(str(data["rationale"]))
            order = data.get("order")
            if not isinstance(order, list):
                order = sorted(durs.keys())   # fallback: keep this batch in order
            clean = []
            for x in order:
                try:
                    x = int(x)
                except Exception:
                    continue
                if lo <= x <= hi and x not in clean:
                    clean.append(x)
            keep_per_batch.append(clean)
            for k, v in (data.get("notes") or {}).items():
                try:
                    notes[int(k)] = str(v)
                except Exception:
                    pass

        # Concatenate batch orderings in batch order (each batch already in the
        # order Claude chose; cross-batch ordering stays sequential to keep the
        # story coherent across chunks).
        final_order = [x for batch_order in keep_per_batch for x in batch_order]
        if not final_order:
            final_order = list(range(1, n + 1))
        return {
            "transition": transition or "cut",
            "order": final_order,
            "notes": notes,
            "rationale": " ".join(rationales)[:1500],
            "frames_seen": n,
            "batches": len(batches),
        }

    def _plan_synced_batch(self, frames, durs, lo, hi, total, user_brief, master_prompt):
        dur_lines = ", ".join(f"#{k}={v}s" for k, v in durs.items())
        system = (
            "You are a film editor. Each frame has a FIXED on-screen duration "
            "(equal to the narration spoken over it) — you must NOT change any "
            "duration. Your only freedom is to choose the ORDER frames play and "
            "to DROP frames that don't help. Return STRICT JSON ONLY:\n"
            '{ "transition": "cut"|"fade"|"crossfade", '
            '"order": [int, ...], '            # global indices, the play order
            '"notes": {"<index>": str}, "rationale": str }\n'
            "Rules: 'order' is GLOBAL 1-based frame numbers from the list below, "
            "in the sequence they should play. Include each kept frame once; omit "
            "frames you drop. Do not invent indices."
        )
        instr = [
            f"This batch is frames {lo}..{hi} of {total} (use ONLY these indices).",
            f"Fixed durations (do not change): {dur_lines}",
        ]
        if user_brief.strip():
            instr.append(f"USER EDIT DIRECTION:\n{user_brief.strip()}")
        if master_prompt.strip():
            instr.append(f"WORLD / STYLE BIBLE:\n{master_prompt.strip()}")
        instr.append("Choose the order (and any drops). JSON only.")
        raw = self.vision_describe(frames, "\n\n".join(instr), system=system,
                                   max_tokens=2000)
        try:
            return extract_json(raw)
        except Exception:
            return {}

    @staticmethod
    def _normalise_durations(shots, target_total):
        """Scale shot durations so they sum to target_total (clamped sensibly).

        Two passes: the 0.2s floor on very short shots can push the total past
        the target, which would drift the cut out of sync with the audio — the
        second pass redistributes that overshoot across the unclamped shots.
        """
        if target_total <= 0:
            return
        for _ in range(2):
            cur = sum(max(0.0, s["duration"]) for s in shots)
            if cur <= 0:
                return
            k = target_total / cur
            for s in shots:
                s["duration"] = round(max(0.2, s["duration"] * k), 3)
            if abs(sum(s["duration"] for s in shots) - target_total) < 0.05:
                break
        # Absorb any residual rounding drift in the final shot.
        drift = round(target_total - sum(s["duration"] for s in shots), 3)
        if shots and abs(drift) >= 0.01:
            shots[-1]["duration"] = round(max(0.2, shots[-1]["duration"] + drift), 3)


class OpenAIClient(ClaudeClient):
    """Drop-in replacement for ClaudeClient that routes through OpenAI's API.
    Inherits all high-level methods (generate_script, vision_describe, etc.)."""

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        self.api_key = api_key or config.OPENAI_API_KEY
        self.base_url = (base_url or config.OPENAI_BASE_URL).rstrip("/")
        self.model = model or config.OPENAI_MODEL
        self._sdk = None

    def _require_key(self):
        if not self.api_key:
            raise RuntimeError(
                "No OpenAI API key set. Paste your key in Settings → OpenAI."
            )

    def ping(self):
        if not self.api_key:
            return {"ok": False, "error": "no api key set"}
        ids = self._list_models_best_effort()
        try:
            r = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model, "max_completion_tokens": 100,
                      "messages": [{"role": "user", "content": "Say ok"}]},
                timeout=20,
            )
            if r.status_code == 401:
                return {"ok": False, "error": f"auth rejected — {r.text[:300]}",
                        "models": ids, "base_url": self.base_url}
            if r.status_code == 400 and "max_tokens" in (r.text or "") and "reached" in (r.text or ""):
                pass  # model processed request but hit token limit — connection works
            elif r.status_code >= 400:
                body = r.text[:400] if r.text else str(r.status_code)
                return {"ok": False, "error": f"API {r.status_code}: {body}",
                        "models": ids, "base_url": self.base_url}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "base_url": self.base_url}
        return {"ok": True, "models": ids, "configured_model": self.model,
                "base_url": self.base_url}

    def _list_models_best_effort(self):
        try:
            r = requests.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=20,
            )
            if r.status_code < 400:
                data = r.json()
                return [m.get("id") for m in (data.get("data") or [])][:30]
        except Exception:
            pass
        return []

    @staticmethod
    def _to_openai_blocks(blocks):
        out = []
        for b in blocks:
            if b.get("type") == "text":
                out.append({"type": "text", "text": b["text"]})
            elif b.get("type") == "image":
                src = b.get("source", {})
                mt = src.get("media_type", "image/png")
                d = src.get("data", "")
                out.append({"type": "image_url",
                            "image_url": {"url": f"data:{mt};base64,{d}"}})
            else:
                out.append(b)
        return out

    def _msg(self, content_blocks, system=None, max_tokens=4096, stream=False):
        self._require_key()
        oai_content = self._to_openai_blocks(content_blocks)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": oai_content})
        payload = {"model": self.model, "max_completion_tokens": max_tokens,
                   "messages": messages}
        _log(f"openai chat/completions model={self.model} max_tokens={max_tokens} "
             f"blocks={len(content_blocks)} base={self.base_url}")

        def _backoff(attempt):
            raw = CLAUDE_BACKOFF_BASE_MS * (2 ** (attempt - 1)) + \
                random.uniform(0, CLAUDE_BACKOFF_BASE_MS)
            return min(raw, CLAUDE_BACKOFF_MAX_MS) / 1000.0

        attempt = 0
        while True:
            try:
                with _CLAUDE_SEM:
                    r = requests.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}",
                                 "Content-Type": "application/json"},
                        json=payload, timeout=600,
                    )
                if r.status_code == 401:
                    raise RuntimeError(
                        f"OpenAI API rejected the key — {r.text[:300]}")
                if r.status_code == 429:
                    attempt += 1
                    if attempt > CLAUDE_MAX_RETRIES:
                        raise RuntimeError(
                            f"OpenAI rate limit — gave up after {attempt-1} retries")
                    ra = r.headers.get("retry-after")
                    wait = (float(ra) if ra else 5.0) + random.uniform(0, 1.0)
                    _log(f"429 rate limit (attempt {attempt}) — waiting {wait:.1f}s")
                    time.sleep(wait)
                    continue
                if 500 <= r.status_code < 600:
                    attempt += 1
                    if attempt > CLAUDE_MAX_RETRIES:
                        raise RuntimeError(
                            f"OpenAI server error {r.status_code} — gave up after "
                            f"{attempt-1} retries")
                    wait = _backoff(attempt)
                    _log(f"server {r.status_code} (attempt {attempt}) — backing off "
                         f"{wait:.1f}s")
                    time.sleep(wait)
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(
                        f"OpenAI API error {r.status_code} — {r.text[:500]}")
                data = r.json()
                break
            except RuntimeError:
                raise
            except requests.exceptions.ConnectionError as e:
                attempt += 1
                if attempt > CLAUDE_MAX_RETRIES:
                    raise RuntimeError(f"could not reach OpenAI API — {e}")
                wait = _backoff(attempt)
                _log(f"connection error (attempt {attempt}) — backing off {wait:.1f}s")
                time.sleep(wait)
            except requests.exceptions.Timeout as e:
                attempt += 1
                if attempt > CLAUDE_MAX_RETRIES:
                    raise RuntimeError(f"OpenAI API timeout — {e}")
                wait = _backoff(attempt)
                _log(f"timeout (attempt {attempt}) — backing off {wait:.1f}s")
                time.sleep(wait)

        text = ((data.get("choices") or [{}])[0].get("message", {}).get(
            "content") or "").strip()
        finish = (data.get("choices") or [{}])[0].get("finish_reason", "")
        if finish == "length":
            _log(f"WARNING finish_reason=length (output hit the {max_tokens} "
                 f"cap; response likely truncated)")
        if not text:
            raise RuntimeError(
                "empty response from model (no content; "
                f"finish_reason={finish or 'unknown'})")
        return text


def _repair_json(t: str):
    """Try to fix truncated JSON from long generations (e.g. 600s scripts).
    Works by: closing open strings, removing the last incomplete element,
    then closing all open brackets/braces."""
    # Close any open string literal
    in_str, escaped = False, False
    for ch in t:
        if escaped:
            escaped = False
            continue
        if ch == '\\':
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
    if in_str:
        t += '"'

    # Remove trailing incomplete key-value pairs or array elements.
    # A truncated entry often looks like: ,"prompt": "some text..."
    # or { "n": 45, "heading": "Some... (cut off mid-object)
    # Strategy: find the last complete object/array element by finding
    # the last }, then trim everything after it except closing brackets.
    # First strip trailing commas and whitespace
    t = t.rstrip()
    t = re.sub(r',\s*$', '', t)

    # Count unclosed brackets
    depth_brace = 0
    depth_sq = 0
    for ch in t:
        if ch == '{': depth_brace += 1
        elif ch == '}': depth_brace -= 1
        elif ch == '[': depth_sq += 1
        elif ch == ']': depth_sq -= 1

    if depth_brace > 0 or depth_sq > 0:
        # Find the last successfully closed brace/bracket and trim after it,
        # then re-close remaining. This drops the incomplete trailing element.
        last_close = max(t.rfind('}'), t.rfind(']'))
        if last_close > len(t) // 2:
            t = t[:last_close + 1]
            t = re.sub(r',\s*$', '', t.rstrip())
            # Recount
            depth_brace = sum(1 for c in t if c == '{') - sum(1 for c in t if c == '}')
            depth_sq = sum(1 for c in t if c == '[') - sum(1 for c in t if c == ']')

    t += ']' * max(0, depth_sq)
    t += '}' * max(0, depth_brace)
    # Final cleanup of trailing commas before closers
    t = re.sub(r',\s*([}\]])', r'\1', t)
    return t


def extract_json(text: str):
    """Pull the first JSON value out of a possibly-fenced Claude response.

    Handles the common failure modes seen with long generations:
      • markdown ```json fences and leading prose
      • a top-level ARRAY ([{...},{...}]) — not just an object
      • trailing "Extra data" after a complete value (model kept talking)
      • truncated JSON (closes open strings/brackets via _repair_json)
    """
    if not text:
        raise ValueError("empty response")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass

    # Anchor at the EARLIEST of '{' or '[' — a response may be a bare array,
    # in which case jumping to the first '{' would land inside it and produce
    # an "Extra data" error on the following comma.
    br = t.find('{')
    sq = t.find('[')
    cands = [i for i in (br, sq) if i >= 0]
    if not cands:
        raise ValueError(f"no JSON found in response: {text[:300]}")
    start = min(cands)
    raw = t[start:]

    # raw_decode parses ONE complete JSON value and tells us where it ended,
    # so trailing prose / extra values after it no longer break parsing.
    try:
        obj, _end = json.JSONDecoder().raw_decode(raw)
        return obj
    except json.JSONDecodeError:
        pass

    # Last resort: assume truncation and repair (close strings/brackets).
    repaired = _repair_json(raw)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass
    # Repaired text may itself still have trailing junk — try raw_decode on it.
    try:
        obj, _end = json.JSONDecoder().raw_decode(repaired)
        return obj
    except json.JSONDecodeError as e:
        raise ValueError(
            f"JSON repair failed ({e}); first 500 chars: {raw[:500]}"
        ) from e
