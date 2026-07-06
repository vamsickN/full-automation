"""Agent 8: Code Cleanup — Run this script to clean the repo.

Removes:
- Dead backup files
- Log files
- Response JSON files
- __pycache__ directories

Usage: python cleanup.py [--dry-run]
"""
import os
import sys
import shutil

PATTERNS_TO_DELETE = [
    # Backup files
    "app.py.bak",
    "app.py.gate",
    "app.py.previous_gate",
    "config.py.bak_8013",
    # Log files
    "server.log",
    "server_run.log",
    "server_run.err.log",
    "server_run.out.log",
    # Response dumps
    "create_repo_claude_edit.json",
    "create_repo_claude_edit_response.json",
    "create_yt_repo_response.json",
    # Auth files (should not be in repo)
    "users.json",
    "codes.json",
]

DIRS_TO_DELETE = [
    "__pycache__",
    ".pytest_cache",
    "installer_output",
]


def cleanup(dry_run: bool = False):
    root = os.path.dirname(os.path.abspath(__file__))
    removed = []

    # Files
    for pattern in PATTERNS_TO_DELETE:
        path = os.path.join(root, pattern)
        if os.path.exists(path):
            if dry_run:
                print(f"[DRY] Would delete: {path}")
            else:
                os.remove(path)
                print(f"[DEL] {path}")
            removed.append(path)

    # Directories
    for dirp in DIRS_TO_DELETE:
        path = os.path.join(root, dirp)
        if os.path.isdir(path):
            if dry_run:
                print(f"[DRY] Would delete dir: {path}")
            else:
                shutil.rmtree(path)
                print(f"[DEL] {path}/")
            removed.append(path)

    # Walk for __pycache__ anywhere
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            if d == "__pycache__":
                full = os.path.join(dirpath, d)
                if dry_run:
                    print(f"[DRY] Would delete: {full}")
                else:
                    shutil.rmtree(full)
                    print(f"[DEL] {full}/")
                removed.append(full)

    # Summary
    print(f"\n{'Would remove' if dry_run else 'Removed'} {len(removed)} items.")
    return removed


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    cleanup(dry_run=dry)
