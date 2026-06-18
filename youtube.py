"""YouTube reference ingestion for the 'Inspire from a video' feature.

Claude can't watch YouTube directly, so we gather three things here and hand
them to the model:

  1. the transcript            -> topic + way of speaking (youtube-transcript-api)
  2. a handful of frames       -> visual style / cinematography (yt-dlp + ffmpeg)
  3. lightweight metadata      -> title / channel / duration / thumbnail (yt-dlp)

Everything is best-effort and time-boxed: if the video can't be downloaded
(age-gated, region-locked, throttled) we fall back to the hi-res thumbnail as
the single visual reference, so the feature always returns *something* usable.
"""
import os
import re
import sys

import requests

import store
import video as videomod


def _log(msg):
    print(f"[youtube] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
#  URL parsing
# --------------------------------------------------------------------------- #
_ID_RE = re.compile(r"[0-9A-Za-z_-]{11}")


def extract_video_id(url: str):
    """Pull the 11-char video id out of any common YouTube URL form."""
    if not url:
        return None
    url = url.strip()
    # youtu.be/<id>
    m = re.search(r"youtu\.be/([0-9A-Za-z_-]{11})", url)
    if m:
        return m.group(1)
    # watch?v=<id>
    m = re.search(r"[?&]v=([0-9A-Za-z_-]{11})", url)
    if m:
        return m.group(1)
    # /shorts/<id>  /embed/<id>  /live/<id>
    m = re.search(r"/(?:shorts|embed|live|v)/([0-9A-Za-z_-]{11})", url)
    if m:
        return m.group(1)
    # bare id
    if _ID_RE.fullmatch(url):
        return url
    return None


def is_youtube_url(url: str) -> bool:
    return extract_video_id(url) is not None


# --------------------------------------------------------------------------- #
#  Channel listing + search (Wave 5)
# --------------------------------------------------------------------------- #
def channel_info(url: str, limit: int = 20) -> dict:
    """Flat-list a channel/playlist's recent videos.
    Returns {channel, items:[{url,title}]}. Best-effort; never raises."""
    out = {"channel": "", "items": []}
    try:
        import yt_dlp
        opts = {"quiet": True, "skip_download": True, "no_warnings": True,
                "extract_flat": True, "playlistend": limit, "socket_timeout": 30}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url.strip(), download=False)
        out["channel"] = info.get("title") or info.get("channel") or ""
        for e in (info.get("entries") or [])[:limit]:
            vid = e.get("id") or ""
            u = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
            if u:
                out["items"].append({"url": u, "title": e.get("title") or ""})
    except Exception as e:
        _log(f"channel_info failed: {e}")
    return out


def search_videos(query: str, limit: int = 12) -> list:
    """YouTube search via yt-dlp. Returns [{title, channel, url, views}]."""
    out = []
    try:
        import yt_dlp
        opts = {"quiet": True, "skip_download": True, "no_warnings": True,
                "extract_flat": True, "socket_timeout": 30}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        for e in (info.get("entries") or [])[:limit]:
            vid = e.get("id") or ""
            out.append({
                "title": e.get("title") or "",
                "channel": e.get("uploader") or e.get("channel") or "",
                "url": e.get("url") or (f"https://youtube.com/watch?v={vid}" if vid else ""),
                "views": e.get("view_count") or 0,
            })
    except Exception as e:
        _log(f"search failed: {e}")
    return out


# --------------------------------------------------------------------------- #
#  Transcript -> topic + speaking style
# --------------------------------------------------------------------------- #
def fetch_transcript(video_id: str, max_chars: int = 6000) -> str:
    """Return the spoken transcript as plain text, or '' on any failure.

    Supports both the new (>=1.x instance) and old (<=0.6 static) API shapes of
    youtube-transcript-api.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi as API
    except Exception as e:
        _log(f"transcript api unavailable: {e}")
        return ""

    segments = None
    # New API (1.x): instance .fetch() -> FetchedTranscript (iterable of snippets)
    try:
        api = API()
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        segments = [getattr(s, "text", "") for s in fetched]
    except Exception as e1:
        # Old API (<=0.6): static get_transcript -> list[dict]
        try:
            data = API.get_transcript(video_id, languages=["en", "en-US", "en-GB"])
            segments = [d.get("text", "") for d in data]
        except Exception as e2:
            _log(f"transcript fetch failed: {e1} | {e2}")
            return ""

    text = " ".join(t.strip() for t in (segments or []) if t and t.strip())
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + " …"
    return text


# --------------------------------------------------------------------------- #
#  Metadata + thumbnail (no download)
# --------------------------------------------------------------------------- #
def fetch_metadata(url: str, video_id: str = None) -> dict:
    """Best-effort title/channel/duration/thumbnail via yt-dlp (no download)."""
    out = {"title": "", "channel": "", "duration": 0, "thumbnail": ""}
    try:
        import yt_dlp
        opts = {"quiet": True, "skip_download": True, "no_warnings": True,
                "noplaylist": True, "socket_timeout": 20}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        out["title"] = info.get("title") or ""
        out["channel"] = info.get("uploader") or info.get("channel") or ""
        out["duration"] = int(info.get("duration") or 0)
        out["thumbnail"] = info.get("thumbnail") or ""
    except Exception as e:
        _log(f"metadata failed: {e}")
    if not out["thumbnail"] and video_id:
        out["thumbnail"] = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    return out


def thumbnail_bytes(video_id: str, thumb_url: str = "") -> bytes:
    """Download a single representative still. Tries the given URL then the
    standard maxres/hq thumbnail endpoints. Returns b'' on failure."""
    candidates = [u for u in [thumb_url,
                  f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
                  f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"] if u]
    for u in candidates:
        try:
            r = requests.get(u, timeout=20)
            if r.status_code < 400 and r.content and len(r.content) > 1000:
                return r.content
        except Exception:
            continue
    return b""


# --------------------------------------------------------------------------- #
#  Frame download (yt-dlp -> ffmpeg)
# --------------------------------------------------------------------------- #
def download_frames(url: str, max_frames: int = 12, duration_hint: int = 0):
    """Download the video (capped quality) and sample frames.

    Returns (frame_web_paths, downloaded_path). On any failure returns ([], None)
    so the caller can fall back to the thumbnail. Never raises.
    """
    try:
        import yt_dlp
    except Exception as e:
        _log(f"yt-dlp unavailable: {e}")
        return [], None

    os.makedirs(store.UPLOADS_DIR, exist_ok=True)
    tag = store.new_id("yt")
    outtmpl = os.path.join(store.UPLOADS_DIR, f"{tag}.%(ext)s")
    # Prefer a small mp4 to keep the download fast — we only need stills.
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 30, "outtmpl": outtmpl,
        "format": "best[height<=480][ext=mp4]/best[height<=480]/best[ext=mp4]/best",
        "max_filesize": 220 * 1024 * 1024,
    }
    path = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as e:
        _log(f"download failed: {e}")
        return [], None

    if not path or not os.path.exists(path):
        # yt-dlp may have remuxed to a different extension.
        import glob
        hits = glob.glob(os.path.join(store.UPLOADS_DIR, f"{tag}.*"))
        path = hits[0] if hits else None
    if not path or not os.path.exists(path):
        return [], None

    # Sample evenly across the clip. Pick an fps that yields ~max_frames frames.
    try:
        dur = duration_hint or 0
        if not dur:
            try:
                import editor
                dur = editor.probe_duration(path)
            except Exception:
                dur = 0
        fps = (max_frames / dur) if dur and dur > 0 else 0.2
        fps = max(0.05, min(2.0, fps))
        frames = videomod.extract_frames(path, fps=fps, max_frames=max_frames)
        return frames, path
    except Exception as e:
        _log(f"frame extract failed: {e}")
        return [], path


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #
def ingest(url: str, max_frames: int = 12) -> dict:
    """Gather everything Claude needs from a YouTube URL.

    Returns:
      { video_id, url, title, channel, duration, transcript,
        frame_urls: [..], source: 'frames'|'thumbnail'|'none', notes: str }
    """
    vid = extract_video_id(url)
    if not vid:
        raise ValueError("That doesn't look like a YouTube link.")

    meta = fetch_metadata(url, vid)
    transcript = fetch_transcript(vid)

    frames, _path = download_frames(url, max_frames=max_frames,
                                    duration_hint=meta.get("duration", 0))
    source = "frames"
    notes = ""
    if not frames:
        # Fallback: one thumbnail still.
        tb = thumbnail_bytes(vid, meta.get("thumbnail", ""))
        if tb:
            web, _p = store.write_binary("frames", tb, ext="jpg",
                                         name_hint=f"ytthumb_{vid}")
            frames = [web]
            source = "thumbnail"
            notes = ("Couldn't download the video (likely blocked); used the "
                     "thumbnail for visual style.")
        else:
            source = "none"
            notes = "Couldn't fetch frames or thumbnail; analysis uses transcript only."

    if not transcript and source == "none":
        raise RuntimeError(
            "Couldn't read this video — no transcript, frames, or thumbnail "
            "available. Try a different link (one with captions).")

    return {
        "video_id": vid, "url": url,
        "title": meta.get("title", ""), "channel": meta.get("channel", ""),
        "duration": meta.get("duration", 0),
        "transcript": transcript, "frame_urls": frames,
        "source": source, "notes": notes,
    }
