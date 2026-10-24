"""Wrap agy in a macOS sandbox-exec profile that limits where it can write.

The kernel enforces this for agy, its file tools and every command it
starts, independently of the hook. Reads and network are left open: agy
needs to read the repository and reach Google's API.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


def available() -> bool:
    return sys.platform == "darwin" and SANDBOX_EXEC.is_file()


def _quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(writable: list[Path], protected: list[Path] | None = None) -> str:
    """Allow writes to the given paths and temporary space, then deny writes to
    the protected paths (the user's checkout) even inside those, then allow
    the writable paths that sit inside a protected one (the worktree's admin
    directory under the checkout's .git). Later SBPL rules take precedence."""
    roots = {os.path.realpath(str(p)) for p in writable}
    temp = {
        os.path.realpath(tempfile.gettempdir()),
        "/private/tmp",
        "/private/var/folders",
    }
    guarded = sorted({os.path.realpath(str(p)) for p in protected or []})
    reopened = sorted(r for r in roots if any(r == g or r.startswith(g + os.sep) for g in guarded))
    allowed = "\n".join(f"  (subpath {_quote(r)})" for r in sorted(roots | temp))
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        "(allow file-write*",
        allowed,
        '  (literal "/dev/null")',
        '  (literal "/dev/tty")',
        '  (literal "/dev/ptmx")',  # agy runs commands in a pseudo-terminal
        '  (regex #"^/dev/fd/")',
        '  (regex #"^/dev/tty")',
        ")",
    ]
    if guarded:
        lines += ["(deny file-write*", *[f"  (subpath {_quote(g)})" for g in guarded], ")"]
    if reopened:
        lines += ["(allow file-write*", *[f"  (subpath {_quote(r)})" for r in reopened], ")"]
    return "\n".join(lines) + "\n"


def wrap(
    command: list[str], profile_path: Path, writable: list[Path], protected: list[Path] | None = None
) -> list[str]:
    profile_path.write_text(profile(writable, protected), encoding="utf-8")
    return [str(SANDBOX_EXEC), "-f", str(profile_path), *command]
