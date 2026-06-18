"""Audio transcription with word-level timestamps for the Audio->Video tab.

Two $0-or-existing-key engines (NO OpenAI dependency):

  * "local"      -> faster-whisper running on this machine. Free forever, word
                    timestamps, no API. One-time model download on first use.
  * "elevenlabs" -> ElevenLabs Scribe speech-to-text. Reuses the ElevenLabs key
                    the user already has. Word timestamps included.

Default is "local". The caller picks the engine per job.

Every engine returns the SAME normalized dict:
    {
      "text":  "<full transcript>",
      "duration": <float seconds>,
      "words": [ {"word": str, "start": float, "end": float}, ... ],
      "segments": [ {"text": str, "start": float, "end": float}, ... ],
      "engine": "local" | "elevenlabs",
    }
"""
import os

import requests

import config


_EL_TIMEOUT = 300
# Local model size: "base"/"small" are fast + plenty accurate for clear speech
# on a modest CPU. Override with WHISPER_LOCAL_MODEL in the env.
_LOCAL_MODEL = os.environ.get("WHISPER_LOCAL_MODEL", "base")


# --------------------------------------------------------------------------- #
#  Public entry point
# --------------------------------------------------------------------------- #
def transcribe_audio(audio_path: str, settings: dict = None,
                     engine: str = "local", language: str = None) -> dict:
    """Transcribe ``audio_path`` with the requested engine.

    engine: "local" (faster-whisper, default) or "elevenlabs" (Scribe).
    Raises RuntimeError with a clear, actionable message on failure.
    """
    if not os.path.exists(audio_path):
        raise RuntimeError(f"audio file not found: {audio_path}")
    engine = (engine or "local").lower()
    if engine in ("elevenlabs", "scribe", "11labs"):
        return _transcribe_elevenlabs(audio_path, settings, language)
    return _transcribe_local(audio_path, language)


# --------------------------------------------------------------------------- #
#  Engine 1: local faster-whisper ($0, word timestamps)
# --------------------------------------------------------------------------- #
def local_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


_LOCAL_MODEL_CACHE = {}


def _get_local_model(size: str):
    if size in _LOCAL_MODEL_CACHE:
        return _LOCAL_MODEL_CACHE[size]
    from faster_whisper import WhisperModel
    # int8 on CPU keeps memory + speed sane on a modest box.
    model = WhisperModel(size, device="cpu", compute_type="int8")
    _LOCAL_MODEL_CACHE[size] = model
    return model


def _ensure_wav(audio_path: str) -> str:
    """Convert any audio file to 16kHz mono WAV via ffmpeg. Returns the WAV
    path (or the original path if it's already WAV). Deletes the temp WAV on
    interpreter exit so we don't leak."""
    ext = os.path.splitext(audio_path)[1].lower()
    if ext == ".wav":
        return audio_path
    import tempfile, atexit
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    atexit.register(lambda p=wav_path: os.path.exists(p) and os.remove(p))
    import subprocess
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", audio_path, "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", wav_path],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not os.path.exists(wav_path):
        raise RuntimeError(
            f"ffmpeg conversion failed (code {proc.returncode}): "
            f"{(proc.stderr or '')[-300:]}. Is ffmpeg installed and on PATH?")
    return wav_path


def _transcribe_local(audio_path: str, language: str = None) -> dict:
    if not local_available():
        raise RuntimeError(
            "Local Whisper isn't installed. Install it once with:\n"
            "    pip install faster-whisper\n"
            "…or switch the Audio->Video transcription engine to ElevenLabs "
            "Scribe (uses your existing ElevenLabs key)."
        )
    try:
        model = _get_local_model(_LOCAL_MODEL)
    except Exception as e:
        raise RuntimeError(f"Could not load local Whisper model '{_LOCAL_MODEL}': {e}")

    # Pre-convert to 16kHz mono WAV so Whisper never chokes on exotic formats.
    try:
        wav_path = _ensure_wav(audio_path)
    except Exception as e:
        raise RuntimeError(f"Audio format conversion failed: {e}")

    try:
        segments_iter, info = model.transcribe(
            wav_path,
            language=language,
            word_timestamps=True,
            vad_filter=True,            # skip long silences -> tighter timing
        )
    except Exception as e:
        raise RuntimeError(f"Local transcription failed: {e}")

    words, segments, text_parts = [], [], []
    for seg in segments_iter:
        seg_text = (seg.text or "").strip()
        if seg_text:
            text_parts.append(seg_text)
        segments.append({
            "text": seg_text,
            "start": float(seg.start or 0.0),
            "end": float(seg.end or 0.0),
        })
        for w in (getattr(seg, "words", None) or []):
            tok = (w.word or "").strip()
            if not tok:
                continue
            words.append({
                "word": tok,
                "start": float(w.start or 0.0),
                "end": float(w.end or 0.0),
            })

    duration = float(getattr(info, "duration", 0.0) or 0.0)
    if duration <= 0.0:
        if words:
            duration = words[-1]["end"]
        elif segments:
            duration = segments[-1]["end"]

    return {
        "text": " ".join(text_parts).strip(),
        "duration": duration,
        "words": words,
        "segments": segments,
        "engine": "local",
    }


# --------------------------------------------------------------------------- #
#  Engine 2: ElevenLabs Scribe (reuses the existing ElevenLabs key)
# --------------------------------------------------------------------------- #
def _el_key(settings: dict = None) -> str:
    if settings and settings.get("elevenlabs_api_key"):
        return settings["elevenlabs_api_key"]
    return getattr(config, "ELEVENLABS_API_KEY", "") or ""


def _transcribe_elevenlabs(audio_path: str, settings: dict = None,
                           language: str = None) -> dict:
    key = _el_key(settings)
    if not key:
        raise RuntimeError(
            "No ElevenLabs API key set — add it in Settings to use ElevenLabs "
            "Scribe for transcription (or switch the engine to Local Whisper)."
        )
    base = (getattr(config, "ELEVENLABS_BASE_URL", "")
            or "https://api.elevenlabs.io/v1").rstrip("/")
    url = f"{base}/speech-to-text"

    data = {"model_id": "scribe_v1", "timestamps_granularity": "word"}
    if language:
        data["language_code"] = language

    fname = os.path.basename(audio_path) or "audio.mp3"
    with open(audio_path, "rb") as fh:
        files = {"file": (fname, fh, "application/octet-stream")}
        try:
            r = requests.post(
                url,
                headers={"xi-api-key": key},
                data=data,
                files=files,
                timeout=_EL_TIMEOUT,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"ElevenLabs Scribe request failed: {e}")
    if r.status_code >= 400:
        raise RuntimeError(f"ElevenLabs Scribe failed [{r.status_code}]: {r.text[:400]}")
    try:
        payload = r.json()
    except Exception:
        raise RuntimeError("ElevenLabs Scribe returned a non-JSON response")

    # Scribe returns {text, words:[{text,type,start,end,...}]}. Words include
    # "spacing"/punctuation entries; keep only actual word tokens for timing.
    words = []
    for w in (payload.get("words") or []):
        if (w.get("type") or "word") not in ("word", "audio_event"):
            continue
        tok = (w.get("text") or "").strip()
        if not tok:
            continue
        try:
            words.append({
                "word": tok,
                "start": float(w.get("start", 0.0)),
                "end": float(w.get("end", 0.0)),
            })
        except (TypeError, ValueError):
            continue

    text = (payload.get("text") or "").strip()
    # Scribe gives no segment list — synthesize sentence-ish segments from the
    # word stream so the scene splitter has timed lines to work with.
    segments = _segments_from_words(words) if words else (
        [{"text": text, "start": 0.0,
          "end": (words[-1]["end"] if words else 0.0)}] if text else [])

    duration = words[-1]["end"] if words else (
        segments[-1]["end"] if segments else 0.0)

    return {
        "text": text,
        "duration": float(duration),
        "words": words,
        "segments": segments,
        "engine": "elevenlabs",
    }


def _segments_from_words(words: list, max_words: int = 10,
                         max_gap: float = 0.6) -> list:
    """Group a flat word stream into readable timed segments by sentence-ending
    punctuation, a word-count cap, or a speech gap."""
    segs, cur, cur_start = [], [], None
    for i, w in enumerate(words):
        if cur_start is None:
            cur_start = w["start"]
        cur.append(w["word"])
        end_punct = w["word"].endswith((".", "!", "?", "…"))
        gap = (words[i + 1]["start"] - w["end"]) if i + 1 < len(words) else 0.0
        if end_punct or len(cur) >= max_words or gap >= max_gap:
            segs.append({"text": " ".join(cur).strip(),
                         "start": float(cur_start), "end": float(w["end"])})
            cur, cur_start = [], None
    if cur:
        segs.append({"text": " ".join(cur).strip(),
                     "start": float(cur_start), "end": float(words[-1]["end"])})
    return segs


# --------------------------------------------------------------------------- #
#  Shared: word timestamps -> character-level alignment (for _holds_from_alignment)
# --------------------------------------------------------------------------- #
def words_to_char_alignment(words: list) -> dict:
    """Convert word timestamps into the SAME character-level alignment shape
    that ``voice.synthesize_with_timestamps`` returns, so the existing
    ``_holds_from_alignment`` sync path consumes it unchanged."""
    chars, starts, ends = [], [], []
    for w in words:
        token = w.get("word") or ""
        ws = float(w.get("start", 0.0))
        we = float(w.get("end", ws))
        n = max(1, len(token))
        span = max(0.0, we - ws)
        step = span / n if n else 0.0
        for i, ch in enumerate(token):
            chars.append(ch)
            starts.append(round(ws + step * i, 4))
            ends.append(round(ws + step * (i + 1), 4))
        chars.append(" ")
        starts.append(round(we, 4))
        ends.append(round(we, 4))
    return {
        "characters": chars,
        "character_start_times_seconds": starts,
        "character_end_times_seconds": ends,
    }
