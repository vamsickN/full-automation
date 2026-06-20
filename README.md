<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0EA5E9,50:A855F7,100:EC4899&height=220&section=header&text=Full%20Automation&fontSize=64&fontColor=fff&animation=fadeIn&fontAlignY=35&desc=AI%20Video%20Studio%20%E2%80%94%20Audio%20to%20Video%20%C2%B7%20YouTube%20Workflow%20%C2%B7%20Continuity%20Engine&descSize=16&descAlignY=55" width="100%"/>

<br/>

<a href="https://github.com/sharmiladevi888/FullAutomation/stargazers"><img src="https://img.shields.io/github/stars/sharmiladevi888/FullAutomation?style=for-the-badge&logo=github&color=0EA5E9" alt="Stars"/></a>
<a href="https://github.com/sharmiladevi888/FullAutomation/network/members"><img src="https://img.shields.io/github/forks/sharmiladevi888/FullAutomation?style=for-the-badge&logo=github&color=A855F7" alt="Forks"/></a>
<a href="https://github.com/sharmiladevi888/FullAutomation/blob/master/LICENSE"><img src="https://img.shields.io/github/license/sharmiladevi888/FullAutomation?style=for-the-badge&color=EC4899" alt="License"/></a>

<a href="#-quick-start"><img src="https://img.shields.io/badge/Quick_Start-5_min-orange?style=for-the-badge" alt="Quick Start"/></a>
<a href="#-documentation"><img src="https://img.shields.io/badge/Read_the_Docs-blue?style=for-the-badge" alt="Docs"/></a>
<a href="#-troubleshooting"><img src="https://img.shields.io/badge/Troubleshooting-amber?style=for-the-badge" alt="Help"/></a>

<br/><br/>

**Generate consistent image sequences, character sheets, narrated scripts, and fully assembled videos — from a YouTube link or your own audio.**

Two workflows, one studio. Production-ready video pipeline, runs locally on your machine.

<br/>

`FastAPI` · `Claude` · `GPT-Image-2` · `ElevenLabs` · `FFmpeg` · `yt-dlp`

</div>

---

## Table of Contents

- [Overview](#-overview)
- [Two Workflows](#-two-workflows)
- [Features](#-features)
- [Quick Start](#-quick-start)
- [Step-by-Step Usage](#-step-by-step-usage)
  - [First-Time Setup](#1-first-time-setup)
  - [Workflow A — YouTube Autopilot](#2-workflow-a--youtube-autopilot-paste-a-link--get-a-video)
  - [Workflow B — Audio-to-Video](#3-workflow-b--audio-to-video-your-audio--sample-link--video)
  - [Manual Tab Walkthrough](#4-manual-tab-walkthrough)
- [Configuration](#-configuration)
- [Project Map](#-project-map)
- [API Reference](#-api-reference)
- [Troubleshooting](#-troubleshooting)
- [Tech Stack](#-tech-stack)
- [License](#-license)

---

## Overview

**Full Automation** is an AI-native creative pipeline that ships a finished video in one click.

It solves the hardest problem in AI video: **visual continuity**. The same character looks the same across 50 frames. The same art style carries from the reference video through every generated scene. Cuts land on the beat. Lip sync lands on the word.

Drop in a YouTube link, or drop in your own audio + a sample-video style link. The engine analyses the look, plans the script, casts the characters, renders style-locked frames, narrates with ElevenLabs, and assembles a final MP4 with frame-accurate A/V sync.

---

## Two Workflows

```
┌─────────────────────────────────────────────────────────────┐
│                    FULL AUTOMATION                          │
│                                                             │
│   ┌──────────────────────┐    ┌──────────────────────┐     │
│   │  A. YT AUTOPILOT     │    │  B. AUDIO → VIDEO    │     │
│   │                      │    │                      │     │
│   │  YouTube link ───┐   │    │  Your audio ───┐     │     │
│   │                 ▼   │    │                ▼     │     │
│   │  Style + speech    │    │  Word-level transcript │     │
│   │  ─────────────►    │    │  ────────────────────► │     │
│   │  Topics + script   │    │  1 scene / segment     │     │
│   │  ─────────────►    │    │  ────────────────────► │     │
│   │  Characters        │    │  Render frames         │     │
│   │  ─────────────►    │    │  ────────────────────► │     │
│   │  Style-locked      │    │  Style-locked          │     │
│   │  frame sequence    │    │  frame sequence        │     │
│   │  ─────────────►    │    │  ────────────────────► │     │
│   │  ElevenLabs VO     │    │  Sync to YOUR audio    │     │
│   │  ─────────────►    │    │  ────────────────────► │     │
│   │  ┌──────────────┐   │    │  ┌──────────────┐      │     │
│   │  │ FINAL MP4    │   │    │  │ FINAL MP4    │      │     │
│   │  │ + thumbnail  │   │    │  │ + thumbnail  │      │     │
│   │  └──────────────┘   │    │  └──────────────┘      │     │
│   └──────────────────────┘    └──────────────────────┘     │
└─────────────────────────────────────────────────────────────┘
```

Both share the same engine: style-locked frame generation, auto-cast character sheets, micro-cut continuity, and frame-accurate A/V sync.

---

## Features

### AI Video Engine

| Capability | What it does |
|------------|--------------|
| Style-locked continuity | Source video's art style drives every generated frame |
| Auto-cast characters | Claude decides how many characters the story needs |
| Contact-sheet refs | Multi-reference composition for complex scenes |
| Micro-cut pacing | Shot relation (`cut` / `continue`) tells the renderer when to reuse vs compose fresh |
| Frame-accurate A/V sync | Whisper word timestamps drive per-scene hold durations |
| Ken-Burns motion | Subtle camera moves on static holds for retention |
| Bulk render queue | Rate-limited, cost-tracked, concurrent-safe |

### Provider Mesh

| Role | Providers |
|------|-----------|
| **AI / Text** | Claude (direct, DeRouter, 9Router, AgentRouter), Gemini |
| **Image Gen** | DeRouter (gpt-image-2), 9Router, direct OpenAI-compatible |
| **Voice** | ElevenLabs, Xiaomi MiMo, Deepgram Aura, Piper (local, free) |
| **Transcription** | Local Whisper (faster-whisper, free), ElevenLabs Scribe |
| **Sound** | ElevenLabs SFX (rumble bed + contextual point-SFX), 5 cut-click styles |

### 10-Tab Studio

| # | Tab | What it does |
|---|-----|--------------|
| 00 | ⬡ Workflow | One-click autopilot — paste a link, get a video |
| 01 | Universe | Project hub — World Bible, style anchors, all-in-one overview |
| 02 | ▶ YT Analyser | Reverse-engineer reference videos into 10 topic ideas |
| 03 | Script Generator | AI writes hook + body + CTA with virality scoring |
| 04 | Characters | Auto-cast + style-anchored character sheets |
| 05 | Sequence | Per-scene shot list with style-locked prompts |
| 06 | Edit (Audio + Video) | Render frames, narrate, assemble final MP4 |
| 🎵→🎬 | Audio → Video | Upload your own audio + sample-video link → synced video |
| 🖼 | Thumbnail | 16:9 style-matched click-worthy thumbnail studio |
| 07 | Timeline | Frame-by-frame timeline view of the final cut |
| $ | Usage | Per-generation cost + token tracking dashboard |

Plus: ⤓ Export ZIP · Cloudflare Tunnel · Multiple projects · Encrypted vault.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/sharmiladevi888/FullAutomation.git
cd FullAutomation

# 2. Virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Install local Whisper for free word-level transcription
pip install faster-whisper

# 5. Launch the studio
python -m uvicorn app:app --host 127.0.0.1 --port 8000

# 6. Open the UI
#    → http://localhost:8000
```

That's it. First launch drops you into **Settings** to plug in your API keys (Claude + GPT-Image-2 at minimum). Everything else is optional.

### Hardware

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| Python | 3.11+ | 3.12+ |
| RAM | 8 GB | 16 GB |
| Disk | 5 GB | 20 GB+ (for asset cache) |
| OS | Windows 10 / macOS 13 / Ubuntu 22 | Windows 11 / Ubuntu 24 |
| GPU | — | Any CUDA (faster Whisper + image gen) |

---

## Step-by-Step Usage

### 1. First-Time Setup

When you first open `http://localhost:8000`, the **Settings** panel opens automatically.

#### Required keys (pick at least one of each)

```
┌──────────────────────────────────────────────────────────────┐
│  AI / Text      →  Claude key  (Anthropic, or DeRouter,      │
│                                or 9Router, or AgentRouter)   │
│  Image Gen      →  GPT-Image-2  (via DeRouter or 9Router)    │
└──────────────────────────────────────────────────────────────┘
```

#### Recommended keys (for the full experience)

- **ElevenLabs** — TTS narration + SFX (rumble bed, point SFX, cut clicks)
- **Gemini** — fallback AI provider (vision + text)
- **Local Whisper** — `pip install faster-whisper` (free, on-device transcription)

#### Provider comparison

| Provider | Best for | Cost | Auth |
|----------|----------|------|------|
| **Anthropic direct** | Lowest latency | $$$ API billing | `ANTHROPIC_API_KEY` |
| **DeRouter** | gpt-image-2 image gen | $$ pooled credits | `DEROUTER_*` |
| **9Router** | Local proxy, token savings | $ local | `NINEROUTER_API_KEY` + localhost:20128 |
| **AgentRouter** | Free Claude tier | Free | `AGENTROUTER_API_KEY` (https://agentrouter.org) |
| **Piper TTS** | Free local voice-over | Free (local) | None — voice auto-downloaded |

All keys are encrypted at rest in `vault.json` and never leave your machine.

### Optional: Piper TTS (free local voice-over)

```bash
pip install piper-tts
# Optional GPU acceleration (5-10x faster, needs NVIDIA GPU):
pip install onnxruntime-gpu
```

Then in **Settings → Voice provider → 🆓 Piper TTS** — pick a voice (Amy, Lessac, Kristin, Kusal, Joe, Danny, Ryan, Alba, Jenny), click Connect. The voice model auto-downloads on first use (~60 MB, cached in `data/piper_models/`). No API key, no per-character cost, no internet at synthesis time after the first download. Runs on CPU at ~1× real-time; on CUDA GPU at 5-10× real-time.

---

### 2. Workflow A — YouTube Autopilot (paste a link → get a video)

The fastest path. One paste, one click, one video.

```
Step 1 →  Open the ⬡ Workflow tab
Step 2 →  Paste a YouTube link (e.g. https://youtube.com/watch?v=...)
Step 3 →  Click ⚡ Auto (top topic)     — or pick from 10 suggestions
Step 4 →  Watch the pipeline run:
            analyse → script → characters → frames → video → thumbnail → SEO
Step 5 →  Download the MP4 + thumbnail + SEO package
```

What happens under the hood:

1. **Analyse** — yt-dlp pulls frames + transcript. Claude deconstructs art style, pacing, voice, storytelling.
2. **Suggest** — 10 virality-scored topic ideas in the same spirit as the source.
3. **Script** — Hook + body + CTA written with energy-aware pacing.
4. **Characters** — Auto-cast sheets anchored to the source video's look.
5. **Frames** — Style-locked generation with previous-frame continuity + contact-sheet refs.
6. **Voice** — ElevenLabs narration with word-level timestamps.
7. **Build** — FFmpeg assembles MP4 with frame-accurate A/V sync.
8. **Thumbnail** — 16:9 style-matched thumbnail, multiple variants.
9. **SEO** — Title, description, tags, chapters.

Total wall time: ~3-8 minutes for a 60-second video (depends on provider latency).

---

### 3. Workflow B — Audio-to-Video (your audio + sample link → video)

The control path. Your audio, your voice, your music — visual style locked to a sample video.

```
Step 1 →  Open the 🎵→🎬 Audio→Video tab
Step 2 →  Upload your audio file (.mp3 / .wav / .m4a)
Step 3 →  (Optional) Paste a sample-video link for art-style reference
Step 4 →  Click ⚡ Analyse & generate
Step 5 →  Review the scene list — Claude wrote 1 visual per transcript segment
Step 6 →  Adjust scene prompts if you want (or hit ▶ Render)
Step 7 →  Hit Render — frames generate in parallel with style-lock continuity
Step 8 →  Hit 🎬 Build Video — MP4 assembled, synced to YOUR words
```

The killer feature: **Whisper word timestamps drive exact per-scene hold durations**, so the cut lands exactly when you say it. No more off-by-one lip sync.

If you don't paste a sample link, the engine falls back to your pinned style anchors + World Bible.

---

### 4. Manual Tab Walkthrough

Use this when you want fine-grained control over each step.

#### Tab 01 — Universe

Your project hub. Holds the World Bible (character lore, location notes, mood boards) and pinned style anchors. Everything else reads from here.

#### Tab 02 — ▶ YT Analyser

- Paste **one** link → Analyse & suggest 10 topics.
- Paste **many** links → Analyse all + extract shared style/pacing/voice across the whole set.
- Paste a **channel URL** → Analyse channel → find content gaps → pull trending topics.
- Click any suggestion → auto-loads Script Generator with that idea.

#### Tab 03 — Script Generator

- Auto-fills from the Analyser (or write from scratch).
- Outputs: hook, body, CTA, total duration, scene count, voiceover style.
- Punch-up mode for tightening weak sections.

#### Tab 04 — Characters

- Claude decides how many characters the story needs.
- Generates style-anchored sheets (one per character, multiple angles).
- Edit the description / regenerate any single sheet.

#### Tab 05 — Sequence

- Per-scene shot list with style-locked prompts.
- Toggle `shot_relation` per scene:
  - `cut` — fresh composition from refs
  - `continue` — reuse previous frame, micro-evolve
- Adjust hold durations (seconds per scene).

#### Tab 06 — Edit (Audio + Video)

- **Render** — bulk frame generation with progress bar.
- **Voiceover** — ElevenLabs narration (per-scene or continuous).
- **Build Video** — FFmpeg assembly with transitions, SFX bed, cut clicks.

#### 🎵→🎬 Audio→Video

See [Workflow B](#3-workflow-b--audio-to-video-your-audio--sample-link--video) above.

#### 🖼 Thumbnail

- Auto-generates 16:9 thumbnails matched to your rendered frames.
- Click-worthy compositions with bold text overlays.
- Generate as many variants as you like.

#### Tab 07 — Timeline

- Frame-by-frame scrubber for the final cut.
- See exactly which audio segment drives each scene.

#### $ Usage

- Per-generation cost tracking.
- Token counts per provider.
- Rate-limit headroom.

#### ⤓ Export ZIP

- One-click download of the entire project: script, characters, frames, audio, MP4, thumbnail.

---

## Configuration

### Environment variables (`.env`)

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...              # OR one of the proxies below
DEROUTER_BASE_URL=https://api-direct.derouter.network/openai/v1
DEROUTER_API_KEY=...
ELEVENLABS_API_KEY=...

# Optional
NINEROUTER_BASE_URL=http://localhost:20128
NINEROUTER_API_KEY=...
AGENTROUTER_BASE_URL=https://agentrouter.org
AGENTROUTER_API_KEY=...
AGENTROUTER_MODEL=claude-sonnet-4-6
GEMINI_API_KEY=...
OPENAI_API_KEY=...                        # for direct OpenAI-compatible image gen
```

All keys can also be set in-app via the **Settings** panel — the in-app vault (`vault.json`, encrypted) takes precedence over `.env`.

### Vault

`vault.json` stores all your keys with AES-256 encryption. The passphrase is your login password. **Never commit `vault.json`** — it's in `.gitignore`.

---

## Project Map

```
FullAutomation/
├── app.py                FastAPI routes, auth, autopilot, A2V engine
├── claude_client.py      AI client (Claude/Gemini/OpenAI) + script/scene gen
├── transcribe.py         Audio transcription (local Whisper + ElevenLabs Scribe)
├── voice.py              ElevenLabs TTS (with timestamp chunking for long VO)
├── pipeline.py           Prompt assembly, contact sheets, style locking
├── editor.py             FFmpeg video assembly (concurrent-safe temp dirs)
├── youtube.py            yt-dlp + transcript ingest (paste-link workflow)
├── derouter.py           GPT-Image-2 client
├── image_queue.py        Rate-limited bulk frame generation
├── store.py              State + asset persistence under data/
├── config.py             Env-driven settings
├── vault_crypto.py       Encrypted API key storage
├── punchup.py            Script enhancement
├── gen_with_refs.py      Multi-reference frame composition
├── requirements.txt      Python dependencies
├── .env.example          Template for local config
├── static/
│   └── index.html        Full UI (10-tab single-file SPA)
└── data/                 Generated assets, uploads, renders (git-ignored)
```

---

## API Reference

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/api/autopilot` | Full YouTube pipeline (link → video) |
| POST | `/api/audio-to-video` | Audio-to-Video (your audio + sample link → video) |
| POST | `/api/audio-to-video/upload` | Upload audio/video files |
| POST | `/api/audio-to-video/sample-link` | Fetch style frames from YouTube link |
| POST | `/api/youtube/analyze` | Analyse single YouTube link → 10 topics |
| POST | `/api/youtube/analyze-multi` | Analyse many links → shared style + 10 topics |
| POST | `/api/youtube/analyze-channel` | Analyse a channel → content gaps |
| POST | `/api/generate` | Render one frame |
| POST | `/api/generate/batch` | Batch render with continuity |
| POST | `/api/script` | AI script generator |
| POST | `/api/characters` | Generate character sheet |
| POST | `/api/voiceover/auto-flow` | Natural-flow narrated video |
| POST | `/api/build-video` | Assemble frames + audio to MP4 |
| POST | `/api/analyse-scene` | Vision-analyse a single frame |
| POST | `/api/settings` | Save API keys + provider config |
| GET  | `/api/health` | Connection test for all providers |
| GET  | `/api/export/package` | ZIP download of project |

All video endpoints accept `cut_clicks`, `cut_click_volume`, and `cut_click_style` — a short SFX is mixed at every frame change (cached in `data/sfx_cache/`).

---

## Troubleshooting

### "Couldn't read any of those videos. Try links with captions or that aren't region-locked."

Three fallbacks are tried per URL: transcript → frame download → thumbnail. All three failed.

**Likely causes:**
- Video is private / unlisted / deleted
- Video is region-locked from your IP
- YouTube's bot detection blocked the download (common on cloud IPs)

**Fixes (try in order):**
1. Test with a known-public video (e.g. `https://youtube.com/watch?v=dQw4w9WgXcQ`)
2. Upgrade `yt-dlp`: `pip install -U yt-dlp`
3. For bot-blocked IPs, the engine now uses iOS + Android player clients as fallback (auto, since 2026-06)
4. For region-locked, use a VPN or different network

### "No AI key set"

Open **Settings** (gear icon) and add your Claude key (or DeRouter / 9Router / AgentRouter key).

### Frame generation returns blank images

Check the `$ Usage` tab — rate limit hit. Switch providers in Settings, or wait 60s.

### ElevenLabs narration cuts off mid-sentence

The engine auto-chunks long VO. If a single sentence exceeds the model limit, lower `total_duration` per scene in the Sequence tab.

### `ffmpeg` not found

```bash
# Windows (chocolatey)
choco install ffmpeg

# macOS
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg
```

### Port 8000 already in use

```bash
python -m uvicorn app:app --port 8001
```

### venv issues on Windows

Use the full path to the venv Python:

```bash
"C:/path/to/.venv/Scripts/python.exe" -m uvicorn app:app --port 8000
```

---

## Tech Stack

| Layer | Tooling |
|-------|---------|
| **Frontend** | Vanilla JS, single-file SPA (`static/index.html`) |
| **Backend** | FastAPI + Uvicorn (async) |
| **AI / LLM** | Claude (Anthropic SDK), Gemini, OpenAI-compatible proxies |
| **Image Gen** | GPT-Image-2 via DeRouter / 9Router / direct OpenAI |
| **Transcription** | Local Whisper (faster-whisper, free) + ElevenLabs Scribe |
| **Voice** | ElevenLabs TTS (with word-level timestamps) · Piper TTS (local, free, CPU/GPU) · Deepgram Aura · Xiaomi MiMo |
| **Sound** | ElevenLabs SFX + 5 cut-click styles |
| **Video** | FFmpeg + FFprobe (fade, crossfade, motion transitions) |
| **YouTube** | yt-dlp + youtube-transcript-api (multi-language fallback) |
| **Storage** | Local JSON (vault, users, project state) — encrypted at rest |
| **Hosting** | Localhost-first, Cloudflare Tunnel-ready |

### Design principles

- **Local-first** — your assets, your keys, your machine. No cloud lock-in.
- **Portable** — pure Python, deploy anywhere with 3.11+
- **Cost-aware** — per-generation tracking, rate-limit backoff, budget-conscious defaults
- **No external DB** — local JSON for users, vault, and project state
- **Concurrent-safe** — unique temp dirs per render, locked state writes
- **Security** — vault.json encrypted at rest, all uploads sanitized, secrets git-ignored

---

## Contributing

PRs welcome. The cleanest path:

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/your-feature`)
3. Make your change + add a test if it's a route or provider
4. Run the studio locally + smoke-test the affected tab
5. Commit + push + open a PR

---

## License

MIT — see [LICENSE](./LICENSE).

---

<div align="center">

**Built with love for the continuity-first creative workflow.**

If this saves you time, [drop a star ⭐](https://github.com/sharmiladevi888/FullAutomation) — it helps more than you think.

<br/>

<sub>Made with [Claude](https://anthropic.com) · [GPT-Image-2](https://openai.com) · [ElevenLabs](https://elevenlabs.io) · [yt-dlp](https://github.com/yt-dlp/yt-dlp) · [FFmpeg](https://ffmpeg.org)</sub>

</div>
