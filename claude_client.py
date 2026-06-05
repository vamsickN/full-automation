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
        self._sdk = anthropic.Anthropic(
            api_key=self.api_key or "unset",
            base_url=self.base_url,
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
    def _msg(self, content_blocks, system: Optional[str] = None, max_tokens: int = 4096,
             stream: bool = False):
        self._require_key()
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
        attempt = 0
        while True:
            try:
                with _CLAUDE_SEM:
                    if stream:
                        try:
                            with self._sdk.messages.stream(**kwargs) as s:
                                resp = s.get_final_message()
                        except (AssertionError, AttributeError):
                            # Proxy gave us a non-standard stream — retry without it.
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
                    raise RuntimeError(
                        f"Claude API rate limit — gave up after {attempt-1} "
                        f"retries: {self._format_err(e)}")
                wait = _retry_after_seconds(e, default=5.0) + random.uniform(0, 1.0)
                _log(f"429 rate_limit (attempt {attempt}) — waiting {wait:.1f}s "
                     f"then retrying")
                time.sleep(wait)
            except (APIConnectionError, APITimeoutError) as e:
                attempt += 1
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
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                out.append(block.text)
        text = "\n".join(out).strip()
        # If the model ran out of room, the JSON is truncated → parsing fails
        # downstream with a confusing error. Surface the real cause instead.
        if getattr(resp, "stop_reason", None) == "max_tokens":
            _log(f"WARNING stop_reason=max_tokens (output hit the {max_tokens} "
                 f"cap; response likely truncated)")
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
                        dialogue: bool = False):
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

        scene_count = max(1, min(120, round((total_duration or 1) /
                                            max(0.1, pacing_seconds or 1))))

        system = (
            "You are a film director, narrator and screenwriter for a "
            "continuity-driven visual series produced as a sequence of still "
            "images shown in time with a voice-over. Return STRICT JSON ONLY "
            "(no prose, no markdown fences) shaped EXACTLY as:\n"
            "{\n"
            '  "title": str,\n'
            '  "logline": str,\n'
            '  "voiceover": str,            // the full narration script, plain text\n'
            '  "pacing_seconds": number,    // seconds each image is on screen\n'
            '  "total_duration": number,    // seconds (approx VO read length)\n'
            '  "scene_count": int,\n'
            '  "characters": [ { "name": str, "sheet_prompt": str } ],\n'
            '  "scenes": [ { "n": int, "heading": str, "action": str, "vo": str, "prompt": str } ]\n'
            "}\n"
            "RULES:\n"
            f"- Produce EXACTLY {scene_count} scenes (one image per scene). The "
            f"narration must read aloud in about {total_duration:.0f} seconds at a "
            "natural pace.\n"
            '- Each scene "vo" is the slice of narration spoken while that image is '
            'on screen; concatenated in order they equal the full "voiceover".\n'
            '- Each scene "prompt" is a self-contained image-generation prompt '
            "(subject, framing, lighting, mood, palette). Name any recurring "
            "character by their exact short name so a reference sheet can be "
            "auto-attached.\n"
            '- Each character "sheet_prompt" is ONE rich paragraph describing that '
            "character's canonical look (face, hair, build, outfit, colors, vibe) — "
            "written so it can be sent straight to a character-sheet generator. Use "
            "the exact same names as in the scene prompts."
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
        bits.append(f"Pacing: {pacing_seconds:g} second(s) per image "
                    f"=> EXACTLY {scene_count} scenes/images")
        if num_characters and num_characters > 0:
            bits.append(f"Number of distinct recurring characters to define: "
                        f"{num_characters}")
        else:
            bits.append("Define however many recurring characters the story needs.")
        if style_notes.strip():
            bits.append(f"STYLE NOTES:\n{style_notes.strip()}")
        if master_prompt.strip():
            bits.append(f"WORLD / STYLE BIBLE:\n{master_prompt.strip()}")
        # The script is the one long generation (up to 16k tokens) that can run
        # long enough to hit the proxy's edge timeout, so stream it — with an
        # automatic non-streamed fallback if the proxy's stream is malformed.
        return self.chat_text("\n\n".join(bits), system=system, max_tokens=16000,
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
            '     "image_prompt_style": str,       // the look every frame shares\n'
            '     "pacing_seconds": number,        // seconds each image is on screen\n'
            '     "total_duration": number,        // seconds\n'
            '     "scene_count": int               // = round(total_duration/pacing)\n'
            "  } ]\n"
            "}\n"
            f"RULES:\n- Produce EXACTLY {n_suggestions} suggestions, varied but all "
            "true to the reference's style and audience.\n"
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
            "consistent (scene_count ≈ total_duration / pacing_seconds)."
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
            '     "num_characters": int,          // 0-8 recurring characters\n'
            '     "characters": [ {"name": str, "sheet_prompt": str} ],\n'
            '     "voiceover_style": str,          // how this VO should sound\n'
            '     "image_prompt_style": str,       // the look every frame shares\n'
            '     "pacing_seconds": number,        // seconds each image is on screen\n'
            '     "total_duration": number,        // seconds\n'
            '     "scene_count": int               // = round(total_duration/pacing)\n'
            "  } ]\n"
            "}\n"
            f"RULES:\n- Produce EXACTLY {n_suggestions} suggestions, varied but all "
            "true to the references' combined style and audience.\n"
            "- characters[].sheet_prompt is ONE rich paragraph of that character's "
            "canonical look, ready for a character-sheet generator. If num_characters "
            "is 0, characters is an empty list.\n"
            "- Keep pacing_seconds, total_duration and scene_count internally "
            "consistent (scene_count ≈ total_duration / pacing_seconds)."
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

    # ----- Wave 5: YT growth helpers -----
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
                  user_brief: str = "", master_prompt: str = ""):
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
        for offset, batch in batches:
            share = audio_duration * (len(batch) / n) if n else audio_duration
            lo, hi = offset + 1, offset + len(batch)
            data = self._plan_edit_batch(
                batch, share, lo, hi, n, user_brief, master_prompt)
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
                         user_brief, master_prompt):
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
            "those numbers exactly. You may repeat or omit frames. The sum of "
            "durations should be about the batch audio share. Keep shots between "
            "0.4 and 8 seconds unless the brief says otherwise."
        )
        instr = [
            f"This batch contains frames {lo}..{hi} of {total} total "
            f"(use ONLY indices {lo}..{hi}).",
            f"Fill about {share_duration:.2f} seconds of audio with this batch.",
        ]
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
        """Scale shot durations so they sum to target_total (clamped sensibly)."""
        cur = sum(max(0.0, s["duration"]) for s in shots)
        if cur <= 0 or target_total <= 0:
            return
        k = target_total / cur
        for s in shots:
            s["duration"] = round(max(0.2, s["duration"] * k), 3)


def extract_json(text: str):
    """Pull the first JSON object out of a possibly-fenced Claude response."""
    if not text:
        raise ValueError("empty response")
    # Strip code fences if present.
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    # Try direct parse first.
    try:
        return json.loads(t)
    except Exception:
        pass
    # Fall back: greedy find of {...}.
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        raise ValueError(f"no JSON found in response: {text[:300]}")
    return json.loads(m.group(0))
