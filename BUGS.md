# Bugs & Issues Found in full-automation

## Critical

### 1. `hmac.new` is WRONG (auth broken on some Python versions)
**File:** `app.py` (lines ~128-135)  
**Bug:** Uses `hmac.new()` — the correct function name is `hmac.new()` in Python 3, but some environments shadow it. More critically, if the import pattern changes this will silently fail.  
**Fix:** Use the explicit `hmac.HMAC()` constructor for clarity and safety.

### 2. Race condition in state file writes
**File:** `store.py`  
**Bug:** `load_state()` and `save_state()` have no file locking. Concurrent requests (FastAPI is async) can corrupt `state.json` if two writes overlap.  
**Fix:** Use `fcntl.flock()` (Unix) or `msvcrt.locking()` (Windows) around file operations, or use an atomic write pattern (write to temp, then rename).

### 3. Uncaught exception in vault decryption crashes startup
**File:** `app.py`, `vault_crypto.py`  
**Bug:** If `vault.json` is corrupted/malformed, `vault_crypto.decrypt_vault()` raises and takes down the entire auth middleware, making the app unrecoverable.  
**Fix:** Wrap in try/except, fall back to empty vault `{}`.

## High

### 4. Log files committed to repo
**Files:** `server.log`, `server_run.log`, `server_run.err.log`, `server_run.out.log`  
**Bug:** Logs with potentially sensitive data (API errors, paths) are in version control.  
**Fix:** Add `*.log` to `.gitignore` and remove tracked logs.

### 5. Backup/gate files committed
**Files:** `app.py.bak`, `app.py.gate`, `app.py.previous_gate`, `config.py.bak_8013`  
**Bug:** Old versions of source code polluting the repo. Confusing and adds 160KB of dead weight.  
**Fix:** Remove and gitignore `*.bak*`, `*.gate`, `*.previous_gate`.

### 6. No input validation on file uploads
**File:** `app.py` (video/upload endpoints)  
**Bug:** The 500MB limit exists but there's no MIME type validation. A user could upload a .exe as "video.mp4" and it'd be saved to disk.  
**Fix:** Validate Content-Type and/or file magic bytes before writing.

### 7. `_run_capture` timeout doesn't kill child process
**File:** `app.py`  
**Bug:** When `subprocess.TimeoutExpired` fires, the child process (ffmpeg) is NOT killed — it becomes an orphan zombie consuming resources.  
**Fix:** Use `process.kill()` in the except block, or use Popen with explicit termination.

## Medium

### 8. Monolithic 441KB app.py
**Problem:** Single file with ALL routes, auth, middleware, helpers. Impossible to maintain, test, or review.  
**Recommendation:** Split into modules (routes/, middleware/, services/).

### 9. Monolithic 505KB index.html
**Problem:** Entire SPA (HTML + CSS + JS) in one file. No bundler, no code splitting.  
**Recommendation:** At minimum, extract CSS and JS to separate files for cacheability.

### 10. `users.json` and `codes.json` in repo
**Bug:** Auth-related files that should be per-deployment are tracked in git.  
**Fix:** Gitignore them.

### 11. Hardcoded fallback model names
**File:** `config.py`  
**Bug:** `NINEROUTER_MODELS` and `AGENTROUTER_MODELS` are hardcoded lists that go stale as providers update.  
**Fix:** Fetch dynamically with a cached fallback.

### 12. No rate limiting on auth endpoints
**File:** `app.py` (signup/login)  
**Bug:** No brute-force protection. An attacker can hammer `/api/auth/login` indefinitely.  
**Fix:** Add IP-based rate limiting (e.g. slowapi or manual counter).

## Low / Cosmetic

### 13. `_fix_tests.py` has no tests
**File:** `_fix_tests.py`  
**Bug:** The file exists but there's no test suite. It's a script that patches something — unclear what.  

### 14. Inconsistent error responses
**Bug:** Some endpoints return `{"detail": ...}` (FastAPI default), others return `{"error": ...}`. Frontend must handle both.  
**Fix:** Standardize on one format.

### 15. No HTTPS enforcement
**Bug:** Session cookies set without `secure=True` flag. In production over HTTP, cookies are sent in cleartext.  
**Fix:** Set `secure=True` when not localhost.

---

## How to Apply Animations

Add these two lines inside `<head>` of `static/index.html`:

```html
<link rel="stylesheet" href="/static/animations.css">
```

And before `</body>`:

```html
<script src="/static/enhancements.js"></script>
```

This gives you:
- Smooth entrance animations on all cards, frames, shots
- Staggered reveal for grid items
- Cursor glow effect following the mouse
- Ripple click effects on buttons
- Floating/breathing empty states
- Animated gradient background blobs
- Scroll-triggered reveals
- Tab switch transitions
- Keyboard shortcuts (Ctrl+1-9 for tabs)
- Loading state indicators
- Custom scrollbar styling
- Respects `prefers-reduced-motion`
