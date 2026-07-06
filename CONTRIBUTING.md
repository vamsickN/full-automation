# Contributing to Continuity Studio

## Quick Start

```bash
# Clone
git clone https://github.com/vamsickN/full-automation.git
cd full-automation

# Setup
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API keys

# Run (dev)
uvicorn app:app --reload --port 8000

# Run (Docker)
docker compose up --build
```

## Project Structure

```
.
├── app.py                 # Main FastAPI app (routes + middleware)
├── config.py              # Centralized configuration
├── store.py               # State persistence
├── atomic_store.py        # Thread-safe atomic file ops
├── pipeline.py            # Prompt construction logic
├── claude_client.py       # Claude/Anthropic API client
├── derouter.py            # Image generation client
├── voice.py               # Multi-provider TTS
├── editor.py              # Video editing (ffmpeg)
├── video.py               # Frame extraction
├── security.py            # Auth, rate limiting, validation
├── middleware.py          # Request middleware stack
├── api_response.py        # Standardized API responses
├── process_manager.py     # Safe subprocess handling
├── image_queue_v2.py      # Async image gen queue
├── healthcheck.py         # System diagnostics
├── logger.py              # Structured logging
├── vault_crypto.py        # API key encryption
├── static/
│   ├── index.html         # SPA frontend
│   ├── animations.css     # UI enhancement layer
│   └── enhancements.js    # Interactive effects
├── tests/
│   ├── test_store.py      # Unit tests
│   └── conftest.py
├── Dockerfile
├── docker-compose.yml
└── BUGS.md                # Known issues tracker
```

## Running Tests

```bash
pip install pytest
pytest tests/ -v
```

## Code Style

- Python: Follow PEP 8, use type hints
- JS/CSS: 2-space indent, no semicolons optional
- Commits: prefix with `[Agent N: Role]` or conventional commits

## Key Principles

1. **Never commit secrets** — use .env + vault_crypto
2. **Atomic state writes** — use atomic_store.py for any file persistence
3. **Kill your children** — always use process_manager.py for subprocess calls
4. **Standardize responses** — use api_response.py for all API returns
5. **Log, don't print** — use logger.py for structured output

## Activating the UI Enhancements

Add to `static/index.html`:

```html
<!-- In <head> -->
<link rel="stylesheet" href="/static/animations.css">

<!-- Before </body> -->
<script src="/static/enhancements.js"></script>
```

This adds: glassmorphism, cursor glow, ripple clicks, staggered animations,
scroll reveals, keyboard shortcuts (Ctrl+1-9), loading bar, card tilt effects.
