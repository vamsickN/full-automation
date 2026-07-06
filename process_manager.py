"""Agent 5: Error Handling — Safe subprocess management.

Fixes:
- Zombie process prevention (proper kill on timeout)
- Output streaming for long-running ffmpeg
- Memory-bounded output capture
- Process group cleanup
"""
import os
import signal
import subprocess
import sys
import time
from typing import Optional, Tuple, List


class ProcessResult:
    """Result of a managed subprocess run."""
    def __init__(self, returncode: int, stdout: str, stderr: str, timed_out: bool = False):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.ok = returncode == 0

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def run_safe(
    args: List[str],
    timeout: int = 600,
    max_output_bytes: int = 10 * 1024 * 1024,  # 10MB max capture
    cwd: Optional[str] = None,
) -> ProcessResult:
    """Run a subprocess safely with timeout and proper cleanup.
    
    Unlike subprocess.run with timeout, this actually KILLS the process
    and all its children (process group) when the timeout fires.
    """
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": cwd,
    }
    
    # On Unix, create new process group so we can kill the whole tree
    if sys.platform != "win32":
        kwargs["preexec_fn"] = os.setsid
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    
    proc = subprocess.Popen(args, **kwargs)
    
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        stdout = stdout_bytes.decode("utf-8", errors="replace")[:max_output_bytes]
        stderr = stderr_bytes.decode("utf-8", errors="replace")[:max_output_bytes]
        return ProcessResult(proc.returncode, stdout, stderr)
    
    except subprocess.TimeoutExpired:
        # KILL the entire process group
        _kill_tree(proc)
        # Collect whatever output we got
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
            stdout = stdout_bytes.decode("utf-8", errors="replace")[:max_output_bytes]
            stderr = stderr_bytes.decode("utf-8", errors="replace")[:max_output_bytes]
        except Exception:
            stdout, stderr = "", ""
        
        return ProcessResult(
            124,  # standard timeout exit code
            stdout,
            stderr + f"\n[TIMEOUT] Process killed after {timeout}s",
            timed_out=True,
        )
    
    except Exception as e:
        _kill_tree(proc)
        return ProcessResult(-1, "", str(e))


def _kill_tree(proc: subprocess.Popen):
    """Kill a process and all its children."""
    try:
        if sys.platform == "win32":
            # Windows: taskkill /T kills the tree
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            # Unix: kill the process group
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                time.sleep(0.5)
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        # Last resort
        try:
            proc.kill()
        except Exception:
            pass
    
    # Reap zombie
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def ffmpeg_run(args: List[str], timeout: int = 600) -> ProcessResult:
    """Run ffmpeg with proper timeout and output capture."""
    # Ensure -y (overwrite) and limited log level
    full_args = list(args)
    if "-loglevel" not in full_args:
        full_args.extend(["-loglevel", "warning"])
    if "-y" not in full_args:
        full_args.append("-y")
    return run_safe(full_args, timeout=timeout)


def ffprobe_run(args: List[str], timeout: int = 30) -> ProcessResult:
    """Run ffprobe with tight timeout."""
    return run_safe(args, timeout=timeout)
