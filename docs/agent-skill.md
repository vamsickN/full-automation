---
name: continuity-studio
description: Use when working on the Continuity Studio project at E:\\time now\\continuity-studio — a YouTube-to-vertical-video autopilot that analyzes reference videos via Claude, generates character sheets and scene frames via gpt-image-2, and assembles them with TTS voice-over into short-form videos. Covers the hard-won gotchas of the past several sessions.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [continuity-studio, video-generation, autopilot, gpt-image, derouter, elevenlabs, piper, whisper, a-v-sync, art-style, bug-bash]
    related_skills: [requesting-code-review, plan, test-driven-development]
---

# Continuity Studio

## Overview

Continuity Studio is a single-file FastAPI app (`app.py`, ~8000 lines) plus
`pipeline.py`, `derouter.py` (image client), `claude_client.py` (script +
analysis), `editor.py` (ffmpeg assembly), `voice.py` (multi-provider TTS),
`transcribe.py` (local Whisper), `image_queue.py` (rate-limit-aware
job queue), `audio_gen.py` (SFX), `vault_crypto.py` (encrypted
per-user settings in `vault.json`), and `static/index.html` (the UI).

The flagship feature is the **autopilot workflow**: paste YouTube links,
Claude analyzes their shared art style / pacing / voice, generates a script
in the same style, renders per-scene character sheets and image frames via
gpt-image-2 on derouter, synthesizes voice-over, and assembles the final MP4
with per-frame A/V sync.

The repo is at `https://github.com/sharmiladevi888/full-automation` on
`master` — push workflow is `git add -A && git commit -m "..." && git push
origin master`. Local mirrors under `~/.hermes/profiles/.../skills/` are
seeded from this canonical repo.

## When to Use

- Editing anything in `E:\time now\continuity-studio\continuity-studio` (the
  single canonical working copy)
- Debugging a "no / wrong / desynced" issue in the autopilot pipeline
- Adding a new TTS / image / video provider
- Touching the World Bible, master prompt, character sheets, scene prompts,
  video assembler, or A/V sync logic
- Any time the user pastes a Continuity Studio log line or describes a
  "generated video doesn't match source / frames desync / aspect ratio wrong"
  symptom

**Don't use for:** generic image gen, generic ffmpeg work, or projects under
`E:\time now\continuity-studio\<other-project>` (this skill is scoped to
`continuity-studio\continuity-studio` only — the full-automation repo).

## Run / Restart Cheatsheet

The app lives on port 8000. Stale uvicorn processes hold the port → **always
free it before restarting**. On Windows MSYS / git-bash:

```bash
# 1. Find and kill any listener on 8000
for p in $(netstat -ano | grep ':8000.*LISTENING' | awk '{print $5}'); do
  taskkill //F //PID $p
done

# 2. Start the server in the background
cd "/e/time now/continuity-studio/continuity-studio" && \
  /c/Users/sickv/continuity-venv/Scripts/python.exe -m uvicorn app:app \
    --host 127.0.0.1 --port 8000

# 3. Verify
sleep 4 && curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8000/
```

The `kill` requires user approval when it touches a tracked process — ask once,
don't batch.

## Config + Vault (the settings source of truth)

Settings live in **two places** and both must agree:

1. `config.py` — env-driven defaults. New keys: add here with
   `_get("KEY", "default")`. Examples: `MULTI_IMAGE_EDIT`, `STYLE_REF_COUNT`,
   `DEFAULT_SIZE`, `DEFAULT_QUALITY`, `IMAGE_MAX_CONCURRENCY`,
   `IMAGE_RATE_LIMIT_COOLDOWN_MS`, `IMAGE_BACKOFF_BASE_MS`.
2. `vault.json` — **encrypted**, keyed by user email. Decrypted with
   `vault_crypto.decrypt_vault`. Per-user overrides for `multi_image_edit`,
   `api_key`, `base_url`, `model`, `voice_provider`, all TTS keys, etc.

```python
import app
v = app.load_vault()
s = v["sickvamsick@gmail.com"]  # or current user
api_key = s["api_key"]          # str
base_url = s["base_url"]        # e.g. https://api-direct.derouter.network/openai/v1
model = s["model"]              # e.g. gpt-image-2
multi_image_edit = s.get("multi_image_edit", config.MULTI_IMAGE_EDIT)

# To persist a per-user setting change for ALL users:
for k, sub in v.items():
    if isinstance(sub, dict) and "base_url" in sub:
        sub["multi_image_edit"] = True
app.save_vault(v)
```

## Hard-Won Gotchas (READ THESE BEFORE EDITING)

These are the bugs we discovered and fixed across a long multi-session debug.
**Don't reintroduce them.** If you see any of these patterns in the code, do
not "clean them up" — they encode past lessons.

### 1. Art style MUST follow the source video

The codebase was originally hard-wired to a stick-figure / apocalypse project.
Six separate hooks were silently forcing stick figures + a lava/meteor theme
and discarding the analysed video style. **All six were removed** — the
behaviour is now "reproduce the source video's actual art style, whatever it
genuinely is." If you need to add another style hint, add a positive
STYLE FIDELITY instruction, never a NEGATIVE style ban.

The six removed hooks (do not re-add):
- `claude_client.generate_script` — old "STYLE BAN" forcing stick figures
- `claude_client.generate_missing_scenes` — same
- `claude_client` analyzer (×2) — same in `image_prompt_style` suggestion
- `claude_client.transcript_scenes` — same
- `app._sanitize_prompt` — **the worst one**: regex-rewrote ANY
  hand-drawn/cartoon/doodle keyword in every scene + character prompt to
  "stick man / stick figure style" RIGHT BEFORE rendering. This ran on
  every prompt. Now a no-op (`return text or ""`).

### 2. Aspect ratio: 16:9 selected but 9:16 generated

gpt-image-2's `/images/edits` endpoint IGNORES the `size` parameter and
anchors the output to the **input reference image's aspect ratio**. So
when refs are composited into a portrait contact-sheet, every frame came
back portrait regardless of the requested size.

**Fixes in place (do not undo):**
- `pipeline.enforce_aspect()` — hard guarantee. After every generate/edit,
  center-crop to the target aspect, then resize to exact pixels.
  Wired into BOTH `derouter._generate_once` AND `derouter._edit_once` returns.
- `pipeline.contact_sheet(target_size=...)` — pads the ref grid onto the
  target aspect so the model is biased to the right orientation in the first
  place.
- `pipeline._fit_canvas_to_aspect` — letterbox/pillarbox helper.
- `config.DEFAULT_SIZE = "1536x1024"` for landscape; vertical projects
  override to `"1024x1536"`.

### 3. References: SEPARATE images, NOT contact-sheet

Derouter gpt-image-2 supports repeated `image[]` fields (verified with a
2-image and 3-image HTTP 200 probe). Feeding refs separately gives the model
sharper style guidance than a grid and avoids the portrait-grid → portrait-
output trap.

**Multi-image is ON by default. Wire-up (don't break it):**
- `config.MULTI_IMAGE_EDIT = True` (env default)
- `vault.json` per-user `multi_image_edit: true` for all users
- `derouter.ImageClient.edit(..., multi_image_edit=...)` accepts per-call
  override; `_edit_once` honors it (this is critical — derouter was
  originally reading only `config.MULTI_IMAGE_EDIT` and ignoring per-call,
  so the vault toggle had no effect on autopilot frames)
- All 6 `client.edit(...)` call sites in `app.py` pass
  `multi_image_edit=multi_image_edit` to actually use the toggle
- Contact-sheet path is the **fallback** on multi-image failure (try
  multi first, fall back to single contact-sheet on error)
- When refs go as separate images, the per-image captions burned into
  contact-sheet cells are LOST. Compensate with `pipeline.ref_manifest()`
  which adds a text "Image 1: STYLE REF, copy art style only / Image 2:
  STYLE REF / Image 3: CHARACTER SHEET for Maya, identity only" block
  in the prompt — wired into `pipeline.build_full_prompt(ref_meta=...)`
  only when `multi_image_edit=True` and `len(refs) > 1`

### 4. Style-anchor count

`config.SYLE_REF_COUNT` (default 5) — how many source-video frames to feed
as STYLE REFs. Applied in BOTH char-sheet paths and BOTH scene paths via
`config.STYLE_REF_COUNT` (NOT hardcoded `[:3]` or `[:4]`). The user wants
4-5 frames because that gives a fuller read of source style variation.

### 5. A/V sync: pin each frame to its OWN VO line

The desync bug for non-ElevenLabs setups (MiMo/Deepgram/Piper + Whisper +
Claude edit): `_build_flow_video` mapped script scene VO to the rendered
sequence **positionally** (`tts_lines[i]` for `seq[i]`). When frames failed
to render (e.g. transient image-API 502), the surviving sequence was
shorter than the script, so every VO line after the first gap shifted onto
the wrong frame → cascading desync.

**Fix in place:** `_render_one` and `_render_one_for_queue` now accept
`scene_vo` and store it on the frame record as `rec["vo"]`.
`_build_flow_video` reads VO from `seq[i]["vo"]` first (frame-pinned,
sync-safe), falling back to the old positional script mapping only for
older sequences that predate `vo`-on-frame.

The autopilot frame loop passes the scene's vo: `scene_vo=(sc.get("vo") or
sc.get("narration") or sc.get("voice_over") or "")`.

The `_synth_per_scene_track` path (each scene synthesized separately,
measured, frame holds exactly that long) is already frame-perfect by
construction — it's the positional-mapping paths (timestamp + weight +
smart_edit) that needed hardening.

### 6. Duration: video length = narration length (intentional)

The video length is the TTS audio length by design — that's what keeps
A/V sync frame-accurate. If the script's narration is shorter than the
user's `target_seconds`, the video is shorter than asked. **Do not**
stretch frame holds to hit the target — the audio is fixed length, so
stretching frames while audio stays put desyncs the narration from the
picture. Also, the VO-only mux path uses `-shortest` which truncates back
to audio length anyway.

The correct lever for a longer video is **more narration** — strengthen
the script word-count target so Claude writes enough words to fill the
duration (~2.5 words/second). The script prompt now spells out
"~N words total, don't come in short" using `total_duration * 2.5`.

If the user specifically wants the exact target length even at the cost
of a music-bed-only tail, the right implementation is to pad the audio
track (not the frames) to the target — but this isn't built yet. If
asked, build it in `editor.assemble_video` using `apad` and dropping
`-shortest` only when a music bed is present and the video is longer
than the audio.

### 7. Image provider hardening

- `derouter._snap_size` rounds dims to multiples of 16 (gpt-image rejects
  e.g. 1920x1080 → 1072; we send 1920x1072)
- OpenAI images/edits spec used for the multipart shape
- Contact-sheet path labelled with per-image captions
- Edit fallback chain: try `image[]` first, on 4xx/5xx fall back to
  single contact-sheet, on 402 (wallet) try OpenRouter fallback
  (`_openrouter_fallback()`), then surface the billing message
- `_EDITS_UNSUPPORTED` set remembers bases that 500 on `/images/edits`
  (e.g. 9Router) and routes those calls to plain `generate()` instead
- 502 bad-gateway = upstream derouter outage, not a code bug; our retry/
  backoff/cooldown is already in `image_queue.run_with_retry`

### 8. Prompt sanitization

`_sanitize_prompt` is now a no-op. The function still exists (many call
sites reference it) and is kept as a stub for backwards compatibility. Do
NOT reintroduce any "style keyword rewrite" logic — it will destroy
style fidelity the moment the user's source video uses hand-drawn/cartoon
language. If you need to strip something from prompts, do it specifically
(e.g. remove profanity, not art style).

### 9. Story / scene quality

The "DYNAMIC / HIGH-RETENTION MODE" prompt block in
`claude_client.generate_script` is intentionally **theme-agnostic** now.
It used to hardcode "lava ocean / toxic sky / meteor storm" which forced
every dynamic-mode video into an apocalypse — removed. Now it's:
"every scene shows the subject ACTIVELY doing the story beat" +
"escalate stakes" + "vary environments" + "camera/composition cue per
prompt" + "hook + payoff". This works for any story.

The system prompt also has new "STORY OVER FILLER" and "RICH BACKGROUNDS"
directives so Claude doesn't write "character stands/walks/looks around"
as the whole beat or ship plain empty backgrounds.

## File Map (what lives where)

- `app.py` — FastAPI routes, autopilot pipeline, frame rendering, video
  assembly orchestration. **Largest file, ~8000 lines.**
- `pipeline.py` — prompt assembly (`build_full_prompt`, `build_sheet_prompt`),
  contact-sheet compositor, `enforce_aspect`, `ref_manifest`, character
  matching
- `derouter.py` — OpenAI-compatible image client; `generate()`, `edit()`,
  `_enforce_aspect` wrapper, `_openrouter_fallback`, 9Router detection
- `claude_client.py` — script gen, missing scenes, analyzer,
  `plan_edit`, `edit_holds`, `prompts_from_video_frames`, `extract_json`
- `editor.py` — ffmpeg wrappers: `assemble_video`, `split_long_holds`,
  `trim_silence`, `probe_duration`, `mix_sfx`
- `voice.py` — TTS clients: ElevenLabs (with timestamps), MiMo, Deepgram,
  Piper, Edge
- `transcribe.py` — local Whisper via `faster-whisper` (CPU int8)
- `image_queue.py` — bounded-concurrency queue, exponential backoff,
  rate-limit cooldown, job state
- `audio_gen.py` — SFX + music bed generation
- `vault_crypto.py` — encrypted settings store
- `store.py` — state I/O (`load_state`, `save_state`, `load_state_for`)
- `config.py` — all env-driven defaults
- `static/index.html` — the entire UI (single file, ~7000 lines)
- `vault.json` — encrypted per-user settings (TRACKED in git)
- `data/` — projects, characters, frames, images, audio, usage logs

## Autopilot Pipeline Quick Map

For when you need to reason about which step to touch:

1. **Analyse** (`_deep_analyze_urls`) — YouTube → transcript + frames →
   Claude deconstructs style/pacing/voice/story → returns N ranked
   suggestions
2. **Pin style frames** (autopilot step 1.5) — 4 evenly-spaced source
   frames become `state.style_frames` (visual anchors for every later gen)
3. **Rewrite World Bible** — `master_prompt` replaced with
   "VISUAL STYLE — match reference video exactly: <style_summary>" so a
   stale bible from a previous project doesn't override
4. **Pick suggestion + generate script** (`generate_script`) — word count
   target derived from `total_duration * 2.5` so narration fills the length
5. **Fill missing scenes** via `generate_missing_scenes` if Claude
   undershoots; trim to `expected_scenes` if it overshoots
6. **Render character sheets** — up to 5 STYLE REFs as separate images
   (`_multi=True`), prompt with manifest, saves to `data/characters/`
7. **Render sequence frames** — each scene gets: 5 STYLE REFs + that
   scene's character sheet(s) + (prev frame only on micro-cuts), all
   separate `image[]` fields, prompt with manifest labelling each
8. **Voice-over + video assembly** (`_build_flow_video`) — per-scene
   synth for non-ElevenLabs (frame-perfect by construction), or
   ElevenLabs char-timestamps, or weight-proportional. Each frame's
   `vo` read from the frame itself (sync-safe).
9. **Thumbnail** — pinned style frames + first sequence frame as ref,
   bold scroll-stopping 16:9
10. **SEO** — title/description/tags/hashtags from the script

## Concurrency / Rate-Limit Knobs

- `IMAGE_MAX_CONCURRENCY = 2` (default — derouter rate-limits aggressive
  concurrency; raise with caution and only if you have headroom)
- `IMAGE_REQUEST_DELAY_MS = 50` — minimum spacing between requests
- `IMAGE_MAX_RETRIES = 6` — exponential backoff for transient errors
- `IMAGE_BACKOFF_BASE_MS = 1500`, `IMAGE_BACKOFF_MAX_MS = 30000`
- `IMAGE_RATE_LIMIT_COOLDOWN_MS = 18000` — global cooldown after any 429
- `IMAGE_FALLBACK_ON_402 = true` — fall back to OpenRouter on wallet errors
- `MULTI_IMAGE_EDIT = true` — feed refs as separate `image[]` fields

## Testing Without Burning Wallet

The `derouter._format_openai_error` and HTTP-502 storms make it tempting to
add tests, but most of this code is integration-dependent. Faster local
probes:

```bash
# Probe whether derouter accepts multi-image edits
python -c "
import requests, base64, io
from PIL import Image
import app
s = app.load_vault()['sickvamsick@gmail.com']
def png(c):
    b=io.BytesIO(); Image.new('RGB',(256,256),c).save(b,format='PNG'); return b.getvalue()
files=[('image[]',('a.png',png((30,30,30)),'image/png')),
       ('image[]',('b.png',png((220,180,40)),'image/png'))]
r=requests.post(s['base_url']+'/images/edits',
  headers={'Authorization':'Bearer '+s['api_key']},
  files=files, data={'model':s['model'],'prompt':'test','size':'1536x1024'},
  timeout=120)
print('HTTP', r.status_code, 'len', len(r.content))
"
```

The current project's rendered images are at
`E:\time now\continuity-studio\continuity-studio\data\images\*.png` — their
aspect ratios are the live ground truth for whether `enforce_aspect` is
working (should all be 1536x1024 for landscape projects).

## Common Pitfalls

1. **Restarting the server without killing the listener.** Stale uvicorn
   from an earlier session holds port 8000 → new server exits with
   `WinError 10048`. Use the `for p in $(netstat ...)` loop above.
2. **Forgetting `multi_image_edit` on the derouter call site.** The
   vault toggle has no effect unless every `client.edit()` site passes
   the per-call override. 6 sites — keep them all in sync.
3. **Re-introducing style bans or prompt sanitization.** "Cleaner"
   prompt rules that strip "hand-drawn" / "cartoon" / "doodle" will
   destroy the source-video style the moment the user's video uses
   those words. Add STYLE FIDELITY, never STYLE BANS.
4. **Stretching holds to hit target_seconds.** Breaks A/V sync
   (audio is fixed length, frames run ahead of words). The correct
   lever is more narration upstream.
5. **Position-mapping VO to frames in any new code path.** If you add
   another video assembly path, read VO from the frame's `vo` field,
   not from the script positionally.
6. **Setting `multi_image_edit` per-call without passing it through
   `derouter.edit()`.** The signature now has a per-call param — use
   it. If you call without it, derouter falls back to
   `config.MULTI_IMAGE_EDIT` which is True so it's fine, but be
   explicit at the call site for clarity.
7. **Committing `vault.json` is fine** — it's encrypted and keyed by
   email; the user explicitly tracks it.

## Verification Checklist

Before declaring an autopilot fix done:

- [ ] Imports clean: `python -c "import app, pipeline, derouter, claude_client, config, editor, transcribe; print('OK')"`
- [ ] Server restarts: port 8000 free → uvicorn background → `HTTP 200` on `/`
- [ ] No `STYLE BAN` / `stick man` / `lava ocean` / `toxic sky` reintroduced
- [ ] `_sanitize_prompt` is still a no-op
- [ ] All 6 `client.edit()` call sites pass `multi_image_edit=`
- [ ] Each frame record has `"vo"` set (when autopilot renders)
- [ ] Character sheet path: 5 STYLE REFs (config.STYLE_REF_COUNT) as
  separate images, prompt includes manifest
- [ ] Scene frame path: same — refs as separate images, manifest
- [ ] No frame-stretching for duration (only narration length determines
  video length; sync must hold)
- [ ] `git add -A && git commit -m "..." && git push origin master` —
  confirm push via `git log -1 --oneline origin/master` matching local

## One-Shot Recipes

### "Fix the latest bug + push"

```bash
# 1. Edit code (no in-progress server reload needed during dev)
# 2. Verify imports
cd "/e/time now/continuity-studio/continuity-studio" && \
  python -c "import app, pipeline, derouter, claude_client, config; print('OK')"
# 3. Restart server
for p in $(netstat -ano | grep ':8000.*LISTENING' | awk '{print $5}'); do
  taskkill //F //PID $p
done
sleep 1
# 4. Commit + push
cd "/e/time now/continuity-studio/continuity-studio" && \
  git add -A && git commit -m "<type>(<scope>): <imperative summary>" && \
  git push origin master
# 5. (optional) Restart for live testing
# /c/Users/sickv/continuity-venv/Scripts/python.exe -m uvicorn app:app \
#   --host 127.0.0.1 --port 8000
```

### "Flip multi_image_edit for all users"

```python
import app
v = app.load_vault()
for k, sub in v.items():
    if isinstance(sub, dict) and "base_url" in sub:
        sub["multi_image_edit"] = True
app.save_vault(v)
```

### "Check whether last run's frames are landscape"

```python
import glob, os
from PIL import Image
from collections import Counter
c = Counter()
for p in glob.glob("E:/time now/continuity-studio/continuity-studio/data/images/*.png"):
    w, h = Image.open(p).size
    c[(w, h, "land" if w > h else "port" if h > w else "sq")] += 1
print(c.most_common(10))
```

If you see (1024, 1536, "port") — `enforce_aspect` is being bypassed on
that render path. The fix is wired into `derouter._generate_once` and
`derouter._edit_once`; check that the render actually goes through
derouter (not Pollinations / diffusers fallback) and that the `size`
param is non-empty at the call site.
