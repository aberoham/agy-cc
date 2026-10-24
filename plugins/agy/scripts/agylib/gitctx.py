"""Git helpers: review targets and context, job worktrees, and change export.

Every agy job runs in a detached worktree that starts from the user's HEAD
plus their uncommitted and untracked changes, so agy never touches the
user's checkout. The starting state is committed inside the worktree as a
baseline, and a rescue job's changes are the diff from that baseline.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

MAX_UNTRACKED_BYTES = 256 * 1024
MAX_INLINE_DIFF_BYTES = 400 * 1024
HOOKS_PATH = os.path.join(".agents", "hooks.json")
# Files that commands run in a job's working copy leave behind; never exported.
EXPORT_EXCLUDES = (
    f":(exclude){HOOKS_PATH}",
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)**/.pytest_cache/**",
)
BASELINE_IDENTITY = ["-c", "user.name=agy plugin", "-c", "user.email=agy-plugin@localhost"]


class GitError(RuntimeError):
    pass


# The plugin's own git calls run outside every sandbox, so they must not run
# programs the repository configures: hooks (post-checkout and post-commit run
# even with --no-verify) and the fsmonitor helper. Diff commands also pass
# --no-ext-diff and --no-textconv.
SAFE_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def git(cwd: str | Path, *args: str, check: bool = True, input_bytes: bytes | None = None) -> str:
    proc = subprocess.run(
        [*SAFE_GIT, *args],
        cwd=str(cwd),
        input=input_bytes,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode(errors="replace")


def git_bytes(cwd: str | Path, *args: str) -> bytes:
    proc = subprocess.run([*SAFE_GIT, *args], cwd=str(cwd), capture_output=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def repo_root(cwd: str | Path) -> Path:
    try:
        return Path(git(cwd, "rev-parse", "--show-toplevel").strip()).resolve()
    except (GitError, FileNotFoundError) as exc:
        raise GitError("agy jobs need a git repository; run this inside one") from exc


def has_head(cwd: str | Path) -> bool:
    return (
        subprocess.run([*SAFE_GIT, "rev-parse", "--verify", "HEAD"], cwd=str(cwd), capture_output=True).returncode == 0
    )


def untracked_files(cwd: str | Path) -> list[str]:
    out = git(cwd, "ls-files", "--others", "--exclude-standard", "-z")
    return [p for p in out.split("\0") if p]


def default_branch(cwd: str | Path) -> str:
    ref = git(cwd, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False).strip()
    if ref:
        return ref.replace("refs/remotes/", "", 1)
    for name in ("main", "master", "trunk"):
        if git(cwd, "rev-parse", "--verify", "--quiet", name, check=False).strip():
            return name
    raise GitError("cannot find a default branch; pass --base <ref>")


def working_tree_dirty(cwd: str | Path) -> bool:
    return bool(git(cwd, "status", "--porcelain", "--untracked-files=all").strip())


def resolve_target(cwd: str | Path, base: str | None, scope: str) -> dict[str, Any]:
    if scope not in ("auto", "working-tree", "branch"):
        raise GitError(f"unknown --scope {scope}; use auto, working-tree or branch")
    if base or scope == "branch":
        ref = base or default_branch(cwd)
        return {"mode": "branch", "base": ref, "label": f"branch diff against {ref}"}
    if scope == "working-tree" or working_tree_dirty(cwd):
        return {"mode": "working-tree", "label": "uncommitted changes"}
    ref = default_branch(cwd)
    return {"mode": "branch", "base": ref, "label": f"branch diff against {ref}"}


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n{body.strip() or '(none)'}\n"


def _untracked_body(cwd: Path, rel: str) -> str:
    path = cwd / rel
    try:
        size = path.stat().st_size
    except OSError:
        return f"### {rel}\n(skipped: unreadable)"
    if path.is_dir():
        return f"### {rel}\n(skipped: directory)"
    if size > MAX_UNTRACKED_BYTES:
        return f"### {rel}\n(skipped: {size} bytes)"
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return f"### {rel}\n(skipped: binary)"
    return f"### {rel}\n```\n{data.decode(errors='replace').rstrip()}\n```"


def collect_context(cwd: str | Path, target: dict[str, Any]) -> dict[str, Any]:
    """Return a summary, the changed files and the context text for the prompt."""
    root = repo_root(cwd)
    if target["mode"] == "working-tree":
        status = git(root, "status", "--short", "--untracked-files=all")
        staged = git(root, "diff", "--cached", "--no-ext-diff", "--no-textconv")
        unstaged = git(root, "diff", "--no-ext-diff", "--no-textconv")
        untracked = untracked_files(root)
        files = sorted(set(git(root, "diff", "--name-only", "HEAD").split() if has_head(root) else []) | set(untracked))
        diff = staged + unstaged
        parts = [_section("Git status", status)]
        if len(diff.encode()) <= MAX_INLINE_DIFF_BYTES:
            parts += [_section("Staged diff", staged), _section("Unstaged diff", unstaged)]
        else:
            parts.append(
                _section(
                    "Diff",
                    "(too large to include; read the changed files in the working copy)",
                )
            )
        parts.append(_section("Untracked files", "\n\n".join(_untracked_body(root, f) for f in untracked)))
        summary = f"{len(files)} changed file(s) in the working tree"
    else:
        base = target["base"]
        merge_base = git(root, "merge-base", "HEAD", base).strip()
        rng = f"{merge_base}..HEAD"
        files = git(root, "diff", "--name-only", rng).split()
        log = git(root, "log", "--oneline", rng)
        stat = git(root, "diff", "--stat", rng)
        diff = git(root, "diff", "--no-ext-diff", "--no-textconv", rng)
        parts = [_section("Commit log", log), _section("Diff stat", stat)]
        if len(diff.encode()) <= MAX_INLINE_DIFF_BYTES:
            parts.append(_section("Branch diff", diff))
        else:
            parts.append(_section("Branch diff", "(too large to include; run git diff yourself)"))
        summary = f"{len(files)} changed file(s) on this branch since {base}"
    return {"summary": summary, "files": files, "content": "\n".join(parts)}


def create_worktree(cwd: str | Path, dest: Path) -> str:
    """Make a detached worktree of HEAD plus the user's changes; return the baseline commit."""
    root = repo_root(cwd)
    if not has_head(root):
        raise GitError("the repository has no commits yet")
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(root, "worktree", "add", "--detach", "--quiet", str(dest), "HEAD")
    patch = git_bytes(root, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "HEAD")
    if patch.strip():
        git(dest, "apply", "--binary", "--whitespace=nowarn", input_bytes=patch)
    for rel in untracked_files(root):
        source = root / rel
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), target)  # copy the link, never its target
        elif source.is_file():
            shutil.copy2(source, target)
    git(dest, "add", "-A")
    git(
        dest,
        *BASELINE_IDENTITY,
        "commit",
        "--quiet",
        "--no-verify",
        "--allow-empty",
        "-m",
        "agy job baseline",
    )
    return git(dest, "rev-parse", "HEAD").strip()


def install_hooks(dest: Path, hook_command: str) -> None:
    """Add the plugin's PreToolUse hook to the worktree, keeping any existing hooks.

    A symlinked .agents or hooks.json could point back into the user's
    checkout, so the job refuses to start rather than write through it.
    """
    path = dest / HOOKS_PATH
    for candidate in (path.parent, path):
        if candidate.is_symlink():
            raise GitError(f"{candidate.relative_to(dest)} is a symlink; agy jobs cannot run in this repository")
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, ValueError):
        existing = {}
    existing["agy-plugin-policy"] = {
        "PreToolUse": [
            {
                "matcher": ".*",
                "hooks": [{"type": "command", "command": hook_command, "timeout": 30}],
            }
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")


def export_changes(dest: Path, baseline: str) -> dict[str, Any]:
    """Return agy's changes in the worktree: a byte-exact patch, a file list, and the paths."""
    git(dest, "add", "-A")
    try:
        files = git(dest, "diff", "--cached", "--name-status", baseline, "--", ".", *EXPORT_EXCLUDES)
        raw = git(
            dest, "diff", "--cached", "--name-status", "-z", "--no-renames", baseline, "--", ".", *EXPORT_EXCLUDES
        )
        patch = git_bytes(
            dest,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            baseline,
            "--",
            ".",
            *EXPORT_EXCLUDES,
        )
    finally:
        git(dest, "reset", "--quiet")
    fields = [f for f in raw.split("\0") if f]
    paths = [[status, path] for status, path in zip(fields[0::2], fields[1::2])]
    return {"name_status": files.strip(), "patch": patch, "paths": paths}


def _user_entry(root: Path, rel: str) -> tuple[str, str] | None:
    """(mode, blob id) of a path in the user's working tree, as git would record it."""
    path = root / rel
    if path.is_symlink():
        blob = git(root, "hash-object", "--stdin", input_bytes=os.readlink(path).encode(), check=False).strip()
        return "120000", blob
    if not path.is_file():
        return None
    mode = "100755" if os.access(path, os.X_OK) else "100644"
    return mode, git(root, "hash-object", "--no-filters", "--", rel, check=False).strip()


def baseline_mismatches(root: Path, dest: Path, baseline: str, paths: list[list[str]]) -> list[str]:
    """Paths whose content, type or mode in the user's tree no longer matches the job's baseline."""
    mismatched = []
    for status, rel in paths:
        actual = _user_entry(root, rel)
        if status == "A":
            if actual is not None or (root / rel).exists():
                mismatched.append(rel)
            continue
        listed = git(dest, "ls-tree", baseline, "--", rel, check=False).split()
        expected = (listed[0], listed[2]) if len(listed) >= 3 else None
        if actual != expected:
            mismatched.append(rel)
    return mismatched


def apply_patch(cwd: str | Path, patch: bytes, check_only: bool = False) -> str:
    """Apply a job's patch to the user's working tree.

    The patch is relative to the job's baseline, which included the user's
    uncommitted changes, so it applies to the working tree, not the index.
    """
    root = repo_root(cwd)
    args = ["apply", "--binary", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    return git(root, *args, input_bytes=patch)


def remove_worktree(cwd: str | Path, dest: Path) -> None:
    root = repo_root(cwd)
    git(root, "worktree", "remove", "--force", str(dest), check=False)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    git(root, "worktree", "prune", check=False)
