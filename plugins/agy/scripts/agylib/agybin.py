"""Find the agy binary and build its command line.

Prompts always go to agy on stdin as stream-json, never as an argument, so
nothing depends on shell quoting. `--print=` (empty, attached with `=`) turns
on print mode without letting `-p` swallow the next flag as its prompt.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

AGY_BIN_ENV = "AGY_BIN"
MIN_VERSION = (1, 1, 12)  # first release where print mode honours --model and --mode
# Only a container job gets this flag, and only container.wrap adds it:
# build_command refuses it, so a host-mode command can never carry it.
SKIP_PERMISSIONS_FLAGS = ("--dangerously-skip-permissions",)

MODEL_ALIASES = {
    "high": "gemini-4-argon-high",
    "medium": "gemini-4-argon-medium",
    "low": "gemini-4-argon-low",
}
DEFAULT_MODEL = "gemini-4-argon-medium"
# When a run stalls, resume on the next model down. Resuming a stalled
# argon-high run on medium finished in about two minutes in testing.
FALLBACK = {
    "gemini-4-argon-high": "gemini-4-argon-medium",
    "gemini-4-argon-medium": "gemini-4-argon-low",
}

STALL_LOG_PATTERN = re.compile(r"stream idle timeout")


def agy_home() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli"


def candidate_paths() -> list[Path]:
    paths = []
    explicit = os.environ.get(AGY_BIN_ENV)
    if explicit:
        paths.append(Path(explicit))
    found = shutil.which("agy")
    if found:
        paths.append(Path(found))
    home = Path.home()
    paths += [
        Path("/usr/local/bin/agy"),
        home / ".local" / "bin" / "agy",
        home / ".local" / "share" / "agy" / "agy",
    ]
    return paths


def find_agy() -> Path | None:
    """Return the first executable agy. Shell functions named agy are invisible here by design."""
    for path in candidate_paths():
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def parse_version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(part) for part in match.groups()) if match else None


def agy_version(binary: Path) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_version(out.stdout + out.stderr)


def resolve_model(name: str | None) -> str:
    if not name:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(name, name)


def build_command(
    binary: Path,
    *,
    model: str,
    mode: str | None,
    log_file: Path,
    conversation_id: str | None = None,
    json_schema: Path | None = None,
) -> list[str]:
    args = [
        str(binary),
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--log-file",
        str(log_file),
        "--print-timeout",
        "0s",
    ]
    if mode:
        args += ["--mode", mode]
    if conversation_id:
        args += ["--conversation", conversation_id]
    if json_schema:
        args += ["--json-schema", str(json_schema)]
    args.append("--print=")
    for flag in SKIP_PERMISSIONS_FLAGS:
        if flag in args:
            raise ValueError(f"refusing to pass {flag} to agy")
    return args


def user_message(text: str) -> str:
    """One stream-json input line carrying a user turn."""
    return json.dumps({"event": "user", "message": {"role": "user", "content": text}}) + "\n"


def read_settings() -> dict[str, Any]:
    try:
        with open(agy_home() / "settings.json", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# One row of `agy models`: the model id, a tab, then its display name.
MODEL_ROW = re.compile(r"^gemini-[\w.-]+\t\S")


def token_present() -> bool:
    token = agy_home() / "antigravity-oauth-token"
    return token.is_file() and token.stat().st_size > 0


def sign_in_check(binary: Path) -> tuple[bool | None, str]:
    """Ask agy itself whether it is signed in, by listing models.

    agy may keep its sign-in in the token file or in the operating system's
    keychain, so the file alone does not decide it. Listing models sends no
    prompt and needs a sign-in the server accepts. agy exits 0 even when it
    refuses, so the output decides: model rows with no other output and a
    zero exit mean signed in, a request to sign in means not, and anything
    else (a timeout, a licence error) is unknown, with agy's message as the
    detail.
    """
    try:
        proc = subprocess.run(
            [str(binary), "models"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None, "`agy models` did not answer within 30 seconds"
    except OSError as exc:
        return None, f"could not run `agy models`: {exc}"
    lines = [line.strip() for line in (proc.stdout + proc.stderr).splitlines()]
    lines = [line for line in lines if line and line != "Fetching available models..."]
    rows = [line for line in lines if MODEL_ROW.match(line)]
    other = [line for line in lines if not MODEL_ROW.match(line)]
    if any("please sign in" in line.lower() for line in other):
        return False, next(line for line in other if "please sign in" in line.lower())
    if rows and not other and proc.returncode == 0:
        return True, "agy listed its models"
    if other:
        return None, other[0][:200]
    return None, f"`agy models` printed no models (exit {proc.returncode})"
