---
name: continuity-studio
description: Use when working on the Continuity Studio project at E:\\time now\\continuity-studio — a YouTube-to-vertical-video autopilot that analyzes reference videos via Claude, generates character sheets and scene frames via gpt-image-2, and assembles them with TTS voice-over into short-form videos. Covers the hard-won gotchas of the past several sessions.
version: 1.4.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [continuity-studio, video-generation, autopilot, gpt-image, derouter, elevenlabs, piper, whisper, a-v-sync, art-style, bug-bash, secret-leak]
    related_skills: [requesting-code-review, plan, test-driven-development]
  windows:
    old_branch: E:\full-automation (port 8010, June 6 build)
    nine_router_ref: references/9router-windows-startup.md
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
- Responding to a secret-leak notification (GitGuardian / similar) for this
  repo

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

## Old Version Branch — June 6 build

There is a stable older build at `E:\full-automation` served on **port 8010**.
`run_all.bat` already references this path. It has its own copy of the
codebase, vault, and data directories, so it is effectively a **separate
instance** from the canonical working copy.

- Code: `E:\full-automation\app.py` (June 6 snapshot)
- Vault: `E:\full-automation\vault.json` (encrypted, separate keyspace)
- Data: `E:\full-automation\data\` (frames, characters, projects)
- Already tested and returns HTTP 200 on `http://127.0.0.1:8010/`

The old vault has 3 accounts (`sickvamsick9@gmail.com`, `sickvamsick@gmail.com`,
and an anonymous fallback). **Difference:** the old vault sets
`multi_image_edit: false` for all accounts, while the canonical working copy
now defaults to `true`. When debugging inconsistencies between the two builds,
check this toggle first.

To boot the old build without the launcher:
```bash
cd \"/e/full-automation\" && \
  /c/Users/sickv/continuity-venv/Scripts/python.exe -m uvicorn app:app \
    --host 127.0.0.1 --port 8010
```

## 9Router Windows Startup Gotcha

`9router` is registered as the Windows service `9router.exe` (Automatic start
type), but `sc start 9router.exe` returns **Access is denied** because the
service is a tray app that doesn't respond to SCM start requests. The npm
`.cmd` wrapper also fails to daemonize correctly via `cmd /c start` paths on
this setup.

**Verified working invocation:**
```bash
background=true, command='node "C:\\Users\\sickv\\AppData\\Roaming\\npm\\node_modules\\9router\\cli.js" --no-browser --skip-update'
```

After startup verify with:
```bash
netstat -ano | grep ':20128.*LISTENING'
curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost:20128/
```

Dashboard: `http://localhost:20128` (returns 307 redirect to the login page).
Right-click the tray icon to open dashboard or quit.

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

### 4. Style-anchor count + reference-image ceiling

`config.STYLE_REF_COUNT` (default **6** as of v1.3) — how many source-video
frames to feed as STYLE REFs. Applied in BOTH char-sheet paths and BOTH scene
paths via `config.STYLE_REF_COUNT` (NOT hardcoded `[:3]`/`[:4]`/`[:5]`). More
anchors = a fuller, more faithful read of the source art style (palette, line
weight, lighting, character-design variation).

**The autopilot style-frame pinning step used to hardcode 4** (`len // 4`,
`[:4]`) instead of `config.STYLE_REF_COUNT` — fixed; it now uses
`max(1, int(config.STYLE_REF_COUNT))`. If you see a bare integer slice on the
style-frame list anywhere, replace it with the config value.

`config.MAX_REF_IMAGES` (default **10**) — hard ceiling on TOTAL refs in one
edit call (style + character sheets + previous frame combined). Enforced in
`_render_one`: when `len(refs) > MAX_REF_IMAGES` it trims **trailing STYLE
anchors first** while ALWAYS keeping character sheets + the continuity/previous
ref (those are load-bearing for identity + continuity; extra style anchors are
diminishing returns). The OpenRouter fallback in `derouter` also honours this
cap (was a bare `images[:4]`). Both `STYLE_REF_COUNT` and `MAX_REF_IMAGES` are
`.env`-overridable. gpt-image-2 handles ~6-8 refs cleanly; beyond that the
contact-sheet packs them but per-ref fidelity drops — don't raise blindly.

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

### 10. Char-step counter lies, and daemon-thread deadlines drop sheets

Two coupled bugs in the autopilot character-sheet step:

1. **"0/6 cast" when 1 was actually saved.** `chars_done` came from an
   in-memory accumulator `char_created` that only appended when
   `_run_with_deadline` returned (finished=True). But `_make_sheet`
   saves the sheet to `state.characters` BEFORE returning — so when
   the per-step deadline hit, the sheet was already in state but
   `char_created` was empty → UI showed "0/6" while the project had
   one real character.

2. **Sheets were lost on deadline.** `_run_with_deadline` ran
   `_make_sheet` in a daemon thread. When `t.join()` timed out, the
   main loop moved on; the daemon thread was still finishing but its
   result was dropped (never appended to `char_created`). Worse, on
   autopilot exit the daemon was killed mid-render. The first character
   usually fit inside the deadline; 2-6 silently died.

Fixes in place:
- `_make_sheet` runs INLINE (no deadline) — character sheets are
  foundational; better to wait than lose them. `image_queue` still
  handles 502 backoff/retry for upstream errors.
- `chars_done` is driven from `state.characters` (the source of
  truth) at each iteration. Picks up sheets from any prior run,
  in-flight renders, or completed iterations.
- The "0 of N" `char_err_hint` mirrors the persisted count, so if 1+
  sheets saved before the step ended the message says "1 of 6 saved"
  instead of "0 of 6 — check API key".

**General pattern (when adding new step progress counters):** the
counter must reflect the PERSISTED state, never an in-memory
accumulator that can drift from it across threads / processes /
re-entrant flows. Read `store.load_state()` (or equivalent) at the
point you compute the counter, not from a closure variable.

See `references/user-stack-and-counter-pattern.md` for the third
instance of this class of bug.

### 10b. VO "cut initial lines" + muddy audio (Deepgram / per-scene synth)

Two separate root causes, often reported together as "voice-over sounds bad
/ unclear and the first words are cut off."

**A. First syllable clipped ("cut initial lines").** `editor.trim_silence`
used `silenceremove start_periods=1 ... detection=peak` at -50 dB on EVERY
clip. Aura/Deepgram (and other neural TTS) frequently open on a soft,
slow-attack consonant or breath whose amplitude ramps up gradually — the
start-trim bites into that onset and the first WORD of every scene comes back
clipped. The `lead_pad_ms` adds silence but cannot restore eaten speech.

Fix in place: `trim_silence(..., trim_start=False)` is now the **default**.
It only strips dead air from the TAIL (never carries speech) plus adds the
lead cushion. Start-trim is opt-in (`trim_start=True`) for sources with a
known long hard-silent intro. The per-scene synth loop
(`_synth_per_scene_track`) and the single-track fallback BOTH call it with
the default, so the fix covers every provider (Deepgram, MiMo, Edge, Piper),
not just Deepgram. **Never flip the default back to start-trimming.**

**B. Muddy / unclear timbre (Deepgram specifically).** Deepgram's default
mp3 output is low-bitrate, and the pipeline then re-encodes mp3-on-mp3
through ffmpeg concat (`-q:a 4`) — lossy-on-lossy mush, and Aura's soft
consonants suffer most. Fix: `config.DEEPGRAM_ENCODING` defaults to **`wav`**
(linear16 @ 24 kHz, Aura's native rate, lossless). The final video mux
re-encodes to AAC once, so raw WAV is never shipped. Per-scene concat bumped
to `-q:a 2`. `_synth_per_scene_track` picks the clip extension from
`vc.__class__.__name__ == "DeepgramVoiceClient"` + `vc.encoding` so the WAV
isn't mislabelled `.mp3` (ffmpeg sniffs by content, but match it anyway).

**C. Counter must be MONOTONIC + read under lock; previews must be STICKY.**
Two more follow-ups to #10 from the same session — a "1/2 → 0/2 going
BACKWARDS" report and a "no preview on the workflow node" report:
- *Counter went backwards.* `chars_done` was read from `store.load_state()`
  OUTSIDE `_state_write_lock` while `_make_sheet` writes UNDER it → a mid-write
  read returned an empty `characters` list → 0. Fix: read state under the same
  lock the writer uses, AND floor the value by the in-memory accumulator so it
  can only go UP: `done = max(len(persisted_with_output), len(in_memory))`.
  Also SEED `chars_done` on the first tick from sheets already on disk
  (`min(seed, total)`) so RESUME doesn't flash "0/N". This applies to ANY
  persisted-state progress counter, not just chars.
- *"2 requested, 1 made" silently.* A single transient image error (502 /
  rate-limit) dropped a character forever. Fix: on a NON-fatal char-sheet
  exception, retry the whole sheet ONCE (`time.sleep(2)` then re-call
  `_make_sheet`) before giving up. `_is_fatal_image_error` (no credits / bad
  key) still aborts the step immediately.
- *No live preview on the node.* The frontend (`engNodeLive`/`apPaint`) sets
  the node img only when `last_image_url` is truthy (sticky). But some call
  sites passed `last_image_url=None` unconditionally and `p.update(kw)`
  overwrote the shown URL with None. Fix: only include the key when truthy —
  `kw={"chars_done":done}; if url: kw["last_image_url"]=url; _ap_prog(run_id,**kw)`.
  Same for the frames step (`rec.get("image_url")`). URLs are `/data/...`
  (served by the `/data/` static route). Node live-img id is `wfl-<node>`;
  `ENG_STEP_NODE` maps characters→`cast`, frames→`seq`.

Verify the fix cheaply (needs a Deepgram key — it lives on the **empty-user
`""`** vault record, with `voice_provider=deepgram`, NOT on the email
records):

```python
import app, voice, config, editor, os, subprocess, json
s = app.load_vault()[""]              # deepgram key is on the "" record
key = s.get("deepgram_api_key")
vc = voice.DeepgramVoiceClient(api_key=key,
        voice_id=s.get("deepgram_voice_id") or config.DEEPGRAM_VOICE_ID)
audio = vc.synthesize("Stop scrolling. This is the one thing nobody told you.")
assert audio[:4] == b"RIFF", "expected lossless WAV, got mp3 — encoding regressed"
open("data/videos/_dg.wav","wb").write(audio)
def dur(p):
    r=subprocess.run(["ffprobe","-v","quiet","-print_format","json",
        "-show_format",p],capture_output=True,text=True)
    return float((json.loads(r.stdout or "{}").get("format") or {}).get("duration") or 0)
raw=dur("data/videos/_dg.wav")
editor.trim_silence("data/videos/_dg.wav", trim_start=False)
print("raw",raw,"trimmed",dur("data/videos/_dg.wav"))  # trimmed should NOT be < raw by an onset chunk
```

Trimmed duration going UP (lead+tail pad, no start chop) confirms the onset
is preserved. If it drops noticeably, start-trim crept back in.

### 11a. Vault encryption key mismatch on packaged exe — auto-recovery

When the app is packaged as an exe (PyInstaller → Inno Setup), the vault
encryption key (`data/.secret`) is auto-generated at first run in
`%LOCALAPPDATA%\ContinuityStudio\data\.secret`. If the key changes (reinstall,
different user, corrupted file, or a dev-machine `.secret` was synced over),
EVERY encrypted field in `vault.json` fails to decrypt → the old code printed
`[vault] WARNING: decryption failed` on every single API key, every request,
forever.

**Fix in place (do not undo):** `vault_crypto.py` now tracks
`_decrypt_failures` globally. When 3+ keys fail in a single `decrypt_vault()`
call, `_nuke_and_recover()` fires:
1. Deletes `data/.secret` (the mismatched key)
2. Deletes `vault.json` in the config dir (the undecryptable file)
3. Resets `_fernet = None` so the next call generates a fresh key
4. Returns `{}` (empty vault) — user re-enters keys in Settings

One clean log line replaces the warning spam:
`[vault] 11 keys failed to decrypt — encryption key mismatch detected.
Regenerating vault (you'll re-enter API keys in Settings).`

**Why also delete vault.json:** without this, the next `load_vault()` reads the
old encrypted file, tries to decrypt with the NEW key, fails again → infinite
nuke loop. Deleting both .secret AND vault.json breaks the cycle.

**Spec-level prevention:** `ContinuityStudio.spec` now strips `vault.json`,
`users.json`, `codes.json`, and `.secret` from the PyInstaller bundle so a
fresh install never ships a dev-machine encrypted vault. The filter:
```python
_strip = {"vault.json", "users.json", "codes.json", ".secret"}
a.datas = [t for t in a.datas if os.path.basename(t[0]) not in _strip]
```

### 11. Vault `_SENSITIVE_KEYS` MUST be exhaustive — missing keys leak plaintext

`vault_crypto._SENSITIVE_KEYS` is an explicit allowlist of field
names that get encrypted on save. Any secret field NOT in this set
is written to `vault.json` in plaintext, and gets committed to git.

This was the cause of a real GitGuardian-flagged Deepgram API key
leak in June 2026: `_SENSITIVE_KEYS` was missing `deepgram_api_key`,
so that key was saved in plaintext for the empty-user record, then
committed in c5c68c3 and persisted through several subsequent commits.

When adding a new API-key-bearing provider:
1. Add its `*_api_key` field name to `_SENSITIVE_KEYS` in
   `vault_crypto.py`. Keep the list exhaustive — pattern: any key
   matching `*_api_key`, `*_secret`, or `*_token` should be in it.
2. Audit existing plaintext keys in `vault.json` — encrypt them on
   the next save (set to empty, force re-entry via Settings UI).

See `references/secret-leak-recovery.md` for the full incident
playbook (git filter-branch with python index-filter to scrub
history, force-push, verify clean across all mirrors).

### 12. State lives on server + disk; browser refresh resets only the UI

User concern repeated several times in this session: "if I refresh,
does everything go away?" Answer: NO. Refresh only resets the
browser tab's view (form inputs, progress bar, result pane). The
server's in-memory autopilot state, the disk JSON files, and all
rendered images are untouched.

Concrete locations:
- Rendered frames: `data/images/*.png` (disk, gitignored)
- Character sheets: `data/characters/*.png` (disk, gitignored)
- Script / state / master_prompt / style_notes:
  `data/projects/<id>.json` (disk, gitignored)
- Videos: `data/videos/*.mp4` (disk, gitignored)
- In-flight autopilot run: `_AUTOPILOT_PROGRESS[run_id]` in the
  running server process (lives until server dies)

What does lose work:
- Killing the server WHILE autopilot is mid-flight — the in-memory
  run state is lost; the disk state (frames already saved) is fine.
  On restart, re-run with `from_cache=true` → only missing frames
  render.
- `rm -rf data/` — unrecoverable; not in git (gitignored).

UI workflow that survives everything:
1. Hard-refresh tab (Ctrl+Shift+R) → blank UI
2. Page reloads `/api/state` from server → all frames/chars return
3. If autopilot is still in flight, `/api/autopilot/progress/<id>`
   starts updating → progress bar catches back up after ~3s
4. All data is back

User concern about "Failed to fetch" is almost always a momentary
server restart, not actual data loss. Check `http://127.0.0.1:8000/`
in a fresh tab to disambiguate.

### 13. Autopilot RESUME / Continue — the run survives a network drop

**13b. `_Stopped` exception must NOT be caught by the autopilot wrapper.**
The `api_autopilot` wrapper (`app.py`) wraps the pipeline in try/except to
capture mid-run failures. `_Stopped(Exception)` is raised by `_check_stop()`
at every step boundary when the user clicks Stop. Because `_Stopped` inherits
`Exception`, the generic `except Exception` caught it and marked the run as
FAILED (breadcrumb `failed=True`, HTTP 500). The `_Stopped` exception handler
at `@app.exception_handler(_Stopped)` returns a clean 200 with steps kept.

**Fix in place:** `except _Stopped: raise` BEFORE the generic `except Exception`
in the wrapper. If you add another try/except around the pipeline, ALWAYS
re-raise `_Stopped` first.

### 13c. Poll interval lifecycle — use AP_POLL, not AP_TIMER

The frontend has TWO global interval vars: `AP_TIMER` (per-step countdown
timer, cleared by `apStartTimer()`) and `AP_POLL` (autopilot progress poller).
Before this fix, `resumeProject` used `AP_TIMER` for polling and never cleared
it on completion → immortal poller + double-polling after a subsequent run.

**Rule (don't break):**
- ALL autopilot progress polling goes through `AP_POLL` (global), never a
  local `const poll` or `AP_TIMER`.
- `stopAutopilot()` clears BOTH `AP_TIMER` and `AP_POLL`.
- `runAutopilot` finally: clears `AP_POLL`, nulls `AP_RUN_ID`.
- `resumeProject`: uses `AP_POLL` with terminal detection (`p.done || p.error`
  stops the poll + re-enables buttons). Has a finally block for cleanup.
- `AP_TIMER` is ONLY for the per-step countdown (500ms tick) in `apStartTimer()`.

### 13d. `api()` timeout — AbortController

`api()` in the frontend now uses `AbortController` with a configurable timeout:
5 min for GETs, 15 min for POSTs. AbortError surfaces as "Request timed out"
and engages the network-retry loop in `runAutopilot`/`resumeProject`. Before
this fix, a stalled/half-open socket (server accepts but never responds) would
hang the UI forever — the "sticks in the middle" symptom.

### 13e. `audio["duration"]` KeyError on partial state

`/api/edit-plan` and the edit-plan logic access `st["audio"]["duration"]`
directly. If `st["audio"]` exists but lacks `duration` (partial migration,
failed upload), this raises `KeyError` → 500. Fixed with
`float((st.get("audio") or {}).get("duration") or 0.0)`.

### 13f. Inline `_MOMENT_CUES` modulo — ZeroDivision risk

Three inline copies of `_MOMENT_CUES[(i % (len(_MOMENT_CUES) - 1)) + 1]`
were used instead of the safe `_angle_cue_for(i)` helper. If `_MOMENT_CUES`
is ever trimmed to 1 element, `len-1 = 0` → `% 0` → `ZeroDivisionError`.
All three replaced with `_angle_cue_for(i)` which guards `span > 0`.

### 14. Resume frame-skip: scene `n` set, NOT positional index

`_already_frames = len(sequence)` assumed frames were rendered in strict
scene order with no gaps. But blank scenes (no prompt/action/vo) are skipped
via `continue` in the frame loop — they consume a scene index but produce NO
sequence entry. After such a skip, `len(sequence)` no longer maps 1:1 to
leading scene indices. On resume `_scene_i < _already_frames` skips the wrong
scenes → frames misaligned with narration, or duplicate frames.

**Fix in place (do not undo):**
- Frame records now store `scene_n` (the script scene's `n` field) via a new
  `scene_n` parameter on `_render_one`. The autopilot loop passes
  `scene_n=sc.get("n")`.
- Resume collects `_rendered_scene_ns = {r.get("scene_n") for r in seq}` (a
  SET of scene numbers that already have frames on disk).
- The frame loop skips scenes whose `n` is in that set, incrementing
  `frames_ok` for each skip so the min-frames gate still counts them.
- Old projects (pre-`scene_n`) have `None` in their frame records → the set
  is empty → resume renders ALL scenes (safe fallback — no worse than before).

**General pattern:** when resuming by positional count, a skip-early path in
the original run breaks the invariant that "position N → output N". Always
match by a STABLE ID (scene_n, record id) rather than by array position.

### 15. Piper model download: temp-file + truncated detection

Piper voice models (`.onnx` + `.onnx.json`) are downloaded from Hugging Face
on first use. The download writes directly to the final path. If it dies
mid-stream (network drop, Ctrl-C), a partial file persists. On the next call,
`os.path.exists(local)` returns True → skip download → corrupt/truncated
model loaded → Piper crash.

**Fix in place (do not undo):**
- Download to `local + ".tmp"` → `os.replace(tmp, local)` on success.
  If the download dies, the `.tmp` file stays and the final path is untouched.
- On exception, the `.tmp` file is deleted (`os.remove(tmp_path)`) so partials
  don't accumulate.
- On existing files, a size sanity check rejects obviously truncated models
  (< 10KB for `.onnx`, < 100 bytes for `.json`) and re-downloads.

### 16. Disk-full / permission errors in store.py

`_save_project`, `_write_index`, `write_image`, `write_binary` all did raw
`open().write()` with no try/except. On `ENOSPC` (disk full) or `EACCES`
(permission), these raise raw `OSError` straight to the caller — confusing
and unhandled in most call sites.

**Fix in place (do not undo):**
- All four catch `OSError` and re-raise as `RuntimeError` with a clear message
  ("Disk full — cannot save project X. Free up space and retry.").
- `_write_usage` is non-critical — silently skips on OSError so the pipeline
  doesn't crash over a logging failure.
- errno 28 = ENOSPC (disk full), errno 13 = EACCES (permission) — both get
  specific messages. Other OSError types get a generic "Could not save" message.

### 17. Google token refresh: non-JSON 200 response

`_refresh_google_token` calls `r.json()` on a 200 response without a
try/except. A 200 with a non-JSON body (proxy error page, HTML redirect)
raises `ValueError` → propagates as 500 on `/api/youtube/upload`,
`/api/export/drive`, `/api/google/status`.

**Fix in place:** wrapped `r.json()` in try/except → returns None on parse
failure. The caller already handles None (returns empty/None gracefully).

## Audit Methodology

When doing a comprehensive bug audit on Continuity Studio, run THREE parallel
crews (each as a subagent or sequential pass):

1. **Backend static + runtime audit** — AST parse all .py files, import all
   modules, search for bare `except:` / `except Exception: pass`, find
   unguarded dict access (KeyError), index errors on empty lists, division by
   zero in pct/duration math. Hit every GET endpoint with curl to find 500s.

2. **Frontend audit** — load the page in browser, capture console errors,
   check the `api()` fetch helper for timeout/error handling, find dead
   onclick handlers, verify interval lifecycle (AP_TIMER/AP_POLL leaks),
   check JSON.parse guards on API responses.

3. **Network/IO resilience audit** — check every external call (derouter,
   Claude, TTS providers, yt-dlp, ffmpeg, whisper) for timeouts, retries,
   and graceful degradation on 402/429/502/connection-reset. Check disk
   writes for OSError handling. Verify image_queue backoff math.

Report every concrete bug with file:line, severity, the exact problem, and
a suggested fix. Do NOT fix during the audit — report first, then fix.

See `references/audit-findings-june2026.md` for the full audit output from
the June 2026 session.

The autopilot is ONE long blocking POST to `/api/autopilot` that runs all 8
steps; progress is polled separately via `/api/autopilot/progress/<run_id>`.
The failure mode the user hits is "Network error, sticks in the middle": the
single POST connection drops (proxy/server blip/restart) mid-pipeline → the
frontend `api()` helper throws `"Network error: ..."` → the whole run looks
dead even though every completed step is already saved to disk.

**Resume system in place (do not break — built to fix exactly this):**

- `AutopilotIn.resume: bool` — when True the handler forces `fresh=False`,
  `from_cache=True`, `keep_style=True` (never wipe prior work) and SKIPS any
  step whose output already exists on disk:
  - script: if `state.script.scenes` exists, reuse it (skip generation/fill)
  - characters: skip names that already have a saved `sheet_url`
  - frames: skip the first N scenes where N = `len(state.sequence)` (frames
    render in scene order and append, so existing count maps to scene index;
    seed `frames_ok = N` so the min-frames gate still passes)
  - video/thumbnail/SEO always re-run (cheap; video MUST rebuild from the now-
    complete frame set)
- `_ap_prog()` writes a lightweight breadcrumb `state["autopilot_run"]`
  (`{run_id, step, done, *_done/_total, video_url, updated}`) on EVERY progress
  tick so an interrupted run is discoverable after the in-memory
  `_AUTOPILOT_PROGRESS` dict is gone (server restart / tab close).
- `GET /api/autopilot/last` → `_autopilot_disk_status()` reports
  `{incomplete, run_id, last_step, has_script/characters/frames/video, ...}`.
  **Incomplete logic:** if `has_video` → complete UNLESS a breadcrumb says
  `run_id present and done=False` (covers pre-existing finished projects that
  predate the breadcrumb — they must NOT trigger a false Continue button).
  If no video → incomplete when any of script/chars/frames exist.
- Frontend: the long POST auto-retries on `Network error` with `resume:true`
  (up to 4 reconnects, 4s apart) so a mid-pipeline blip self-heals. A
  `▶ Continue` button appears in `#apContinueBanner` on page load (via
  `checkIncompleteRun()` in `load()`) and on the failure screen;
  `continueAutopilot()` re-calls `runAutopilot(idx, true, true)`.

`runAutopilot(idx, fromCache, resume)` — the 3rd param threads `resume`
through to the payload. The success path hides the banner.

### 14. Autopilot from an UPLOADED sample video (copy script + art style)

### 14b. Thread leak in `_run_with_deadline`

`_run_with_deadline` spawns daemon threads that outlive their join timeout. Before
the fix, these accumulated forever (image API calls, LLM calls still running in the
background). Now tracked in `_BACKGROUND_THREADS` list with a hard cap of 10. On
cap hit, waits up to 30s for the oldest thread to finish before spawning another.
Completed threads are reaped on every call. Lock: `_BG_THREAD_LOCK`.

### 14c. Google token read-modify-write race

`_google_token_for()` does a read-modify-write on `google_tokens.json`. Concurrent
requests (YouTube upload + Drive export + status check) could interleave and clobber.
Fixed with `_GOOGLE_TOKEN_LOCK` (threading.Lock) around the entire R-M-W. The OAuth
callback also uses the lock. `_save_google_tokens` now uses tmp+rename (was direct
overwrite). `_load_google_tokens` guards `json.load()` against corrupt/partial JSON.

### 14d. Whisper model download hang

`WhisperModel(size, ...)` in `_get_local_model` downloads from HuggingFace on first
use. ctranslate2 has no Python-level timeout. Fixed: runs in a daemon thread with
`t.join(timeout=300)` (5 min cap). On timeout, raises RuntimeError with a manual
pre-download command. Model stays cached in `_LOCAL_MODEL_CACHE` once loaded.

### 14e. Retry-After header handling

Two retry loops were ignoring the server's `Retry-After` header on 429:
- `voice.py _post_with_retry`: now parses `Retry-After` (capped at 60s), falls
  back to exponential backoff if absent.
- `claude_client.py _msg_openai`: same — parses `Retry-After` on 429/5xx.

### 14f. OpenRouter non-JSON 200 responses

`derouter.py` OpenRouter `_generate_once` and `_edit_once` called `resp.json()`
directly on a 200 response. A proxy error page (HTML) → `ValueError` → classified
as transient → retried needlessly. Now wrapped in try/except → RuntimeError (not
retried).

### 14g. YouTube `.part` file cleanup

`youtube.py download_frames`: on download failure, cleans up `.part` files for
that tag. The glob fallback now excludes `.part` files from path resolution.

The autopilot was originally YouTube-only. To make it copy an uploaded sample
video's *exact* script style AND art style there is now a parallel ingest path
— mirror it, don't rebuild it:

- `POST /api/autopilot/from-upload` (multipart `file` + optional
  `nudge/target_seconds/pacing_seconds/orientation`) →
  `_autopilot_analyze_upload(video_path, ...)`:
  1. `video.extract_frames(path, fps=1.0, max_frames=STYLE_REF_COUNT*3)` — a
     generous pool so anchor selection is rich.
  2. `transcribe.transcribe_audio(video_path, engine="local")` — faster-whisper
     (`pip install faster-whisper`, already installed; 1.2.1). `_ensure_wav`
     uses ffmpeg to pull audio straight from the mp4 container, so pass the
     VIDEO path directly. Wrapped in try/except → degrades to visuals-only
     (transcript="") if whisper is missing, so the run never hard-fails.
  3. Same `client.suggest_from_references(frames=..., sources=[{transcript}], ...)`
     deconstruction the YouTube path uses → `_as_analysis_dict` /
     `_normalize_suggestions` / `_suggestions_need_fix` re-ask loop.
  4. Saves to `state["yt_inspiration"]` with the SAME shape as
     `_autopilot_analyze`, but with an `upload://<name>` **sentinel url** in
     `url`/`input_urls`/`urls`, plus `from_upload:True` and `transcript`.
- The main `/api/autopilot` handler recognises the sentinel: when any url
  starts with `upload://` it ALWAYS uses the cached `yt_inspiration` (never
  calls `_autopilot_analyze`, which would reject the non-YouTube url). The
  pinned `frames` from the upload feed the normal STYLE_REF_COUNT anchor step.
- Frontend: `#apSampleFile` (hidden file input) + `apSamplePicked()` +
  `runFromSample()`. `runFromSample` POSTs the file (multipart — do NOT set
  `Content-Type`, let the browser add the boundary), stashes `d.url` in the
  module-level `AP_SAMPLE_URL`, then calls `runAutopilot(0, true, false)`.
  `runAutopilot` reads `AP_SAMPLE_URL` (if set) instead of `_apUrls()`, and
  clears it in `finally` so later YouTube runs aren't hijacked. `AP_SAMPLE_URL`
  is read synchronously at the top of `runAutopilot` before any await, so the
  `finally` clear is safe.

Smoke-test the endpoint without a real video: generate a 3s clip with
`ffmpeg -f lavfi -i "testsrc=...:duration=3" -f lavfi -i "sine=...:duration=3"
-c:v libx264 -c:a aac -shortest clip.mp4`, then
`curl -X POST .../api/autopilot/from-upload -F "file=@clip.mp4;type=video/mp4"`.
First run can take >60s (whisper model load + Claude vision on ~11 frames) —
run the curl in the BACKGROUND and poll, don't block a foreground call.

### 19. Uploaded-sample autopilot must show the SAME 10-topic picker as YouTube

User symptom: "uploaded a sample video, it picks topics on its own, not
showing 10 topics to pick." The YouTube path (`findTopics`) fills
`AP_TOPICS` → `renderTopics()` → user clicks a card → `runAutopilot(i,true)`.
The upload path (`runFromSample`) got back `d.suggestions` (10 ranked angles)
but THREW THEM AWAY and immediately called `runAutopilot(0, true, false)` —
auto-picking #0. **Fix: mirror the YouTube flow** — set `AP_TOPICS =
d.suggestions`, `AP_TOPICS_URL = AP_SAMPLE_URL` (the `upload://` sentinel),
call `renderTopics()`, and DON'T auto-run (only fall back to auto-run when
zero suggestions came back).

`AP_SAMPLE_URL` lifecycle is the tricky part: it must PERSIST from the upload
through the user's later topic-pick click, then be cleared. The clear was in
`runFromSample`'s `finally` (fired too early). Move it: keep `AP_SAMPLE_URL`
set after a successful upload, clear it only in `runAutopilot`'s `finally`
(after it's snapshot-read at the top into `urls`) and on upload FAILURE. The
cache-validity check `urls.join("\n") !== AP_TOPICS_URL` then passes because
both sides equal the sentinel, so `from_cache` stays true and
`suggestion_index=i` maps to the staged `yt_inspiration.suggestions[i]`.
Backend `/api/autopilot/from-upload` already returns `suggestions`
(default `n_suggestions=10`); the gap was purely frontend.

### 26. Build Video must force continuous-track voiceover (no silent per-scene fallback)

User symptom: old projects rendered with the per-scene chop method (pauses
between scenes, robotic boundary pronunciation) still sound choppy after
hitting Build Video, because `_build_flow_video` tried `_synth_continuous_track`
first but SILENTLY fell back to `_synth_per_scene_track` when the continuous
path failed (Whisper missing, synth error, drift). The user had no idea WHY
it fell back — just heard the same choppy audio.

**Fix in place (do not undo):**
- `BuildVideoIn.force_continuous: bool = True` (default ON for Build Video)
- `_build_flow_video(force_continuous=False)` — when True and the continuous
  path fails, raises `HTTPException(500, "Continuous voice-over failed: <reason>.
  Install faster-whisper or switch to ElevenLabs.")` instead of silently
  falling back to per-scene.
- Frontend `apBuildVideo()` and `reviewRerender()` both pass `force_continuous: true`.
- Autopilot pipeline keeps `force_continuous=False` (the fallback stays —
  autopilot should never hard-fail the whole run just because continuous track
  had a transient issue; per-scene is still frame-perfect, just less natural).
- The `_continuous_error` variable captures the actual failure reason so the
  error message is actionable, not generic.

**General pattern:** when a "fix" path has a silent fallback to the OLD
buggy behavior, the user can never tell if the fix actually took effect.
Either force the new path (and error loudly if it fails) or log the fallback
prominently. Silent fallback to the bug the user is trying to fix = guaranteed
confusion ticket.

### 25. "Images don't show one-by-one / generated images are missing" — live gallery

User symptom: "I started a video, images didn't show in a box one by one like
a webpage, and the generated images are nowhere to be found." Two-part
diagnosis, and the SECOND part is usually the real complaint:

1. **Images ARE saving.** They go to `data/images/*.png` (scene frames) and
   `data/characters/*.png` (sheets), served via the `/data/` static mount.
   Confirm with the "Check whether last run's frames are landscape" recipe or
   `ls data/images/`. If files exist, nothing is lost — it's a DISPLAY gap.

2. **The UI only showed ONE preview that got overwritten.** `_ap_prog` tracked
   counters + a single `last_image_url` that each new frame REPLACED. The
   frontend `apPaint` painted just `#apPreviewImg` (one slot). So the user saw
   frames flash by but never a growing grid → felt like images "vanished"
   because nothing accumulated on screen until the whole sequence loaded at the
   end.

**Fix in place (do not undo):**
- Backend `_ap_prog`: accumulate every completed frame URL into a
  `recent_frames` list (dedup, cap `[-200:]` so the in-memory dict can't grow
  unbounded). Pop `last_image_url` from kw, keep it as the single-latest field,
  AND append it to `recent_frames`. The `/api/autopilot/progress/<id>` response
  now carries `recent_frames`.
- Frontend `apPaint`: a growing gallery grid (`#apGalleryGrid`,
  `repeat(auto-fill,minmax(120px,1fr))`) APPENDS only NEW images each poll
  (`for i = grid.children.length .. frames.length`) so there's no flicker/
  rebuild. Each cell is 16:9 `object-fit:cover` with a `#N` badge. Label shows
  `Rendered frames (N/total)`.

**General pattern (any "show progress items as they complete" UI):** the
backend progress dict must expose the LIST of completed item URLs/ids, not just
a count and a single-latest pointer. The frontend appends new items by diffing
`rendered.length` against `data.length` — never rebuild the whole container per
poll (flicker + lost scroll). Same family as the counter gotchas (#10/#10b) but
for the gallery rather than the number.

See also `references/windows-desktop-packaging.md` for the windowless-build
crash trio (uvicorn isatty, daemon-thread crash logging, CMD-window flashes)
discovered alongside this in the packaged-exe debug session.

### 15. Windows ConnectionResetError [WinError 10054] traceback is BENIGN noise

On Windows the asyncio Proactor event loop logs a full `Traceback` whenever a
client drops an in-flight connection mid-stream — most commonly the browser
scrubbing or closing the `<video>` player during a `206 Partial Content` range
request on `/data/videos/*.mp4` or `/data/audio/*.mp3`:

```
Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)
ConnectionResetError: [WinError 10054] An existing connection was forcibly closed
```

It is **harmless** — the request was simply cancelled by the client — but it
spams the log and trips watch-pattern / monitoring alerts on every dropped
stream. It is NOT a crash and NOT a bug in the app code; the server keeps
serving (all subsequent requests stay 200).

**Fix in place (do not remove):** an `@app.on_event("startup")` hook
(`_silence_proactor_connection_lost`) installs an asyncio loop exception
handler that swallows ONLY `ConnectionResetError` / `ConnectionAbortedError` /
`BrokenPipeError` and `_call_connection_lost` / `WinError 10054` callbacks, and
passes everything else through to the previous/default handler unchanged. Verify
by firing aborted range requests:
`for i in 1 2 3 4 5; do timeout 0.25 curl -s -r 0-99999999 http://127.0.0.1:8000/data/videos/<f>.mp4 -o /dev/null; done`
— before the fix each one logged a traceback; after, none do and the server
stays 200.

Do NOT "fix" this by catching the error inside a route or disabling range
requests — the loop handler is the right layer and leaves real errors intact.

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
- `vault_crypto.py` — encrypted settings store (defines `_SENSITIVE_KEYS`)
- `store.py` — state I/O (`load_state`, `save_state`, `load_state_for`)
- `config.py` — all env-driven defaults
- `static/index.html` — the entire UI (single file, ~7000 lines)
- `vault.json` — encrypted per-user settings (TRACKED in git)
- `data/` — projects, characters, frames, images, audio, usage logs (gitignored)
- `docs/agent-skill.md` — copy of this skill committed alongside the code

## Autopilot Pipeline Quick Map

For when you need to reason about which step to touch:

1. **Analyse** (`_deep_analyze_urls`) — YouTube → transcript + frames →
   Claude deconstructs style/pacing/voice/story → returns N ranked
   suggestions. (Uploaded sample video takes the parallel
   `_autopilot_analyze_upload` path — see gotcha #14 — staged with an
   `upload://` sentinel that the handler treats like a cached YT analysis.)
2. **Pin style frames** (autopilot step 1.5) — up to
   `config.STYLE_REF_COUNT` (default 6) evenly-spaced source frames
   become `state.style_frames` (visual anchors for every later gen)
3. **Rewrite World Bible** — `master_prompt` replaced with
   "VISUAL STYLE — match reference video exactly: <style_summary>" so a
   stale bible from a previous project doesn't override
4. **Pick suggestion + generate script** (`generate_script`) — word count
   target derived from `total_duration * 2.5` so narration fills the length
5. **Fill missing scenes** via `generate_missing_scenes` if Claude
   undershoots; trim to `expected_scenes` if it overshoots
6. **Render character sheets** — up to 5 STYLE REFs as separate images
   (`_multi=True`), prompt with manifest, saves to `data/characters/`,
   runs INLINE (no deadline — see gotcha #10)
7. **Render sequence frames** — each scene gets: 5 STYLE REFs + that
   scene's character sheet(s) + (prev frame only on micro-cuts), all
   separate `image[]` fields, prompt with manifest labelling each. Each
   frame's `rec["vo"]` stores its scene VO line for sync-safe assembly
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
   email; the user explicitly tracks it. BUT verify every new
   `*_api_key` field is in `_SENSITIVE_KEYS` first, or it leaks
   plaintext (see gotcha #11).
8. **Wrapping per-item work in `_run_with_deadline`.** A daemon-thread
   timeout drops results even though the work completes. Run char/frame
   gen inline; let `image_queue` handle backoff. See gotcha #10.
9. **Adding a new provider's API key without registering it in
   `_SENSITIVE_KEYS`.** Plaintext leak to git, then GitGuardian
   notification, then history rewrite. See gotcha #11 +
   `references/secret-leak-recovery.md`.
10. **Wrapping a flat code block in a new `if`/`try` with the patch tool
    breaks indentation.** `app.py` has long flat blocks (the script-gen step
    is ~75 lines). When you need to make such a block conditional (e.g. add
    `if not _reuse_script:` around the script-generation step for RESUME),
    DON'T try to re-indent it by hand with patch — the `try:`/body levels go
    out of sync and you get `IndentationError`/`expected an indented block`.
    Instead, add the guard line, then re-indent the whole block
    programmatically via `execute_code`: read the file, locate the marker
    lines (e.g. `if not _reuse_script:` and the `store.log_usage("script",...)`
    end-of-block line), fix the `try:` line to the right level, then prefix
    `+4 spaces` to every non-empty line between them, write back, and verify
    with `python -c "import ast; ast.parse(open('app.py').read())"`. This is
    far more reliable than manual re-indentation for blocks > ~10 lines.
11. **Pushing `last_image_url=None` to `_ap_prog` blanks the live node
    preview.** Build the progress kwargs conditionally — include
    `last_image_url` ONLY when truthy. An unconditional `None` overwrites the
    just-shown sheet/frame thumbnail. See gotcha #10b(C) + #18.
12. **Reading a progress counter from unlocked `store.load_state()`.** A
    read that races a writer under `_state_write_lock` sees an empty
    `characters`/`sequence` list → counter jumps backwards (1/2 → 0/2). Read
    under the same lock AND floor by the in-memory accumulator
    (`max(persisted, in_memory)`) so it's monotonic. See gotcha #10b(C).
13. **A new sample/upload ingest path that auto-picks topic #0 instead of
    showing the picker.** The user expects the SAME 10-topic chooser the
    YouTube path renders. Stage `AP_TOPICS`/`AP_TOPICS_URL` and call
    `renderTopics()`; don't auto-run unless zero suggestions came back. Mind
    the `AP_SAMPLE_URL` lifecycle (persist through the pick, clear in
    `runAutopilot`'s finally). See gotcha #19.
14. **Adding a try/except around the autopilot pipeline without re-raising
    `_Stopped`.** The wrapper `api_autopilot` catches `Exception` to stamp
    failure breadcrumbs. `_Stopped(Exception)` is raised on user Stop —
    catching it marks a clean stop as a crash. Always add `except _Stopped:
    raise` BEFORE the generic `except Exception`. See gotcha #13b.
15. **Using a local `const poll = setInterval(...)` for autopilot progress.**
    The global `AP_POLL` is the single source of truth so `stopAutopilot()`
    can clear it. A local variable is invisible to stop/resume cleanup →
    immortal poller. Use `AP_POLL`, clear it in all exit paths (success,
    error, stop, finally). See gotcha #13c.
16. **Writing to data/ files without catching OSError.** On disk-full
    (ENOSPC) or permission (EACCES), raw `OSError` propagates as 500 with
    a confusing traceback. Wrap all store.py writes with try/except that
    surfaces a clear message. See gotcha #16.
17. **Frame records without `scene_n` break resume.** Resume now matches
    by scene number set, not positional index. If `_render_one` doesn't
    store `scene_n` in the record, resume falls back to rendering ALL
    scenes (safe but wasteful). Always pass `scene_n=sc.get("n")` from
    the autopilot loop. See gotcha #14.
14. **Adding a breadcrumb field without whitelisting it in `_ap_prog`'s
    disk-write block.** That block is an explicit key whitelist — any field
    not listed is silently dropped on save (never reaches disk, never surfaces
    in `/api/projects/summary` or `/api/autopilot/last`). Add the field to the
    whitelist AND both surfacing functions. Same class as the vault
    `_SENSITIVE_KEYS` leak (#11). See gotcha #24.
15. **Not wrapping the autopilot pipeline so mid-run crashes are recorded.**
    A bare exception from the synchronous handler = HTTP 500 with no captured
    reason. Use the thin-wrapper pattern (rename body to `_autopilot_pipeline`,
    wrap in `api_autopilot` with try/except → `_ap_fail`); do NOT re-indent
    the 800-line body (pitfall #10). See gotcha #24.
16. **Orphaning the uploaded source file.** Saving the upload to
    `data/uploads/` is not enough — record its absolute `upload_path` in
    `yt_inspiration` + `last_inputs` or resume can't re-reference the real
    video. Backfill old projects by matching `upload_name` against disk files
    with `.endswith()`, NOT loose `in` (mis-matches similar names). See #23.

### 20. Per-project progress / Resume button / stuck detection

User concern: "if a video is made or stuck at some point there should be a
saved-projects option; if a project is stuck at half and I reload, everything
is gone." The current-project Continue banner (#13) only handled the
MOST-recent incomplete run. Resolved in b395880:

- **Backend:** `GET /api/projects/summary` returns per-project
  `{pct, status, stuck, incomplete, last_step, ...}`. Pure function of
  on-disk state — safe to call for every project on every list request.
- **Stuck detection:** `_STUCK_AFTER_SEC = 2*3600`. A breadcrumb with
  `run_id` set + `done=False` + `updated` older than 2h is flagged stuck
  (almost certainly an orphaned autopilot run that died with the server).
  Status sorts to the top of the list so it gets attention first.
- **`/api/autopilot` accepts `project_id`.** When set, the handler
  switches the active project FIRST (so every subsequent `load_state_for`
  / write goes to the right file) then forces `resume=true`. After
  successful resume the user is left ON the resumed project — that's
  what they expected to see open.
- **Frontend:** each project row in the picker shows a colored progress
  bar (amber in-progress / red stuck / teal done), pct, status badge
  (Done/In progress/Stuck/Partial/Empty), last-step label, and a Resume
  button on incomplete + stuck projects (red + pulsing when stuck).
- **Bug fix in `_rough_pct`:** the previous version used `max(n,1)` as
  BOTH numerator AND denominator for the frames step, so any project with
  frames showed 60% added unconditionally (everything capped at 99%).
  Now uses script scene count or breadcrumb `frames_total` as the
  EXPECTED denominator so mid-frames progress is honest.

Resume button click handler is `resumeProject(id)` in the frontend —
sets `AP_RUN_ID`, shows `#apProgress`, reuses `engStart()` + the same
poll loop as the main `runAutopilot` so no duplicated progress plumbing.

### 21. Resume with empty URLs must fall back to cached yt_inspiration

User symptom: clicking ▶ Resume on an old (or stuck) project asks "paste a
YouTube link" even though the cached analysis is on disk. Root cause:
`api_autopilot` bailed on `if not urls: raise 400` BEFORE the cache check,
so any resume triggered days after the original run (page reload, project
picker, etc.) couldn't recover the URL list — the user had to dig out the
links and paste them again.

Fix in a899893: when `resume=true` and `urls` is empty BUT
`yt_inspiration.suggestions` is present on disk, pull `input_urls` from
the cache and proceed with `use_cache=True`. Same fallback the
disk-status Continue banner uses — just at the handler level so the
project picker path works too. If the cache is genuinely missing, the
400 stays ("paste a YouTube link or upload a sample").

### 23. Uploaded video disk path MUST be persisted (resume re-references real source)

User ask: "when we upload videos or link it should save them; resume should
continue from that." The upload IS saved to `store.UPLOADS_DIR`
(`data/uploads/sample_<id>_<origname>.mp4`) and survives restarts — but the
SAVED DISK PATH was not recorded anywhere, so resume could only fall back to
the cached `frames`/`yt_inspiration` blob and the actual source file was
orphaned (unfindable, can't re-extract frames / re-transcribe).

Fix in 5b9b334: `_autopilot_analyze_upload` now stamps on the `insp` dict:
- `"upload_path": os.path.abspath(video_path)` — the saved file's absolute path
- `"upload_name": source_name or os.path.basename(video_path)`
and the `/api/autopilot` `last_inputs` persist block copies
`"upload_path": cached.get("upload_path")` so the picker + resume both see it.

Verify upload_path resolves on disk for a resumable upload project:
```python
import json, os
st = json.load(open(r"E:\time now\continuity-studio\continuity-studio\data\projects\<pid>.json", encoding="utf-8"))
yi = st["yt_inspiration"]
print(os.path.exists(yi["upload_path"]), len(yi["frames"]), len(st["script"]["scenes"]))
```

**Backfilling** an older upload project missing `upload_path`: match
`yi["upload_name"]` against the files in `data/uploads/` by suffix
(saved name = `sample_<id>_<origname>` or `upload_<id>_<origname>`, so
`fname.endswith(upload_name)` is the reliable test — loose substring `in`
can mis-match a different upload of a similar name). Write it back into both
`yi["upload_path"]` and `last_inputs["upload_path"]`.

### 24. Mid-run failure capture — every interrupted run saves + resumes from its stopped %

User ask (repeated): "if any error happens in the middle it should be saved
as a project; reload, select that project with its % complete, and start from
the stopped point; other projects still accessible."

The breadcrumb (#13/#20) already persisted on every progress TICK, so a run
that died mid-step left a `done=False` breadcrumb → the project showed
resumable. The GAP: `/api/autopilot` was a synchronous handler with NO outer
try/except, so a mid-run exception became a bare HTTP 500 — the *reason* it
stopped was never captured, and a failure BEFORE the first `_ap_prog` tick
left no breadcrumb at all.

Fix in c1be450 — the thin-wrapper pattern (do NOT re-indent the 800-line
pipeline body; see pitfall #10):
- Renamed the real handler `api_autopilot` → `_autopilot_pipeline` (dropped
  its `@app.post` decorator, body and indentation UNCHANGED).
- New thin `@app.post("/api/autopilot") def api_autopilot(...)` wrapper:
  pins `body.run_id` (so the pipeline + `_ap_fail` share one id), calls
  `_autopilot_pipeline` in a try/except, and on ANY exception calls
  `_ap_fail(run_id, e)` then re-raises a friendly 500 ("...your progress is
  saved; click ▶ Resume to continue."). HTTPException 4xx is re-raised as-is;
  only 5xx HTTPExceptions are recorded.
- `_ap_fail(run_id, err)` reads the dying step from the live
  `_AUTOPILOT_PROGRESS[run_id]` and stamps the breadcrumb `done=False,
  error=str(err)[:500], failed=True, failed_step=<step>, failed_at=time()`.
  Work already on disk is never wiped.

**Breadcrumb whitelist gotcha:** `_ap_prog`'s disk-write block is an explicit
key whitelist (only run_id/step/done/counters/video_url/updated). New status
fields are SILENTLY DROPPED unless added to that dict. Had to add
`error`/`failed`/`failed_step` to the whitelist or they never persisted. Same
class of bug as #11 (vault `_SENSITIVE_KEYS`). Whenever you add a breadcrumb
field, add it to BOTH the whitelist AND the surfacing endpoints
(`_autopilot_disk_status`, `_project_summary`).

**done=True must clear stale failure flags.** On resume→complete the same
run_id carries the old `failed`/`error` from the first attempt. `_ap_prog`
now: `if kw.get("done"): kw.setdefault("failed",False); kw.setdefault("error","")`
so a now-finished project doesn't keep showing "stopped: <old err>".

**Frontend:** `checkIncompleteRun()` Continue banner now appends
` — reason: ${s.error}`. The picker rows (#20) already render pct/status/
"stopped at: <step>"; the error rides through `/api/projects/summary`.

Test the capture WITHOUT a real render (no wallet burn):
```python
import app, store
rid = store.new_id("run")
app._ap_prog(rid, step="frames", frames_done=3, frames_total=10)
app._ap_fail(rid, Exception("image API 402: wallet empty"))
ds = app._autopilot_disk_status()
assert ds["incomplete"] and ds["failed"] and ds["error"]
# resume->complete clears it:
app._ap_prog(rid, done=True, video_url="/data/videos/x.mp4")
run = store.load_state()["autopilot_run"]
assert run["done"] and not run["failed"] and run["error"] == ""
```
NOTE: this writes the CURRENT project's breadcrumb — run it against a throwaway
project or restore the breadcrumb after, or you'll falsely mark a real project
failed.

### 22. state.last_inputs — persisted source per project (picker + auto-restore)

User symptom: "when I resume it asks for upload/sample again" — even
though the project already had a cached analysis on disk, the URL
textbox was empty after page reload and the picker Resume button
didn't show what was being resumed with. Two root causes:

1. **No visible source per project.** URLs/uploads lived only in the
   opaque `yt_inspiration` blob. User had to dig out old links.
2. **URL textbox leaked across projects.** Switching projects didn't
   clear the previous project's URLs, so Resume could end up pointing
   at the WRONG cached analysis.

Fix in ae8ce35 (builds on #20 + #21):
- **Backend:** `/api/autopilot` stamps `state.last_inputs =
  {type, urls, upload_name, captured_at}` on every kickoff. The
  `/api/projects/summary` endpoint surfaces it so the picker can
  render a `🔗 N links` (YouTube) or `📁 foo.mp4` (upload) chip per
  row. The Resume button gets a sub-label showing the source.
- **Backfill:** on first deploy, run a one-time script to derive
  `last_inputs` from `yt_inspiration` for any existing project missing
  it. (Done in this commit for all 5 existing projects.)
- **Frontend `load()`:** auto-fills `#apUrl` from `STATE.last_inputs`
  on every project load — page reload, project switch, or Resume
  click. The textbox is PROJECT-SCOPED, so always apply (don't gate
  on emptiness, the old project's text would persist otherwise).
- **Uploads:** the file itself can't be re-attached (browser security
  — file inputs don't persist across reloads), but the cached
  analysis + style anchors drive resume. A red note under the URL
  input tells the user "Previous source: uploaded sample (foo.mp4)".
- **`has_video` ⇒ `done` even without breadcrumb.** A project with a
  rendered video is functionally done; the video IS the proof.
  Without this rule, every fresh page-load of a finished project
  flashed "partial" until the user re-ran the autopilot.

`resumeProject(id)` (picker) and the existing `continueAutopilot()`
(banner) now BOTH work after page reload with zero user input —
URLs come from the cache, source is visible in the picker chip.

### 23. Uploaded sample video — persist the DISK PATH, not just cached frames

User concern (recurring): "when we upload videos or links it should SAVE
them, and scripts generated + used should CONTINUE from there." The upload
file IS saved to `data/uploads/` (gotcha #14), and `frames`/`script`/
`characters` all persist on disk — but the saved video's DISK PATH was never
recorded in state, so the file was effectively orphaned: resume could only
lean on the cached frame URLs, never re-extract / re-transcribe from the real
source video.

Fix (commit 5b9b334):
- `_autopilot_analyze_upload` now stamps `insp["upload_path"] =
  os.path.abspath(video_path)` and `insp["upload_name"]` into the
  `yt_inspiration` blob it saves.
- The `/api/autopilot` `last_inputs` persist block also carries
  `upload_path` (`cached.get("upload_path")`) so the project picker chip +
  resume both know the real file, not just the `upload://<name>` sentinel.
- **Backfill for pre-existing upload projects:** match `yt_inspiration
  .upload_name` against the saved filenames in `data/uploads/` (saved name
  shape is `sample_<id>_<origname>` or `upload_<id>_<origname>`; match by
  suffix/`endswith(name)`, NOT loose substring — loose `in` can match the
  wrong file). Set `upload_path` on both `yt_inspiration` and `last_inputs`.

Persistence audit recipe — what actually survives a reload (run to confirm
before telling the user "it's saved"):

```python
import json, os
st = json.load(open(r"E:\time now\continuity-studio\continuity-studio\data\projects\<pid>.json", encoding="utf-8"))
yi = st.get("yt_inspiration") or {}
p  = yi.get("upload_path", "")
print("upload_path:", p, "| EXISTS:", os.path.exists(p))     # must be True for resume
print("cached frames:", len(yi.get("frames") or []))         # style anchors
print("saved script scenes:", len((st.get("script") or {}).get("scenes") or []))
print("char sheets:", len([c for c in (st.get("characters") or []) if c.get("sheet_url")]))
```

`upload_path EXISTS == True` is the proof the source video is re-referenceable
on resume; the script/frames/chars counts prove continuation works.

### 24. Auditing a "MiniMax left it lil bad" project handoff

Pattern for when the user says a prior agent/model left the project in a bad
state: enumerate EVERY project's health in one pass before touching anything,
then fix the active-project pointer + backfill missing fields. Don't delete
ghosts without asking.

```python
import json, os, glob
base = r"E:\time now\continuity-studio\continuity-studio\data"
cur = json.load(open(os.path.join(base,"projects.json"),encoding="utf-8")).get("current")
for pf in sorted(glob.glob(os.path.join(base,"projects","*.json"))):
    pid = os.path.basename(pf)[:-5]; st = json.load(open(pf,encoding="utf-8"))
    yi = st.get("yt_inspiration") or {}; li = st.get("last_inputs") or {}
    sc = len((st.get("script") or {}).get("scenes") or [])
    ch = len([c for c in (st.get("characters") or []) if c.get("sheet_url")])
    fr = len(st.get("sequence") or [])
    src = "upload" if yi.get("from_upload") else ("yt" if yi.get("url") else "-")
    print(f"{'*' if pid==cur else ' '} {pid} src={src:7} sc={sc:3} ch={ch} fr={fr:2} li={li.get('type','-')}")
```

Red flags this surfaces: `src=-` + `sc=0` + a few orphan frames = a dead run
(the active project pointer was left on garbage). Fix: repoint
`projects.json["current"]` to the healthiest resumable project (highest
script+chars+frames with a real `src`). A project with a 198-scene script but
only 40 frames = Claude script overshoot that never got trimmed to
`expected_scenes` — stuck, not done. **Leave orphan/dead projects in place
(non-destructive) and ASK before deleting** — user may want to inspect them.

**Pitfall (patch tool path resolution):** when the terminal cwd is the
workspace `E:\time now\continuity-studio\continuity-studio` but you pass a
git-bash style path like `/e/time now/.../app.py` to the `patch`/`read_file`
tools, it can mis-resolve to `E:\e\time now\...` (OUTSIDE workspace) and fail.
Use the native Windows absolute path `E:\time now\continuity-studio\continuity-studio\app.py`
with the file-edit tools; keep `/e/...` only for the `terminal` tool.

## Verification Checklist

Before declaring an autopilot fix done:

- [ ] Imports clean: `python -c "import app, pipeline, derouter, claude_client, config, editor, transcribe; print('OK')"`
- [ ] AST clean: `python -c "import ast; ast.parse(open('app.py').read())"`
- [ ] Server restarts: port 8000 free → uvicorn background → `HTTP 200` on `/`
- [ ] All GET endpoints return 200 (no 500s on load): `/api/state`, `/api/projects/summary`, `/api/autopilot/last`
- [ ] No `STYLE BAN` / `stick man` / `lava ocean` / `toxic sky` reintroduced
- [ ] `_sanitize_prompt` is still a no-op
- [ ] All 6 `client.edit()` call sites pass `multi_image_edit=`
- [ ] Each frame record has `"vo"` set (when autopilot renders) AND `"scene_n"` set
- [ ] Character sheet path: STYLE_REF_COUNT STYLE REFs (config, default 6) as
  separate images, prompt includes manifest, runs INLINE
- [ ] Scene frame path: same — refs as separate images, manifest, total refs
  clamped to config.MAX_REF_IMAGES (default 10), style anchors trimmed first
- [ ] No frame-stretching for duration (only narration length determines
  video length; sync must hold)
- [ ] Resume uses `_rendered_scene_ns` set (scene_n matching), NOT `_scene_i < _already_frames`
- [ ] `except _Stopped: raise` appears BEFORE `except Exception` in autopilot wrapper
- [ ] `AP_POLL` cleared in `runAutopilot` finally, `resumeProject` catch/finally, and `stopAutopilot`
- [ ] `api()` has `AbortController` timeout (5min GET / 15min POST)
- [ ] `st["audio"]["duration"]` accessed via `.get()` (not direct indexing)
- [ ] Piper download uses temp-file + atomic rename
- [ ] store.py writes catch `OSError` with clear disk-full message
- [ ] `git add -A && git commit -m "..." && git push origin master` —
  confirm push via `git log -1 --oneline origin/master` matching local
- [ ] If any new `*_api_key` field added: it's in `_SENSITIVE_KEYS` AND
  `vault.json` audit shows no plaintext occurrence
- [ ] VO clarity: `config.DEEPGRAM_ENCODING == "wav"` (lossless); `trim_silence`
  default is `trim_start=False` (onset preserved — trimmed clip duration NOT
  shorter than raw). See gotcha #10b.
- [ ] Progress counters monotonic: read persisted state UNDER
  `_state_write_lock`, floored by in-memory accumulator. `last_image_url`
  pushed only when truthy (node previews stay sticky). See gotcha #10b(C).
- [ ] Sample-video upload renders the 10-topic picker (not auto-pick #0); a
  later YouTube run isn't hijacked by a stale `AP_SAMPLE_URL`. See gotcha #19.
- [ ] Uploaded video's `upload_path` is stored in `yt_inspiration` +
  `last_inputs` and resolves on disk (resume re-references the real source).
  See gotcha #23.
- [ ] Any NEW breadcrumb field is added to the `_ap_prog` disk-write whitelist
  AND to `_autopilot_disk_status` + `_project_summary` returns, or it silently
  vanishes. Mid-run failures stamp `done=False`+`error`+`failed_step`; `done=True`
  clears stale failure flags. See gotcha #24.
- [ ] Uploaded-sample projects have `yt_inspiration.upload_path` set and the
  file EXISTS on disk (resume can re-reference the real source). See gotcha #23.
- [ ] (Packaged exe) `desktop.py main()` calls `_fix_stdio()` THEN
  `nowindow.install()` before importing/launching uvicorn; `app.py` calls
  `nowindow.install()` at import; `"nowindow"` is in the spec hiddenimports. No
  CMD windows flash during render; no uvicorn `isatty` crash. See
  `references/windows-desktop-packaging.md`.
- [ ] (Live render UI) `/api/autopilot/progress/<id>` returns `recent_frames`
  and the gallery APPENDS new images (no per-poll rebuild). Generated frames
  exist on disk under `data/images/` + `data/characters/`. See gotcha #25.
- [ ] Build Video passes `force_continuous: true`; if continuous track fails
  it raises a clear 500 (not silent per-scene fallback). Autopilot keeps
  `force_continuous=False` (fallback stays). See gotcha #26.

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

### "Is the counter lying about how many chars / frames were made?"

When UI shows `0/6 cast` (or `0/N frames`) but the project actually has
real outputs, the counter is reading an in-memory accumulator instead of
the persisted state. Quick check:

```python
import json
pj = json.load(open(r"E:\time now\continuity-studio\continuity-studio\data\projects.json"))
cur = pj["current"]
st = json.load(open(rf"E:\time now\continuity-studio\continuity-studio\data\projects\{cur}.json"))
print("persisted chars:", len([c for c in (st.get("characters") or [])
                               if c.get("sheet_url")]))
print("persisted frames:", len(st.get("sequence") or []))
print("script scenes:", len((st.get("script") or {}).get("scenes") or []))
```

If `persisted chars` > `0/6 cast` shown in the UI, the counter is the
bug — drive it from `store.load_state()` at the progress emit point,
not from the in-memory `char_created` accumulator. See gotcha #10.

### "Disambiguate 'is it the server or my browser?' (Failed to fetch)"

```bash
# Is the server actually responding?
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8000/

# Is the data still there from the server's perspective?
curl -s http://127.0.0.1:8000/api/state | python -c "
import json,sys
d=json.load(sys.stdin)
s=d.get('state',{})
print('seq',len(s.get('sequence',[])),'chars',len(s.get('characters',[])))
"
```

If both return the data you expect → browser tab was stale, hard-refresh
it (Ctrl+Shift+R). If `/api/state` returns empty → server's view of the
project diverged from disk (rare); force-restart server and re-check.