"""ElevenLabs text-to-speech client.

Turns the Claude-written voice-over script into real spoken audio (MP3 bytes).
Used by the Edit tab to either (a) synthesize the whole voice-over into one
track that drops straight into the existing `audio` slot, or (b) synthesize one
clip per numbered scene so each image can be held for exactly its own narration.

Keys are resolved per-user from the request settings (see app.py); this client
just takes whatever key/voice/model it is handed.
"""
import re
import time

import requests

import config


# Status codes worth retrying: transient upstream failures + rate limiting.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _post_with_retry(url, *, headers, json=None, timeout=180,
                     max_retries=2, backoff=1.5, what="request"):
    """POST with clear network errors and automatic retry on transient 5xx/429.

    Raises ``RuntimeError`` with a human-readable message on timeout, connection
    failure, or after exhausting retries. Returns the ``requests.Response`` on
    the first non-retryable outcome (the caller still checks ``status_code`` for
    4xx handling). ``what`` is a short label used in error messages.
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(url, headers=headers, json=json, timeout=timeout)
        except requests.exceptions.Timeout:
            raise RuntimeError(
                f"{what} timed out after {timeout}s — the TTS service did not "
                f"respond. Try a shorter script or raise the timeout."
            )
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"{what} could not reach the TTS service ({e}). Check the base "
                f"URL and your network connection."
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"{what} failed: {e}")
        if r.status_code in _RETRY_STATUS and attempt < max_retries:
            last_exc = f"[{r.status_code}] {r.text[:200]}"
            time.sleep(backoff * (2 ** attempt))
            continue
        return r
    # Exhausted retries on a transient status.
    raise RuntimeError(f"{what} failed after {max_retries + 1} attempts: {last_exc}")


class VoiceClient:
    def __init__(self, api_key=None, base_url=None, model=None, voice_id=None,
                 timeout=180):
        self.api_key = api_key or config.ELEVENLABS_API_KEY
        self.base_url = (base_url or config.ELEVENLABS_BASE_URL).rstrip("/")
        self.model = model or config.ELEVENLABS_MODEL
        self.voice_id = voice_id or config.ELEVENLABS_VOICE_ID
        self.timeout = timeout

    def _require_key(self):
        if not self.api_key:
            raise RuntimeError(
                "No ElevenLabs API key set. Add ELEVENLABS_API_KEY to your .env "
                "or paste a key in the Settings panel."
            )

    def ping(self):
        """Lightweight auth check used by the Settings 'Test connection' panel."""
        if not self.api_key:
            return {"ok": False, "detail": "no key"}
        try:
            r = requests.get(f"{self.base_url}/voices",
                             headers={"xi-api-key": self.api_key}, timeout=20)
            if r.status_code >= 400:
                return {"ok": False, "detail": f"[{r.status_code}] {r.text[:160]}"}
            n = len(r.json().get("voices", []))
            return {"ok": True, "detail": f"{n} voices available"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def list_voices(self):
        """Return [{id, name, category}] for the account's available voices."""
        self._require_key()
        r = requests.get(f"{self.base_url}/voices",
                         headers={"xi-api-key": self.api_key}, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"could not list voices [{r.status_code}]: {r.text[:300]}")
        try:
            payload = r.json()
        except Exception as e:
            raise RuntimeError(f"voices endpoint returned non-JSON: {e}")
        out = []
        for v in (payload.get("voices") or []):
            out.append({
                "id": v.get("voice_id"),
                "name": v.get("name") or v.get("voice_id"),
                "category": v.get("category", ""),
            })
        return out

    def generate_sfx(self, text, duration_seconds=None, prompt_influence=0.4):
        """Text -> sound effect via ElevenLabs Sound Generation. Returns MP3
        bytes. ``text`` is a short description ("deep cinematic boom", "lava
        crackle", "fast whoosh transition"). ``duration_seconds`` 0.5-22 (None =
        let ElevenLabs choose). Raises on failure."""
        self._require_key()
        desc = (text or "").strip()
        if not desc:
            raise RuntimeError("generate_sfx needs a description")
        body = {"text": desc,
                "prompt_influence": max(0.0, min(1.0, float(prompt_influence)))}
        if duration_seconds:
            body["duration_seconds"] = max(0.5, min(22.0, float(duration_seconds)))
        r = _post_with_retry(
            f"{self.base_url}/sound-generation",
            headers={"xi-api-key": self.api_key,
                     "Content-Type": "application/json",
                     "Accept": "audio/mpeg"},
            json=body, timeout=self.timeout, what="SFX generation",
        )
        if r.status_code >= 400:
            raise RuntimeError(f"SFX gen failed [{r.status_code}]: {r.text[:400]}")
        if not r.content:
            raise RuntimeError("SFX gen returned empty audio")
        return r.content

    @staticmethod
    def _chunk_text(text, limit=4500):
        """Split text into chunks under ``limit`` chars at sentence boundaries."""
        if len(text) <= limit:
            return [text]
        sentences = re.split(r'(?<=[.!?])\s+', text)
        chunks, current = [], ""
        for s in sentences:
            if len(current) + len(s) + 1 > limit and current:
                chunks.append(current.strip())
                current = s
            else:
                current = f"{current} {s}" if current else s
        if current.strip():
            chunks.append(current.strip())
        return chunks or [text[:limit]]

    def _synthesize_one(self, text, voice_id, model,
                        stability, similarity_boost, style):
        """Synthesize a single chunk (must be under ElevenLabs char limit)."""
        url = f"{self.base_url}/text-to-speech/{voice_id}"
        body = {
            "text": text,
            "model_id": model,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost,
                "style": style,
                "use_speaker_boost": True,
            },
        }
        r = _post_with_retry(
            url,
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=body,
            timeout=self.timeout,
            what="TTS",
        )
        if r.status_code >= 400:
            raise RuntimeError(f"TTS failed [{r.status_code}]: {r.text[:400]}")
        if not r.content:
            raise RuntimeError("TTS returned empty audio")
        return r.content

    def synthesize(self, text, voice_id=None, model=None,
                   stability=0.5, similarity_boost=0.75, style=0.0):
        """Text -> spoken audio. Returns MP3 bytes. Auto-chunks long text."""
        self._require_key()
        voice_id = voice_id or self.voice_id
        model = model or self.model
        speak = (text or "").strip() or " "

        chunks = self._chunk_text(speak)
        if len(chunks) == 1:
            return self._synthesize_one(
                chunks[0], voice_id, model, stability, similarity_boost, style)

        parts = []
        for i, chunk in enumerate(chunks):
            mp3 = self._synthesize_one(
                chunk, voice_id, model, stability, similarity_boost, style)
            parts.append(mp3)
        return b"".join(parts)

    def _synthesize_one_with_timestamps(self, text, voice_id, model,
                                         stability, similarity_boost, style):
        """Synthesize ONE chunk (must be under the char limit) via the
        with-timestamps endpoint. Returns (mp3_bytes, alignment_dict) or
        (mp3_bytes, None) if the endpoint/plan can't return timing."""
        import base64
        url = f"{self.base_url}/text-to-speech/{voice_id}/with-timestamps"
        body = {
            "text": text,
            "model_id": model,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost,
                "style": style,
                "use_speaker_boost": True,
            },
        }
        r = _post_with_retry(
            url,
            headers={"xi-api-key": self.api_key,
                     "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout,
            what="TTS (timestamps)",
        )
        if r.status_code >= 400:
            mp3 = self._synthesize_one(
                text, voice_id, model, stability, similarity_boost, style)
            return mp3, None
        try:
            data = r.json()
            audio_b64 = data.get("audio_base64") or ""
            mp3 = base64.b64decode(audio_b64) if audio_b64 else b""
            if not mp3:
                mp3 = self._synthesize_one(
                    text, voice_id, model, stability, similarity_boost, style)
                return mp3, None
            alignment = data.get("normalized_alignment") or data.get("alignment")
            return mp3, alignment
        except Exception:
            mp3 = self._synthesize_one(
                text, voice_id, model, stability, similarity_boost, style)
            return mp3, None

    def synthesize_with_timestamps(self, text, voice_id=None, model=None,
                                    stability=0.5, similarity_boost=0.75,
                                    style=0.0):
        """Text -> (MP3 bytes, alignment_dict).

        Uses the ElevenLabs ``with-timestamps`` endpoint which returns
        character-level timing alongside the audio.  ``alignment_dict`` has:
          characters                   list[str]
          character_start_times_seconds list[float]
          character_end_times_seconds   list[float]

        LONG TEXT (> ~4500 chars) is split at sentence boundaries, each chunk is
        synthesized with timestamps, and the per-chunk alignments are STITCHED
        with a cumulative time offset so the whole narration keeps exact
        character-level timing — long videos get the same frame-accurate A/V
        sync as short ones.  Degrades to ``(synthesize(text), None)`` only when
        the endpoint/plan can't return timing at all.
        """
        self._require_key()
        voice_id = voice_id or self.voice_id
        model = model or self.model
        speak = (text or "").strip() or " "

        chunks = self._chunk_text(speak, limit=4500)

        # Single chunk: direct call (fast path).
        if len(chunks) == 1:
            return self._synthesize_one_with_timestamps(
                chunks[0], voice_id, model, stability, similarity_boost, style)

        # Multi-chunk: synth each, concat audio, stitch alignments with a
        # running time offset = end of the previous chunk's audio.
        mp3_parts = []
        merged = {
            "characters": [],
            "character_start_times_seconds": [],
            "character_end_times_seconds": [],
        }
        time_offset = 0.0
        any_alignment = False

        for chunk in chunks:
            mp3, align = self._synthesize_one_with_timestamps(
                chunk, voice_id, model, stability, similarity_boost, style)
            mp3_parts.append(mp3)
            if align and align.get("characters"):
                any_alignment = True
                chars = align.get("characters", [])
                starts = align.get("character_start_times_seconds", [])
                ends = align.get("character_end_times_seconds", [])
                if len(starts) == len(chars) and len(ends) == len(chars):
                    merged["characters"].extend(chars)
                    merged["character_start_times_seconds"].extend(
                        s + time_offset for s in starts)
                    merged["character_end_times_seconds"].extend(
                        e + time_offset for e in ends)
                    # next chunk's audio begins where this one ended
                    time_offset += (ends[-1] if ends else 0.0)
                    # whitespace join between chunks (sentence boundary) — keep
                    # the alignment text aligned with the concatenated speech
                    merged["characters"].append(" ")
                    merged["character_start_times_seconds"].append(time_offset)
                    merged["character_end_times_seconds"].append(time_offset)
                    continue
            # Chunk returned no usable alignment — advance the offset by a
            # rough estimate so later chunks don't collapse onto t=0.
            time_offset += max(1.0, len(chunk) / 14.0)  # ~14 chars/sec speech

        full_mp3 = b"".join(p for p in mp3_parts if p)
        if not any_alignment:
            return full_mp3, None
        return full_mp3, merged


def _concat_wav(wav_list):
    """Concatenate several WAV byte-strings (same MiMo format) into one valid
    WAV. Naive byte-join is INVALID for WAV (each file carries its own header),
    so we re-frame them via the stdlib wave module. Falls back to the first clip
    if anything can't be parsed."""
    import io
    import wave
    wav_list = [w for w in wav_list if w]
    if not wav_list:
        return b""
    if len(wav_list) == 1:
        return wav_list[0]
    try:
        out = io.BytesIO()
        writer = None
        try:
            for w in wav_list:
                with wave.open(io.BytesIO(w), "rb") as rd:
                    frames = rd.readframes(rd.getnframes())
                    if writer is None:
                        writer = wave.open(out, "wb")
                        writer.setnchannels(rd.getnchannels())
                        writer.setsampwidth(rd.getsampwidth())
                        writer.setframerate(rd.getframerate())
                    writer.writeframes(frames)
        finally:
            if writer is not None:
                writer.close()
        return out.getvalue()
    except Exception:
        return wav_list[0]


class MimoVoiceClient:
    """Xiaomi MiMo TTS v2.5 — a free/cheap drop-in alternative to ElevenLabs.

    Exposes the SAME surface as ``VoiceClient`` (synthesize /
    synthesize_with_timestamps / list_voices / ping / generate_sfx) so it slots
    straight into ``get_voice_client()``. MiMo returns WAV audio (base64) and
    has no character-level timestamps, so ``synthesize_with_timestamps`` returns
    ``(audio, None)`` and callers fall back to word-count holds.

    API:  POST {base}/chat/completions
          headers: api-key: <key>
          body: {model, messages:[{role:"user", content:<style>},
                                   {role:"assistant", content:<text>}],
                 audio:{format:"wav", voice:<voice>}, stream:false}
          resp: choices[0].message.audio.data  (base64 WAV)
    """

    # Built-in voices for the mimo-v2.5-tts model (from the v2.5 docs).
    BUILTIN_VOICES = [
        {"id": "Chloe", "name": "Chloe (English, female)", "category": "english"},
        {"id": "Mia", "name": "Mia (English, female)", "category": "english"},
        {"id": "Milo", "name": "Milo (English, male)", "category": "english"},
        {"id": "Dean", "name": "Dean (English, male)", "category": "english"},
        {"id": "mimo_default", "name": "MiMo default", "category": "default"},
        {"id": "冰糖", "name": "冰糖 (Chinese)", "category": "chinese"},
        {"id": "茉莉", "name": "茉莉 (Chinese)", "category": "chinese"},
        {"id": "苏打", "name": "苏打 (Chinese)", "category": "chinese"},
        {"id": "白桦", "name": "白桦 (Chinese)", "category": "chinese"},
    ]

    def __init__(self, api_key=None, base_url=None, model=None, voice_id=None,
                 style=None, timeout=180):
        self.api_key = api_key or config.MIMO_API_KEY
        self.base_url = (base_url or config.MIMO_BASE_URL).rstrip("/")
        self.model = model or config.MIMO_MODEL
        self.voice_id = voice_id or config.MIMO_VOICE_ID
        self.style = style or getattr(config, "MIMO_STYLE",
                                      "Clear, natural, engaging narration voice.")
        self.timeout = timeout

    def _require_key(self):
        if not self.api_key:
            raise RuntimeError(
                "No MiMo API key set. Paste a MiMo key in the Settings panel or "
                "set MIMO_API_KEY in your .env."
            )

    def _coerce_voice(self, vid):
        """MiMo only accepts its own voice names. If a caller passes an unknown
        id (e.g. an ElevenLabs voice id), fall back to the configured default or
        Chloe — so a wrong voice id never 400s the whole video."""
        valid = {v["id"] for v in self.BUILTIN_VOICES}
        vid = vid or self.voice_id
        if vid in valid:
            return vid
        return self.voice_id if self.voice_id in valid else "Chloe"

    def ping(self):
        """Auth check for the Settings 'Test connection' panel — synthesizes a
        one-word clip (cheap; MiMo is free for now)."""
        if not self.api_key:
            return {"ok": False, "detail": "no key"}
        try:
            audio = self._synthesize_one("Hi.", self.voice_id)
            return {"ok": bool(audio),
                    "detail": "MiMo TTS reachable" if audio else "no audio returned"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def list_voices(self):
        """MiMo's built-in voices are a fixed set (no per-account listing)."""
        return [dict(v) for v in self.BUILTIN_VOICES]

    def generate_sfx(self, text, duration_seconds=None, prompt_influence=0.4):
        """MiMo has no sound-effect generation. Raising lets the caller fall
        back to its ffmpeg-synthesized click/whoosh path."""
        raise RuntimeError("MiMo does not support sound-effect generation")

    def _synthesize_one(self, text, voice_id):
        self._require_key()
        body = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": self.style},
                {"role": "assistant", "content": text},
            ],
            "audio": {"format": "wav", "voice": self._coerce_voice(voice_id)},
            "stream": False,
        }
        r = _post_with_retry(
            f"{self.base_url}/chat/completions",
            headers={"api-key": self.api_key, "Content-Type": "application/json"},
            json=body, timeout=self.timeout, what="MiMo TTS",
        )
        if r.status_code >= 400:
            raise RuntimeError(f"MiMo TTS failed [{r.status_code}]: {r.text[:400]}")
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"MiMo returned non-JSON: {e}")
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        b64 = (msg.get("audio") or {}).get("data") or ""
        if not b64:
            raise RuntimeError(f"MiMo returned no audio: {str(data)[:300]}")
        import base64
        return base64.b64decode(b64)

    def synthesize(self, text, voice_id=None, model=None,
                   stability=0.5, similarity_boost=0.75, style=0.0):
        """Text -> spoken audio (WAV bytes). Auto-chunks long text and stitches
        the WAV chunks into one valid file. ``stability``/``similarity_boost``/
        ``style`` are ElevenLabs-only and ignored here (kept for signature
        parity)."""
        self._require_key()
        voice_id = voice_id or self.voice_id
        speak = (text or "").strip() or " "
        chunks = VoiceClient._chunk_text(speak, limit=1500)
        if len(chunks) == 1:
            return self._synthesize_one(chunks[0], voice_id)
        return _concat_wav([self._synthesize_one(c, voice_id) for c in chunks])

    def synthesize_with_timestamps(self, text, voice_id=None, model=None,
                                   stability=0.5, similarity_boost=0.75,
                                   style=0.0):
        """MiMo has no char-level timing — return (audio, None) so callers use
        word-count holds, exactly like the ElevenLabs fallback path."""
        return self.synthesize(text, voice_id=voice_id), None


class DeepgramVoiceClient:
    """Deepgram Aura / Aura-2 TTS — premium, very fast voice synthesis.

    Exposes the SAME surface as ``VoiceClient`` / ``MimoVoiceClient``
    (synthesize / synthesize_with_timestamps / list_voices / ping / generate_sfx)
    so it slots straight into ``get_voice_client()``. Deepgram returns raw audio
    bytes (mp3 by default) and has no character-level timestamps, so
    ``synthesize_with_timestamps`` returns ``(audio, None)`` and callers fall
    back to word-count holds.

    API:  POST {base}/speak?model=<voice>&encoding=<container>
          headers: Authorization: Token <key>
          body: {"text": "..."}
          resp: raw audio bytes (audio/mpeg or audio/wav per encoding)

    The Deepgram \"model\" string IS the voice (e.g. ``aura-2-thalia-en``).
    There is no separate voice-id parameter.
    """

    # Built-in Aura + Aura-2 voices. Deepgram doesn't expose a per-account
    # voice list, so this catalog is shipped with the client.
    BUILTIN_VOICES = [
        # --- Aura 2 (newest, recommended) -------------------------------
        {"id": "aura-2-thalia-en", "name": "Thalia (EN, female, warm)", "category": "aura-2"},
        {"id": "aura-2-andromeda-en", "name": "Andromeda (EN, female, calm)", "category": "aura-2"},
        {"id": "aura-2-helena-en", "name": "Helena (EN, female, bright)", "category": "aura-2"},
        {"id": "aura-2-apollo-en", "name": "Apollo (EN, male, authoritative)", "category": "aura-2"},
        {"id": "aura-2-arcas-en", "name": "Arcas (EN, male, natural)", "category": "aura-2"},
        {"id": "aura-2-aries-en", "name": "Aries (EN, male, friendly)", "category": "aura-2"},
        {"id": "aura-2-cora-en", "name": "Cora (EN, female, smooth)", "category": "aura-2"},
        {"id": "aura-2-luna-en", "name": "Luna (EN, female, youthful)", "category": "aura-2"},
        {"id": "aura-2-orion-en", "name": "Orion (EN, male, grounded)", "category": "aura-2"},
        {"id": "aura-2-orpheus-en", "name": "Orpheus (EN, male, deep)", "category": "aura-2"},
        {"id": "aura-2-zeus-en", "name": "Zeus (EN, male, powerful)", "category": "aura-2"},
        # --- Aura 1 (original, still solid) -----------------------------
        {"id": "aura-asteria-en", "name": "Asteria (EN, female)", "category": "aura"},
        {"id": "aura-luna-en", "name": "Luna (EN, female)", "category": "aura"},
        {"id": "aura-stella-en", "name": "Stella (EN, female)", "category": "aura"},
        {"id": "aura-athena-en", "name": "Athena (EN, female)", "category": "aura"},
        {"id": "aura-hera-en", "name": "Hera (EN, female)", "category": "aura"},
        {"id": "aura-orion-en", "name": "Orion (EN, male)", "category": "aura"},
        {"id": "aura-arcas-en", "name": "Arcas (EN, male)", "category": "aura"},
        {"id": "aura-perseus-en", "name": "Perseus (EN, male)", "category": "aura"},
        {"id": "aura-angus-en", "name": "Angus (EN, male, Irish)", "category": "aura"},
        {"id": "aura-orpheus-en", "name": "Orpheus (EN, male)", "category": "aura"},
        {"id": "aura-helios-en", "name": "Helios (EN, male)", "category": "aura"},
        {"id": "aura-zeus-en", "name": "Zeus (EN, male)", "category": "aura"},
    ]

    def __init__(self, api_key=None, base_url=None, model=None, voice_id=None,
                 encoding=None, timeout=180):
        self.api_key = api_key or config.DEEPGRAM_API_KEY
        self.base_url = (base_url or config.DEEPGRAM_BASE_URL).rstrip("/")
        # In Deepgram-land, voice_id and model are the same thing. Prefer
        # whichever the caller supplied; voice_id wins if both are given.
        self.voice_id = voice_id or model or config.DEEPGRAM_VOICE_ID
        self.model = self.voice_id  # kept for signature parity
        self.encoding = (encoding or getattr(config, "DEEPGRAM_ENCODING", "mp3")).lower()
        self.timeout = timeout

    def _require_key(self):
        if not self.api_key:
            raise RuntimeError(
                "No Deepgram API key set. Paste a Deepgram key in the Settings "
                "panel or set DEEPGRAM_API_KEY in your .env "
                "(get one at https://console.deepgram.com)."
            )

    def _coerce_voice(self, vid):
        """Deepgram's voice id IS the model. Unknown ids would 400, so if the
        caller passes an ElevenLabs / MiMo id by mistake, fall back to the
        configured default (or Thalia)."""
        valid = {v["id"] for v in self.BUILTIN_VOICES}
        vid = vid or self.voice_id
        if vid in valid:
            return vid
        return self.voice_id if self.voice_id in valid else "aura-2-thalia-en"

    def ping(self):
        """Auth check for the Settings 'Test connection' panel — synthesizes a
        short clip (Aura is sub-second so this is cheap)."""
        if not self.api_key:
            return {"ok": False, "detail": "no key"}
        try:
            audio = self._synthesize_one("Hi.", self.voice_id)
            return {"ok": bool(audio),
                    "detail": "Deepgram TTS reachable" if audio else "no audio returned"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def list_voices(self):
        """Deepgram's voices are a fixed catalog (no per-account listing)."""
        return [dict(v) for v in self.BUILTIN_VOICES]

    def generate_sfx(self, text, duration_seconds=None, prompt_influence=0.4):
        """Deepgram has no sound-effect generation. Raising lets the caller fall
        back to its ffmpeg-synthesized click/whoosh path."""
        raise RuntimeError("Deepgram does not support sound-effect generation")

    def _accept_header(self):
        """Pick the right Accept/Content-Type for the configured encoding."""
        if self.encoding in ("wav", "linear16"):
            return "audio/wav"
        if self.encoding in ("opus", "ogg"):
            return "audio/ogg"
        if self.encoding == "flac":
            return "audio/flac"
        return "audio/mpeg"  # mp3 default

    def _synthesize_one(self, text, voice_id):
        """Synthesize a single chunk (must be under the Deepgram per-call cap).
        Deepgram's /v1/speak accepts a few thousand chars per request; callers
        chunk longer text via the shared ``VoiceClient._chunk_text`` helper."""
        self._require_key()
        voice = self._coerce_voice(voice_id)
        # Deepgram exposes engine + voice via the model query param; encoding
        # picks the container the bytes come back in.
        params = []
        params.append(f"model={voice}")
        if self.encoding == "mp3":
            params.append("encoding=mp3")
        elif self.encoding in ("wav", "linear16"):
            params.append("encoding=linear16")
            params.append("container=wav")
            params.append("sample_rate=24000")
        elif self.encoding == "flac":
            params.append("encoding=flac")
        elif self.encoding in ("opus", "ogg"):
            params.append("encoding=opus")
            params.append("container=ogg")
        url = f"{self.base_url}/speak?{'&'.join(params)}"
        body = {"text": (text or "").strip() or " "}
        r = _post_with_retry(
            url,
            headers={
                "Authorization": f"Token {self.api_key}",
                "Content-Type": "application/json",
                "Accept": self._accept_header(),
            },
            json=body,
            timeout=self.timeout,
            what="Deepgram TTS",
        )
        if r.status_code >= 400:
            # Deepgram error envelopes are JSON: {"err_code":"...","err_msg":"..."}
            try:
                err = r.json()
                detail = err.get("err_msg") or err.get("message") or r.text[:300]
            except Exception:
                detail = r.text[:400]
            raise RuntimeError(f"Deepgram TTS failed [{r.status_code}]: {detail}")
        if not r.content:
            raise RuntimeError("Deepgram TTS returned empty audio")
        return r.content

    def synthesize(self, text, voice_id=None, model=None,
                   stability=0.5, similarity_boost=0.75, style=0.0):
        """Text -> spoken audio bytes. Auto-chunks long text and concatenates
        the resulting MP3 segments (MP3 frames concatenate cleanly; for WAV the
        client falls back to ``_concat_wav``). The stability/similarity/style
        params are ElevenLabs-only and ignored here (kept for signature parity).
        """
        self._require_key()
        voice_id = voice_id or self.voice_id
        speak = (text or "").strip() or " "

        # Deepgram's /v1/speak handles ~2000 chars comfortably per call. Chunk
        # longer text at sentence boundaries to keep prosody natural.
        chunks = VoiceClient._chunk_text(speak, limit=1800)
        if len(chunks) == 1:
            return self._synthesize_one(chunks[0], voice_id)

        parts = [self._synthesize_one(c, voice_id) for c in chunks]
        # MP3 frames concatenate fine; WAV needs the wave-aware concat helper.
        if self.encoding in ("wav", "linear16"):
            return _concat_wav(parts)
        return b"".join(p for p in parts if p)

    def synthesize_with_timestamps(self, text, voice_id=None, model=None,
                                   stability=0.5, similarity_boost=0.75,
                                   style=0.0):
        """Deepgram TTS has no char-level timing — return (audio, None) so
        callers fall back to word-count / Whisper-derived holds, exactly like
        the MiMo path."""
        return self.synthesize(text, voice_id=voice_id), None


# ============================================================================
#  Piper TTS — LOCAL, 100% FREE, runs on your CPU (or GPU if installed)
# ============================================================================
#
# Piper (https://github.com/rhasspy/piper) is an open-source ONNX neural TTS
# engine. No API key, no per-character cost, no internet at synthesis time
# once the voice model is downloaded. We auto-download the model on first use
# from Hugging Face's rhasspy/piper-voices repo and cache it under
# data/piper_models/. Works on any machine with Python 3.9+.
#
# Same surface as VoiceClient / MimoVoiceClient / DeepgramVoiceClient so it
# slots straight into ``get_voice_client()``. Piper returns WAV bytes and has
# no char-level timestamps, so ``synthesize_with_timestamps`` returns
# ``(audio, None)`` — callers fall back to word-count holds (existing path).
#
# Performance (real-world on a modern laptop):
#   * "low"    quality voice -> ~1.5x real-time on CPU, ~10x on CUDA GPU
#   * "medium" quality voice -> ~1.0x real-time on CPU, ~5x  on CUDA GPU
#   * "high"   quality voice -> ~0.5x real-time on CPU, ~3x  on CUDA GPU
#
# Enable GPU by (1) `pip install onnxruntime-gpu` and (2) PIPER_USE_GPU=true.

_PIPER_VOICES = [
    # short_id  | HF path (relative to rhasspy/piper-voices main)
    #           | display name                          | quality | lang    | gender
    {"id": "amy",        "name": "Amy (EN-US, female, warm)",      "quality": "medium", "lang": "en_US", "gender": "f", "hf_path": "en/en_US/amy/medium/en_US-amy-medium"},
    {"id": "lessac",     "name": "Lessac (EN-US, female, clear)",  "quality": "medium", "lang": "en_US", "gender": "f", "hf_path": "en/en_US/lessac/medium/en_US-lessac-medium"},
    {"id": "kristin",    "name": "Kristin (EN-US, female, soft)",  "quality": "medium", "lang": "en_US", "gender": "f", "hf_path": "en/en_US/kristin/medium/en_US-kristin-medium"},
    {"id": "kusal",      "name": "Kusal (EN-US, male, narration)", "quality": "medium", "lang": "en_US", "gender": "m", "hf_path": "en/en_US/kusal/medium/en_US-kusal-medium"},
    {"id": "joe",        "name": "Joe (EN-US, male, casual)",      "quality": "medium", "lang": "en_US", "gender": "m", "hf_path": "en/en_US/joe/medium/en_US-joe-medium"},
    {"id": "danny",      "name": "Danny (EN-US, male, low-pitch)", "quality": "medium", "lang": "en_US", "gender": "m", "hf_path": "en/en_US/danny/low/en_US-danny-low"},
    {"id": "ryan",       "name": "Ryan (EN-GB, male, narration)",  "quality": "medium", "lang": "en_GB", "gender": "m", "hf_path": "en/en_GB/ryan/medium/en_GB-ryan-medium"},
    {"id": "alba",       "name": "Alba (EN-GB, female, soft)",     "quality": "medium", "lang": "en_GB", "gender": "f", "hf_path": "en/en_GB/alba/medium/en_GB-alba-medium"},
    {"id": "jenny_dioco","name": "Jenny (EN-US, female, narration)","quality": "medium","lang": "en_US", "gender": "f", "hf_path": "en/en_US/jenny_dioco/medium/en_US-jenny_dioco-medium"},
]

_PIPER_VOICE_INDEX = {v["id"]: v for v in _PIPER_VOICES}
_PIPER_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _piper_models_dir():
    """Resolve where Piper voice models are cached. Defaults to
    data/piper_models/ under the project root. Configurable via
    PIPER_MODELS_DIR or the user settings panel."""
    import os
    d = (getattr(config, "PIPER_MODELS_DIR", "") or "").strip()
    if d:
        return d
    # data/ lives next to app.py — match the rest of the project.
    try:
        import store
        return os.path.join(store.DATA_DIR, "piper_models")
    except Exception:
        return os.path.join("data", "piper_models")


def _piper_voice_paths(short_id: str):
    """Resolve a short voice id (e.g. 'amy') to a (model_path, config_path)
    pair on disk, downloading both from Hugging Face if needed. Raises
    RuntimeError with a clear message on any failure."""
    import os
    info = _PIPER_VOICE_INDEX.get(short_id)
    if not info:
        raise RuntimeError(
            f"Unknown Piper voice: '{short_id}'. Pick one of: "
            + ", ".join(sorted(_PIPER_VOICE_INDEX.keys())))
    base = _piper_models_dir()
    os.makedirs(base, exist_ok=True)
    model_path = os.path.join(base, os.path.basename(info["hf_path"]) + ".onnx")
    cfg_path   = os.path.join(base, os.path.basename(info["hf_path"]) + ".onnx.json")
    if not (os.path.exists(model_path) and os.path.exists(cfg_path)):
        # Lazy import — keep Piper optional until a user actually picks it.
        import requests
        for local, fname in [(model_path, ".onnx"), (cfg_path, ".onnx.json")]:
            if os.path.exists(local):
                continue
            url = f"{_PIPER_BASE_URL}/{info['hf_path']}{fname}"
            r = requests.get(url, timeout=180, stream=True)
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Could not download Piper voice '{short_id}' from "
                    f"Hugging Face (HTTP {r.status_code} on {url}). Check "
                    f"your internet connection or pick a different voice.")
            with open(local, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
    return model_path, cfg_path


def _piper_use_gpu() -> bool:
    """Resolve the GPU toggle: config flag AND a working onnxruntime-gpu."""
    if not getattr(config, "PIPER_USE_GPU", False):
        return False
    try:
        import onnxruntime  # noqa: F401
        providers = onnxruntime.get_available_providers()
        return "CUDAExecutionProvider" in providers
    except Exception:
        return False


class PiperVoiceClient:
    """Local Piper TTS — 100% free, no API key, runs on CPU or GPU on your
    machine. Same surface as the cloud providers (synthesize /
    synthesize_with_timestamps / list_voices / ping / generate_sfx)."""

    # Same voice catalog we show in the Settings UI. Order = default order.
    BUILTIN_VOICES = [
        {**v, "category": v["quality"]} for v in _PIPER_VOICES
    ]

    def __init__(self, voice_id=None, use_gpu=None, length_scale=None,
                 noise_scale=None, noise_w_scale=None, models_dir=None,
                 timeout=600):
        # No key. Voice + knobs are the only knobs.
        self.voice_id = voice_id or config.PIPER_VOICE
        self.use_gpu = (use_gpu if use_gpu is not None
                        else _piper_use_gpu())
        self.length_scale = (length_scale if length_scale is not None
                             else float(getattr(config, "PIPER_LENGTH_SCALE", 1.0)))
        self.noise_scale  = (noise_scale  if noise_scale  is not None
                             else float(getattr(config, "PIPER_NOISE_SCALE", 0.667)))
        self.noise_w_scale = (noise_w_scale if noise_w_scale is not None
                              else float(getattr(config, "PIPER_NOISE_W_SCALE", 0.8)))
        self.models_dir_override = models_dir
        self.timeout = timeout
        self._voice = None          # lazy PiperVoice instance (cache across calls)
        self._loaded_voice_id = None

    # ---- surface parity with the cloud clients ----------------------

    def _require_key(self):  # pragma: no cover — Piper has no key
        return  # always passes; kept so signature matches the cloud clients

    def list_voices(self):
        """Piper ships with a fixed bundled catalog (no per-account listing)."""
        return [dict(v) for v in self.BUILTIN_VOICES]

    def generate_sfx(self, text, duration_seconds=None, prompt_influence=0.4):
        """Piper does not generate sound effects. Raise so the caller falls
        back to its ffmpeg-synthesized click/whoosh path."""
        raise RuntimeError("Piper does not support sound-effect generation")

    def _load_voice(self, voice_id):
        """Lazy-load (and cache) the PiperVoice ONNX model."""
        import os
        vid = voice_id or self.voice_id
        if self._voice is not None and self._loaded_voice_id == vid:
            return self._voice
        try:
            import piper
        except ImportError as e:
            raise RuntimeError(
                "Piper TTS isn't installed. Run: pip install piper-tts") from e
        # Temporarily override the models-dir resolver if the user set one.
        if self.models_dir_override:
            global _piper_models_dir
            _orig = _piper_models_dir
            _piper_models_dir = lambda: self.models_dir_override  # type: ignore
        try:
            model_path, cfg_path = _piper_voice_paths(vid)
            self._voice = piper.PiperVoice.load(
                model_path, cfg_path, use_cuda=self.use_gpu)
            self._loaded_voice_id = vid
            return self._voice
        finally:
            if self.models_dir_override:
                _piper_models_dir = _orig  # type: ignore

    def ping(self):
        """Auth check for the Settings 'Test connection' panel — loads the
        active voice (auto-downloads on first run) and synthesizes a one-word
        clip."""
        try:
            audio = self._synthesize_one("Hi.", self.voice_id)
            return {"ok": bool(audio),
                    "detail": (f"Piper ready ({self._loaded_voice_id}, "
                               f"{'GPU' if self.use_gpu else 'CPU'})")}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _synthesize_one(self, text, voice_id):
        """One Piper synthesize call -> WAV bytes. Lazy-loads the voice.
        Piper always emits mono 16-bit PCM, so we hardcode the WAV header
        (no per-channel / per-width attributes exist on PiperConfig)."""
        import io, wave
        from piper.config import SynthesisConfig
        v = self._load_voice(voice_id)
        buf = io.BytesIO()
        wf = wave.open(buf, "wb")
        wf.setnchannels(1)            # Piper is always mono
        wf.setsampwidth(2)            # Piper is always 16-bit PCM
        wf.setframerate(v.config.sample_rate)
        speak = (text or "").strip() or " "
        # piper >=1.4 takes a SynthesisConfig object (no kwargs on .synthesize).
        syn_cfg = SynthesisConfig(
            length_scale=float(self.length_scale) if self.length_scale else None,
            noise_scale=float(self.noise_scale) if self.noise_scale is not None else None,
            noise_w_scale=float(self.noise_w_scale) if self.noise_w_scale is not None else None,
        )
        for chunk in v.synthesize(speak, syn_config=syn_cfg):
            wf.writeframes(chunk.audio_int16_bytes)
        wf.close()
        return buf.getvalue()

    def synthesize(self, text, voice_id=None, model=None,
                   stability=0.5, similarity_boost=0.75, style=0.0):
        """Text -> spoken audio (WAV bytes). Splits long text at sentence
        boundaries and stitches the WAV chunks via _concat_wav so a 3-minute
        voiceover still produces one valid file. stability/similarity/style
        are ElevenLabs-only and ignored here (kept for signature parity)."""
        self._require_key()
        vid = voice_id or self.voice_id
        speak = (text or "").strip() or " "
        # Piper handles several minutes per call but chunking keeps memory
        # bounded for huge scripts.
        chunks = VoiceClient._chunk_text(speak, limit=1500)
        if len(chunks) == 1:
            return self._synthesize_one(chunks[0], vid)
        return _concat_wav([self._synthesize_one(c, vid) for c in chunks])

    def synthesize_with_timestamps(self, text, voice_id=None, model=None,
                                   stability=0.5, similarity_boost=0.75,
                                   style=0.0):
        """Piper has no char-level timing. Return (audio, None) so callers
        fall back to word-count holds, exactly like the MiMo / Deepgram path.
        (Future: run local Whisper on the synthesized WAV to recover word
        timestamps for the A2V frame-accurate sync path.)"""
        return self.synthesize(text, voice_id=voice_id), None
