"""Job state on disk.

Each repository gets a state directory keyed by its canonical path. Each job
gets a directory under it holding job.json, the prompt, the agy event stream,
agy's own log, the hook policy and the result. Writers replace files
atomically, so a reader never sees half a file.
"""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

STATE_DIR_ENV = "AGY_COMPANION_STATE_DIR"
PLUGIN_DATA_ENV = "CLAUDE_PLUGIN_DATA"
SESSION_ID_ENV = "AGY_COMPANION_SESSION_ID"
MAX_JOBS = 50
# A finished job idle this long keeps its record, log and patch but loses its
# working copy and its agy home, which hold nearly all of its disk space.
IDLE_DAYS = 7

# "stopping" is active so nothing can resume a job while cancel is stopping it.
ACTIVE_STATUSES = ("queued", "running", "resuming", "stopping")
TERMINAL_STATUSES = ("completed", "needs-approval", "failed", "cancelled")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def workspace_root(cwd: str | Path) -> Path:
    """Return the git top level for cwd, or cwd itself outside a repository."""
    try:
        out = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return Path(out).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path(cwd).resolve()


def state_root() -> Path:
    """Absolute, because jobs start agy in another directory and pass it these paths."""
    explicit = os.environ.get(STATE_DIR_ENV)
    if explicit:
        return Path(explicit).resolve()
    plugin_data = os.environ.get(PLUGIN_DATA_ENV)
    if plugin_data:
        return (Path(plugin_data) / "state").resolve()
    return (Path(tempfile.gettempdir()) / "agy-companion").resolve()


def state_dir(cwd: str | Path) -> Path:
    root = workspace_root(cwd)
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", root.name).strip("-") or "workspace"
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return state_root() / f"{slug}-{digest}"


def jobs_dir(cwd: str | Path) -> Path:
    return state_dir(cwd) / "jobs"


def new_job_id(kind: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{kind}-{stamp}-{secrets.token_hex(2)}"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on path + '.lock' for a read-modify-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{path}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def session_index(session_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", session_id)
    return state_root() / "sessions" / f"{safe}.json"


def record_session_job(session_id: str | None, repo_root: Path, job_id: str) -> None:
    """Remember which repository each of a Claude session's jobs is in, so the
    session-end hook can stop them wherever they were started."""
    if not session_id:
        return
    path = session_index(session_id)
    with locked(path):
        entries = read_json(path, []) or []
        entries.append({"repo_root": str(repo_root), "job_id": job_id})
        write_json(path, entries)


class JobStore:
    """Jobs for one repository."""

    def __init__(self, cwd: str | Path):
        self.root = workspace_root(cwd)
        self.dir = state_dir(cwd)
        self.jobs = self.dir / "jobs"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs / job_id

    def create(self, record: dict[str, Any]) -> Path:
        directory = self.job_dir(record["id"])
        directory.mkdir(parents=True, exist_ok=False)
        record.setdefault("created_at", now_iso())
        record["updated_at"] = record["created_at"]
        write_json(directory / "job.json", record)
        self.prune()
        return directory

    def read(self, job_id: str) -> dict[str, Any] | None:
        return read_json(self.job_dir(job_id) / "job.json")

    def update(self, job_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        path = self.job_dir(job_id) / "job.json"
        with locked(path):
            record = read_json(path, {}) or {}
            mutate(record)
            record["updated_at"] = now_iso()
            write_json(path, record)
            return record

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.jobs.is_dir():
            return []
        records = []
        for child in self.jobs.iterdir():
            record = read_json(child / "job.json")
            if isinstance(record, dict) and record.get("id"):
                records.append(record)
        records.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)
        return records

    def resolve(
        self, reference: str | None, predicate: Callable[[dict[str, Any]], bool] | None = None
    ) -> dict[str, Any] | None:
        """Find a job by full id, unique prefix, or (with no reference) the newest match."""
        candidates = [r for r in self.list_jobs() if predicate is None or predicate(r)]
        if not reference:
            return candidates[0] if candidates else None
        exact = [r for r in candidates if r["id"] == reference]
        if exact:
            return exact[0]
        prefixed = [r for r in candidates if r["id"].startswith(reference)]
        return prefixed[0] if len(prefixed) == 1 else None

    def prune(self) -> None:
        """Keep the newest MAX_JOBS finished jobs; never remove an active one.

        A pruned job's working copy goes with it, so no stale worktree stays
        registered in the user's repository. Then, in every repository,
        finished jobs idle for longer than IDLE_DAYS lose their working copy.
        """
        records = self.list_jobs()
        finished = [r for r in records if r.get("status") not in ACTIVE_STATUSES]
        for record in finished[MAX_JOBS:]:
            self._remove_job(record["id"])
        expire_idle_jobs()

    def _remove_job(self, job_id: str) -> None:
        """Remove a finished job and its working copy, rechecking its status under the job's lock.

        A resume takes the same lock to mark the job queued, so a job resumed
        since list_jobs read it is left alone.
        """
        job_dir = self.job_dir(job_id)
        path = job_dir / "job.json"
        with locked(path):
            record = read_json(path)
            if not isinstance(record, dict) or record.get("status") in ACTIVE_STATUSES:
                return
            drop_working_copy(record, job_dir)
            # A container round may leave read-only directories (a module
            # cache, say) in the job's home; make them removable first.
            for root_dir, dirs, _files in os.walk(job_dir):
                for name in dirs:
                    try:
                        os.chmod(os.path.join(root_dir, name), 0o700, follow_symlinks=False)
                    except (OSError, NotImplementedError):
                        pass
            shutil.rmtree(job_dir, ignore_errors=True)


def drop_working_copy(record: dict[str, Any], job_dir: Path) -> bool:
    """Remove a finished job's working copy and its container homes; return whether anything went.

    The job can no longer be resumed afterwards, but its record, agy's log
    and the exported patch stay, so status, result and apply still work.
    """
    from . import container, gitctx

    removed = False
    worktree = Path(record.get("worktree") or "")
    if record.get("worktree") and worktree.exists():
        try:
            gitctx.remove_worktree(record.get("repo_root") or worktree, worktree)
        except gitctx.GitError:
            pass  # The repository has moved or gone, so there is no worktree entry to unregister.
        if worktree.exists():
            container.remove_tree(worktree)
        removed = True
    for home in (container.job_home(job_dir), container.job_mount(job_dir) / "export-home"):
        if _owned_dir(job_dir, home):
            container.remove_tree(home)
            removed = True
    return removed


def drop_finished_working_copy(
    job_dir: Path, should_drop: Callable[[dict[str, Any]], bool] = lambda _record: True
) -> bool:
    """drop_working_copy for a job that is finished and passes should_drop, checked under its lock.

    A resume takes the same lock to mark the job queued and checks there
    that the working copy still exists, so the two cannot interleave.
    """
    path = job_dir / "job.json"
    with locked(path):
        record = read_json(path)
        if not isinstance(record, dict) or record.get("status") in ACTIVE_STATUSES or not should_drop(record):
            return False
        return drop_working_copy(record, job_dir)


def _owned_dir(job_dir: Path, path: Path) -> bool:
    """Whether path is a real directory reached from job_dir through no symbolic link.

    Directories below the job directory are writable from inside the
    container, so a round could replace any of them with a link to the host.
    """
    current = job_dir
    for part in path.relative_to(job_dir).parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return False
    return True


def expire_idle_jobs(idle_days: int = IDLE_DAYS) -> int:
    """Drop the working copy of every finished job, in any repository, idle for longer than idle_days.

    Returns how many jobs lost a working copy. Jobs in repositories that are
    no longer used are reached too, because this walks the whole state root.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=idle_days)
    expired = 0
    for job_file in state_root().glob("*/jobs/*/job.json"):

        def idle(record: dict[str, Any]) -> bool:
            idle_since = parse_iso(record.get("completed_at") or record.get("updated_at"))
            return bool(idle_since and idle_since < cutoff)

        if drop_finished_working_copy(job_file.parent, idle):
            expired += 1
    return expired
