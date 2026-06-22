"""Continuity Studio — Windows desktop launcher.

Boots the FastAPI server on a free localhost port in a background thread, then
opens a native desktop window pointed at it (pywebview). Falls back to the
system default browser if pywebview is unavailable.

Packaged-app aware:
  * All WRITABLE state (data/, vault.json, users.json, codes.json) is redirected
    to %LOCALAPPDATA%\\ContinuityStudio so it works after install into the
    read-only Program Files directory.
  * Bundled ffmpeg/ffprobe (next to the frozen exe under ``ffmpeg/``) is
    prepended to PATH so the app finds them with no system install.

This module sets the environment BEFORE importing the app so config.py and
store.py pick up the writable locations at import time.
"""
import os
import sys
import socket
import threading
import time


def _app_root():
    """Directory that holds bundled read-only resources (static/, ffmpeg/)."""
    if getattr(sys, "frozen", False):
        # PyInstaller: resources are unpacked next to the exe (one-folder) or
        # into _MEIPASS (one-file). _MEIPASS covers both for added datas.
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _writable_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "ContinuityStudio")
    os.makedirs(d, exist_ok=True)
    return d


def _free_port(preferred=8000):
    """Return a usable localhost port — try the preferred one first."""
    for p in (preferred, 8000, 8765, 8123, 0):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", p))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            continue
    return 8000


def _prepare_env():
    root = _app_root()
    state = _writable_dir()

    # Writable locations (read by config.py / app.py at import).
    os.environ.setdefault("DATA_DIR", os.path.join(state, "data"))
    os.environ.setdefault("CS_CONFIG_DIR", state)

    # Bundled ffmpeg/ffprobe -> PATH (no system install required).
    ff = os.path.join(root, "ffmpeg")
    if os.path.isdir(ff):
        os.environ["PATH"] = ff + os.pathsep + os.environ.get("PATH", "")

    # Make sure the working dir is the resource root so any remaining relative
    # lookups (static/index.html via __file__ already absolute) behave.
    try:
        os.chdir(root)
    except Exception:
        pass
    return root, state


def _run_server(port):
    import uvicorn
    # Import AFTER env is prepared so DATA_DIR / CS_CONFIG_DIR take effect.
    import app as _app
    uvicorn.run(_app.app, host="127.0.0.1", port=port, log_level="warning")


def _wait_ready(url, timeout=40.0):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main():
    _prepare_env()
    port = _free_port(8000)
    url = f"http://127.0.0.1:{port}/"

    t = threading.Thread(target=_run_server, args=(port,), daemon=True)
    t.start()

    _wait_ready(url, timeout=45)

    # Preferred: native desktop window via pywebview.
    try:
        import webview
        webview.create_window(
            "Continuity Studio",
            url,
            width=1480,
            height=940,
            min_size=(1024, 680),
            confirm_close=False,
        )
        webview.start()  # blocks until the window is closed
        return
    except Exception:
        pass

    # Fallback: open in the system default browser and keep the server alive.
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    print(f"Continuity Studio running at {url}  (close this window to quit)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
