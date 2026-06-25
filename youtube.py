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
    Returns {channel, items:[{url,title,thumbnail,views,duration,channel}]}.
    Best-effort; never raises."""
    out = {"channel": "", "channel_url": url.strip(), "items": []}
    try:
        import yt_dlp
        opts = {"quiet": True, "skip_download": True, "no_warnings": True,
                "extract_flat": True, "playlistend": limit, "socket_timeout": 30}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url.strip(), download=False)
        ch_name = info.get("title") or info.get("channel") or info.get("uploader") or ""
        # yt-dlp suffixes the playlist tab onto the title (e.g. "TED - Videos").
        # Strip the common tab suffixes so the card shows the clean channel name.
        for _suf in (" - Videos", " - Shorts", " - Home", " - Playlists", " - Live"):
            if ch_name.endswith(_suf):
                ch_name = ch_name[: -len(_suf)]
                break
        out["channel"] = ch_name
        for e in (info.get("entries") or [])[:limit]:
            vid = e.get("id") or ""
            u = e.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else "")
            if not u:
                continue
            # extract_flat exposes a thumbnails list and (sometimes) view_count.
            thumb = ""
            thumbs = e.get("thumbnails") or []
            if thumbs:
                thumb = (thumbs[-1] or {}).get("url") or ""
            if not thumb and vid:
                thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
            out["items"].append({
                "url": u,
                "video_id": vid,
                "title": e.get("title") or "",
                "thumbnail": thumb,
                "views": e.get("view_count") or 0,
                "duration": int(e.get("duration") or 0),
                "channel": e.get("uploader") or e.get("channel") or ch_name,
            })
    except Exception as e:
        _log(f"channel_info failed: {e}")
    return out


def niche_scan(urls, per_channel: int = 12) -> dict:
    """Fetch recent videos from multiple channels and return a flat,
    de-duplicated deck for the 'mini YouTube' grid.
    Returns {channels:[{channel,channel_url,count}], videos:[...]}. Never raises.
    """
    channels, videos, seen = [], [], set()
    for u in urls:
        u = (u or "").strip()
        if not u:
            continue
        ci = channel_info(u, limit=per_channel)
        kept = 0
        for it in ci.get("items", []):
            key = it.get("video_id") or it.get("url")
            if key in seen:
                continue
            seen.add(key)
            videos.append(it)
            kept += 1
        channels.append({
            "channel": ci.get("channel") or u,
            "channel_url": u,
            "count": kept,
        })
    # Sort the deck by views desc so the strongest performers surface first,
    # exactly like a YouTube recommendation rail.
    videos.sort(key=lambda v: v.get("views") or 0, reverse=True)
    return {"channels": channels, "videos": videos}


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
    # New API (1.x): instance .fetch() -> FetchedTranscript (iterable of snippets).
    # Try EN first (most reliable), then fall back to ANY available language and
    # auto-translate to English so non-EN videos still get an analysis transcript.
    try:
        api = API()
        for langs in (["en", "en-US", "en-GB"], None):  # None => "any language"
            try:
                fetched = api.fetch(video_id, languages=langs) if langs else api.fetch(video_id)
                segments = [getattr(s, "text", "") for s in fetched]
                if segments:
                    break
            except Exception:
                continue
        # Last-ditch: ask the API to auto-translate whatever it has into English.
        if not segments:
            try:
                tl = api.fetch(video_id, languages=["en"], translate_to_english=True)
                segments = [getattr(s, "text", "") for s in tl]
            except Exception:
                pass
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
    standard maxres/hq/sd/mq thumbnail endpoints. Returns b'' on failure."""
    candidates = [u for u in [
        thumb_url,
        f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
    ] if u]
    # Last-ditch: ask YouTube's own oEmbed JSON for a thumbnail URL — works
    # even when the standard CDN paths 404 (private / partial / processing).
    try:
        r = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=10,
        )
        if r.status_code < 400:
            import json as _json
            data = _json.loads(r.text or "{}")
            u = data.get("thumbnail_url")
            if u:
                candidates.append(u)
    except Exception:
        pass
    for u in candidates:
        try:
            r = requests.get(u, timeout=20)
            if r.status_code < 400 and r.content and len(r.content) > 1000:
                return r.content
        except Exception:
            continue
    return b""


def storyboard_frames(url: str, video_id: str, max_frames: int = 12):
    """Grab YouTube's seek-preview STORYBOARD sprite sheets and slice them into
    individual real video stills. These are served from a separate CDN endpoint
    that almost always works even when the full video stream download is blocked
    / throttled (the #1 cause of 'only got 1 thumbnail -> weak style copy').

    Returns a list of web paths (saved to data/frames) or [] on failure.
    Never raises.
    """
    try:
        import yt_dlp
        from PIL import Image
        import io as _io
    except Exception as e:
        _log(f"storyboard deps missing: {e}")
        return []
    # 1. Find storyboard formats (format_id starts with 'sb', vcodec 'none',
    #    they carry .fragments = the sprite-sheet image URLs).
    try:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "skip_download": True, "socket_timeout": 25,
                "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}}}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        _log(f"storyboard info failed: {e}")
        return []
    sbs = [f for f in (info.get("formats") or [])
           if str(f.get("format_id", "")).startswith("sb")
           and (f.get("fragments") or f.get("url"))]
    if not sbs:
        _log("no storyboard formats available")
        return []
    # Pick the HIGHEST-resolution storyboard (largest width) for the sharpest
    # stills — that's the best style read.
    sbs.sort(key=lambda f: (f.get("width") or 0) * (f.get("height") or 0),
             reverse=True)
    sb = sbs[0]
    # Each sprite sheet packs a grid of thumbnails (rows x cols). yt-dlp exposes
    # columns/rows; default to a sane grid if absent.
    cols = int(sb.get("columns") or 5)
    rows = int(sb.get("rows") or 5)
    frag_urls = [fr.get("url") for fr in (sb.get("fragments") or [])
                 if fr.get("url")] or ([sb.get("url")] if sb.get("url") else [])
    if not frag_urls:
        return []
    out = []
    for furl in frag_urls:
        if len(out) >= max_frames:
            break
        try:
            r = requests.get(furl, timeout=25)
            if r.status_code >= 400 or not r.content:
                continue
            sheet = Image.open(_io.BytesIO(r.content)).convert("RGB")
            sw, sh = sheet.size
            cw, ch = sw // max(1, cols), sh // max(1, rows)
            if cw < 16 or ch < 16:
                continue
            for ry in range(rows):
                for rx in range(cols):
                    if len(out) >= max_frames:
                        break
                    tile = sheet.crop((rx * cw, ry * ch,
                                       (rx + 1) * cw, (ry + 1) * ch))
                    # Skip near-black/blank padding tiles at the sheet's tail.
                    ex = tile.getextrema()
                    if all(lo == hi for lo, hi in ex):
                        continue
                    buf = _io.BytesIO()
                    tile.save(buf, format="PNG")
                    web, _p = store.write_binary(
                        "frames", buf.getvalue(), ext="png",
                        name_hint=f"sb_{video_id}_{len(out):03d}")
                    out.append(web)
        except Exception as e:
            _log(f"storyboard slice failed: {e}")
            continue
    if out:
        _log(f"storyboard fallback: recovered {len(out)} real stills")
    return out[:max_frames]



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
    # Use iOS+Android player clients as fallbacks: the web client gets bot-
    # blocked from cloud IPs more often than the mobile ones do.
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 30, "outtmpl": outtmpl,
        "format": "best[height<=480][ext=mp4]/best[height<=480]/best[ext=mp4]/best",
        "max_filesize": 220 * 1024 * 1024,
        "extractor_args": {"youtube": {"player_client": ["ios", "android", "web"]}},
    }
    path = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as e:
        _log(f"download failed: {e}")
        # Clean up any partial downloads (.part files) left by yt-dlp
        import glob as _glob
        for _pf in _glob.glob(os.path.join(store.UPLOADS_DIR, f"{tag}.*")):
            if ".part" in _pf:
                try: os.remove(_pf)
                except OSError: pass
        return [], None

    if not path or not os.path.exists(path):
        # yt-dlp may have remuxed to a different extension.
        import glob
        # Exclude .part files — those are incomplete downloads, not valid media
        hits = [h for h in glob.glob(os.path.join(store.UPLOADS_DIR, f"{tag}.*"))
                if ".part" not in h]
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
    # YouTube throttles frame downloads from some IPs transiently — the first
    # attempt returns nothing, a retry seconds later succeeds. Without this the
    # pipeline silently falls back to a SINGLE thumbnail as the only style
    # anchor, which gives the image model far too weak a read of the source art
    # style → "the generated video is a completely different style". Retry the
    # real frame download with WIDENING backoff so the attempts span past a
    # short throttle window, not all inside it.
    if not frames:
        import time as _time
        for _attempt, _wait in enumerate((5, 12), 1):
            _time.sleep(_wait)
            _log(f"frame download empty — retry {_attempt}/2 for {vid}")
            frames, _path = download_frames(
                url, max_frames=max_frames,
                duration_hint=meta.get("duration", 0))
            if frames:
                _log(f"retry {_attempt} succeeded: {len(frames)} frames")
                break
    # Still nothing after retries → the video stream is genuinely blocked from
    # this network. Fall back to YouTube's STORYBOARD sprite sheets: real video
    # stills served from a separate CDN that usually works even when the stream
    # is blocked. This recovers 6-12 real anchors → style copying stays strong,
    # instead of degrading to one thumbnail.
    if not frames:
        _log(f"frame download failed after retries — trying storyboard for {vid}")
        sb = storyboard_frames(url, vid, max_frames=max_frames)
        if sb:
            frames = sb
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
            notes = ("Couldn't download the video OR its storyboard after "
                     "retries (YouTube is hard-blocking this network); used the "
                     "thumbnail only — STYLE COPYING WILL BE WEAK. Re-run the "
                     "analysis in a few minutes, or try a VPN, for real frames.")
            print(f"[youtube] ALL fallbacks exhausted for {vid} — "
                  f"returning 1 thumbnail (style will be weak)", flush=True)
        else:
            source = "none"
            notes = "Couldn't fetch frames or thumbnail; analysis uses transcript only."

    if not transcript and source == "none":
        raise RuntimeError(
            "Couldn't read this video — no transcript, frames, or thumbnail "
            "available. Try a different link (one with captions).")

    # WHISPER FALLBACK for the way-of-speaking. fetch_transcript only reads
    # YouTube CAPTIONS — videos with captions disabled (age-gated, music, many
    # Shorts) return "", which means Claude gets NO transcript and can't learn
    # the narration style. When that happens but we DID download the video file
    # for frames, transcribe that local file with faster-whisper so the speaking
    # style is still captured. Best-effort: never fail ingest over this.
    if not transcript and _path and os.path.exists(_path):
        try:
            import transcribe as _transmod
            if _transmod.local_available():
                _log(f"no captions for {vid} — transcribing audio with Whisper")
                _tr = _transmod.transcribe_audio(_path, engine="local")
                transcript = (_tr.get("text") or "").strip() if isinstance(_tr, dict) else ""
                if transcript:
                    _log(f"whisper transcript ok ({len(transcript)} chars)")
        except Exception as _we:
            _log(f"whisper fallback failed for {vid}: {_we}")

    return {
        "video_id": vid, "url": url,
        "title": meta.get("title", ""), "channel": meta.get("channel", ""),
        "duration": meta.get("duration", 0),
        "transcript": transcript, "frame_urls": frames,
        "source": source, "notes": notes,
    }
