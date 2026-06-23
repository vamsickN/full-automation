"""Suppress console windows for ALL subprocess calls on Windows.

A PyInstaller windowed build (console=False) has no console of its own, so
every subprocess that Python spawns — ffmpeg, ffprobe, yt-dlp's internal
ffmpeg postprocessor, faster-whisper's helpers — pops a brand-new black CMD
window for a fraction of a second. On a busy render that's dozens of flashing
windows.

Rather than thread a ``creationflags`` argument through every single call site
(and miss the ones inside third-party libs like yt_dlp), we monkey-patch
``subprocess.Popen.__init__`` once, at import time, to OR-in the
CREATE_NO_WINDOW flag on Windows. subprocess.run / call / check_output all go
through Popen, so this covers everything in-process, including library code we
don't control.

Import this module as early as possible (before app / uvicorn / yt_dlp).
No-op on non-Windows platforms.
"""
import subprocess
import sys

# CREATE_NO_WINDOW = 0x08000000. Defined in the subprocess module on Windows.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def install():
    """Patch subprocess.Popen so every child process is windowless on Windows."""
    if sys.platform != "win32":
        return  # only Windows spawns console windows for child processes

    if getattr(subprocess.Popen, "_cs_nowindow_patched", False):
        return  # already installed (idempotent)

    _orig_init = subprocess.Popen.__init__

    def _patched_init(self, *args, **kwargs):
        # OR our flag into whatever creationflags the caller passed (default 0).
        flags = kwargs.get("creationflags", 0)
        kwargs["creationflags"] = flags | _CREATE_NO_WINDOW
        # Also pass a hidden STARTUPINFO when none was given — belt and braces
        # for the few callers that rely on startupinfo instead of flags.
        if kwargs.get("startupinfo") is None:
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0  # SW_HIDE
                kwargs["startupinfo"] = si
            except Exception:
                pass
        return _orig_init(self, *args, **kwargs)

    _patched_init._cs_nowindow_patched = True
    subprocess.Popen.__init__ = _patched_init
    subprocess.Popen._cs_nowindow_patched = True
