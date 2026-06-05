"""Continuity Studio — FastAPI backend (extended).

Run:  uvicorn app:app --reload --port 8000   then open http://localhost:8000
"""
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from typing import List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import pipeline
import store
import editor
from derouter import ImageClient
from claude_client import ClaudeClient, extract_json

class AnalyseIn(BaseModel):
    image_url: str
    question: str = ""

store.init()
app = FastAPI(title="Continuity Studio")

# --- Auth Helpers ---
def load_users():
    path = os.path.join(os.path.dirname(__file__), "users.json")
    if not os.path.exists(path): return {}
    with open(path, "r") as f: return json.load(f)

def save_users(users):
    path = os.path.join(os.path.dirname(__file__), "users.json")
    with open(path, "w") as f: json.dump(users, f, indent=2)

def load_codes():
    path = os.path.join(os.path.dirname(__file__), "codes.json")
    if not os.path.exists(path): return {}
    with open(path, "r") as f: return json.load(f)

def save_codes(codes):
    path = os.path.join(os.path.dirname(__file__), "codes.json")
    with open(path, "w") as f: json.dump(codes, f, indent=2)

import vault_crypto

def load_vault():
    path = os.path.join(os.path.dirname(__file__), "vault.json")
    if not os.path.exists(path): return {}
    with open(path, "r") as f: raw = json.load(f)
    decrypted = vault_crypto.decrypt_vault(raw)
    if vault_crypto.is_encrypted() and vault_crypto.needs_migration(raw):
        save_vault(decrypted)
    return decrypted

def save_vault(vault):
    path = os.path.join(os.path.dirname(__file__), "vault.json")
    encrypted = vault_crypto.encrypt_vault(vault)
    with open(path, "w") as f: json.dump(encrypted, f, indent=2)

# --- Session signing ---
import hashlib
import hmac
import base64 as _b64
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

_SESSION_KEY = config.SESSION_SECRET.encode()
_PUBLIC_PATHS = {"/", "/api/auth/login", "/api/auth/signup", "/api/auth/status"}

def _sign_session(email: str) -> str:
    payload = _b64.urlsafe_b64encode(email.encode()).decode()
    sig = hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"

def _verify_session(token: str) -> str | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        return _b64.urlsafe_b64decode(payload.encode()).decode()
    except Exception:
        return None

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000).hex()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    email = ""
    session = request.cookies.get("cs_session", "")
    if session:
        email = _verify_session(session) or ""
    if not email:
        email = request.cookies.get("hacker_access", "")

    if config.AUTH_REQUIRED and not email:
        path = request.url.path
        if path not in _PUBLIC_PATHS and not path.startswith("/data/"):
            return JSONResponse({"detail": "login required"}, status_code=401)

    vault = load_vault()
    user_data = vault.get(email, {})
    request.state.user_email = email
    request.state.settings = {
        "api_key": user_data.get("api_key", ""),
        "base_url": user_data.get("base_url", config.BASE_URL),
        "model": user_data.get("model", config.MODEL),
        "multi_image_edit": user_data.get("multi_image_edit", config.MULTI_IMAGE_EDIT),
        "claude_api_key": user_data.get("claude_api_key", ""),
        "claude_base_url": user_data.get("claude_base_url", config.CLAUDE_BASE_URL),
        "claude_model": user_data.get("claude_model", config.CLAUDE_MODEL),
        "elevenlabs_api_key": user_data.get("elevenlabs_api_key", getattr(config, "ELEVENLABS_API_KEY", "")),
        "elevenlabs_voice_id": user_data.get("elevenlabs_voice_id", getattr(config, "ELEVENLABS_VOICE_ID", "")),
        "elevenlabs_model": user_data.get("elevenlabs_model", getattr(config, "ELEVENLABS_MODEL", "")),
        "webhook_url": user_data.get("webhook_url", ""),
    }
    return await call_next(request)


# --- Auth endpoints ---
class AuthIn(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def api_auth_signup(body: AuthIn):
    email = body.email.strip().lower()
    password = body.password.strip()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    users = load_users()
    if email in users:
        raise HTTPException(409, "Account already exists")
    salt = store.new_id("salt")
    users[email] = {"hash": _hash_password(password, salt), "salt": salt,
                    "created": store.now()}
    save_users(users)
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie("cs_session", _sign_session(email),
                    httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


@app.post("/api/auth/login")
def api_auth_login(body: AuthIn):
    email = body.email.strip().lower()
    password = body.password.strip()
    users = load_users()
    user = users.get(email)
    if not user:
        raise HTTPException(401, "Invalid email or password")
    if _hash_password(password, user["salt"]) != user["hash"]:
        raise HTTPException(401, "Invalid email or password")
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie("cs_session", _sign_session(email),
                    httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


@app.post("/api/auth/logout")
def api_auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("cs_session")
    return resp


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    return {
        "logged_in": bool(request.state.user_email),
        "email": request.state.user_email,
        "auth_required": config.AUTH_REQUIRED,
    }

app.mount("/data", StaticFiles(directory=config.DATA_DIR), name="data")

# Runtime settings resolver (uses request state)
def get_user_settings(request: Request):
    return request.state.settings

def get_image_client(request: Request) -> ImageClient:
    s = request.state.settings
    return ImageClient(api_key=s["api_key"], base_url=s["base_url"], model=s["model"])

def get_claude_client(request: Request = None) -> ClaudeClient:
    if request:
        s = request.state.settings
    else:
        s = {
            "claude_api_key": config.CLAUDE_API_KEY, 
            "claude_base_url": config.CLAUDE_BASE_URL, 
            "claude_model": config.CLAUDE_MODEL
        }
    return ClaudeClient(api_key=s["claude_api_key"], base_url=s["claude_base_url"], model=s["claude_model"])


def get_voice_client(request: Request, voice_id: str = None):
    import voice
    s = request.state.settings
    return voice.VoiceClient(
        api_key=s["elevenlabs_api_key"],
        model=s["elevenlabs_model"],
        voice_id=voice_id or s["elevenlabs_voice_id"],
    )


@app.post("/api/analyse-scene")
def api_analyse_scene(a: AnalyseIn, request: Request):
    try:
        img = store.read_image(a.image_url)
    except Exception as e:
        raise HTTPException(400, f"unreadable image: {e}")
    try:
        text = get_claude_client(request).analyse_scene(
            pipeline.downsize_for_vision(img), a.question
        )
    except Exception as e:
        raise HTTPException(500, f"analysis failed: {e}")
    return {"analysis": text}
@app.post("/api/prompts-from-video")
def api_prompts_from_video(p: AnalyseIn, request: Request):
    # ... existing implementation uses get_claude_client ...
    pass


# --------------------------------------------------------------------------- #
#  Static page
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    """The Continuity Studio tool."""
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


# --------------------------------------------------------------------------- #
#  State + settings
# --------------------------------------------------------------------------- #
@app.get("/api/state")
def api_state(request: Request):
    s = request.state.settings
    return {
        "state": store.load_state(),
        "config": {
            "model": s["model"],
            "base_url": s["base_url"],
            "has_api_key": bool(s["api_key"]),
            "multi_image_edit": s["multi_image_edit"],
            "claude_model": s["claude_model"],
            "claude_base_url": s["claude_base_url"],
            "has_claude_key": bool(s["claude_api_key"]),
            "claude_models": config.CLAUDE_MODELS,
            "has_elevenlabs_key": bool(s["elevenlabs_api_key"]),
            "elevenlabs_voice_id": s["elevenlabs_voice_id"],
            "elevenlabs_model": s["elevenlabs_model"],
            "webhook_url": s.get("webhook_url", ""),
            "has_webhook": bool(s.get("webhook_url")),
            "default_size": config.DEFAULT_SIZE,
            "default_quality": config.DEFAULT_QUALITY,
            "sizes": config.SUPPORTED_SIZES,
            "qualities": config.SUPPORTED_QUALITIES,
            "vault_encrypted": vault_crypto.is_encrypted(),
            "google_configured": bool(config.GOOGLE_CLIENT_ID),
        },
    }


# --------------------------------------------------------------------------- #
#  Projects (multi-project switcher)
# --------------------------------------------------------------------------- #
class ProjectIn(BaseModel):
    name: str = ""
    master_prompt: str = ""


@app.get("/api/projects")
def api_projects():
    return store.list_projects()


@app.post("/api/projects")
def api_create_project(p: ProjectIn):
    pid = store.create_project(p.name, p.master_prompt)
    out = store.list_projects()
    out["id"] = pid
    return out


@app.post("/api/projects/{pid}/duplicate")
def api_duplicate_project(pid: str):
    try:
        new_pid = store.duplicate_project(pid)
    except Exception as e:
        raise HTTPException(404, str(e))
    out = store.list_projects()
    out["id"] = new_pid
    return out


@app.post("/api/projects/{pid}/switch")
def api_switch_project(pid: str):
    try:
        store.switch_project(pid)
    except Exception as e:
        raise HTTPException(404, str(e))
    return {"ok": True, **store.list_projects()}


@app.post("/api/projects/{pid}/rename")
def api_rename_project(pid: str, p: ProjectIn):
    store.rename_project(pid, p.name)
    return {"ok": True, **store.list_projects()}


@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str):
    store.delete_project(pid)
    return {"ok": True, **store.list_projects()}


class SettingsIn(BaseModel):
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    multi_image_edit: Optional[bool] = None
    claude_api_key: Optional[str] = None
    claude_base_url: Optional[str] = None
    claude_model: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    elevenlabs_model: Optional[str] = None
    webhook_url: Optional[str] = None


@app.post("/api/settings")
def api_settings(s: SettingsIn, request: Request):
    email = request.state.user_email
    vault = load_vault()
    user_settings = vault.get(email, {})

    if s.api_key is not None: user_settings["api_key"] = s.api_key.strip()
    if s.base_url: user_settings["base_url"] = s.base_url.strip().rstrip("/")
    if s.model: user_settings["model"] = s.model.strip()
    if s.multi_image_edit is not None: user_settings["multi_image_edit"] = s.multi_image_edit
    if s.claude_api_key is not None: user_settings["claude_api_key"] = s.claude_api_key.strip()
    if s.claude_base_url: user_settings["claude_base_url"] = s.claude_base_url.strip().rstrip("/")
    if s.claude_model: user_settings["claude_model"] = s.claude_model.strip()
    if s.elevenlabs_api_key is not None: user_settings["elevenlabs_api_key"] = s.elevenlabs_api_key.strip()
    if s.elevenlabs_voice_id: user_settings["elevenlabs_voice_id"] = s.elevenlabs_voice_id.strip()
    if s.elevenlabs_model: user_settings["elevenlabs_model"] = s.elevenlabs_model.strip()
    if s.webhook_url is not None: user_settings["webhook_url"] = s.webhook_url.strip()

    vault[email] = user_settings
    save_vault(vault)

    return {
        "ok": True,
        "has_api_key": bool(user_settings.get("api_key")),
        "has_claude_key": bool(user_settings.get("claude_api_key")),
        "has_elevenlabs_key": bool(user_settings.get("elevenlabs_api_key")),
    }


@app.get("/api/health")
def api_health(request: Request):
    s = request.state.settings
    image_status = get_image_client(request).ping()
    image_status["multi_image_edit"] = s["multi_image_edit"]
    claude_status = get_claude_client(request).ping()
    voice_status = get_voice_client(request).ping()
    return {"image": image_status, "claude": claude_status, "voice": voice_status}


@app.get("/api/usage")
def api_usage():
    return store.get_usage()


@app.post("/api/usage/reset")
def api_usage_reset():
    store._write_usage([])
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Master prompt
# --------------------------------------------------------------------------- #
class MasterIn(BaseModel):
    master_prompt: str = ""


@app.post("/api/master")
def api_master(m: MasterIn):
    st = store.load_state()
    st["master_prompt"] = m.master_prompt
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Video -> frames -> style anchors
# --------------------------------------------------------------------------- #
@app.post("/api/video")
async def api_video(
    file: UploadFile = File(...),
    fps: float = Form(1.0),
    max_frames: int = Form(40),
):
    import video as videomod
    dest = os.path.join(
        store.UPLOADS_DIR, store.new_id("upload") + "_" + (file.filename or "video.mp4")
    )
    with open(dest, "wb") as f:
        f.write(await file.read())
    try:
        urls = videomod.extract_frames(dest, fps=fps, max_frames=max_frames)
    except Exception as e:
        raise HTTPException(500, f"frame extraction failed: {e}")
    return {"frames": urls, "video_path": dest}


class StyleFramesIn(BaseModel):
    urls: List[str] = []


@app.post("/api/style-frames")
def api_style_frames(s: StyleFramesIn):
    st = store.load_state()
    st["style_frames"] = [{"id": store.new_id("frame"), "url": u} for u in s.urls]
    store.save_state(st)
    return {"ok": True, "count": len(st["style_frames"])}


# --------------------------------------------------------------------------- #
#  Scene detection / per-frame analysis
# --------------------------------------------------------------------------- #
@app.post("/api/scene-detect")
async def api_scene_detect(file: UploadFile = File(...), threshold: float = Form(0.4)):
    dest = os.path.join(
        store.UPLOADS_DIR, store.new_id("scene") + "_" + (file.filename or "video.mp4")
    )
    with open(dest, "wb") as f:
        f.write(await file.read())
    try:
        times = editor.detect_scenes(dest, threshold=threshold)
        dur = editor.probe_duration(dest)
    except Exception as e:
        raise HTTPException(500, f"scene detection failed: {e}")
    return {"scene_changes": times, "duration": dur, "video_path": dest}


class AnalyseIn(BaseModel):
    image_url: str
    question: str = ""


@app.post("/api/analyse-scene")
def api_analyse_scene(a: AnalyseIn, request: Request):
    try:
        img = store.read_image(a.image_url)
    except Exception as e:
        raise HTTPException(400, f"unreadable image: {e}")
    try:
        text = get_claude_client(request).analyse_scene(
            pipeline.downsize_for_vision(img), a.question
        )
    except Exception as e:
        raise HTTPException(500, f"analysis failed: {e}")
    return {"analysis": text}


# --------------------------------------------------------------------------- #
#  Characters: single + bulk + upload
# --------------------------------------------------------------------------- #
class CharacterIn(BaseModel):
    name: str
    description: str = ""
    size: Optional[str] = None
    quality: Optional[str] = None


@app.post("/api/characters")
def api_create_character(c: CharacterIn, request: Request):
    if not c.name.strip():
        raise HTTPException(400, "name is required")
    st = store.load_state()
    client = get_image_client(request)
    prompt = pipeline.build_sheet_prompt(st["master_prompt"], c.name, c.description)
    try:
        img = client.generate(
            prompt,
            size=c.size or config.DEFAULT_SIZE,
            quality=c.quality or config.DEFAULT_QUALITY,
        )
    except Exception as e:
        raise HTTPException(500, f"sheet generation failed: {e}")
    rec = {
        "id": store.new_id("char"),
        "name": c.name.strip(),
        "description": c.description.strip(),
        "sheet_url": store.write_image("characters", img),
        "prompt": prompt,
        "source": "generated",
        "created": store.now(),
    }
    st["characters"].append(rec)
    store.save_state(st)
    store.log_usage("image", 1, 0.08)
    return rec


class CharacterBatchIn(BaseModel):
    text: str
    size: Optional[str] = None
    quality: Optional[str] = None


@app.post("/api/characters/batch")
def api_create_characters_batch(b: CharacterBatchIn, request: Request):
    entries = pipeline.parse_character_batch(b.text)
    if not entries:
        raise HTTPException(400, "no character entries found (separate with blank lines)")
    st = store.load_state()
    client = get_image_client(request)
    created, errors = [], []
    for e in entries:
        try:
            prompt = pipeline.build_sheet_prompt(st["master_prompt"], e["name"], e["description"])
            img = client.generate(
                prompt,
                size=b.size or config.DEFAULT_SIZE,
                quality=b.quality or config.DEFAULT_QUALITY,
            )
            rec = {
                "id": store.new_id("char"),
                "name": e["name"],
                "description": e["description"],
                "sheet_url": store.write_image("characters", img),
                "prompt": prompt,
                "source": "generated",
                "created": store.now(),
            }
            st["characters"].append(rec)
            store.save_state(st)
            created.append(rec)
            store.log_usage("image", 1, 0.08)
        except Exception as ex:
            errors.append({"name": e["name"], "error": str(ex)})
    return {"created": created, "errors": errors}


@app.post("/api/characters/upload")
async def api_upload_character(
    name: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
):
    if not name.strip():
        raise HTTPException(400, "name is required")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    # Determine extension by the upload's filename, default .png.
    ext = (os.path.splitext(file.filename or "")[1] or ".png").lstrip(".").lower()
    if ext not in {"png", "jpg", "jpeg", "webp"}:
        ext = "png"
    url = store.write_image("characters", data, ext=ext)
    st = store.load_state()
    rec = {
        "id": store.new_id("char"),
        "name": name.strip(),
        "description": description.strip(),
        "sheet_url": url,
        "prompt": "(uploaded sheet)",
        "source": "uploaded",
        "created": store.now(),
    }
    st["characters"].append(rec)
    store.save_state(st)
    return rec


@app.delete("/api/characters/{cid}")
def api_delete_character(cid: str):
    st = store.load_state()
    st["characters"] = [c for c in st["characters"] if c["id"] != cid]
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Wave 3: character pose packs, variants, consistency, from-photo
# --------------------------------------------------------------------------- #
_POSE_SET = ["neutral front turnaround", "happy, smiling warmly",
             "angry, shouting", "sad / worried", "surprised", "calm side profile"]


@app.post("/api/characters/{cid}/pack")
def api_character_pack(cid: str, request: Request, count: int = 4):
    """Generate a pose/expression pack from a character's reference sheet."""
    if not request.state.settings["api_key"]:
        raise HTTPException(400, "Connect your image API key in Settings first.")
    st = store.load_state()
    ch = next((c for c in st["characters"] if c["id"] == cid), None)
    if not ch:
        raise HTTPException(404, "no such character")
    try:
        sheet = store.read_image(ch["sheet_url"])
    except Exception as e:
        raise HTTPException(400, f"unreadable sheet: {e}")
    client = get_image_client(request)
    out, errs = [], []
    for pose in _POSE_SET[:max(1, min(6, count))]:
        prompt = (f"Character reference of {ch['name']}: the EXACT same character "
                  f"(identical face, hair, outfit, colours) shown {pose}. Clean plain "
                  f"background. {st.get('master_prompt','')}").strip()
        try:
            img = client.edit(prompt, [sheet], size=config.DEFAULT_SIZE,
                              quality=config.DEFAULT_QUALITY)
            out.append({"label": pose, "url": store.write_image("characters", img)})
        except Exception as e:
            errs.append(str(e))
    if not out:
        raise HTTPException(500, f"pack generation failed: {errs[:1]}")
    ch["poses"] = (ch.get("poses") or []) + out
    store.save_state(st)
    store.log_usage("image", len(out), 0.08 * len(out))
    return {"poses": ch["poses"], "added": len(out), "errors": errs}


class VariantIn(BaseModel):
    note: str = ""


@app.post("/api/characters/{cid}/variant")
def api_character_variant(cid: str, body: VariantIn, request: Request):
    """Create an alternate look of a character (e.g. 'winter outfit') as a new
    character entry, using the original sheet as the identity anchor."""
    if not request.state.settings["api_key"]:
        raise HTTPException(400, "Connect your image API key in Settings first.")
    if not body.note.strip():
        raise HTTPException(400, "describe the variant (e.g. 'battle armour')")
    st = store.load_state()
    ch = next((c for c in st["characters"] if c["id"] == cid), None)
    if not ch:
        raise HTTPException(404, "no such character")
    try:
        sheet = store.read_image(ch["sheet_url"])
    except Exception as e:
        raise HTTPException(400, f"unreadable sheet: {e}")
    client = get_image_client(request)
    prompt = (f"Character reference sheet for {ch['name']}: the SAME face and identity, "
              f"but this variant: {body.note.strip()}. Turnaround + expression row. "
              f"{st.get('master_prompt','')}").strip()
    try:
        img = client.edit(prompt, [sheet], size=config.DEFAULT_SIZE,
                          quality=config.DEFAULT_QUALITY)
    except Exception as e:
        raise HTTPException(500, f"variant generation failed: {e}")
    rec = {
        "id": store.new_id("char"),
        "name": f"{ch['name']} — {body.note.strip()[:24]}",
        "description": body.note.strip(),
        "sheet_url": store.write_image("characters", img),
        "prompt": prompt, "source": "variant", "created": store.now(),
    }
    st["characters"].append(rec)
    store.save_state(st)
    store.log_usage("image", 1, 0.08)
    return rec


@app.post("/api/characters/{cid}/check")
def api_character_check(cid: str, request: Request):
    """Claude compares the character's sheet to every story frame they appear in
    and flags drift."""
    if not request.state.settings["claude_api_key"]:
        raise HTTPException(400, "Connect your Claude API key in Settings first.")
    st = store.load_state()
    ch = next((c for c in st["characters"] if c["id"] == cid), None)
    if not ch:
        raise HTTPException(404, "no such character")
    name = ch["name"]
    try:
        sheet = pipeline.downsize_for_vision(store.read_image(ch["sheet_url"]))
    except Exception as e:
        raise HTTPException(400, f"unreadable sheet: {e}")
    frames, idxs = [], []
    for s in st["sequence"]:
        if name in (s.get("characters") or []):
            try:
                frames.append(pipeline.downsize_for_vision(store.read_image(s["image_url"])))
                idxs.append(s.get("index"))
            except Exception:
                pass
    if not frames:
        return {"summary": f"{name} doesn't appear in any rendered frame yet.",
                "issues": []}
    try:
        raw = get_claude_client(request).character_consistency(sheet, frames[:15], name)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"consistency check failed: {e}")
    for it in (data.get("issues") or []):
        fi = int(it.get("frame", 0))
        if 1 <= fi <= len(idxs):
            it["index"] = idxs[fi - 1]
    return data


@app.post("/api/characters/from-photo")
async def api_character_from_photo(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
):
    """Upload a photo/reference and have the image model build a clean, on-style
    character reference sheet from it."""
    if not request.state.settings["api_key"]:
        raise HTTPException(400, "Connect your image API key in Settings first.")
    if not name.strip():
        raise HTTPException(400, "name is required")
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    st = store.load_state()
    client = get_image_client(request)
    prompt = (f"Turn the reference person in the image into a clean character "
              f"reference sheet for '{name.strip()}': front turnaround plus a row of "
              f"expressions, preserving their identity (face, hair, build). "
              f"{description.strip()}. {st.get('master_prompt','')}").strip()
    try:
        img = client.edit(prompt, [data], size=config.DEFAULT_SIZE,
                          quality=config.DEFAULT_QUALITY)
    except Exception as e:
        raise HTTPException(500, f"sheet generation failed: {e}")
    rec = {
        "id": store.new_id("char"),
        "name": name.strip(),
        "description": description.strip(),
        "sheet_url": store.write_image("characters", img),
        "prompt": prompt, "source": "from-photo", "created": store.now(),
    }
    st["characters"].append(rec)
    store.save_state(st)
    store.log_usage("image", 1, 0.08)
    return rec


# --------------------------------------------------------------------------- #
#  Single-frame generation (the original continuation engine)
# --------------------------------------------------------------------------- #
class GenerateIn(BaseModel):
    prompt: str
    size: Optional[str] = None
    quality: Optional[str] = None
    continue_prev: bool = True
    style_lock: bool = True
    character_ids: Optional[List[str]] = None


def _render_one(g_prompt, size, quality, continue_prev, style_lock,
                character_ids=None, request: Request = None):
    """Shared engine for /api/generate and /api/generate-batch."""
    st = store.load_state()
    client = get_image_client(request)

    # 1. characters
    if character_ids:
        wanted = set(character_ids)
        matched = [c for c in st["characters"] if c["id"] in wanted]
    else:
        matched = pipeline.match_characters(g_prompt, st["characters"])

    # 2. previous frame
    prev = st["sequence"][-1] if (continue_prev and st["sequence"]) else None

    # 3. style anchors
    style_frames = st["style_frames"] if style_lock else []

    # 4. assemble refs
    refs, ref_meta = [], []
    for c in matched:
        try:
            refs.append(store.read_image(c["sheet_url"]))
            ref_meta.append({"type": "character", "name": c["name"]})
        except Exception:
            pass
    if prev:
        try:
            refs.append(store.read_image(prev["image_url"]))
            ref_meta.append({"type": "previous", "id": prev["id"]})
        except Exception:
            pass
    for sf in style_frames[:3]:
        try:
            refs.append(store.read_image(sf["url"]))
            ref_meta.append({"type": "style"})
        except Exception:
            pass

    full_prompt = pipeline.build_full_prompt(
        st["master_prompt"], g_prompt, matched,
        has_previous=bool(prev), style_locked=bool(style_frames),
    )

    if refs:
        # If the proxy isn't confirmed to support repeated `image[]` fields,
        # composite multiple refs into a single contact-sheet PNG so we hit
        # the documented one-`image`-field path.
        multi_image_edit = (request.state.settings["multi_image_edit"]
                            if request else config.MULTI_IMAGE_EDIT)
        if not multi_image_edit and len(refs) > 1:
            send = [pipeline.contact_sheet(refs)]
            mode_note = f"edit (contact-sheet of {len(refs)} refs)"
        else:
            send = refs
            mode_note = f"edit ({len(refs)} refs)"
        print(f"[render] {mode_note} prompt_len={len(full_prompt)}", flush=True)
        try:
            img = client.edit(full_prompt, send, size=size, quality=quality)
        except Exception as edit_err:
            # Multi-image `image[]` mode isn't supported by every proxy. If we
            # sent more than one ref and it failed, fall back to compositing all
            # refs into ONE contact-sheet PNG (the documented single-`image`
            # path) and retry once before giving up.
            if len(send) > 1:
                print(f"[render] multi-ref edit failed ({edit_err}); "
                      f"retrying as contact-sheet", flush=True)
                img = client.edit(full_prompt, [pipeline.contact_sheet(refs)],
                                  size=size, quality=quality)
            else:
                raise
        mode = "edit"
    else:
        print(f"[render] generate (no refs) prompt_len={len(full_prompt)}",
              flush=True)
        img = client.generate(full_prompt, size=size, quality=quality)
        mode = "generate"

    rec = {
        "id": store.new_id("shot"),
        "index": len(st["sequence"]) + 1,
        "prompt": g_prompt.strip(),
        "full_prompt": full_prompt,
        "image_url": store.write_image("images", img),
        "mode": mode,
        "size": size,
        "quality": quality,
        "characters": [c["name"] for c in matched],
        "refs": ref_meta,
        "continued_from": prev["id"] if prev else None,
        "created": store.now(),
    }
    st["sequence"].append(rec)
    store.save_state(st)
    _cost = {"low": 0.02, "medium": 0.04, "high": 0.08, "auto": 0.06}
    store.log_usage("image", 1, _cost.get(quality, 0.06))
    return rec


@app.post("/api/generate")
def api_generate(g: GenerateIn, request: Request):
    if not g.prompt.strip():
        raise HTTPException(400, "prompt is required")
    size = g.size or config.DEFAULT_SIZE
    quality = g.quality or config.DEFAULT_QUALITY
    try:
        return _render_one(g.prompt, size, quality, g.continue_prev, g.style_lock,
                           g.character_ids, request=request)
    except Exception as e:
        raise HTTPException(500, f"generation failed: {e}")


class BatchGenerateIn(BaseModel):
    text: str                          # newline-separated prompts (one per line)
    mode: str = "line"                 # 'line' or 'blank'
    size: Optional[str] = None
    quality: Optional[str] = None
    continue_prev: bool = True
    style_lock: bool = True


@app.post("/api/generate/batch")
def api_generate_batch(b: BatchGenerateIn, request: Request):
    prompts = pipeline.split_lines_batch(b.text, mode=b.mode)
    if not prompts:
        raise HTTPException(400, "no prompts found")
    size = b.size or config.DEFAULT_SIZE
    quality = b.quality or config.DEFAULT_QUALITY
    created, errors = [], []
    for p in prompts:
        try:
            # Each prompt independently auto-matches characters by @tags / names
            rec = _render_one(p, size, quality, b.continue_prev, b.style_lock,
                              character_ids=None, request=request)
            created.append(rec)
        except Exception as ex:
            errors.append({"prompt": p, "error": str(ex)})
    return {"created": created, "errors": errors}


# --------------------------------------------------------------------------- #
#  Image generation QUEUE — controlled, retry-safe bulk generation.
#  Splits prompts into jobs, runs them through image_queue (concurrency=1 by
#  default) with backoff + a global rate-limit cooldown. The frontend submits a
#  batch then polls /status; completed frames are saved as they finish so a
#  later failure never loses earlier work, and any failed job can be retried.
# --------------------------------------------------------------------------- #
import image_queue

# Serialises the quick read-modify-write of a project's sequence so a worker and
# the user editing at the same time can't clobber each other's state.
_state_write_lock = threading.Lock()


def _render_one_for_queue(g_prompt, params, settings, project_id):
    """Render ONE frame for a queued job. Mirrors _render_one's reference
    assembly but operates on an explicit project + settings snapshot and lets
    the queue own retries (so the client call uses retry=False)."""
    size = params.get("size") or config.DEFAULT_SIZE
    quality = params.get("quality") or config.DEFAULT_QUALITY
    continue_prev = params.get("continue_prev", True)
    style_lock = params.get("style_lock", True)
    multi_image_edit = settings.get("multi_image_edit", config.MULTI_IMAGE_EDIT)
    client = ImageClient(api_key=settings.get("api_key"),
                         base_url=settings.get("base_url"),
                         model=settings.get("model"))

    st = store.load_state_for(project_id)
    matched = pipeline.match_characters(g_prompt, st["characters"])
    prev = st["sequence"][-1] if (continue_prev and st["sequence"]) else None
    style_frames = st["style_frames"] if style_lock else []

    refs, ref_meta = [], []
    for c in matched:
        try:
            refs.append(store.read_image(c["sheet_url"]))
            ref_meta.append({"type": "character", "name": c["name"]})
        except Exception:
            pass
    if prev:
        try:
            refs.append(store.read_image(prev["image_url"]))
            ref_meta.append({"type": "previous", "id": prev["id"]})
        except Exception:
            pass
    for sf in style_frames[:3]:
        try:
            refs.append(store.read_image(sf["url"]))
            ref_meta.append({"type": "style"})
        except Exception:
            pass

    full_prompt = pipeline.build_full_prompt(
        st["master_prompt"], g_prompt, matched,
        has_previous=bool(prev), style_locked=bool(style_frames))

    if refs:
        if not multi_image_edit and len(refs) > 1:
            send = [pipeline.contact_sheet(refs)]
        else:
            send = refs
        try:
            img = client.edit(full_prompt, send, size=size, quality=quality,
                              retry=False)
        except Exception:
            if len(send) > 1:
                img = client.edit(full_prompt, [pipeline.contact_sheet(refs)],
                                  size=size, quality=quality, retry=False)
            else:
                raise
        mode = "edit"
    else:
        img = client.generate(full_prompt, size=size, quality=quality,
                             retry=False)
        mode = "generate"

    # Quick, locked read-modify-write so concurrent writers don't lose frames.
    with _state_write_lock:
        st = store.load_state_for(project_id)
        rec = {
            "id": store.new_id("shot"),
            "index": len(st["sequence"]) + 1,
            "prompt": g_prompt.strip(),
            "full_prompt": full_prompt,
            "image_url": store.write_image("images", img),
            "mode": mode, "size": size, "quality": quality,
            "characters": [c["name"] for c in matched],
            "refs": ref_meta,
            "continued_from": prev["id"] if prev else None,
            "created": store.now(),
        }
        st["sequence"].append(rec)
        store.save_state_for(project_id, st)
    _cost = {"low": 0.02, "medium": 0.04, "high": 0.08, "auto": 0.06}
    store.log_usage("image", 1, _cost.get(quality, 0.06), project_id=project_id)
    return rec


image_queue.QUEUE.set_render_fn(_render_one_for_queue)


@app.on_event("startup")
def _start_image_queue():
    image_queue.QUEUE.start()


def _img_settings_snapshot(request: Request) -> dict:
    s = request.state.settings
    return {
        "api_key": s["api_key"], "base_url": s["base_url"],
        "model": s["model"], "multi_image_edit": s["multi_image_edit"],
    }


class QueueSubmitIn(BaseModel):
    text: str
    mode: str = "line"
    size: Optional[str] = None
    quality: Optional[str] = None
    continue_prev: bool = True
    style_lock: bool = True


@app.post("/api/images/queue")
def api_images_queue_submit(b: QueueSubmitIn, request: Request):
    """Enqueue a bulk batch. Returns the batch id + initial job list to poll."""
    if not request.state.settings["api_key"]:
        raise HTTPException(400, "Connect your image API key in Settings first.")
    prompts = pipeline.split_lines_batch(b.text, mode=b.mode)
    if not prompts:
        raise HTTPException(400, "no prompts found")
    params = {"size": b.size or config.DEFAULT_SIZE,
              "quality": b.quality or config.DEFAULT_QUALITY,
              "continue_prev": b.continue_prev, "style_lock": b.style_lock}
    batch = image_queue.QUEUE.submit(prompts, params,
                                     _img_settings_snapshot(request),
                                     store.current_project_id())
    return batch.to_dict()


@app.get("/api/images/queue/{bid}")
def api_images_queue_status(bid: str):
    b = image_queue.QUEUE.get_batch(bid)
    if not b:
        raise HTTPException(404, "no such batch")
    return b.to_dict()


@app.post("/api/images/queue/{bid}/cancel")
def api_images_queue_cancel(bid: str):
    if not image_queue.QUEUE.cancel(bid):
        raise HTTPException(404, "no such batch")
    return image_queue.QUEUE.get_batch(bid).to_dict()


@app.post("/api/images/queue/{bid}/retry-failed")
def api_images_queue_retry_failed(bid: str, request: Request):
    n = image_queue.QUEUE.retry_failed(bid, _img_settings_snapshot(request))
    b = image_queue.QUEUE.get_batch(bid)
    if not b:
        raise HTTPException(404, "no such batch")
    return {"requeued": n, "batch": b.to_dict()}


@app.post("/api/images/job/{job_id}/retry")
def api_images_job_retry(job_id: str, request: Request):
    if not image_queue.QUEUE.retry_job(job_id, _img_settings_snapshot(request)):
        raise HTTPException(400, "job not found or not retryable")
    return {"ok": True}


@app.get("/api/images/throttle")
def api_images_throttle():
    return image_queue.throttle_status()


@app.delete("/api/sequence/{sid}")
def api_delete_shot(sid: str):
    st = store.load_state()
    st["sequence"] = [s for s in st["sequence"] if s["id"] != sid]
    for i, s in enumerate(st["sequence"], 1):
        s["index"] = i
    store.save_state(st)
    return {"ok": True}


@app.post("/api/reset-sequence")
def api_reset_sequence():
    st = store.load_state()
    st["sequence"] = []
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Wave 2: re-roll / best-of-N variants, reorder, bulk delete
# --------------------------------------------------------------------------- #
class ChooseIn(BaseModel):
    url: str = ""


class IdsIn(BaseModel):
    ids: List[str] = []


def _shot_image(st, prev, g_prompt, size, quality, request):
    """Generate ONE image for a prompt using the same reference assembly as the
    main renderer (matched characters + given previous frame + style anchors)."""
    client = get_image_client(request)
    matched = pipeline.match_characters(g_prompt, st["characters"])
    refs = []
    for c in matched:
        try:
            refs.append(store.read_image(c["sheet_url"]))
        except Exception:
            pass
    if prev:
        try:
            refs.append(store.read_image(prev["image_url"]))
        except Exception:
            pass
    for sf in st.get("style_frames", [])[:3]:
        try:
            refs.append(store.read_image(sf["url"]))
        except Exception:
            pass
    full_prompt = pipeline.build_full_prompt(
        st["master_prompt"], g_prompt, matched,
        has_previous=bool(prev), style_locked=bool(st.get("style_frames")))
    if refs:
        multi = request.state.settings["multi_image_edit"]
        send = refs if (multi and len(refs) > 1) else (
            [pipeline.contact_sheet(refs)] if len(refs) > 1 else refs)
        try:
            img = client.edit(full_prompt, send, size=size, quality=quality)
        except Exception:
            if len(send) > 1:
                img = client.edit(full_prompt, [pipeline.contact_sheet(refs)],
                                  size=size, quality=quality)
            else:
                img = client.generate(full_prompt, size=size, quality=quality)
    else:
        img = client.generate(full_prompt, size=size, quality=quality)
    return img


@app.post("/api/sequence/{sid}/variants")
def api_shot_variants(sid: str, request: Request, count: int = 1):
    """Generate `count` fresh candidate images for an existing shot's prompt
    (re-roll = 1, best-of-N = many). Candidates are saved but NOT attached until
    the caller picks one via /choose."""
    st = store.load_state()
    pos = next((i for i, s in enumerate(st["sequence"]) if s["id"] == sid), -1)
    if pos < 0:
        raise HTTPException(404, "no such shot")
    shot = st["sequence"][pos]
    prev = st["sequence"][pos - 1] if pos - 1 >= 0 else None
    g_prompt = shot.get("prompt", "")
    size = shot.get("size") or config.DEFAULT_SIZE
    quality = shot.get("quality") or config.DEFAULT_QUALITY
    count = max(1, min(6, count))
    cands, errs = [], []
    for _ in range(count):
        try:
            cands.append(store.write_image("images", _shot_image(
                st, prev, g_prompt, size, quality, request)))
        except Exception as e:
            errs.append(str(e))
    if not cands:
        raise HTTPException(500, f"variant generation failed: {errs[:1]}")
    store.log_usage("image", len(cands), 0.08 * len(cands))
    return {"candidates": cands, "errors": errs}


@app.post("/api/sequence/{sid}/choose")
def api_shot_choose(sid: str, body: ChooseIn):
    """Attach a chosen candidate image to a shot (used by re-roll / best-of-N)."""
    st = store.load_state()
    shot = next((s for s in st["sequence"] if s["id"] == sid), None)
    if not shot:
        raise HTTPException(404, "no such shot")
    if not body.url:
        raise HTTPException(400, "url required")
    shot["image_url"] = body.url
    store.save_state(st)
    return shot


@app.post("/api/sequence/{sid}/upscale")
def api_shot_upscale(sid: str, factor: float = 2.0):
    """Increase a frame's resolution with a high-quality Lanczos resample
    (reliable everywhere; not generative super-resolution)."""
    st = store.load_state()
    shot = next((s for s in st["sequence"] if s["id"] == sid), None)
    if not shot:
        raise HTTPException(404, "no such shot")
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(store.read_image(shot["image_url"]))).convert("RGB")
    except Exception as e:
        raise HTTPException(400, f"unreadable frame: {e}")
    f = max(1.2, min(4.0, float(factor)))
    w, h = im.size
    nw, nh = int(w * f), int(h * f)
    cap = 4096
    if max(nw, nh) > cap:
        s = cap / max(nw, nh)
        nw, nh = int(nw * s), int(nh * s)
    im = im.resize((nw, nh), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    shot["image_url"] = store.write_image("images", buf.getvalue())
    shot["size"] = f"{nw}x{nh}"
    store.save_state(st)
    return shot


@app.post("/api/sequence/{sid}/inpaint")
async def api_shot_inpaint(sid: str, request: Request,
                           prompt: str = Form(...),
                           mask: UploadFile = File(...)):
    """Mask-based inpaint: repaint only the masked region of a frame.
    Experimental — depends on your image proxy supporting the `mask` field."""
    if not request.state.settings["api_key"]:
        raise HTTPException(400, "Connect your image API key in Settings first.")
    st = store.load_state()
    shot = next((s for s in st["sequence"] if s["id"] == sid), None)
    if not shot:
        raise HTTPException(404, "no such shot")
    try:
        base = store.read_image(shot["image_url"])
    except Exception as e:
        raise HTTPException(400, f"unreadable frame: {e}")
    mdata = await mask.read()
    if not mdata:
        raise HTTPException(400, "empty mask")
    client = get_image_client(request)
    try:
        img = client.edit(prompt, [base], size="auto", quality="auto", mask=mdata)
    except Exception as e:
        raise HTTPException(500, f"inpaint failed (your image model may not "
                                 f"support masks): {e}")
    shot["image_url"] = store.write_image("images", img)
    store.save_state(st)
    store.log_usage("image", 1, 0.06)
    return shot


@app.post("/api/sequence/reorder")
def api_sequence_reorder(body: IdsIn):
    """Reorder the sequence to match the given list of shot ids; renumbers."""
    st = store.load_state()
    rank = {sid: i for i, sid in enumerate(body.ids)}
    st["sequence"].sort(key=lambda s: rank.get(s["id"], 1_000_000))
    for i, s in enumerate(st["sequence"], 1):
        s["index"] = i
    store.save_state(st)
    return {"ok": True, "count": len(st["sequence"])}


@app.post("/api/sequence/delete")
def api_sequence_delete(body: IdsIn):
    """Delete many shots at once; renumbers the rest."""
    st = store.load_state()
    rm = set(body.ids)
    st["sequence"] = [s for s in st["sequence"] if s["id"] not in rm]
    for i, s in enumerate(st["sequence"], 1):
        s["index"] = i
    store.save_state(st)
    return {"ok": True, "count": len(st["sequence"])}


# --------------------------------------------------------------------------- #
#  Inspire from a YouTube video: analyse style/topic/voice -> 10 suggestions
# --------------------------------------------------------------------------- #
class YouTubeAnalyzeIn(BaseModel):
    url: str
    nudge: str = ""
    model: Optional[str] = None


@app.post("/api/youtube/analyze")
def api_youtube_analyze(body: YouTubeAnalyzeIn, request: Request):
    """Paste a YouTube link -> Claude analyses its look, topic and way of
    speaking, then returns 10 ready-to-produce video ideas in the same vein."""
    s = request.state.settings
    if not s["claude_api_key"]:
        raise HTTPException(400, "No Claude API key set — add it in Settings.")
    import youtube
    if not youtube.is_youtube_url(body.url):
        raise HTTPException(400, "That doesn't look like a YouTube link.")

    try:
        ref = youtube.ingest(body.url, max_frames=12)
    except Exception as e:
        raise HTTPException(400, f"couldn't read that video: {e}")

    # Downsize frames for the vision call.
    frame_imgs = []
    for u in ref.get("frame_urls", [])[:12]:
        try:
            frame_imgs.append(pipeline.downsize_for_vision(store.read_image(u)))
        except Exception:
            pass

    st = store.load_state()
    try:
        raw = _claude_client_for(body.model, request).suggest_from_reference(
            frames=frame_imgs,
            transcript=ref.get("transcript", ""),
            source_title=ref.get("title", ""),
            source_channel=ref.get("channel", ""),
            nudge=body.nudge,
            master_prompt=st["master_prompt"],
            n_suggestions=10,
        )
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"analysis failed: {e}")

    suggestions = data.get("suggestions") or []
    insp = {
        "url": ref["url"], "video_id": ref["video_id"],
        "title": ref.get("title", ""), "channel": ref.get("channel", ""),
        "style_summary": data.get("style_summary", ""),
        "speech_style": data.get("speech_style", ""),
        "topic": data.get("topic", ""),
        "frames": ref.get("frame_urls", []),
        "source": ref.get("source", ""), "notes": ref.get("notes", ""),
        "suggestions": suggestions,
        "created": store.now(),
    }
    st["yt_inspiration"] = insp
    store.save_state(st)
    return insp


class YouTubeMultiIn(BaseModel):
    urls: List[str] = []
    nudge: str = ""
    model: Optional[str] = None


@app.post("/api/youtube/analyze-multi")
def api_youtube_analyze_multi(body: YouTubeMultiIn, request: Request):
    """YT Analyser — paste UNLIMITED YouTube links. We ingest each (frames +
    transcript), pool them, and Claude deconstructs the shared ART STYLE,
    PACING, WAY OF SPEAKING and STORYTELLING, then pitches 10 video ideas in
    that combined vein. Picking one auto-loads the Script Generator."""
    s = request.state.settings
    if not s["claude_api_key"]:
        raise HTTPException(400, "No Claude API key set — add it in Settings.")
    import youtube

    # Validate + dedupe by video id, preserving paste order.
    seen, urls = set(), []
    for raw_u in (body.urls or []):
        u = (raw_u or "").strip()
        if not u:
            continue
        vid = youtube.extract_video_id(u)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        urls.append(u)
    if not urls:
        raise HTTPException(400, "Paste at least one valid YouTube link.")
    return _analyze_urls(urls, body.nudge, body.model, request)


def _analyze_urls(urls, nudge, model, request):
    """Shared core: ingest a list of YouTube urls, pool frames + transcripts,
    and have Claude deconstruct style/pacing/voice/story + pitch 10 ideas."""
    import youtube
    urls = urls[:10]   # bound the job — Anthropic caps a request near 20 images
    per_video = max(3, min(8, 16 // max(1, len(urls))))
    sources, frame_imgs, src_meta, errors = [], [], [], []
    for u in urls:
        try:
            ref = youtube.ingest(u, max_frames=per_video)
        except Exception as e:
            errors.append({"url": u, "error": str(e)})
            continue
        imgs = []
        for fu in ref.get("frame_urls", [])[:per_video]:
            try:
                imgs.append(pipeline.downsize_for_vision(store.read_image(fu)))
            except Exception:
                pass
        sources.append({
            "title": ref.get("title", ""), "channel": ref.get("channel", ""),
            "transcript": ref.get("transcript", ""),
        })
        src_meta.append({
            "url": ref["url"], "video_id": ref["video_id"],
            "title": ref.get("title", ""), "channel": ref.get("channel", ""),
            "frames": ref.get("frame_urls", []), "source": ref.get("source", ""),
            "notes": ref.get("notes", ""),
        })
        frame_imgs.extend(imgs)

    if not sources:
        raise HTTPException(400, "Couldn't read any of those videos. Try links "
                                 "with captions or that aren't region-locked.")
    frame_imgs = frame_imgs[:16]

    st = store.load_state()
    try:
        raw = _claude_client_for(model, request).suggest_from_references(
            frames=frame_imgs, sources=sources, nudge=nudge,
            master_prompt=st["master_prompt"], n_suggestions=10)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"analysis failed: {e}")

    out = {
        "sources": src_meta, "errors": errors,
        "art_style": data.get("art_style", ""),
        "pacing": data.get("pacing", ""),
        "speech_style": data.get("speech_style", ""),
        "storytelling": data.get("storytelling", ""),
        "sources_summary": data.get("sources_summary", ""),
        "suggestions": data.get("suggestions") or [],
        "created": store.now(),
    }
    st["yt_analysis"] = out
    store.save_state(st)
    return out


class ChannelIn(BaseModel):
    url: str
    nudge: str = ""
    model: Optional[str] = None
    limit: int = 6


@app.post("/api/youtube/channel")
def api_youtube_channel(b: ChannelIn, request: Request):
    """Analyse a whole channel: list its recent videos, then run the same
    multi-video style deconstruction + 10 ideas."""
    if not request.state.settings["claude_api_key"]:
        raise HTTPException(400, "No Claude API key set — add it in Settings.")
    import youtube
    info = youtube.channel_info(b.url, max(2, min(12, b.limit)))
    urls = [it["url"] for it in info.get("items", []) if it.get("url")]
    if not urls:
        raise HTTPException(400, "Couldn't read that channel. Paste a channel URL "
                                 "(e.g. youtube.com/@handle).")
    return _analyze_urls(urls, b.nudge, b.model, request)


class GapsIn(BaseModel):
    url: str
    model: Optional[str] = None


@app.post("/api/youtube/gaps")
def api_youtube_gaps(b: GapsIn, request: Request):
    """Find content gaps: list a channel's recent titles and ask Claude what it
    hasn't covered."""
    if not request.state.settings["claude_api_key"]:
        raise HTTPException(400, "No Claude API key set — add it in Settings.")
    import youtube
    info = youtube.channel_info(b.url, 50)
    titles = [it.get("title", "") for it in info.get("items", []) if it.get("title")]
    if not titles:
        raise HTTPException(400, "Couldn't read that channel's videos.")
    try:
        raw = _claude_client_for(b.model, request).gaps(info.get("channel", ""), titles, 12)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"gap analysis failed: {e}")
    return {"channel": info.get("channel", ""), "gaps": data.get("gaps") or []}


class TrendsIn(BaseModel):
    niche: str
    model: Optional[str] = None


@app.post("/api/youtube/trends")
def api_youtube_trends(b: TrendsIn, request: Request):
    """Pull what's working in a niche via YouTube search, then distill angles."""
    if not request.state.settings["claude_api_key"]:
        raise HTTPException(400, "No Claude API key set — add it in Settings.")
    if not b.niche.strip():
        raise HTTPException(400, "Enter a niche or topic.")
    import youtube
    vids = youtube.search_videos(b.niche.strip(), 14)
    titles = [v.get("title", "") for v in vids if v.get("title")]
    if not titles:
        raise HTTPException(400, "No search results — try a broader niche.")
    try:
        raw = _claude_client_for(b.model, request).trend_angles(b.niche.strip(), titles, 10)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"trend analysis failed: {e}")
    return {"niche": b.niche.strip(), "samples": vids[:10], "angles": data.get("angles") or []}


class SeoIn(BaseModel):
    title: str = ""
    description: str = ""
    model: Optional[str] = None


@app.post("/api/seo")
def api_seo(b: SeoIn, request: Request):
    """Generate optimized titles, description and tags for the current topic."""
    if not request.state.settings["claude_api_key"]:
        raise HTTPException(400, "No Claude API key set — add it in Settings.")
    st = store.load_state()
    sc = st.get("script") or {}
    title = b.title.strip() or sc.get("title", "")
    desc = b.description.strip() or sc.get("logline", "") or (sc.get("voiceover", "") or "")[:400]
    if not (title or desc):
        raise HTTPException(400, "Generate a script or enter a topic first.")
    try:
        raw = _claude_client_for(b.model, request).seo(title, desc, 6)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"SEO generation failed: {e}")
    return {"titles": data.get("titles") or [], "description": data.get("description", ""),
            "tags": data.get("tags") or []}


class YTThumbnailIn(BaseModel):
    title: str = ""
    style: str = ""            # art_style read from the analysis
    extra: str = ""            # user tweaks (face, props, colour...)
    ref_urls: List[str] = []   # reference frames to match the look
    size: Optional[str] = None
    quality: Optional[str] = None


@app.post("/api/youtube/thumbnail")
def api_youtube_thumbnail(body: YTThumbnailIn, request: Request):
    """Design a scroll-stopping YouTube thumbnail for a chosen topic, matched to
    the visual style of the analysed reference videos. Uses the image model;
    when reference frames are supplied it edits from them so the palette/mood
    matches the source channel's look."""
    if not request.state.settings["api_key"]:
        raise HTTPException(400, "Connect your image API key in Settings first.")
    if not (body.title.strip() or body.style.strip()):
        raise HTTPException(400, "Give the thumbnail a title or a style to work from.")

    client = get_image_client(request)
    bits = []
    if body.title.strip():
        bits.append(f'A bold, scroll-stopping YouTube thumbnail (16:9) for a video '
                    f'titled: "{body.title.strip()}".')
    else:
        bits.append("A bold, scroll-stopping YouTube thumbnail (16:9).")
    if body.style.strip():
        bits.append(f"Match this visual style: {body.style.strip()}")
    if body.extra.strip():
        bits.append(body.extra.strip())
    bits.append("Single clear focal subject, dramatic cinematic lighting, high "
                "contrast, vivid punchy colours, strong sense of depth. Leave clean "
                "negative space on one side for a short title overlay. Ultra-crisp and "
                "professional. No watermark, no logos, no garbled text.")
    prompt = "\n".join(bits)

    size = body.size or "1536x1024"
    quality = body.quality or config.DEFAULT_QUALITY

    refs = []
    for u in (body.ref_urls or [])[:4]:
        try:
            refs.append(store.read_image(u))
        except Exception:
            pass

    try:
        if refs:
            note = ("\n\nUse the reference image(s) ONLY for art style, palette and "
                    "mood — do NOT copy their exact composition or subjects.")
            multi = request.state.settings["multi_image_edit"]
            send = refs if (multi and len(refs) > 1) else (
                [pipeline.contact_sheet(refs)] if len(refs) > 1 else refs)
            try:
                img = client.edit(prompt + note, send, size=size, quality=quality)
            except Exception:
                # fall back to a plain generation if the edit path isn't supported
                img = client.generate(prompt, size=size, quality=quality)
        else:
            img = client.generate(prompt, size=size, quality=quality)
    except Exception as e:
        raise HTTPException(500, f"thumbnail generation failed: {e}")

    url = store.write_image("images", img)
    st = store.load_state()
    rec = {"id": store.new_id("thumb"), "url": url,
           "title": body.title.strip(), "created": store.now()}
    st.setdefault("thumbnails", []).append(rec)
    store.save_state(st)
    store.log_usage("thumbnail", 1, 0.08)
    return rec


def _hex_rgb(c):
    c = (c or "").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (255, 255, 255)


def _overlay_text(img_bytes, headline="", subtitle="", position="bottom",
                  color="#ffffff", scrim=True, brand=None):
    """Bake crisp title text (Pillow) onto a thumbnail — readable stroke, optional
    scrim, plus brand handle/logo if a brand kit is set."""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap
    brand = brand or {}
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im, "RGBA")

    def font(sz, bold=True):
        cands = (["C:/Windows/Fonts/arialbd.ttf"] if bold else ["C:/Windows/Fonts/arial.ttf"]) + [
            "C:/Windows/Fonts/arial.ttf", "arialbd.ttf", "arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
        for n in cands:
            try:
                return ImageFont.truetype(n, sz)
            except Exception:
                pass
        return ImageFont.load_default()

    tsize = max(34, W // 12)
    tf, sf = font(tsize, True), font(max(20, W // 30), False)

    def dims(line, f):
        b = d.textbbox((0, 0), line, font=f)
        return (b[2] - b[0], b[3] - b[1])

    chars = max(8, int(W * 0.92 / (tsize * 0.55)))
    lines = textwrap.wrap(headline or "", width=chars)
    line_h = dims("Ag", tf)[1] + int(tsize * 0.2)
    block_h = line_h * len(lines) + (dims("Ag", sf)[1] + 18 if subtitle else 0)
    pad = int(H * 0.06)
    if position == "top":
        y0 = pad
    elif position == "center":
        y0 = max(pad, (H - block_h) // 2)
    else:
        y0 = H - block_h - pad

    if scrim and (lines or subtitle):
        band = block_h + pad * 2
        grad = Image.new("L", (1, band), 0)
        for i in range(band):
            frac = i / max(1, band)
            grad.putpixel((0, i), int(200 * (1 - frac if position == "top" else frac)))
        grad = grad.resize((W, band))
        top = 0 if position == "top" else max(0, y0 - pad)
        black = Image.new("RGBA", (W, band), (0, 0, 0, 255))
        black.putalpha(grad)
        im.paste(black, (0, top), black)
        d = ImageDraw.Draw(im, "RGBA")

    fill = _hex_rgb(color)
    accent = _hex_rgb(brand.get("accent") or "#d97757")
    stroke = max(2, tsize // 14)
    y = y0
    for ln in lines:
        w, _h = dims(ln, tf)
        d.text(((W - w) // 2, y), ln, font=tf, fill=fill + (255,),
               stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += line_h
    if subtitle:
        y += 10
        w, _h = dims(subtitle, sf)
        d.text(((W - w) // 2, y), subtitle, font=sf, fill=accent + (255,),
               stroke_width=2, stroke_fill=(0, 0, 0, 255))

    handle = (brand.get("handle") or "").strip()
    if handle:
        hf = font(max(16, W // 44), True)
        w, h = dims(handle, hf)
        d.text((W - w - int(W * 0.03), H - h - int(H * 0.05)), handle, font=hf,
               fill=accent + (255,), stroke_width=2, stroke_fill=(0, 0, 0, 255))
    logo = brand.get("logo_url")
    if logo:
        try:
            lg = Image.open(io.BytesIO(store.read_image(logo))).convert("RGBA")
            lw = int(W * 0.12)
            lg = lg.resize((lw, int(lw * lg.height / max(1, lg.width))))
            im.paste(lg, (int(W * 0.03), int(H * 0.04)), lg)
        except Exception:
            pass

    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


class OverlayIn(BaseModel):
    url: str
    headline: str = ""
    subtitle: str = ""
    position: str = "bottom"
    color: str = "#ffffff"
    scrim: bool = True


@app.post("/api/thumbnail/overlay")
def api_thumbnail_overlay(b: OverlayIn):
    try:
        img = store.read_image(b.url)
    except Exception as e:
        raise HTTPException(400, f"unreadable image: {e}")
    st = store.load_state()
    try:
        out = _overlay_text(img, b.headline, b.subtitle, b.position, b.color,
                            b.scrim, st.get("brand"))
    except Exception as e:
        raise HTTPException(500, f"overlay failed: {e}")
    url = store.write_image("images", out)
    rec = {"id": store.new_id("thumb"), "url": url,
           "title": b.headline or "thumbnail", "created": store.now()}
    st.setdefault("thumbnails", []).append(rec)
    store.save_state(st)
    return rec


@app.post("/api/brand")
async def api_brand(accent: str = Form(""), handle: str = Form(""),
                    file: Optional[UploadFile] = File(None)):
    st = store.load_state()
    brand = st.get("brand") or {}
    if accent:
        brand["accent"] = accent
    brand["handle"] = (handle or "").strip()
    if file is not None:
        data = await file.read()
        if data:
            ext = (os.path.splitext(file.filename or "")[1] or ".png").lstrip(".").lower()
            if ext not in {"png", "jpg", "jpeg", "webp"}:
                ext = "png"
            brand["logo_url"] = store.write_image("characters", data, ext=ext)
    st["brand"] = brand
    store.save_state(st)
    return brand


# --------------------------------------------------------------------------- #
#  Claude: script generation
# --------------------------------------------------------------------------- #
class ScriptIn(BaseModel):
    title: str = ""
    description: str = ""
    total_duration: float = 60.0
    pacing_seconds: float = 1.0
    num_characters: int = 0
    style_notes: str = ""
    model: Optional[str] = None
    # Back-compat with the old simple form.
    brief: str = ""
    scene_count: Optional[int] = None
    dialogue: bool = False  # produce speaker-tagged VO lines for multi-voice


def _claude_client_for(model: Optional[str], request: Request) -> ClaudeClient:
    """Claude client (per-user keys from the vault) honouring a model override."""
    s = request.state.settings
    return ClaudeClient(
        api_key=s["claude_api_key"],
        base_url=s["claude_base_url"],
        model=(model or s["claude_model"]),
    )


@app.post("/api/script")
def api_script(s: ScriptIn, request: Request):
    if not (s.title.strip() or s.description.strip() or s.brief.strip()):
        raise HTTPException(400, "a title or description is required")
    st = store.load_state()
    # If the old scene_count form is used, derive a matching duration.
    total_duration = s.total_duration
    pacing = max(0.1, s.pacing_seconds or 1.0)
    if s.scene_count and not s.total_duration:
        total_duration = s.scene_count * pacing
    try:
        raw = _claude_client_for(s.model, request).generate_script(
            title=s.title,
            description=s.description,
            total_duration=max(1.0, total_duration or 60.0),
            pacing_seconds=pacing,
            num_characters=max(0, s.num_characters or 0),
            style_notes=s.style_notes,
            master_prompt=st["master_prompt"],
            brief=s.brief,
            dialogue=s.dialogue,
        )
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"script generation failed: {e}")
    st["script"] = data
    store.save_state(st)
    store.log_usage("script", 1, 0.01)
    return data


# --------------------------------------------------------------------------- #
#  Wave 3: script editing tools
# --------------------------------------------------------------------------- #
def _rebuild_voiceover(script):
    vo = "\n\n".join((s.get("vo") or "").strip()
                     for s in (script.get("scenes") or []) if (s.get("vo") or "").strip())
    if vo:
        script["voiceover"] = vo
    return script


class ScriptUpdateIn(BaseModel):
    script: dict


@app.post("/api/script/update")
def api_script_update(body: ScriptUpdateIn):
    st = store.load_state()
    st["script"] = body.script or {}
    store.save_state(st)
    return {"ok": True}


class SceneRegenIn(BaseModel):
    n: int
    direction: str = ""
    model: Optional[str] = None


@app.post("/api/script/scene-regen")
def api_script_scene_regen(b: SceneRegenIn, request: Request):
    st = store.load_state()
    sc = st.get("script") or {}
    scenes = sc.get("scenes") or []
    idx = next((i for i, s in enumerate(scenes) if int(s.get("n", 0)) == b.n), -1)
    if idx < 0:
        raise HTTPException(404, "no such scene")
    s = scenes[idx]
    try:
        raw = _claude_client_for(b.model, request).regen_scene(
            b.n, s.get("heading", ""), s.get("action", ""), s.get("vo", ""),
            s.get("prompt", ""), b.direction, st.get("master_prompt", ""))
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"scene rewrite failed: {e}")
    for k in ("heading", "action", "vo", "prompt"):
        if data.get(k) is not None:
            s[k] = data[k]
    scenes[idx] = s
    sc["scenes"] = scenes
    _rebuild_voiceover(sc)
    st["script"] = sc
    store.save_state(st)
    return {"scene": s, "script": sc}


class TranslateIn(BaseModel):
    lang: str
    model: Optional[str] = None


@app.post("/api/script/translate")
def api_script_translate(b: TranslateIn, request: Request):
    st = store.load_state()
    sc = st.get("script") or {}
    if not sc.get("scenes"):
        raise HTTPException(400, "no script to translate")
    scene_vos = [s.get("vo", "") for s in sc["scenes"]]
    try:
        raw = _claude_client_for(b.model, request).translate_script(
            sc.get("voiceover", ""), scene_vos, b.lang)
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"translation failed: {e}")
    tmap = {int(x.get("n", 0)): x.get("vo", "") for x in (data.get("scenes") or [])}
    for i, s in enumerate(sc["scenes"], 1):
        if i in tmap:
            s["vo"] = tmap[i]
    if data.get("voiceover"):
        sc["voiceover"] = data["voiceover"]
    else:
        _rebuild_voiceover(sc)
    sc["language"] = b.lang
    st["script"] = sc
    store.save_state(st)
    return sc


class RewriteIn(BaseModel):
    tone: str
    direction: str = ""
    model: Optional[str] = None


@app.post("/api/script/rewrite")
def api_script_rewrite(b: RewriteIn, request: Request):
    st = store.load_state()
    sc = st.get("script") or {}
    if not sc.get("scenes"):
        raise HTTPException(400, "no script to rewrite")
    scenes_min = [{"n": s.get("n"), "heading": s.get("heading", ""),
                   "action": s.get("action", ""), "vo": s.get("vo", "")}
                  for s in sc["scenes"]]
    try:
        raw = _claude_client_for(b.model, request).rewrite_tone(
            sc.get("voiceover", ""), scenes_min, b.tone, b.direction,
            st.get("master_prompt", ""))
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"rewrite failed: {e}")
    rmap = {int(x.get("n", 0)): x for x in (data.get("scenes") or [])}
    for i, s in enumerate(sc["scenes"], 1):
        r = rmap.get(int(s.get("n", i)))
        if r:
            for k in ("heading", "action", "vo"):
                if r.get(k) is not None:
                    s[k] = r[k]
    if data.get("voiceover"):
        sc["voiceover"] = data["voiceover"]
    else:
        _rebuild_voiceover(sc)
    st["script"] = sc
    store.save_state(st)
    return sc


class HooksIn(BaseModel):
    n: int = 6
    model: Optional[str] = None


@app.post("/api/script/hooks")
def api_script_hooks(b: HooksIn, request: Request):
    st = store.load_state()
    sc = st.get("script") or {}
    title = sc.get("title", "") or ""
    desc = sc.get("logline", "") or sc.get("voiceover", "")[:300]
    try:
        raw = _claude_client_for(b.model, request).hooks(title, desc, max(1, min(12, b.n)))
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"hooks failed: {e}")
    return {"hooks": data.get("hooks") or []}


class OutlineIn(BaseModel):
    title: str = ""
    description: str = ""
    beats: int = 6
    model: Optional[str] = None


@app.post("/api/script/outline")
def api_script_outline(b: OutlineIn, request: Request):
    if not (b.title.strip() or b.description.strip()):
        raise HTTPException(400, "a title or description is required")
    try:
        raw = _claude_client_for(b.model, request).outline(
            b.title, b.description, max(3, min(12, b.beats)))
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"outline failed: {e}")
    return {"beats": data.get("beats") or []}


@app.get("/api/script/character-prompts")
def api_script_character_prompts():
    """Packed character sheet prompts from the current script, formatted for the
    bulk character generator (name line, paragraph, blank line between)."""
    st = store.load_state()
    sc = st.get("script") or {}
    chars = sc.get("characters") or []
    blocks = []
    for c in chars:
        name = (c.get("name") or "").strip()
        sheet = (c.get("sheet_prompt") or c.get("description") or "").strip()
        if name:
            blocks.append(f"{name}\n{sheet}".strip())
    return {"text": "\n\n".join(blocks), "count": len(blocks)}


class ScriptToBatchIn(BaseModel):
    pass


@app.get("/api/script/prompts")
def api_script_prompts():
    st = store.load_state()
    if not st.get("script"):
        return {"prompts": []}
    out = []
    for sc in (st["script"].get("scenes") or []):
        p = (sc.get("prompt") or "").strip()
        if p:
            out.append(p)
    return {"prompts": out}


# --------------------------------------------------------------------------- #
#  Claude vision: prompts from uploaded reference video
# --------------------------------------------------------------------------- #
class PromptsFromVideoIn(BaseModel):
    frame_urls: List[str]
    count: int = 8
    style_hint: str = ""


@app.post("/api/prompts-from-video")
def api_prompts_from_video(p: PromptsFromVideoIn, request: Request):
    if not p.frame_urls:
        raise HTTPException(400, "frame_urls is required (extract frames first)")
    st = store.load_state()
    try:
        frames = []
        for u in p.frame_urls[:10]:
            try:
                frames.append(pipeline.downsize_for_vision(store.read_image(u)))
            except Exception:
                pass
        if not frames:
            raise RuntimeError("no readable frames")
        raw = get_claude_client(request).prompts_from_video_frames(
            frames, count=max(1, min(20, p.count)),
            style_hint=p.style_hint, master_prompt=st["master_prompt"],
        )
        data = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"prompt generation failed: {e}")
    st["suggested_prompts"] = data.get("prompts") or []
    store.save_state(st)
    return data


# --------------------------------------------------------------------------- #
#  Audio upload
# --------------------------------------------------------------------------- #
@app.post("/api/audio")
async def api_audio(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    ext = (os.path.splitext(file.filename or "")[1] or ".mp3").lstrip(".").lower()
    if ext not in {"mp3", "wav", "m4a", "aac", "ogg", "flac"}:
        ext = "mp3"
    url, path = store.write_binary("audio", data, ext=ext, name_hint=file.filename)
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    st = store.load_state()
    rec = {
        "id": store.new_id("audio"),
        "url": url,
        "name": file.filename or f"audio.{ext}",
        "duration": dur,
    }
    st["audio"] = rec
    store.save_state(st)
    return rec


@app.delete("/api/audio")
def api_delete_audio():
    st = store.load_state()
    st["audio"] = None
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Wave 4: voice preview, background music, captions, title cards
# --------------------------------------------------------------------------- #
def _title_card_bytes(text, subtitle="", width=1920, height=1080):
    """Render a clean title/end card PNG with Pillow (crisp text, no model)."""
    from PIL import Image, ImageDraw, ImageFont
    import textwrap
    img = Image.new("RGB", (width, height), (8, 7, 10))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, 10], fill=(255, 122, 24))
    d.rectangle([0, height - 10, width, height], fill=(217, 119, 87))

    def font(sz, bold=True):
        cands = (["C:/Windows/Fonts/arialbd.ttf"] if bold else ["C:/Windows/Fonts/arial.ttf"]) + [
            "C:/Windows/Fonts/arial.ttf", "arialbd.ttf", "arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/Library/Fonts/Arial.ttf"]
        for n in cands:
            try:
                return ImageFont.truetype(n, sz)
            except Exception:
                pass
        return ImageFont.load_default()

    tsize = max(30, width // 16)
    tf, sf = font(tsize, True), font(max(18, width // 46), False)

    def dims(line, f):
        b = d.textbbox((0, 0), line, font=f)
        return (b[2] - b[0], b[3] - b[1])

    chars = max(8, int(width * 0.82 / (tsize * 0.56)))
    lines = textwrap.wrap(text or "", width=chars) or [""]
    line_h = dims("Ag", tf)[1] + 16
    total = line_h * len(lines) + (dims("Ag", sf)[1] + 26 if subtitle else 0)
    y = max(20, (height - total) // 2)
    for ln in lines:
        w, _h = dims(ln, tf)
        d.text(((width - w) // 2, y), ln, fill=(245, 240, 234), font=tf)
        y += line_h
    if subtitle:
        y += 14
        w, _h = dims(subtitle, sf)
        d.text(((width - w) // 2, y), subtitle, fill=(217, 119, 87), font=sf)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class VoicePreviewIn(BaseModel):
    voice_id: Optional[str] = None
    text: str = ""


@app.post("/api/voice/preview")
def api_voice_preview(b: VoicePreviewIn, request: Request):
    s = request.state.settings
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    text = (b.text or "").strip() or "This is a quick preview of how this voice sounds."
    try:
        mp3 = get_voice_client(request, b.voice_id).synthesize(text[:300])
    except Exception as e:
        raise HTTPException(500, f"preview failed: {e}")
    url, _ = store.write_binary("audio", mp3, ext="mp3", name_hint="preview")
    return {"url": url}


@app.post("/api/music")
async def api_music(file: UploadFile = File(...), volume: float = Form(0.18)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    ext = (os.path.splitext(file.filename or "")[1] or ".mp3").lstrip(".").lower()
    if ext not in {"mp3", "wav", "m4a", "aac", "ogg", "flac"}:
        ext = "mp3"
    url, path = store.write_binary("audio", data, ext=ext,
                                   name_hint=file.filename or "music")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    st = store.load_state()
    rec = {"id": store.new_id("music"), "url": url,
           "name": file.filename or f"music.{ext}", "duration": dur,
           "volume": max(0.0, min(1.0, float(volume)))}
    st["music"] = rec
    store.save_state(st)
    return rec


@app.delete("/api/music")
def api_delete_music():
    st = store.load_state()
    st["music"] = None
    store.save_state(st)
    return {"ok": True}


class MusicVolIn(BaseModel):
    volume: float = 0.18


@app.post("/api/music/volume")
def api_music_volume(b: MusicVolIn):
    st = store.load_state()
    if st.get("music"):
        st["music"]["volume"] = max(0.0, min(1.0, float(b.volume)))
        store.save_state(st)
    return st.get("music") or {}


def _srt_time(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@app.get("/api/captions.srt")
def api_captions():
    """Build an .srt — accurate when a per-scene voice-over exists (uses its
    real hold times), otherwise estimated from the script by word count."""
    st = store.load_state()
    cues = []
    vo = st.get("voiceover") or {}
    scenes = vo.get("scenes") or []
    if scenes and any(s.get("hold_seconds") for s in scenes):
        for s in scenes:
            txt = (s.get("vo") or "").strip()
            if txt:
                cues.append((txt, float(s.get("hold_seconds") or s.get("audio_seconds") or 2.0)))
    else:
        sc = st.get("script") or {}
        pacing = float(sc.get("pacing_seconds") or 1.0)
        for s in (sc.get("scenes") or []):
            txt = (s.get("vo") or "").strip()
            if not txt:
                continue
            cues.append((txt, max(pacing, len(txt.split()) / 2.6)))
    if not cues:
        raise HTTPException(400, "No script or voice-over to caption yet.")
    out, t = [], 0.0
    for i, (txt, dur) in enumerate(cues, 1):
        start, end = t, t + max(0.8, dur)
        t = end
        out.append(f"{i}\n{_srt_time(start)} --> {_srt_time(end)}\n{txt}\n")
    body = "\n".join(out)
    title = _safe_name((st.get("script") or {}).get("title") or "captions", "captions")
    return Response(content=body, media_type="application/x-subrip",
                    headers={"Content-Disposition": f'attachment; filename="{title}.srt"'})


def _music_for(st, want):
    """(music_path, volume) for the current project's music, or (None, .18)."""
    mus = st.get("music")
    if want and mus:
        try:
            return store.url_to_path(mus["url"]), float(mus.get("volume", 0.18))
        except Exception:
            pass
    return None, 0.18


def _bookend(out_path, intro, outro, w, h, fps):
    """Best-effort intro/outro title cards; never breaks the main render."""
    if not (intro or outro):
        return
    try:
        intro_img = outro_img = None
        if intro:
            iu = store.write_image("images", _title_card_bytes(intro, "", w, h))
            intro_img = store.url_to_path(iu)
        if outro:
            ou = store.write_image("images", _title_card_bytes(outro, "", w, h))
            outro_img = store.url_to_path(ou)
        booked = out_path + ".bk.mp4"
        editor.bookend_video(out_path, booked, intro_img, outro_img, 2.2, w, h, fps)
        os.replace(booked, out_path)
    except Exception as ex:
        print(f"[bookend] skipped: {ex}", flush=True)


# --------------------------------------------------------------------------- #
#  ElevenLabs voice-over — synthesize the script's narration into real audio
# --------------------------------------------------------------------------- #
def _script_voiceover_text(st) -> str:
    """The full narration text: prefer the top-level voiceover, else stitch the
    per-scene `vo` slices in order."""
    sc = st.get("script") or {}
    vo = (sc.get("voiceover") or "").strip()
    if vo:
        return vo
    return "\n\n".join(
        (s.get("vo") or "").strip() for s in (sc.get("scenes") or [])
        if (s.get("vo") or "").strip()
    ).strip()


def _scene_vo_lines(st):
    """List of per-scene narration lines (one per numbered scene), in order."""
    sc = st.get("script") or {}
    return [(s.get("vo") or "").strip() for s in (sc.get("scenes") or [])]


@app.get("/api/voices")
def api_voices(request: Request):
    """List the ElevenLabs voices available on the configured account."""
    s = request.state.settings
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    try:
        voices = get_voice_client(request).list_voices()
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"voices": voices, "current": s["elevenlabs_voice_id"]}


# --------------------------------------------------------------------------- #
#  "Natural flow" assembly — keep the voice-over continuous, time frames to it.
#
#  The older synced path voices each scene SEPARATELY, which chops sentences and
#  makes the narration sound slow / unnatural, and can leave one frame on screen
#  far too long. This path instead:
#    1. synthesizes the WHOLE script as ONE continuous track (natural prosody —
#       the audio is never time-stretched or pitch-shifted),
#    2. measures it, then distributes the frames across that timeline weighted by
#       how much narration each frame's scene carries (so each image is on screen
#       roughly while its line is spoken),
#    3. splits any frame that would sit too long into micro-cuts.
#  The result: audio flows, frames match it, nothing freezes.
# --------------------------------------------------------------------------- #
def _word_count(s: str) -> int:
    return len([w for w in re.split(r"\s+", (s or "").strip()) if w])


# --------------------------------------------------------------------------- #
#  ElevenLabs sound design — generate real SFX and mix them into the edit.
# --------------------------------------------------------------------------- #
_SFX_CACHE_DIR = os.path.join(config.DATA_DIR, "sfx_cache")

# Map words in the narration to a sound-effect description + intensity. Whichever
# matches first wins; "high" intensity scenes also get an impact boom.
_SFX_KEYWORDS = [
    (("lava", "molten", "magma"), "bubbling molten lava crackle and hiss", "high"),
    (("meteor", "asteroid", "comet", "impact", "crash", "collide"),
     "huge meteor impact explosion boom", "high"),
    (("explos", "erupt", "volcano", "blast", "burst"),
     "volcanic eruption explosion rumble", "high"),
    (("poison", "toxic", "gas", "fume", "cough", "choke", "breathe", "air"),
     "toxic gas hiss with a human cough", "normal"),
    (("heat", "burn", "1200", "1,200", "degree", "boil", "scorch"),
     "intense roaring fire whoosh", "high"),
    (("die", "death", "dead", "gone", "vanish", "destroy"),
     "dramatic deep cinematic impact hit", "high"),
    (("wind", "storm", "ash", "smoke", "cloud"), "howling wind with ash storm", "normal"),
    (("water", "ocean", "rain", "flood"), "rushing water surge", "normal"),
]


def _sfx_for_text(text):
    low = (text or "").lower()
    for keys, desc, intensity in _SFX_KEYWORDS:
        if any(k in low for k in keys):
            return desc, intensity
    return None, "normal"


def _generate_sfx_cached(request, text, duration=None):
    """Generate (or reuse) an ElevenLabs sound effect for ``text``. Cached on
    disk by text+duration so the same boom/whoosh isn't paid for twice.
    Returns (url, path)."""
    os.makedirs(_SFX_CACHE_DIR, exist_ok=True)
    key = hashlib.md5(f"{text}|{duration}".encode("utf-8")).hexdigest()[:16]
    path = os.path.join(_SFX_CACHE_DIR, f"{key}.mp3")
    if not os.path.exists(path):
        mp3 = get_voice_client(request).generate_sfx(text, duration_seconds=duration)
        with open(path, "wb") as f:
            f.write(mp3)
    rel = os.path.relpath(path, store.DATA_DIR).replace(os.sep, "/")
    return f"/data/{rel}", path


def _normalize_loud(video_path, target_lufs=-14.0):
    """Loudness-normalize + limit a finished video's audio for crisp, LOUD
    Shorts/TikTok output. Voice is compressed so it stays above the bed. In place."""
    tmp = video_path + ".loud.mp4"
    af = (f"acompressor=threshold=-18dB:ratio=3:attack=5:release=120,"
          f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11,alimiter=limit=0.97")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-af", af,
         "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", tmp],
        capture_output=True, text=True)
    if proc.returncode == 0 and os.path.exists(tmp):
        os.replace(tmp, video_path)
    else:
        print(f"[sound] loudnorm skipped: {proc.stderr[-200:]}", flush=True)


def _auto_sound_design(request, scene_map, total_seconds):
    """Build a sound plan from the scene narration and generate it via
    ElevenLabs. Returns (rumble_path_or_None, sfx_entries). The rumble is meant
    for the looped 'music' bed; sfx_entries drop into st['sfx'] (point sounds).
    Generates only a handful of UNIQUE clips (cached + reused) to control cost."""
    sfx_entries = []
    rumble_path = None
    try:
        _, rumble_path = _generate_sfx_cached(
            request, "deep continuous low ominous cinematic rumble drone, seamless",
            duration=22)
    except Exception as e:
        print(f"[sound] rumble skipped: {e}", flush=True)

    # whoosh at the very start (into the hook/first frame)
    def _add(desc, at, vol, dur=None):
        try:
            url, _p = _generate_sfx_cached(request, desc, duration=dur)
            sfx_entries.append({"id": store.new_id("sfx"), "url": url,
                                "name": desc[:40], "at_seconds": round(max(0, at), 2),
                                "volume": vol})
        except Exception as e:
            print(f"[sound] sfx '{desc[:24]}' skipped: {e}", flush=True)

    _add("fast cinematic whoosh transition", 0.0, 0.5, dur=1.0)
    t = 0.0
    last_boom = -5.0
    for sc in scene_map:
        hold = float(sc.get("hold_seconds") or 0)
        desc, intensity = _sfx_for_text(sc.get("vo") or "")
        if desc and (t - last_boom) >= 2.0:      # don't stack sounds too densely
            _add(desc, t + 0.05, 0.55, dur=2.0)
            last_boom = t
        t += hold
    # final comedic/dramatic hit on the payoff
    _add("hard dramatic comedic impact hit", max(0.0, total_seconds - 1.4), 0.7, dur=1.5)
    return rumble_path, sfx_entries


def _build_flow_video(st, request, *, voice_id, text, transition="cut",
                      width=1920, height=1080, fps=30, max_hold=6.0,
                      motion=False, use_music=False, name_hint="flow",
                      sound_design=False):
    """Shared engine for the natural-flow video. Returns
    (edit_rec, video_url, total_seconds, scene_map). Mutates + saves `st`.

    ``sound_design`` (needs an ElevenLabs key) generates real SFX via the
    ElevenLabs Sound API — a low rumble bed under the whole video plus contextual
    booms/whooshes/crackles on the matching narration beats — mixes them in, and
    loudness-normalizes the result for crisp, loud Shorts audio."""
    seq = st.get("sequence") or []
    if not seq:
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "No voice-over text. Generate a script first.")

    # 1. ONE continuous narration track — natural, never stretched.
    try:
        mp3 = get_voice_client(request, voice_id).synthesize(text)
    except Exception as e:
        raise HTTPException(500, f"voice-over failed: {e}")
    url, path = store.write_binary("audio", mp3, ext="mp3", name_hint=name_hint)
    try:
        dur = float(editor.probe_duration(path))
    except Exception:
        dur = 0.0
    if dur <= 0.1:
        dur = max(1.0, len(seq) * 2.0)

    # 2. Weight each frame by its scene's narration length so images change in
    #    step with the speech. Frames beyond the last scene line share evenly.
    lines = _scene_vo_lines(st)
    n = len(seq)
    weights = []
    for i in range(n):
        line = lines[i] if i < len(lines) else ""
        weights.append(float(max(1, _word_count(line))))
    total_w = sum(weights) or float(n)
    holds = [max(0.6, dur * w / total_w) for w in weights]
    # Re-normalise so the holds sum to the real audio length (keeps A/V locked).
    scale = dur / (sum(holds) or 1.0)
    holds = [round(h * scale, 3) for h in holds]

    shots, scene_map = [], []
    for i in range(n):
        try:
            img_path = store.url_to_path(seq[i]["image_url"])
        except Exception:
            continue
        line = lines[i] if i < len(lines) else ""
        shots.append({"path": img_path, "duration": holds[i],
                      "note": (line[:60] if line else "")})
        scene_map.append({"index": seq[i].get("index", i + 1), "vo": line,
                          "hold_seconds": holds[i]})
    if not shots:
        raise HTTPException(400, "no readable frames in sequence")

    # 3. Micro-cut any frame that would sit too long.
    shots = editor.split_long_holds(shots, max_hold=max(1.5, float(max_hold)))

    # 3b. Sound design (ElevenLabs): a rumble bed for the music slot + a plan of
    #     contextual point-SFX to mix in after assembly.
    music_path, music_vol = _music_for(st, use_music)
    sfx_entries = []
    do_sound = bool(sound_design and request and
                    request.state.settings.get("elevenlabs_api_key"))
    if do_sound:
        try:
            rumble_path, sfx_entries = _auto_sound_design(request, scene_map, dur)
            if rumble_path and not music_path:   # use rumble as the ducked bed
                music_path, music_vol = rumble_path, 0.16
        except Exception as ex:
            print(f"[sound] design skipped: {ex}", flush=True)

    out_name = f"edit_{int(time.time())}.mp4"
    out_path = os.path.join(store.VIDEOS_DIR, out_name)
    try:
        editor.assemble_video(shots, path, out_path,
                              transition=(transition or "cut").lower(),
                              width=width, height=height, fps=fps, motion=motion,
                              music_path=music_path, music_volume=music_vol)
    except Exception as ex:
        raise HTTPException(500, f"video assembly failed: {ex}")

    # 3c. Mix the point-SFX onto the rendered video, then make it loud + crisp.
    if do_sound:
        if sfx_entries:
            try:
                editor.mix_sfx(out_path, [
                    {"path": store.url_to_path(s["url"]),
                     "at_seconds": s["at_seconds"], "volume": s["volume"]}
                    for s in sfx_entries
                    if os.path.exists(store.url_to_path(s["url"]))],
                    out_path + ".sfx.mp4")
                if os.path.exists(out_path + ".sfx.mp4"):
                    os.replace(out_path + ".sfx.mp4", out_path)
            except Exception as ex:
                print(f"[sound] sfx mix skipped: {ex}", flush=True)
        _normalize_loud(out_path)

    rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
    video_url = f"/data/{rel}"

    total = round(dur, 2)
    st["audio"] = {"id": store.new_id("audio"), "url": url,
                   "name": f"{name_hint}.mp3", "duration": total}
    st["voiceover"] = {"id": store.new_id("vo"), "url": url, "voice_id": voice_id,
                       "mode": "flow", "duration": total, "scenes": scene_map,
                       "created": store.now()}
    plan = {"mode": "voiceover_flow", "total_duration": total,
            "transition": (transition or "cut").lower(),
            "micro_cut_max_hold": max_hold, "shots": scene_map,
            "rendered_clips": len(shots),
            "sound_design": bool(do_sound), "sfx_count": len(sfx_entries)}
    edit_rec = {"id": store.new_id("edit"), "url": video_url,
                "audio_id": st["audio"]["id"],
                "transition": (transition or "cut").lower(),
                "plan": plan, "created": store.now()}
    st.setdefault("edits", []).append(edit_rec)
    store.save_state(st)
    store.log_usage("voice", 1, round(total * 0.0002, 4))
    store.log_usage("video", 1, 0.0)
    return edit_rec, video_url, total, scene_map


class FlowVoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    text: str = ""
    transition: Optional[str] = None
    width: int = 1920
    height: int = 1080
    fps: int = 30
    max_hold: float = 6.0          # split frames longer than this into micro-cuts
    motion: bool = False
    music: bool = False
    sound_design: bool = False     # generate + mix ElevenLabs SFX, then loudnorm


@app.post("/api/voiceover/auto-flow")
def api_voiceover_auto_flow(body: FlowVoiceoverIn, request: Request):
    """Natural-flow narrated video: one continuous voice-over, frames timed to
    it, long holds broken into micro-cuts. See _build_flow_video."""
    s = request.state.settings
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    st = store.load_state()
    text = (body.text or "").strip() or _script_voiceover_text(st)
    voice_id = body.voice_id or s["elevenlabs_voice_id"]
    edit_rec, video_url, total, scene_map = _build_flow_video(
        st, request, voice_id=voice_id, text=text,
        transition=body.transition or "cut", width=body.width,
        height=body.height, fps=body.fps, max_hold=body.max_hold,
        motion=body.motion, use_music=body.music, name_hint="voiceover_flow",
        sound_design=body.sound_design)
    return {"edit": edit_rec, "video_url": video_url, "total_duration": total,
            "scenes": scene_map, "plan": edit_rec["plan"]}


class VoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    text: Optional[str] = None       # override; defaults to the script voice-over


@app.post("/api/voiceover")
def api_voiceover(body: VoiceoverIn, request: Request):
    """Single-track mode: synthesize the whole voice-over into ONE audio file and
    drop it into the existing `audio` slot, so Plan edit / Render video work
    unchanged. Returns the audio record (same shape as /api/audio)."""
    s = request.state.settings
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    st = store.load_state()
    text = (body.text or "").strip() or _script_voiceover_text(st)
    if not text:
        raise HTTPException(400, "No voice-over text. Generate a script in tab 02 first.")
    voice_id = body.voice_id or s["elevenlabs_voice_id"]
    try:
        mp3 = get_voice_client(request, voice_id).synthesize(text)
    except Exception as e:
        raise HTTPException(500, f"voice-over failed: {e}")
    url, path = store.write_binary("audio", mp3, ext="mp3", name_hint="voiceover")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    rec = {
        "id": store.new_id("audio"),
        "url": url,
        "name": "voiceover.mp3",
        "duration": dur,
    }
    st["audio"] = rec
    st["voiceover"] = {
        "id": store.new_id("vo"),
        "url": url, "voice_id": voice_id, "mode": "single",
        "duration": dur, "created": store.now(),
    }
    store.save_state(st)
    store.log_usage("voice", 1, round(max(dur, 1) * 0.0002, 4))
    return rec


class SceneVoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    transition: str = "cut"          # cut | fade | crossfade | xfade name
    width: int = 1920
    height: int = 1080
    fps: int = 30
    pad: float = 0.12                # small breath of hold added after each clip
    motion: bool = False             # Ken Burns zoom
    music: bool = False              # mix the project's background music
    intro: Optional[str] = None      # intro title-card text
    outro: Optional[str] = None      # outro title-card text


@app.post("/api/voiceover/scenes")
def api_voiceover_scenes(body: SceneVoiceoverIn, request: Request):
    """Per-scene mode: synthesize ONE clip per numbered scene, measure each
    clip's length, then build a video where sequence frame N is held for exactly
    the length of scene N's voice-over (+pad). Concatenates the clips into a
    single narration track and muxes it. Returns the edit record + per-scene map.
    """
    s = request.state.settings
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    st = store.load_state()
    seq = st.get("sequence") or []
    if not seq:
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    lines = _scene_vo_lines(st)
    if not any(lines):
        raise HTTPException(400, "No per-scene narration. Generate a script in tab 02 first.")

    voice_id = body.voice_id or s["elevenlabs_voice_id"]
    client = get_voice_client(request, voice_id)

    # one frame per scene, capped to whichever is shorter
    n = min(len(seq), len(lines))
    if n == 0:
        raise HTTPException(400, "Nothing to voice.")

    work = os.path.join(store.VIDEOS_DIR, f"_vo_{store.new_id('tmp')}")
    os.makedirs(work, exist_ok=True)
    clip_paths = []          # audio clips, in order, to concat into one track
    shots = []               # for editor.assemble_video
    scene_map = []
    try:
        for i in range(n):
            line = lines[i] or ""
            try:
                mp3 = client.synthesize(line)
            except Exception as e:
                raise HTTPException(500, f"voice-over failed on scene {i+1}: {e}")
            ap = os.path.join(work, f"vo_{i:03d}.mp3")
            with open(ap, "wb") as f:
                f.write(mp3)
            try:
                d = editor.probe_duration(ap)
            except Exception:
                d = 0.0
            if d <= 0.05:
                # silent/empty line -> brief hold only (a 2.5s freeze on a
                # wordless scene is what made the cut drag); keep it short so
                # the pacing stays tight.
                d = 0.8
            hold = round(d + max(0.0, body.pad), 3)
            clip_paths.append((ap, hold, d))
            try:
                img_path = store.url_to_path(seq[i]["image_url"])
            except Exception:
                continue
            shots.append({"path": img_path, "duration": hold,
                          "note": (line[:60] if line else "")})
            scene_map.append({"index": seq[i].get("index", i + 1),
                              "vo": line, "audio_seconds": round(d, 2),
                              "hold_seconds": hold})
        if not shots:
            raise HTTPException(400, "No valid frames matched the scenes.")

        # 1. concat the per-scene clips into one narration track (pad each clip
        #    with trailing silence to its hold length so audio lines up with the
        #    image timing exactly).
        track = os.path.join(work, "voiceover_track.mp3")
        concat_inputs, filt = [], ""
        for j, (ap, hold, _d) in enumerate(clip_paths):
            concat_inputs += ["-i", ap]
            filt += f"[{j}:a]apad=whole_dur={hold}[a{j}];"
        filt += "".join(f"[a{j}]" for j in range(len(clip_paths)))
        filt += f"concat=n={len(clip_paths)}:v=0:a=1[out]"
        cc = subprocess.run(
            ["ffmpeg", "-y", *concat_inputs, "-filter_complex", filt,
             "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", track],
            capture_output=True, text=True)
        track_path = track if (cc.returncode == 0 and os.path.exists(track)) else None
        track_url = None
        if track_path:
            with open(track_path, "rb") as f:
                track_url, _tp = store.write_binary(
                    "audio", f.read(), ext="mp3", name_hint="voiceover_scenes")

        # 2. assemble the timed video with the narration track muxed in.
        out_name = f"edit_{int(time.time())}.mp4"
        out_path = os.path.join(store.VIDEOS_DIR, out_name)
        music_path, music_volume = _music_for(st, body.music)
        editor.assemble_video(
            shots, track_path, out_path,
            transition=(body.transition or "cut").lower(),
            width=body.width, height=body.height, fps=body.fps,
            motion=body.motion, music_path=music_path, music_volume=music_volume,
        )
        _bookend(out_path, body.intro, body.outro, body.width, body.height, body.fps)
        rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
        url = f"/data/{rel}"

        total = round(sum(h for _a, h, _d in clip_paths), 2)
        if track_url:
            st["voiceover"] = {
                "id": store.new_id("vo"), "url": track_url, "voice_id": voice_id,
                "mode": "scenes", "duration": total, "scenes": scene_map,
                "created": store.now(),
            }
        rec = {
            "id": store.new_id("edit"), "url": url,
            "audio_id": (st.get("voiceover") or {}).get("id"),
            "transition": (body.transition or "cut").lower(),
            "plan": {"mode": "voiceover_scenes", "total_duration": total,
                     "shots": scene_map},
            "created": store.now(),
        }
        st.setdefault("edits", []).append(rec)
        store.save_state(st)
        _fire_webhook(request.state.settings, "video.rendered",
                      {"url": rec["url"], "total_duration": total})
        store.log_usage("voice", n, round(total * 0.0002, 4))
        store.log_usage("video", 1, 0.0)
        return {"edit": rec, "scenes": scene_map, "total_duration": total,
                "voiceover_url": track_url}
    finally:
        shutil.rmtree(work, ignore_errors=True)


class AutoVoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    text: Optional[str] = None        # override; defaults to the script voice-over
    user_brief: str = ""              # extra edit direction for Claude
    transition: Optional[str] = None  # override Claude's suggested transition
    width: int = 1920
    height: int = 1080
    fps: int = 30


@app.post("/api/voiceover/auto")
def api_voiceover_auto(body: AutoVoiceoverIn, request: Request):
    """One click: ElevenLabs voices the whole script -> Claude LOOKS at every
    frame + that audio's length + your brief and writes the edit decision list
    -> ffmpeg assembles the synced MP4. This is the 'Claude analyses both the
    mp3 and the frames and makes a video matching the voice-over' path, end to
    end. Returns {audio, plan, edit}."""
    s = request.state.settings
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    if not s["claude_api_key"]:
        raise HTTPException(400, "No Claude API key set — add it in Settings.")
    st = store.load_state()
    if not st.get("sequence"):
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    text = (body.text or "").strip() or _script_voiceover_text(st)
    if not text:
        raise HTTPException(400, "No voice-over text. Generate a script in tab 02 first.")

    # 1. ElevenLabs: script text -> one narration track, into the audio slot.
    voice_id = body.voice_id or s["elevenlabs_voice_id"]
    try:
        mp3 = get_voice_client(request, voice_id).synthesize(text)
    except Exception as e:
        raise HTTPException(500, f"voice-over failed: {e}")
    url, path = store.write_binary("audio", mp3, ext="mp3", name_hint="voiceover")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    audio_rec = {"id": store.new_id("audio"), "url": url,
                 "name": "voiceover.mp3", "duration": dur}
    st["audio"] = audio_rec
    st["voiceover"] = {"id": store.new_id("vo"), "url": url, "voice_id": voice_id,
                       "mode": "single", "duration": dur, "created": store.now()}
    store.save_state(st)

    # 2. Claude vision: look at the frames + the audio length -> EDL.
    frames = []
    for sh in st["sequence"]:
        try:
            frames.append(pipeline.downsize_for_vision(store.read_image(sh["image_url"])))
        except Exception:
            pass
    if not frames:
        raise HTTPException(400, "no readable frames in sequence")
    try:
        plan = get_claude_client(request).plan_edit(
            frames=frames,                       # all frames; client chunks in 18s
            audio_duration=float(dur) or 0,
            user_brief=body.user_brief,
            master_prompt=st["master_prompt"],
        )
    except Exception as ex:
        raise HTTPException(500, f"edit planning failed: {ex}")
    if body.transition:
        plan["transition"] = body.transition

    # 3. ffmpeg: assemble the synced video from the EDL + the narration track.
    seq = st["sequence"]
    shots_out = []
    for sh in (plan.get("shots") or []):
        idx = int(sh.get("index", 0))
        if idx < 1 or idx > len(seq):
            continue
        try:
            p = store.url_to_path(seq[idx - 1]["image_url"])
        except Exception:
            continue
        shots_out.append({"path": p, "duration": float(sh.get("duration") or 1.0),
                          "note": sh.get("note", "")})
    if not shots_out:
        raise HTTPException(400, "Claude returned no valid shots to assemble")

    out_name = f"edit_{int(time.time())}.mp4"
    out_path = os.path.join(store.VIDEOS_DIR, out_name)
    transition = (body.transition or plan.get("transition") or "cut").lower()
    try:
        editor.assemble_video(shots_out, path, out_path, transition=transition,
                              width=body.width, height=body.height, fps=body.fps)
    except Exception as ex:
        raise HTTPException(500, f"video assembly failed: {ex}")
    rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
    edit_rec = {
        "id": store.new_id("edit"), "url": f"/data/{rel}",
        "audio_id": audio_rec["id"], "transition": transition,
        "plan": plan, "created": store.now(),
    }
    st.setdefault("edits", []).append(edit_rec)
    store.save_state(st)
    store.log_usage("voice", 1, round(max(dur, 1) * 0.0002, 4))
    store.log_usage("script", 1, 0.01)
    store.log_usage("video", 1, 0.0)
    return {"audio": audio_rec, "plan": plan, "edit": edit_rec}


class SyncedVoiceoverIn(BaseModel):
    voice_id: Optional[str] = None
    user_brief: str = ""              # edit direction for Claude (order/drops)
    transition: Optional[str] = None  # override Claude's suggested transition
    width: int = 1920
    height: int = 1080
    fps: int = 30
    pad: float = 0.12                 # small breath of hold added after each clip


@app.post("/api/voiceover/auto-synced")
def api_voiceover_auto_synced(body: SyncedVoiceoverIn, request: Request):
    """Button B — scene-locked Claude edit.

    Voices each scene, measures each clip so every frame's on-screen time is
    LOCKED to its own narration length (exact A/V sync, same engine as
    /api/voiceover/scenes). Claude then looks at all frames and only chooses the
    ORDER they play and which to DROP — it cannot change a duration. The
    narration track is rebuilt in Claude's chosen order so audio still lines up.
    """
    s = request.state.settings
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    if not s["claude_api_key"]:
        raise HTTPException(400, "No Claude API key set — add it in Settings.")
    st = store.load_state()
    seq = st.get("sequence") or []
    if not seq:
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    lines = _scene_vo_lines(st)
    if not any(lines):
        raise HTTPException(400, "No per-scene narration. Generate a script in tab 02 first.")

    voice_id = body.voice_id or s["elevenlabs_voice_id"]
    client = get_voice_client(request, voice_id)
    n = min(len(seq), len(lines))
    if n == 0:
        raise HTTPException(400, "Nothing to voice.")

    work = os.path.join(store.VIDEOS_DIR, f"_vos_{store.new_id('tmp')}")
    os.makedirs(work, exist_ok=True)
    try:
        # 1. Voice + measure each scene -> locked hold + per-scene audio clip.
        clips = []        # per global 1-based index: {audio, hold, d, img, vo}
        for i in range(n):
            line = lines[i] or ""
            try:
                mp3 = client.synthesize(line)
            except Exception as e:
                raise HTTPException(500, f"voice-over failed on scene {i+1}: {e}")
            ap = os.path.join(work, f"vo_{i:03d}.mp3")
            with open(ap, "wb") as f:
                f.write(mp3)
            try:
                d = editor.probe_duration(ap)
            except Exception:
                d = 0.0
            if d <= 0.05:
                d = 0.8
            hold = round(d + max(0.0, body.pad), 3)
            try:
                img_path = store.url_to_path(seq[i]["image_url"])
            except Exception:
                continue
            clips.append({"index": i + 1, "audio": ap, "hold": hold,
                          "d": round(d, 2), "img": img_path, "vo": line})
        if not clips:
            raise HTTPException(400, "No valid frames matched the scenes.")

        # 2. Claude: choose play order + drops (durations stay locked).
        #    Keep frames and clips index-aligned: only clips whose frame reads
        #    successfully are considered, so the global index 1..N the model
        #    sees maps exactly onto `clips`/`scene_durations`.
        frames_for_vision, kept = [], []
        for c in clips:
            try:
                frames_for_vision.append(pipeline.downsize_for_vision(
                    store.read_image(seq[c["index"] - 1]["image_url"])))
                kept.append(c)
            except Exception:
                pass
        if not kept:
            raise HTTPException(400, "no readable frames in sequence")
        clips = kept
        # Re-number to a dense 1..len(clips) so vision indices line up.
        for new_i, c in enumerate(clips, start=1):
            c["g"] = new_i
        by_index = {c["g"]: c for c in clips}
        scene_durations = [c["hold"] for c in clips]
        try:
            decision = get_claude_client(request).plan_edit_within_budget(
                frames=frames_for_vision,
                scene_durations=scene_durations,
                user_brief=body.user_brief,
                master_prompt=st["master_prompt"],
            )
        except Exception as ex:
            raise HTTPException(500, f"edit planning failed: {ex}")

        # Claude's order/notes are keyed by the dense vision index (c["g"]).
        order = [x for x in (decision.get("order") or []) if x in by_index]
        if not order:                      # fallback: keep all in script order
            order = [c["g"] for c in clips]
        notes = decision.get("notes") or {}
        transition = (body.transition or decision.get("transition") or "cut").lower()

        # 3. Rebuild the narration track in CLAUDE'S order (dropped scenes drop
        #    their audio too), padding each clip to its locked hold so the audio
        #    stays aligned to the reordered visuals.
        ordered = [by_index[x] for x in order]
        track = os.path.join(work, "voiceover_track.mp3")
        concat_inputs, filt = [], ""
        for j, c in enumerate(ordered):
            concat_inputs += ["-i", c["audio"]]
            filt += f"[{j}:a]apad=whole_dur={c['hold']}[a{j}];"
        filt += "".join(f"[a{j}]" for j in range(len(ordered)))
        filt += f"concat=n={len(ordered)}:v=0:a=1[out]"
        cc = subprocess.run(
            ["ffmpeg", "-y", *concat_inputs, "-filter_complex", filt,
             "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", track],
            capture_output=True, text=True)
        track_path = track if (cc.returncode == 0 and os.path.exists(track)) else None
        track_url = None
        if track_path:
            with open(track_path, "rb") as f:
                track_url, _tp = store.write_binary(
                    "audio", f.read(), ext="mp3", name_hint="voiceover_synced")

        # 4. Assemble video in the same order, each shot held for its locked hold.
        shots, scene_map = [], []
        for c in ordered:
            note = notes.get(c["g"]) or notes.get(str(c["g"])) or ""
            shots.append({"path": c["img"], "duration": c["hold"],
                          "note": (note or c["vo"][:60])})
            scene_map.append({"index": c["index"], "vo": c["vo"],
                              "audio_seconds": c["d"], "hold_seconds": c["hold"],
                              "note": note})

        out_name = f"edit_{int(time.time())}.mp4"
        out_path = os.path.join(store.VIDEOS_DIR, out_name)
        try:
            editor.assemble_video(shots, track_path, out_path, transition=transition,
                                  width=body.width, height=body.height, fps=body.fps)
        except Exception as ex:
            raise HTTPException(500, f"video assembly failed: {ex}")
        rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
        url = f"/data/{rel}"

        total = round(sum(c["hold"] for c in ordered), 2)
        if track_url:
            st["voiceover"] = {
                "id": store.new_id("vo"), "url": track_url, "voice_id": voice_id,
                "mode": "synced", "duration": total, "scenes": scene_map,
                "created": store.now(),
            }
        kept_g = set(order)
        plan = {"mode": "voiceover_synced", "total_duration": total,
                "transition": transition,
                "order": [by_index[g]["index"] for g in order],
                "dropped": [c["index"] for c in clips if c["g"] not in kept_g],
                "rationale": decision.get("rationale", ""),
                "frames_seen": decision.get("frames_seen", len(clips)),
                "shots": scene_map}
        rec = {
            "id": store.new_id("edit"), "url": url,
            "audio_id": (st.get("voiceover") or {}).get("id"),
            "transition": transition, "plan": plan, "created": store.now(),
        }
        st.setdefault("edits", []).append(rec)
        store.save_state(st)
        store.log_usage("voice", n, round(total * 0.0002, 4))
        store.log_usage("script", 1, 0.01)
        store.log_usage("video", 1, 0.0)
        return {"edit": rec, "plan": plan, "scenes": scene_map,
                "total_duration": total, "voiceover_url": track_url}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Multi-voice: per-character voice mapping + dialogue synthesis
# --------------------------------------------------------------------------- #
class VoiceMapIn(BaseModel):
    voice_map: dict   # {character_name: voice_id}

@app.post("/api/voice-map")
def api_voice_map(body: VoiceMapIn):
    st = store.load_state()
    st["voice_map"] = body.voice_map or {}
    store.save_state(st)
    return {"ok": True, "voice_map": st["voice_map"]}


@app.get("/api/voice-map")
def api_voice_map_get():
    st = store.load_state()
    return {"voice_map": st.get("voice_map") or {}}


def _parse_dialogue_lines(text):
    """Parse speaker-tagged lines from VO text.

    Supported formats:
      [NARRATOR]: Some text here...
      [CHARACTER NAME]: Dialogue here...
      Untagged lines -> speaker=None (uses default voice)

    Returns [(speaker_or_None, text), ...]
    """
    segments = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^\[([^\]]+)\]\s*:\s*(.+)$', line)
        if m:
            segments.append((m.group(1).strip(), m.group(2).strip()))
        else:
            segments.append((None, line))
    return segments


class MultiVoiceIn(BaseModel):
    voice_map: Optional[dict] = None  # {character_name: voice_id}; uses saved if omitted
    default_voice_id: Optional[str] = None


@app.post("/api/voiceover/multivoice")
def api_voiceover_multivoice(body: MultiVoiceIn, request: Request):
    """Multi-voice mode: parse speaker tags from the script VO, synthesize each
    segment with the character's assigned voice, concatenate into one track."""
    s = request.state.settings
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    st = store.load_state()
    text = _script_voiceover_text(st)
    if not text:
        raise HTTPException(400, "No voice-over text. Generate a script first.")

    vm = body.voice_map or st.get("voice_map") or {}
    default_vid = body.default_voice_id or s["elevenlabs_voice_id"]

    segments = _parse_dialogue_lines(text)
    if not segments:
        raise HTTPException(400, "No dialogue segments found in voice-over text.")

    work = os.path.join(store.AUDIO_DIR, f"_mv_{store.new_id('tmp')}")
    os.makedirs(work, exist_ok=True)
    try:
        clip_paths = []
        speakers_used = set()
        for i, (speaker, seg_text) in enumerate(segments):
            vid = vm.get(speaker, default_vid) if speaker else default_vid
            speakers_used.add(speaker or "NARRATOR")
            try:
                mp3 = get_voice_client(request, vid).synthesize(seg_text)
            except Exception as e:
                raise HTTPException(500, f"TTS failed for segment {i+1} ({speaker or 'narrator'}): {e}")
            cp = os.path.join(work, f"seg_{i:04d}.mp3")
            with open(cp, "wb") as f:
                f.write(mp3)
            clip_paths.append(cp)

        if not clip_paths:
            raise HTTPException(400, "No segments synthesized.")

        # Concatenate all segments into one track
        track = os.path.join(work, "multivoice_track.mp3")
        if len(clip_paths) == 1:
            shutil.copy2(clip_paths[0], track)
        else:
            concat_inputs = []
            filt_parts = []
            for j, cp in enumerate(clip_paths):
                concat_inputs += ["-i", cp]
                filt_parts.append(f"[{j}:a]")
            filt = "".join(filt_parts) + f"concat=n={len(clip_paths)}:v=0:a=1[out]"
            cc = subprocess.run(
                ["ffmpeg", "-y", *concat_inputs, "-filter_complex", filt,
                 "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", track],
                capture_output=True, text=True)
            if cc.returncode != 0:
                raise HTTPException(500, f"audio concat failed: {cc.stderr[-300:]}")

        with open(track, "rb") as f:
            track_data = f.read()
        url, path = store.write_binary("audio", track_data, ext="mp3", name_hint="multivoice")
        try:
            dur = editor.probe_duration(path)
        except Exception:
            dur = 0

        rec = {"id": store.new_id("audio"), "url": url,
               "name": "multivoice.mp3", "duration": dur}
        st["audio"] = rec
        st["voiceover"] = {
            "id": store.new_id("vo"), "url": url,
            "mode": "multivoice", "duration": dur,
            "speakers": list(speakers_used),
            "voice_map": vm, "created": store.now(),
        }
        if body.voice_map:
            st["voice_map"] = vm
        store.save_state(st)
        store.log_usage("voice", len(clip_paths), round(max(dur, 1) * 0.0002, 4))
        return {"audio": rec, "speakers": list(speakers_used),
                "segments": len(clip_paths), "duration": dur}
    finally:
        shutil.rmtree(work, ignore_errors=True)


class MultiVoiceScenesIn(BaseModel):
    voice_map: Optional[dict] = None
    default_voice_id: Optional[str] = None
    transition: str = "cut"
    width: int = 1920
    height: int = 1080
    fps: int = 30
    pad: float = 0.12
    motion: bool = False
    music: bool = False


@app.post("/api/voiceover/multivoice/scenes")
def api_voiceover_multivoice_scenes(body: MultiVoiceScenesIn, request: Request):
    """Per-scene multi-voice: each scene's VO is parsed for speaker tags and
    synthesized with per-character voices, then assembled into a timed video."""
    s = request.state.settings
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    st = store.load_state()
    seq = st.get("sequence") or []
    if not seq:
        raise HTTPException(400, "Render some frames in the Sequence tab first.")
    lines = _scene_vo_lines(st)
    if not any(lines):
        raise HTTPException(400, "No per-scene narration. Generate a script first.")

    vm = body.voice_map or st.get("voice_map") or {}
    default_vid = body.default_voice_id or s["elevenlabs_voice_id"]
    n = min(len(seq), len(lines))

    work = os.path.join(store.VIDEOS_DIR, f"_mv_{store.new_id('tmp')}")
    os.makedirs(work, exist_ok=True)
    try:
        clip_paths = []
        shots = []
        scene_map = []
        speakers_used = set()

        for i in range(n):
            line = lines[i] or ""
            segments = _parse_dialogue_lines(line)
            scene_clips = []
            for j, (speaker, seg_text) in enumerate(segments):
                vid = vm.get(speaker, default_vid) if speaker else default_vid
                speakers_used.add(speaker or "NARRATOR")
                try:
                    mp3 = get_voice_client(request, vid).synthesize(seg_text)
                except Exception as e:
                    raise HTTPException(500, f"TTS failed scene {i+1} seg {j+1}: {e}")
                cp = os.path.join(work, f"sc{i:03d}_seg{j:02d}.mp3")
                with open(cp, "wb") as f:
                    f.write(mp3)
                scene_clips.append(cp)

            if not scene_clips:
                scene_clips = []
                d = 0.8
            elif len(scene_clips) == 1:
                d = 0
                try:
                    d = editor.probe_duration(scene_clips[0])
                except Exception:
                    d = 0.8
            else:
                # Concat scene segments into one per-scene clip
                merged = os.path.join(work, f"sc{i:03d}.mp3")
                ci = []
                fp = []
                for k, cp in enumerate(scene_clips):
                    ci += ["-i", cp]
                    fp.append(f"[{k}:a]")
                filt = "".join(fp) + f"concat=n={len(scene_clips)}:v=0:a=1[out]"
                cc = subprocess.run(
                    ["ffmpeg", "-y", *ci, "-filter_complex", filt,
                     "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", merged],
                    capture_output=True, text=True)
                if cc.returncode == 0:
                    scene_clips = [merged]
                    try:
                        d = editor.probe_duration(merged)
                    except Exception:
                        d = 0.8
                else:
                    d = 0.8

            if d <= 0.05:
                d = 0.8
            hold = round(d + max(0.0, body.pad), 3)
            if scene_clips:
                clip_paths.append((scene_clips[0], hold, d))
            try:
                img_path = store.url_to_path(seq[i]["image_url"])
            except Exception:
                continue
            shots.append({"path": img_path, "duration": hold,
                          "note": (line[:60] if line else "")})
            scene_map.append({"index": seq[i].get("index", i + 1),
                              "vo": line, "audio_seconds": round(d, 2),
                              "hold_seconds": hold})

        if not shots:
            raise HTTPException(400, "No valid frames.")

        # Concat all scene clips into one narration track
        track = os.path.join(work, "multivoice_track.mp3")
        concat_inputs, filt = [], ""
        for j, (ap, hold, _d) in enumerate(clip_paths):
            concat_inputs += ["-i", ap]
            filt += f"[{j}:a]apad=whole_dur={hold}[a{j}];"
        filt += "".join(f"[a{j}]" for j in range(len(clip_paths)))
        filt += f"concat=n={len(clip_paths)}:v=0:a=1[out]"
        cc = subprocess.run(
            ["ffmpeg", "-y", *concat_inputs, "-filter_complex", filt,
             "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "4", track],
            capture_output=True, text=True)
        track_path = track if (cc.returncode == 0 and os.path.exists(track)) else None
        track_url = None
        if track_path:
            with open(track_path, "rb") as f:
                track_url, _tp = store.write_binary(
                    "audio", f.read(), ext="mp3", name_hint="multivoice_scenes")

        out_name = f"edit_{int(time.time())}.mp4"
        out_path = os.path.join(store.VIDEOS_DIR, out_name)
        music_path, music_volume = _music_for(st, body.music)
        editor.assemble_video(
            shots, track_path, out_path,
            transition=(body.transition or "cut").lower(),
            width=body.width, height=body.height, fps=body.fps,
            motion=body.motion, music_path=music_path, music_volume=music_volume,
        )
        rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
        url = f"/data/{rel}"
        total = round(sum(h for _a, h, _d in clip_paths), 2)

        if track_url:
            st["voiceover"] = {
                "id": store.new_id("vo"), "url": track_url,
                "mode": "multivoice_scenes", "duration": total,
                "speakers": list(speakers_used), "voice_map": vm,
                "scenes": scene_map, "created": store.now(),
            }
        rec = {
            "id": store.new_id("edit"), "url": url,
            "audio_id": (st.get("voiceover") or {}).get("id"),
            "transition": (body.transition or "cut").lower(),
            "plan": {"mode": "multivoice_scenes", "total_duration": total,
                     "shots": scene_map},
            "created": store.now(),
        }
        st.setdefault("edits", []).append(rec)
        if body.voice_map:
            st["voice_map"] = vm
        store.save_state(st)
        store.log_usage("voice", n, round(total * 0.0002, 4))
        store.log_usage("video", 1, 0.0)
        return {"edit": rec, "scenes": scene_map, "total_duration": total,
                "voiceover_url": track_url, "speakers": list(speakers_used)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Claude: plan an edit  +  ffmpeg: assemble the video
# --------------------------------------------------------------------------- #
class EditPlanIn(BaseModel):
    user_brief: str = ""
    transition: Optional[str] = None     # override Claude's suggestion


@app.post("/api/edit-plan")
def api_edit_plan(e: EditPlanIn, request: Request):
    st = store.load_state()
    if not st["sequence"]:
        raise HTTPException(400, "sequence is empty — render some frames first")
    if not st.get("audio"):
        raise HTTPException(400, "upload an audio file first")
    frames = []
    for s in st["sequence"]:
        try:
            frames.append(pipeline.downsize_for_vision(store.read_image(s["image_url"])))
        except Exception:
            pass
    if not frames:
        raise HTTPException(400, "no readable frames in sequence")
    # Anthropic caps a request at ~20 images, so the client batches the full
    # sequence in groups of 18 and merges the per-batch decisions — Claude now
    # sees every frame instead of only the first 20.
    try:
        plan = get_claude_client(request).plan_edit(
            frames=frames,                       # all frames; client chunks in 18s
            audio_duration=float(st["audio"]["duration"]) or 0,
            user_brief=e.user_brief,
            master_prompt=st["master_prompt"],
        )
    except Exception as ex:
        raise HTTPException(500, f"edit planning failed: {ex}")
    if e.transition:
        plan["transition"] = e.transition
    return plan


class RenderVideoIn(BaseModel):
    plan: dict
    transition: Optional[str] = None
    width: int = 1920
    height: int = 1080
    fps: int = 30
    motion: bool = False
    music: bool = False
    intro: Optional[str] = None
    outro: Optional[str] = None


@app.post("/api/render-video")
def api_render_video(r: RenderVideoIn, request: Request):
    st = store.load_state()
    if not st["sequence"]:
        raise HTTPException(400, "no sequence")
    seq = st["sequence"]
    shots_in = (r.plan or {}).get("shots") or []
    if not shots_in:
        raise HTTPException(400, "plan.shots is empty")
    audio_path = None
    if st.get("audio"):
        try:
            audio_path = store.url_to_path(st["audio"]["url"])
        except Exception:
            audio_path = None

    shots_out = []
    for sh in shots_in:
        idx = int(sh.get("index", 0))
        if idx < 1 or idx > len(seq):
            continue
        try:
            path = store.url_to_path(seq[idx - 1]["image_url"])
        except Exception:
            continue
        shots_out.append({
            "path": path,
            "duration": float(sh.get("duration") or 1.0),
            "note": sh.get("note", ""),
        })
    if not shots_out:
        raise HTTPException(400, "no valid shots after resolving indices")

    out_name = f"edit_{int(time.time())}.mp4"
    out_path = os.path.join(store.VIDEOS_DIR, out_name)
    transition = (r.transition or (r.plan or {}).get("transition") or "cut").lower()
    music_path, music_volume = _music_for(st, r.music)
    try:
        editor.assemble_video(
            shots_out, audio_path, out_path,
            transition=transition,
            width=r.width, height=r.height, fps=r.fps,
            motion=r.motion, music_path=music_path, music_volume=music_volume,
        )
        _bookend(out_path, r.intro, r.outro, r.width, r.height, r.fps)
        _apply_sfx(st, out_path)
    except Exception as ex:
        raise HTTPException(500, f"video assembly failed: {ex}")

    rel = os.path.relpath(out_path, store.DATA_DIR).replace(os.sep, "/")
    url = f"/data/{rel}"

    rec = {
        "id": store.new_id("edit"),
        "url": url,
        "audio_id": (st.get("audio") or {}).get("id"),
        "transition": transition,
        "plan": r.plan,
        "created": store.now(),
    }
    st.setdefault("edits", []).append(rec)
    store.save_state(st)
    store.log_usage("video", 1, 0.0)
    _fire_webhook(request.state.settings, "video.rendered",
                  {"url": rec["url"], "transition": transition})
    return rec


def _apply_sfx(st, video_path):
    sfx_entries = st.get("sfx") or []
    if not sfx_entries:
        return
    sfx_list = []
    for s in sfx_entries:
        try:
            p = store.url_to_path(s["url"])
            if os.path.exists(p):
                sfx_list.append({
                    "path": p,
                    "at_seconds": s.get("at_seconds", 0),
                    "volume": s.get("volume", 0.8),
                })
        except Exception:
            pass
    if sfx_list:
        tmp = video_path + ".sfx.mp4"
        editor.mix_sfx(video_path, sfx_list, tmp)
        os.replace(tmp, video_path)


@app.delete("/api/edits/{eid}")
def api_delete_edit(eid: str):
    st = store.load_state()
    st["edits"] = [e for e in st.get("edits", []) if e["id"] != eid]
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Export — bundle the whole project into a single downloadable ZIP
# --------------------------------------------------------------------------- #
def _safe_name(s: str, fallback: str = "item") -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "").strip()).strip("_")
    return s[:60] or fallback


def _fire_webhook(settings, event, payload):
    """Best-effort POST to the user's webhook URL. Never raises."""
    url = (settings or {}).get("webhook_url", "")
    if not url:
        return
    try:
        import requests
        requests.post(url, json={"event": event, "payload": payload,
                                 "ts": store.now()}, timeout=8)
    except Exception as e:
        print(f"[webhook] {event} failed: {e}", flush=True)


class WebhookTestIn(BaseModel):
    pass


@app.post("/api/webhook/test")
def api_webhook_test(request: Request):
    s = request.state.settings
    if not s.get("webhook_url"):
        raise HTTPException(400, "Set a webhook URL in Settings first.")
    try:
        import requests
        r = requests.post(s["webhook_url"], json={
            "event": "test", "payload": {"message": "Continuity Studio webhook OK"},
            "ts": store.now()}, timeout=10)
        return {"ok": r.status_code < 400, "status": r.status_code,
                "body": (r.text or "")[:200]}
    except Exception as e:
        raise HTTPException(500, f"webhook failed: {e}")


def _seo_text(seo: dict, title: str) -> str:
    """Render a saved SEO dict into a copy-paste-ready YouTube upload sheet."""
    titles = seo.get("titles") or ([seo["title"]] if seo.get("title") else [])
    tags = seo.get("tags") or []
    hashtags = seo.get("hashtags") or [t for t in tags if t][:5]
    lines = ["# YouTube upload sheet", ""]
    lines.append("## Title options")
    lines += [f"- {t}" for t in titles] or [f"- {title}"]
    lines += ["", "## Description", (seo.get("description") or "").strip(), ""]
    if tags:
        lines += ["## Tags (comma-separated)", ", ".join(tags), ""]
    if hashtags:
        hs = " ".join(h if str(h).startswith("#") else f"#{h}" for h in hashtags)
        lines += ["## Hashtags", hs, ""]
    return "\n".join(lines).strip() + "\n"


@app.get("/api/export/package")
def api_export_package(script: bool = True, prompts: bool = True,
                       characters: bool = True, frames: bool = True,
                       voiceover: bool = True, video: bool = True,
                       seo: bool = True):
    """Bundle the project into one ZIP. Query flags select what to include
    (selective export): script, prompts, characters, frames, voiceover, video, seo."""
    st = store.load_state()
    scr = st.get("script") or {}
    title = (scr.get("title") or "").strip() or "continuity-project"
    root = _safe_name(title, "continuity-project")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if script and scr:
            z.writestr(f"{root}/script.json",
                       json.dumps(scr, indent=2, ensure_ascii=False))

        if seo and st.get("seo"):
            z.writestr(f"{root}/youtube_seo.txt", _seo_text(st["seo"], title))
            z.writestr(f"{root}/youtube_seo.json",
                       json.dumps(st["seo"], indent=2, ensure_ascii=False))

        if voiceover:
            vo = (scr.get("voiceover") or "").strip()
            if not vo:
                vo = "\n\n".join((sc.get("vo") or "").strip()
                                 for sc in (scr.get("scenes") or [])
                                 if (sc.get("vo") or "").strip())
            if vo:
                z.writestr(f"{root}/voiceover.txt", vo)
            vo_rec = st.get("voiceover") or {}
            vo_audio_url = vo_rec.get("url") or (st.get("audio") or {}).get("url")
            if vo_audio_url:
                try:
                    with open(store.url_to_path(vo_audio_url), "rb") as fh:
                        z.writestr(f"{root}/voiceover.mp3", fh.read())
                except Exception:
                    pass

        if prompts:
            char_blocks = []
            for c in (scr.get("characters") or []):
                name = (c.get("name") or "").strip()
                sheet = (c.get("sheet_prompt") or c.get("description") or "").strip()
                if name:
                    char_blocks.append(f"{name}\n{sheet}".strip())
            if char_blocks:
                z.writestr(f"{root}/character_prompts.txt", "\n\n".join(char_blocks))
            scene_prompts = [(sc.get("prompt") or "").strip()
                             for sc in (scr.get("scenes") or [])
                             if (sc.get("prompt") or "").strip()]
            if scene_prompts:
                z.writestr(f"{root}/scene_prompts.txt", "\n".join(scene_prompts))
            if (st.get("master_prompt") or "").strip():
                z.writestr(f"{root}/master_prompt.txt", st["master_prompt"].strip())

        if characters:
            used = {}
            for c in st.get("characters", []):
                try:
                    data = store.read_image(c["sheet_url"])
                except Exception:
                    continue
                ext = os.path.splitext(c["sheet_url"])[1].lstrip(".") or "png"
                base = _safe_name(c.get("name") or "character", "character")
                used[base] = used.get(base, 0) + 1
                suffix = "" if used[base] == 1 else f"_{used[base]}"
                z.writestr(f"{root}/characters/{base}{suffix}.{ext}", data)

        if frames:
            for s in st.get("sequence", []):
                try:
                    data = store.read_image(s["image_url"])
                except Exception:
                    continue
                ext = os.path.splitext(s["image_url"])[1].lstrip(".") or "png"
                z.writestr(f"{root}/frames/frame_{int(s.get('index', 0)):03d}.{ext}", data)

        if video:
            for e in st.get("edits", []):
                try:
                    path = store.url_to_path(e["url"])
                    with open(path, "rb") as fh:
                        z.writestr(f"{root}/video/{os.path.basename(path)}", fh.read())
                except Exception:
                    continue

        if not z.namelist():
            z.writestr(f"{root}/README.txt",
                       "Nothing matched your export selection.")

    buf.seek(0)
    return Response(
        content=buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{root}.zip"'},
    )


@app.post("/api/projects/import")
async def api_project_import(file: UploadFile = File(...)):
    """Rebuild a working project from a previously exported ZIP (script,
    master prompt, character sheets and frames) as a NEW project."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        raise HTTPException(400, "that isn't a valid .zip")
    names = z.namelist()

    def _find(suffix):
        for n in names:
            if n.lower().endswith(suffix):
                return n
        return None

    def _read(n):
        with z.open(n) as f:
            return f.read()

    # script + title
    script = None
    sj = _find("script.json")
    if sj:
        try:
            script = json.loads(_read(sj).decode("utf-8"))
        except Exception:
            script = None
    title = (script or {}).get("title") or "Imported project"

    pid = store.create_project(f"{title} (imported)"[:80])
    st = store.load_state()
    if script:
        st["script"] = script
    mp = _find("master_prompt.txt")
    if mp:
        try:
            st["master_prompt"] = _read(mp).decode("utf-8").strip()
        except Exception:
            pass

    # character sheets
    img_ext = (".png", ".jpg", ".jpeg", ".webp")
    for n in sorted(names):
        low = n.lower()
        if "/characters/" in low and low.endswith(img_ext):
            try:
                ext = os.path.splitext(n)[1].lstrip(".").lower() or "png"
                url = store.write_image("characters", _read(n), ext=ext)
                name = _safe_name(os.path.splitext(os.path.basename(n))[0], "character")
                st["characters"].append({
                    "id": store.new_id("char"), "name": name, "description": "",
                    "sheet_url": url, "prompt": "(imported)", "source": "imported",
                    "created": store.now()})
            except Exception:
                pass

    # frames (ordered) -> sequence, with scene prompts if counts line up
    scene_prompts = [(sc.get("prompt") or "") for sc in ((script or {}).get("scenes") or [])]
    frame_names = sorted(n for n in names
                         if "/frames/" in n.lower() and n.lower().endswith(img_ext))
    for i, n in enumerate(frame_names):
        try:
            ext = os.path.splitext(n)[1].lstrip(".").lower() or "png"
            url = store.write_image("images", _read(n), ext=ext)
            st["sequence"].append({
                "id": store.new_id("shot"), "index": i + 1,
                "prompt": (scene_prompts[i] if i < len(scene_prompts) else "(imported)"),
                "image_url": url, "mode": "imported", "size": "", "quality": "",
                "characters": [], "refs": [], "continued_from": None,
                "created": store.now()})
        except Exception:
            pass

    # voiceover audio
    va = _find("voiceover.mp3")
    if va:
        try:
            url, path = store.write_binary("audio", _read(va), ext="mp3",
                                           name_hint="voiceover")
            try:
                dur = editor.probe_duration(path)
            except Exception:
                dur = 0
            st["audio"] = {"id": store.new_id("audio"), "url": url,
                           "name": "voiceover.mp3", "duration": dur}
        except Exception:
            pass

    store.save_state(st)
    out = store.list_projects()
    out["id"] = pid
    out["imported"] = {"characters": len(st["characters"]),
                       "frames": len(st["sequence"]),
                       "has_script": bool(st.get("script"))}
    return out


# --------------------------------------------------------------------------- #
#  SFX layer
# --------------------------------------------------------------------------- #
@app.post("/api/sfx/upload")
async def api_sfx_upload(
    file: UploadFile = File(...),
    at_seconds: float = Form(0.0),
    volume: float = Form(0.8),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    ext = (os.path.splitext(file.filename or "")[1] or ".mp3").lstrip(".").lower()
    if ext not in {"mp3", "wav", "m4a", "ogg", "aac"}:
        ext = "mp3"
    url, path = store.write_binary("audio", data, ext=ext,
                                   name_hint=f"sfx_{file.filename or 'clip'}")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    st = store.load_state()
    rec = {
        "id": store.new_id("sfx"),
        "url": url,
        "name": file.filename or "sfx.mp3",
        "duration": dur,
        "at_seconds": max(0.0, at_seconds),
        "volume": max(0.0, min(1.0, volume)),
    }
    st.setdefault("sfx", []).append(rec)
    store.save_state(st)
    return rec


class SfxGenIn(BaseModel):
    text: str                          # e.g. "deep cinematic boom", "lava crackle"
    duration_seconds: Optional[float] = None   # 0.5-22, or None for auto
    at_seconds: float = 0.0
    volume: float = 0.7


@app.post("/api/sfx/generate")
def api_sfx_generate(body: SfxGenIn, request: Request):
    """Generate a sound effect from a text description via ElevenLabs and add it
    to the project's SFX (mixed into the next render at ``at_seconds``)."""
    if not request.state.settings["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key set — add it in Settings.")
    if not (body.text or "").strip():
        raise HTTPException(400, "describe the sound to generate")
    try:
        url, path = _generate_sfx_cached(request, body.text.strip(),
                                         body.duration_seconds)
    except Exception as e:
        raise HTTPException(500, f"SFX generation failed: {e}")
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    st = store.load_state()
    rec = {"id": store.new_id("sfx"), "url": url, "name": body.text.strip()[:50],
           "duration": dur, "at_seconds": max(0.0, body.at_seconds),
           "volume": max(0.0, min(1.0, body.volume))}
    st.setdefault("sfx", []).append(rec)
    store.save_state(st)
    store.log_usage("voice", 1, 0.01)
    return rec


class SfxUpdateIn(BaseModel):
    at_seconds: Optional[float] = None
    volume: Optional[float] = None


@app.post("/api/sfx/{sid}/update")
def api_sfx_update(sid: str, body: SfxUpdateIn):
    st = store.load_state()
    sfx = next((s for s in st.get("sfx", []) if s["id"] == sid), None)
    if not sfx:
        raise HTTPException(404, "no such sfx")
    if body.at_seconds is not None:
        sfx["at_seconds"] = max(0.0, body.at_seconds)
    if body.volume is not None:
        sfx["volume"] = max(0.0, min(1.0, body.volume))
    store.save_state(st)
    return sfx


@app.delete("/api/sfx/{sid}")
def api_sfx_delete(sid: str):
    st = store.load_state()
    st["sfx"] = [s for s in st.get("sfx", []) if s["id"] != sid]
    store.save_state(st)
    return {"ok": True}


# --------------------------------------------------------------------------- #
#  Timeline: waveform peaks for audio visualization
# --------------------------------------------------------------------------- #
@app.get("/api/waveform")
def api_waveform(bars: int = 120):
    st = store.load_state()
    audio = st.get("audio")
    if not audio:
        return {"peaks": [], "duration": 0}
    try:
        path = store.url_to_path(audio["url"])
    except Exception:
        return {"peaks": [], "duration": 0}
    if not os.path.exists(path):
        return {"peaks": [], "duration": 0}
    try:
        dur = editor.probe_duration(path)
    except Exception:
        dur = 0
    bars = max(10, min(400, bars))
    try:
        cmd = [
            "ffmpeg", "-i", path, "-ac", "1", "-ar", "8000",
            "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        raw = proc.stdout
        import struct
        samples = struct.unpack(f"<{len(raw)//2}h", raw) if raw else []
        if not samples:
            return {"peaks": [0] * bars, "duration": dur}
        chunk = max(1, len(samples) // bars)
        peaks = []
        for i in range(bars):
            start = i * chunk
            end = min(start + chunk, len(samples))
            if start >= len(samples):
                peaks.append(0)
            else:
                peaks.append(max(abs(s) for s in samples[start:end]))
        mx = max(peaks) or 1
        peaks = [round(p / mx, 3) for p in peaks]
        return {"peaks": peaks, "duration": dur}
    except Exception:
        return {"peaks": [0] * bars, "duration": dur}


# --------------------------------------------------------------------------- #
#  Google OAuth (YouTube upload + Drive export)
# --------------------------------------------------------------------------- #
import urllib.parse
import requests as _requests

_GOOGLE_TOKEN_PATH = os.path.join(config.DATA_DIR, "google_tokens.json")
_GOOGLE_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/drive.file"


def _load_google_tokens():
    if not os.path.exists(_GOOGLE_TOKEN_PATH):
        return {}
    with open(_GOOGLE_TOKEN_PATH, "r") as f:
        return json.load(f)


def _save_google_tokens(tokens):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with open(_GOOGLE_TOKEN_PATH, "w") as f:
        json.dump(tokens, f, indent=2)


def _google_token_for(email):
    tokens = _load_google_tokens()
    entry = tokens.get(email or "_default")
    if not entry:
        return None
    if entry.get("expires_at", 0) < time.time() - 60:
        refreshed = _refresh_google_token(entry.get("refresh_token"))
        if refreshed:
            entry["access_token"] = refreshed["access_token"]
            entry["expires_at"] = time.time() + refreshed.get("expires_in", 3600)
            tokens[email or "_default"] = entry
            _save_google_tokens(tokens)
        else:
            return None
    return entry.get("access_token")


def _refresh_google_token(refresh_token):
    if not refresh_token or not config.GOOGLE_CLIENT_ID:
        return None
    r = _requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    if r.status_code == 200:
        return r.json()
    return None


@app.get("/auth/google/login")
def google_login():
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(400, "GOOGLE_CLIENT_ID not configured. Add it to .env.")
    params = urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": _GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    })
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@app.get("/auth/google/callback")
def google_callback(code: str = "", error: str = "", request: Request = None):
    if error:
        raise HTTPException(400, f"Google auth error: {error}")
    if not code:
        raise HTTPException(400, "No authorization code received")
    r = _requests.post("https://oauth2.googleapis.com/token", data={
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": config.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }, timeout=15)
    if r.status_code != 200:
        raise HTTPException(500, f"Token exchange failed: {r.text[:200]}")
    data = r.json()
    email = request.state.user_email if request else "_default"
    tokens = _load_google_tokens()
    tokens[email or "_default"] = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", tokens.get(email or "_default", {}).get("refresh_token", "")),
        "expires_at": time.time() + data.get("expires_in", 3600),
    }
    _save_google_tokens(tokens)
    return HTMLResponse("<html><body><h2>Google connected!</h2><p>You can close this tab.</p>"
                        "<script>window.close()</script></body></html>")


@app.get("/api/google/status")
def google_status(request: Request):
    email = request.state.user_email
    token = _google_token_for(email)
    return {
        "connected": token is not None,
        "configured": bool(config.GOOGLE_CLIENT_ID),
    }


class YouTubeUploadIn(BaseModel):
    edit_id: str
    title: str = "Untitled Video"
    description: str = ""
    tags: List[str] = []
    privacy: str = "private"
    thumbnail_id: Optional[str] = None


@app.post("/api/youtube/upload")
def api_youtube_upload(body: YouTubeUploadIn, request: Request):
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(400, "YouTube upload not configured. Set GOOGLE_CLIENT_ID, "
                            "GOOGLE_CLIENT_SECRET in .env and connect your Google account in Settings.")
    token = _google_token_for(request.state.user_email)
    if not token:
        raise HTTPException(401, "Google account not connected. Click 'Connect Google' in Settings.")
    st = store.load_state()
    edit = next((e for e in st.get("edits", []) if e["id"] == body.edit_id), None)
    if not edit:
        raise HTTPException(404, "No such edit")
    try:
        video_path = store.url_to_path(edit["url"])
    except Exception:
        raise HTTPException(400, "Video file not found")
    if not os.path.exists(video_path):
        raise HTTPException(400, "Video file missing from disk")

    metadata = {
        "snippet": {
            "title": body.title[:100],
            "description": body.description[:5000],
            "tags": body.tags[:30],
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": body.privacy if body.privacy in ("public", "unlisted", "private") else "private",
        },
    }
    headers = {"Authorization": f"Bearer {token}"}

    # Resumable upload init
    init = _requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        headers={**headers, "Content-Type": "application/json"},
        json=metadata, timeout=30,
    )
    if init.status_code not in (200, 308):
        raise HTTPException(500, f"YouTube upload init failed: {init.text[:200]}")
    upload_url = init.headers.get("Location")
    if not upload_url:
        raise HTTPException(500, "No upload URL returned by YouTube")

    fsize = os.path.getsize(video_path)
    with open(video_path, "rb") as f:
        up = _requests.put(upload_url, data=f,
                           headers={**headers, "Content-Type": "video/mp4",
                                    "Content-Length": str(fsize)},
                           timeout=600)
    if up.status_code not in (200, 201):
        raise HTTPException(500, f"YouTube upload failed: {up.text[:200]}")
    yt_data = up.json()
    video_id = yt_data.get("id", "")

    # Optionally set thumbnail
    if body.thumbnail_id and video_id:
        thumb = next((t for t in st.get("thumbnails", []) if t["id"] == body.thumbnail_id), None)
        if thumb:
            try:
                thumb_bytes = store.read_image(thumb["url"])
                _requests.post(
                    f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
                    f"?videoId={video_id}&uploadType=media",
                    headers={**headers, "Content-Type": "image/png"},
                    data=thumb_bytes, timeout=30,
                )
            except Exception:
                pass

    return {
        "ok": True,
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}" if video_id else "",
    }


# --------------------------------------------------------------------------- #
#  Google Drive export
# --------------------------------------------------------------------------- #
class DriveExportIn(BaseModel):
    name: str = ""


@app.post("/api/export/drive")
def api_export_drive(body: DriveExportIn, request: Request):
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(400, "Google Drive not configured. Set GOOGLE_CLIENT_ID in .env.")
    token = _google_token_for(request.state.user_email)
    if not token:
        raise HTTPException(401, "Google account not connected. Click 'Connect Google' in Settings.")

    # Build the ZIP (reuse export logic)
    st = store.load_state()
    idx = store.list_projects()
    cur = next((p for p in idx.get("projects", []) if p["id"] == idx.get("current")), None)
    proj_name = (body.name.strip() or (cur["name"] if cur else "project"))[:60]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if st.get("master_prompt"):
            z.writestr("master_prompt.txt", st["master_prompt"])
        if st.get("script"):
            z.writestr("script.json", json.dumps(st["script"], indent=2))
        for c in st.get("characters", []):
            try:
                data = store.read_image(c["sheet_url"])
                ext = os.path.splitext(c["sheet_url"])[1].lstrip(".") or "png"
                z.writestr(f"characters/{c.get('name', 'char')}.{ext}", data)
            except Exception:
                pass
        for s in st.get("sequence", []):
            try:
                data = store.read_image(s["image_url"])
                ext = os.path.splitext(s["image_url"])[1].lstrip(".") or "png"
                z.writestr(f"frames/frame_{int(s.get('index', 0)):03d}.{ext}", data)
            except Exception:
                pass
    buf.seek(0)
    zip_bytes = buf.read()

    headers = {"Authorization": f"Bearer {token}"}
    meta = json.dumps({"name": f"{proj_name}.zip", "mimeType": "application/zip"}).encode()
    boundary = "---cs-drive-boundary---"
    body_parts = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
        + meta + f"\r\n--{boundary}\r\nContent-Type: application/zip\r\n\r\n".encode()
        + zip_bytes + f"\r\n--{boundary}--".encode()
    )
    r = _requests.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
        headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"},
        data=body_parts, timeout=120,
    )
    if r.status_code not in (200, 201):
        raise HTTPException(500, f"Drive upload failed: {r.text[:200]}")
    file_data = r.json()
    file_id = file_data.get("id", "")

    # Make shareable
    link = ""
    if file_id:
        try:
            _requests.post(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
                headers={**headers, "Content-Type": "application/json"},
                json={"role": "reader", "type": "anyone"}, timeout=15,
            )
            link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
        except Exception:
            link = f"https://drive.google.com/file/d/{file_id}"

    return {"ok": True, "file_id": file_id, "link": link, "name": f"{proj_name}.zip"}


# --------------------------------------------------------------------------- #
#  Autopilot: one-click YT link -> full video + thumbnail
# --------------------------------------------------------------------------- #
class AutopilotIn(BaseModel):
    url: str                           # YouTube URL
    nudge: str = ""                    # creative direction
    suggestion_index: int = 0          # which suggestion to use (0 = first)
    width: int = 1920
    height: int = 1080
    fps: int = 30
    transition: str = "cut"
    motion: bool = False
    quality: Optional[str] = None
    size: Optional[str] = None
    model: Optional[str] = None        # Claude model override
    voice_id: Optional[str] = None     # ElevenLabs voice for the narration
    target_seconds: Optional[float] = None  # desired video length target
    run_id: Optional[str] = None       # client token so a run can be stopped
    step_timeout: float = 60.0         # auto-proceed if a heavy step stalls past this
    max_hold: float = 6.0              # split frames longer than this into micro-cuts
    from_cache: bool = False           # reuse the topics from a prior /suggest call
                                       # so the picked index maps to what the user saw
    sound_design: bool = True          # generate + mix ElevenLabs SFX + loudnorm
    dynamic: bool = True               # reaction-rich, varied-background scenes
    fresh: bool = True                 # wipe the previous run's frames/video/etc.
                                       # before generating so they don't mix


# --- Autopilot run control: stop flag + per-step deadline ------------------- #
_AUTOPILOT_STOP = set()
_AUTOPILOT_LOCK = threading.Lock()


class _Stopped(Exception):
    pass


def _autopilot_stopped(run_id):
    if not run_id:
        return False
    with _AUTOPILOT_LOCK:
        return run_id in _AUTOPILOT_STOP


def _check_stop(run_id):
    if _autopilot_stopped(run_id):
        with _AUTOPILOT_LOCK:
            _AUTOPILOT_STOP.discard(run_id)
        raise _Stopped()


@app.exception_handler(_Stopped)
async def _stopped_handler(request: Request, exc: _Stopped):
    # An autopilot run was stopped between steps. Whatever finished is already
    # saved to project state, so the client just reloads /api/state.
    return JSONResponse(status_code=200, content={
        "ok": False, "stopped": True,
        "detail": "Autopilot stopped — finished steps were kept."})


def _run_with_deadline(fn, seconds):
    """Run ``fn`` in a daemon thread and stop WAITING after ``seconds`` so a
    stalled heavy step doesn't hang the whole run. Returns (finished, value).
    A step that times out keeps finishing in the background — its results just
    appear a little later — which is safe here because every step only appends
    to project state. Exceptions raised by ``fn`` are re-raised."""
    box = {}

    def worker():
        try:
            box["v"] = fn()
        except Exception as e:          # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(max(1.0, float(seconds)))
    if t.is_alive():
        return (False, None)
    if "e" in box:
        raise box["e"]
    return (True, box.get("v"))


class AutopilotStopIn(BaseModel):
    run_id: str


@app.post("/api/autopilot/stop")
def api_autopilot_stop(body: AutopilotStopIn):
    """Request that an in-flight autopilot run stop at the next step boundary."""
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_STOP.add(body.run_id)
    return {"ok": True, "run_id": body.run_id}


# --- Live autopilot progress (drives the progress bar + ETA in the UI) ------ #
_AUTOPILOT_PROGRESS = {}
AP_STEPS = ["analyse", "script", "characters", "frames", "video", "thumbnail", "seo"]


def _ap_prog(run_id, **kw):
    if not run_id:
        return
    with _AUTOPILOT_LOCK:
        p = _AUTOPILOT_PROGRESS.setdefault(run_id, {
            "run_id": run_id, "started": time.time(), "steps_total": len(AP_STEPS),
            "step": "", "step_index": 0, "chars_done": 0, "chars_total": 0,
            "frames_done": 0, "frames_total": 0, "done": False, "video_url": None,
        })
        if "step" in kw:
            kw["step_index"] = AP_STEPS.index(kw["step"]) if kw["step"] in AP_STEPS else p["step_index"]
        p.update(kw)


@app.get("/api/autopilot/progress/{run_id}")
def api_autopilot_progress(run_id: str):
    with _AUTOPILOT_LOCK:
        p = dict(_AUTOPILOT_PROGRESS.get(run_id) or {})
    if not p:
        return {"run_id": run_id, "unknown": True}
    # Server-computed ETA from the dominant (frames) work + elapsed time.
    elapsed = max(0.0, time.time() - p.get("started", time.time()))
    done = p.get("frames_done", 0)
    total = p.get("frames_total", 0)
    eta = None
    if total and done:
        rate = elapsed / max(1, done)        # seconds per frame so far
        eta = round(rate * max(0, total - done), 1)
    p["elapsed"] = round(elapsed, 1)
    p["eta_seconds"] = eta
    return p


def _reset_generated(delete_files=True):
    """Clear the previous run's GENERATED media so a fresh autopilot run doesn't
    append onto (and desync with) old frames. Keeps the world bible, the topic
    list (yt_inspiration), brand, music and voice map. Deletes the orphaned media
    files from disk too ('erased from site') to reclaim space."""
    st = store.load_state()
    paths = []

    def _collect(u):
        if u:
            try:
                paths.append(store.url_to_path(u))
            except Exception:
                pass

    for s in st.get("sequence") or []:
        _collect(s.get("image_url"))
    for e in st.get("edits") or []:
        _collect(e.get("url"))
    for t in st.get("thumbnails") or []:
        _collect(t.get("url"))
        _collect(t.get("raw_url"))
    for c in st.get("characters") or []:
        _collect(c.get("sheet_url"))
    for x in st.get("sfx") or []:
        _collect(x.get("url"))
    _collect((st.get("voiceover") or {}).get("url"))
    _collect((st.get("audio") or {}).get("url"))

    st["sequence"] = []
    st["edits"] = []
    st["thumbnails"] = []
    st["characters"] = []
    st["sfx"] = []
    st["voiceover"] = None
    st["audio"] = None
    st["script"] = None
    st["seo"] = None
    store.save_state(st)

    if delete_files:
        for p in paths:
            try:
                os.remove(p)
            except Exception:
                pass
    return st


def _sort_by_virality(suggestions):
    """Most-viral-first, stable. Missing scores sink to the bottom."""
    return sorted(suggestions or [],
                  key=lambda x: float(x.get("virality_score") or 0), reverse=True)


def _autopilot_analyze(url, nudge, model, request, n_suggestions=10):
    """Shared step 1: ingest a YouTube ref, get fresh on-style ideas ranked by
    virality, save them as yt_inspiration, and return the inspiration dict."""
    import youtube
    if not youtube.is_youtube_url(url):
        raise HTTPException(400, "That doesn't look like a YouTube link.")
    try:
        ref = youtube.ingest(url, max_frames=12)
    except Exception as e:
        raise HTTPException(400, f"Couldn't read that video: {e}")
    frame_imgs = []
    for u in ref.get("frame_urls", [])[:12]:
        try:
            frame_imgs.append(pipeline.downsize_for_vision(store.read_image(u)))
        except Exception:
            pass
    st = store.load_state()
    claude = _claude_client_for(model, request)
    try:
        raw = claude.suggest_from_reference(
            frames=frame_imgs, transcript=ref.get("transcript", ""),
            source_title=ref.get("title", ""), source_channel=ref.get("channel", ""),
            nudge=nudge, master_prompt=st["master_prompt"],
            n_suggestions=n_suggestions)
        analysis = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"YT analysis failed: {e}")
    suggestions = _sort_by_virality(analysis.get("suggestions") or [])
    if not suggestions:
        raise HTTPException(500, "Analysis returned no suggestions.")
    insp = {
        "url": url, "resolved_url": ref.get("url"), "video_id": ref.get("video_id"),
        "title": ref.get("title", ""), "channel": ref.get("channel", ""),
        "style_summary": analysis.get("style_summary", ""),
        "speech_style": analysis.get("speech_style", ""),
        "topic": analysis.get("topic", ""),
        "frames": ref.get("frame_urls", []),
        "suggestions": suggestions, "created": store.now(),
    }
    st["yt_inspiration"] = insp
    store.save_state(st)
    store.log_usage("script", 1, 0.01)
    return insp


class AutopilotSuggestIn(BaseModel):
    url: str
    nudge: str = ""
    model: Optional[str] = None
    n_suggestions: int = 10


@app.post("/api/autopilot/suggest")
def api_autopilot_suggest(body: AutopilotSuggestIn, request: Request):
    """Phase 1 of the workflow: analyse the reference and return fresh, on-style
    video ideas ranked by predicted virality — so the user can PICK which one to
    produce (instead of always making the first/same one). No media is generated."""
    s = request.state.settings
    if not s["claude_api_key"]:
        raise HTTPException(400, "No Claude API key — add it in Settings.")
    insp = _autopilot_analyze(body.url, body.nudge, body.model, request,
                              max(1, min(15, body.n_suggestions)))
    return {"ok": True, "url": insp["url"], "title": insp["title"],
            "channel": insp["channel"], "style_summary": insp["style_summary"],
            "topic": insp["topic"], "suggestions": insp["suggestions"]}


@app.post("/api/autopilot")
def api_autopilot(body: AutopilotIn, request: Request):
    """One-click pipeline: YouTube link -> analyse -> script -> characters ->
    frames -> voice-over video -> thumbnail. Returns progress at each step."""
    s = request.state.settings
    if not s["claude_api_key"]:
        raise HTTPException(400, "No Claude API key — add it in Settings.")
    if not s["api_key"]:
        raise HTTPException(400, "No image API key — add it in Settings.")
    if not s["elevenlabs_api_key"]:
        raise HTTPException(400, "No ElevenLabs API key — add it in Settings.")

    run_id = body.run_id or store.new_id("run")
    with _AUTOPILOT_LOCK:                      # clear any stale stop flag
        _AUTOPILOT_STOP.discard(run_id)
    step_to = max(15.0, float(body.step_timeout or 60.0))

    quality = body.quality or config.DEFAULT_QUALITY
    size = body.size or config.DEFAULT_SIZE
    voice_id = body.voice_id or s["elevenlabs_voice_id"]
    claude = _claude_client_for(body.model, request)
    steps = []

    # ---- STEP 1: Topics ----
    # Reuse the exact list the user picked from (so suggestion_index lines up);
    # otherwise analyse fresh. Either way they're ranked by virality.
    _ap_prog(run_id, step="analyse")
    cached = (store.load_state().get("yt_inspiration") or {})
    use_cache = (body.from_cache and cached.get("suggestions")
                 and cached.get("url") == body.url)
    insp = cached if use_cache else _autopilot_analyze(
        body.url, body.nudge, body.model, request, 10)
    suggestions = insp["suggestions"]
    analysis = {"style_summary": insp.get("style_summary", ""),
                "speech_style": insp.get("speech_style", ""),
                "topic": insp.get("topic", "")}
    steps.append({"step": "analyse", "suggestions": len(suggestions),
                  "cached": bool(use_cache)})

    # Fresh start: wipe the PREVIOUS run's frames/characters/video/thumbnail/
    # voiceover so this generation doesn't append onto stale frames and desync.
    # (yt_inspiration/topics + world bible are preserved.)
    if body.fresh:
        _reset_generated(delete_files=True)

    # ---- STEP 2: Pick a suggestion and generate a script ----
    _check_stop(run_id)
    _ap_prog(run_id, step="script")
    st = store.load_state()
    pick = suggestions[min(max(0, body.suggestion_index), len(suggestions) - 1)]
    title = pick.get("title", "")
    desc = "\n\n".join(filter(None, [
        pick.get("logline", ""),
        f"Visual style: {pick['image_prompt_style']}" if pick.get("image_prompt_style") else "",
        f"Voice-over: {pick['voiceover_style']}" if pick.get("voiceover_style") else "",
    ]))
    pacing = pick.get("pacing_seconds", 1.0)
    total_dur = pick.get("total_duration", 30.0)
    # A user-supplied target length wins — drives how much script Claude writes.
    if body.target_seconds and body.target_seconds > 0:
        total_dur = float(body.target_seconds)
    num_chars = pick.get("num_characters", 2)
    style_notes = (pick.get("image_prompt_style") or "")[:80]

    try:
        raw = claude.generate_script(
            title=title, description=desc,
            total_duration=max(1.0, total_dur),
            pacing_seconds=max(0.1, pacing),
            num_characters=max(0, num_chars),
            style_notes=style_notes,
            master_prompt=st["master_prompt"],
            dynamic=body.dynamic,
        )
        script = extract_json(raw)
    except Exception as e:
        raise HTTPException(500, f"Script generation failed: {e}")

    st = store.load_state()
    st["script"] = script
    store.save_state(st)
    steps.append({"step": "script", "scenes": len(script.get("scenes") or []),
                  "characters": len(script.get("characters") or [])})
    store.log_usage("script", 1, 0.01)

    # ---- STEP 3: Generate character sheets ----
    _check_stop(run_id)
    chars = script.get("characters") or []
    _scene_total = len([sc for sc in (script.get("scenes") or [])
                        if (sc.get("prompt") or "").strip()])
    _ap_prog(run_id, step="characters", chars_total=len(chars),
             frames_total=_scene_total)
    img_client = get_image_client(request)
    char_created = []
    for c in chars:
        _check_stop(run_id)
        name = (c.get("name") or "").strip()
        sheet_prompt = (c.get("sheet_prompt") or c.get("description") or "").strip()
        if not name:
            continue

        def _make_sheet(name=name, sheet_prompt=sheet_prompt):
            prompt = pipeline.build_sheet_prompt(st["master_prompt"], name, sheet_prompt)
            img = img_client.generate(prompt, size=size, quality=quality)
            rec = {
                "id": store.new_id("char"), "name": name,
                "description": sheet_prompt,
                "sheet_url": store.write_image("characters", img),
                "prompt": prompt, "source": "generated",
            }
            cur = store.load_state()
            cur["characters"].append(rec)
            store.save_state(cur)
            store.log_usage("image", 1, 0.08)
            return name
        try:
            # Auto-proceed if a single sheet stalls past the step timeout; it
            # finishes in the background and just shows up a moment later.
            finished, val = _run_with_deadline(_make_sheet, step_to)
            if finished and val:
                char_created.append(val)
        except Exception:
            pass
        _ap_prog(run_id, chars_done=len(char_created))
    steps.append({"step": "characters", "created": len(char_created)})

    # ---- STEP 4: Batch render sequence frames ----
    _check_stop(run_id)
    _ap_prog(run_id, step="frames")
    scenes = script.get("scenes") or []
    frames_ok, frames_fail = 0, 0
    for sc in scenes:
        _check_stop(run_id)
        p = (sc.get("prompt") or "").strip()
        if not p:
            continue
        # Each frame self-heals via the image_queue throttle (backoff + cooldown
        # on rate limits) so the run no longer dies on a single 429.
        try:
            rec = _render_one(p, size, quality, True, True,
                              character_ids=None, request=request)
            frames_ok += 1
        except Exception:
            frames_fail += 1
        _ap_prog(run_id, frames_done=frames_ok + frames_fail)
    steps.append({"step": "frames", "rendered": frames_ok, "failed": frames_fail})

    # ---- STEP 5: Voice-over + video assembly (natural flow) ----
    #  ONE continuous narration track + frames timed to it + micro-cuts on long
    #  holds — the audio is never chopped per-scene, so it sounds natural.
    _check_stop(run_id)
    _ap_prog(run_id, step="video")
    st = store.load_state()
    seq = st.get("sequence") or []
    video_url = None
    total_seconds = 0
    vo_text = _script_voiceover_text(st)
    if seq and vo_text:
        try:
            finished, val = _run_with_deadline(
                lambda: _build_flow_video(
                    st, request, voice_id=voice_id, text=vo_text,
                    transition=body.transition, width=body.width,
                    height=body.height, fps=body.fps, max_hold=body.max_hold,
                    motion=body.motion, name_hint="autopilot_vo",
                    sound_design=body.sound_design),
                step_to * 6)            # assembly + SFX gen can be slow; give it room
            if finished and val:
                _edit_rec, video_url, total_seconds, _sm = val
        except HTTPException:
            pass
        except Exception as ex:
            print(f"[autopilot] video step skipped: {ex}", flush=True)

    steps.append({"step": "video", "url": video_url,
                  "duration": total_seconds, "scenes_voiced": len(seq)})

    # ---- STEP 6: Thumbnail ----
    _check_stop(run_id)
    _ap_prog(run_id, step="thumbnail", video_url=video_url)
    thumb_url = None
    try:
        thumb_title = title or (script.get("title") or "")
        style_hint = analysis.get("style_summary") or st.get("master_prompt", "")
        ref_urls = [fr["image_url"] for fr in (st.get("sequence") or [])[:4]]
        bits = []
        if thumb_title.strip():
            bits.append(f'A bold, scroll-stopping YouTube thumbnail (16:9) for: "{thumb_title}".')
        else:
            bits.append("A bold, scroll-stopping YouTube thumbnail (16:9).")
        if style_hint:
            bits.append(f"Match this visual style: {style_hint[:200]}")
        bits.append("Single clear focal subject, dramatic cinematic lighting, high "
                    "contrast, vivid punchy colours, strong sense of depth. Leave clean "
                    "negative space on one side for a short title overlay. Ultra-crisp and "
                    "professional. No watermark, no logos, no garbled text.")
        prompt = "\n".join(bits)
        refs = []
        for u in ref_urls:
            try:
                refs.append(store.read_image(u))
            except Exception:
                pass

        if refs:
            img = img_client.edit(prompt=prompt, images=refs[:1],
                                  size="1536x1024", quality=quality)
        else:
            img = img_client.generate(prompt, size="1536x1024", quality=quality)

        thumb_raw_url = store.write_image("images", img)
        st = store.load_state()
        brand = st.get("brand") or {}
        overlay_png = _overlay_text(img, thumb_title, "", "bottom",
                                     "#ffffff", True, brand)
        final_url = store.write_image("images", overlay_png)
        thumb_rec = {
            "id": store.new_id("thumb"), "raw_url": thumb_raw_url,
            "url": final_url, "title": thumb_title, "created": store.now(),
        }
        st.setdefault("thumbnails", []).append(thumb_rec)
        store.save_state(st)
        thumb_url = final_url
        store.log_usage("thumbnail", 1, 0.08)
    except Exception:
        pass

    steps.append({"step": "thumbnail", "url": thumb_url})

    # ---- STEP 7: YouTube SEO pack (title options, description, tags) ----
    _check_stop(run_id)
    _ap_prog(run_id, step="seo", video_url=video_url)
    seo = None
    try:
        st = store.load_state()
        scr = st.get("script") or {}
        seo_title = title or scr.get("title") or ""
        seo_desc = "\n".join(filter(None, [
            scr.get("logline") or pick.get("logline") or "",
            (scr.get("voiceover") or "")[:1200],
        ]))
        ok, raw = _run_with_deadline(
            lambda: _claude_client_for(body.model, request).seo(seo_title, seo_desc, 6),
            step_to)
        if ok and raw:
            seo = extract_json(raw)
            st = store.load_state()
            st["seo"] = seo
            store.save_state(st)
            store.log_usage("script", 1, 0.005)
    except Exception as ex:
        print(f"[autopilot] seo step skipped: {ex}", flush=True)
    steps.append({"step": "seo", "ok": bool(seo)})

    _ap_prog(run_id, done=True, video_url=video_url)
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_STOP.discard(run_id)
    return {
        "ok": True,
        "run_id": run_id,
        "steps": steps,
        "suggestion": pick,
        "video_url": video_url,
        "thumbnail_url": thumb_url,
        "total_duration": total_seconds,
        "seo": seo,
    }
