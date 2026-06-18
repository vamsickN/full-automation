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

    def synthesize_with_timestamps(self, text, voice_id=None, model=None,
                                    stability=0.5, similarity_boost=0.75,
                                    style=0.0):
        """Text -> (MP3 bytes, alignment_dict).

        Uses the ElevenLabs ``with-timestamps`` endpoint which returns
        character-level timing alongside the audio.  ``alignment_dict`` has:
          characters                   list[str]
          character_start_times_seconds list[float]
          character_end_times_seconds   list[float]

        Falls back to ``(synthesize(text), None)`` if the endpoint is
        unavailable (older API plans) or returns a non-JSON response.
        Only works for texts under ~4 500 chars (one request); for longer
        texts the caller should fall back to plain ``synthesize()``.
        """
        self._require_key()
        voice_id = voice_id or self.voice_id
        model = model or self.model
        speak = (text or "").strip() or " "

        if len(speak) > 4500:
            # timestamp endpoint doesn't support chunking; synthesize the FULL
            # text via the chunking path (no content dropped) and signal the
            # caller (alignment=None) to fall back to word-count holds.
            return self.synthesize(
                speak, voice_id, model, stability, similarity_boost, style
            ), None

        url = f"{self.base_url}/text-to-speech/{voice_id}/with-timestamps"
        body = {
            "text": speak,
            "model_id": model,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity_boost,
                "style": style,
                "use_speaker_boost": True,
            },
        }
        import base64
        r = _post_with_retry(
            url,
            headers={"xi-api-key": self.api_key,
                     "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout,
            what="TTS (timestamps)",
        )
        if r.status_code >= 400:
            # Endpoint not available on this plan — degrade gracefully.
            mp3 = self._synthesize_one(
                speak, voice_id, model, stability, similarity_boost, style)
            return mp3, None
        try:
            data = r.json()
            audio_b64 = data.get("audio_base64") or ""
            mp3 = base64.b64decode(audio_b64) if audio_b64 else b""
            if not mp3:
                mp3 = self._synthesize_one(
                    speak, voice_id, model, stability, similarity_boost, style)
                return mp3, None
            # Prefer normalized_alignment (numbers/symbols expanded as spoken).
            alignment = data.get("normalized_alignment") or data.get("alignment")
            return mp3, alignment
        except Exception:
            mp3 = self._synthesize_one(
                speak, voice_id, model, stability, similarity_boost, style)
            return mp3, None


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
        for w in wav_list:
            with wave.open(io.BytesIO(w), "rb") as rd:
                frames = rd.readframes(rd.getnframes())
                if writer is None:
                    writer = wave.open(out, "wb")
                    writer.setnchannels(rd.getnchannels())
                    writer.setsampwidth(rd.getsampwidth())
                    writer.setframerate(rd.getframerate())
                writer.writeframes(frames)
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
