"""Run one agy turn for a job, follow its event stream, and recover from stalls.

agy can stall on a model for minutes: its log shows "stream idle timeout
(5m0s)" while it retries quietly. The worker treats that log line, or a long
gap with no stream event, as a stall. It stops agy and resumes the same
conversation on the next model down (FALLBACK in agybin).
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

from . import agybin, gitctx, sandbox
from .policy import WRITE_TOOLS
from .state import JobStore, now_iso, read_json, write_json

DEFAULT_STALL_SECONDS = 330  # agy's own idle timeout is 5 minutes
# While a tool step is ACTIVE the stream is silent until the tool finishes
# (a hook or a long command), so allow much longer before calling it a stall.
DEFAULT_TOOL_STALL_SECONDS = 1800
STOP_GRACE_SECONDS = 10
DEFAULT_MAX_STALL_RESUMES = 2
DEFAULT_MAX_MINUTES = 60
PROGRESS_WRITE_INTERVAL = 1.0
PERMISSION_DENIED = re.compile(r'permission check failed for command "(.*)": ', re.S)
AUTH_HINTS = ("authentication required", "authentication timed out", "not signed in")

CONTINUE_AFTER_STALL = (
    "Your previous attempt stopped because the model stream stalled. Continue the same "
    "task from where you stopped and give the complete final answer."
)


class TurnOutcome(NamedTuple):
    kind: str  # "result", "stall", "exited", "timeout"
    detail: str
    result: dict[str, Any] | None


def _reader(stream: Any, sink: queue.Queue) -> None:
    for raw in iter(stream.readline, b""):
        sink.put(raw.decode("utf-8", errors="replace"))
    sink.put(None)


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _stop(proc: subprocess.Popen) -> None:
    """Stop agy and everything it started; agy leads its own process group."""
    if proc.poll() is not None:
        _signal_group(proc.pid, signal.SIGKILL)  # reap any orphans left in the group
        return
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
    _signal_group(proc.pid, signal.SIGKILL)


def run_turn(
    command: list[str],
    *,
    cwd: Path,
    message: str,
    events_path: Path,
    log_path: Path,
    stderr_path: Path,
    stall_seconds: float,
    deadline: float,
    on_event: Callable[[dict[str, Any]], None],
    env: dict[str, str] | None = None,
    tool_stall_seconds: float = DEFAULT_TOOL_STALL_SECONDS,
    on_start: Callable[[int], None] | None = None,
) -> TurnOutcome:
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    with open(stderr_path, "ab") as stderr:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            env=env,
            start_new_session=True,
        )
    try:
        if on_start:
            on_start(proc.pid)
        return _follow(
            proc, message, events_path, log_path, log_offset, stall_seconds, tool_stall_seconds, deadline, on_event
        )
    finally:
        _stop(proc)


def _follow(
    proc: subprocess.Popen,
    message: str,
    events_path: Path,
    log_path: Path,
    log_offset: int,
    stall_seconds: float,
    tool_stall_seconds: float,
    deadline: float,
    on_event: Callable[[dict[str, Any]], None],
) -> TurnOutcome:
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(agybin.user_message(message).encode())
        proc.stdin.close()
    except BrokenPipeError:
        pass  # agy exited before reading; its stderr says why
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, lines), daemon=True).start()

    last_event = time.monotonic()
    active_tools: set[int] = set()
    result: dict[str, Any] | None = None
    with open(events_path, "a", encoding="utf-8") as events:
        while True:
            try:
                line = lines.get(timeout=2)
            except queue.Empty:
                line = ""
            if line is None:
                break
            now = time.monotonic()
            if line.strip():
                last_event = now
                events.write(line if line.endswith("\n") else line + "\n")
                events.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                on_event(event)
                if event.get("event") == "result":
                    result = event.get("result") or {}
                step = event.get("step_update") or {}
                if step.get("step_type") == "tool":
                    index = int(step.get("step_index") or 0)
                    if step.get("state") == "ACTIVE":
                        active_tools.add(index)
                    else:
                        active_tools.discard(index)
            if now > deadline:
                return TurnOutcome("timeout", "job exceeded its time limit", result)
            limit = tool_stall_seconds if active_tools else stall_seconds
            if now - last_event > limit:
                return TurnOutcome("stall", f"no agy event for {int(limit)} s", result)
            if log_path.exists():
                with open(log_path, "rb") as log:
                    log.seek(log_offset)
                    chunk = log.read().decode("utf-8", errors="replace")
                    log_offset = log.tell()
                match = agybin.STALL_LOG_PATTERN.search(chunk)
                if match and result is None:
                    return TurnOutcome("stall", f"agy log: {match.group(0)}", result)
    # stdout is closed; agy should exit now, but keep enforcing the deadline.
    while proc.poll() is None:
        if time.monotonic() > deadline:
            return TurnOutcome("timeout", "job exceeded its time limit", result)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if result is not None or time.monotonic() - last_event > stall_seconds:
                break  # the turn is over or agy is hung; the caller's finally stops it
    if result is not None:
        return TurnOutcome("result", str(result.get("status", "")), result)
    return TurnOutcome("exited", f"agy exited with code {proc.returncode} and no result", None)


class JobRunner:
    """Runs a job's pending turn, then resumes after stalls until it finishes."""

    def __init__(self, store: JobStore, job_id: str):
        self.store = store
        self.job_id = job_id
        self.dir = store.job_dir(job_id)
        self.job = store.read(job_id) or {}
        self.progress: dict[str, Any] = {"steps": 0, "tools": 0, "last_tool": None, "text": ""}
        self.plan_paths: list[str] = []
        self.denied_commands: list[str] = []
        self.step_usage: dict[str, int] = {}
        self._last_write = 0.0

    # Event handling -------------------------------------------------------

    def _save_progress(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < PROGRESS_WRITE_INTERVAL:
            return
        self._last_write = now
        snapshot = dict(self.progress)
        snapshot["text"] = snapshot["text"][-400:]
        self.store.update(self.job_id, lambda r: r.update({"progress": snapshot}))

    def on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "init":
            conversation = event.get("conversation_id")
            if conversation and conversation != self.job.get("conversation_id"):
                self.job["conversation_id"] = conversation
                self.store.update(self.job_id, lambda r: r.update({"conversation_id": conversation}))
            return
        if kind != "step_update":
            return
        step = event.get("step_update") or {}
        if step.get("state") == "DONE":
            for key, value in (step.get("usage") or {}).items():
                if isinstance(value, int):
                    self.step_usage[key] = self.step_usage.get(key, 0) + value
        self.progress["steps"] = max(self.progress["steps"], int(step.get("step_index") or 0))
        if step.get("text_delta"):
            self.progress["text"] += step["text_delta"]
        if step.get("step_type") == "tool":
            name = step.get("tool_name")
            info = step.get("tool_info") or {}
            params = info.get("parameters") or {}
            self.progress["last_tool"] = name
            if step.get("state") == "DONE":
                self.progress["tools"] += 1
                path = params.get("Path") or params.get("TargetFile")
                if name in WRITE_TOOLS and isinstance(path, str) and "/brain/" in path and path.endswith(".md"):
                    self.plan_paths.append(path)
            if step.get("state") == "ERROR":
                message = str((info.get("error") or {}).get("message", ""))
                match = PERMISSION_DENIED.search(message)
                if match:
                    self.denied_commands.append(match.group(1))
            self._save_progress(force=True)
        else:
            self._save_progress()

    # Turn loop --------------------------------------------------------------

    def _record_agy_pid(self, pid: int) -> None:
        self.store.update(self.job_id, lambda r: r.update({"agy_pid": pid}))

    def _command(self, model: str, conversation: str | None) -> list[str]:
        binary = Path(self.job["agy_bin"])
        schema = self.job.get("json_schema")
        command = agybin.build_command(
            binary,
            model=model,
            mode=self.job["mode"],
            log_file=self.dir / "agy.log",
            conversation_id=conversation,
            json_schema=Path(schema) if schema else None,
        )
        if self.job.get("os_sandbox"):
            command = sandbox.wrap(
                command,
                self.dir / "sandbox.sb",
                [Path(p) for p in self.job["writable_paths"]],
                protected=[Path(self.job["repo_root"])],
            )
        return command

    def run(self) -> dict[str, Any]:
        message = (self.dir / "next-message.md").read_text(encoding="utf-8")
        model = self.job["model"]
        conversation = self.job.get("conversation_id")
        stall_seconds = float(self.job.get("stall_seconds", DEFAULT_STALL_SECONDS))
        max_resumes = int(self.job.get("max_stall_resumes", DEFAULT_MAX_STALL_RESUMES))
        deadline = time.monotonic() + 60 * float(self.job.get("max_minutes", DEFAULT_MAX_MINUTES))
        queued_before = _count_lines(self.dir / "queued.ndjson")
        round_record: dict[str, Any] = {
            "started_at": now_iso(),
            "model": model,
            "stalls": [],
            "kind": self.job.get("next_round_kind", "initial"),
        }
        if self.job.get("pending_approval_wait_s") is not None:
            round_record["approval_wait_s"] = self.job["pending_approval_wait_s"]
        started = time.monotonic()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        usage_total: dict[str, int] = {}

        while True:
            self.step_usage = {}
            outcome = run_turn(
                self._command(model, conversation),
                cwd=Path(self.job["worktree"]),
                message=message,
                events_path=self.dir / "events.ndjson",
                log_path=self.dir / "agy.log",
                stderr_path=self.dir / "agy.stderr.txt",
                stall_seconds=stall_seconds,
                deadline=deadline,
                on_event=self.on_event,
                env=env,
                tool_stall_seconds=float(self.job.get("tool_stall_seconds", DEFAULT_TOOL_STALL_SECONDS)),
                on_start=self._record_agy_pid,
            )
            # A finished turn reports its total; an interrupted one only its steps.
            turn_usage = (outcome.result or {}).get("usage") or self.step_usage
            for key, value in turn_usage.items():
                if isinstance(value, int):
                    usage_total[key] = usage_total.get(key, 0) + value
            conversation = self.job.get("conversation_id") or conversation
            if outcome.kind == "stall" and conversation and len(round_record["stalls"]) < max_resumes:
                next_model = agybin.FALLBACK.get(model, model)
                round_record["stalls"].append(
                    {"at": now_iso(), "model": model, "reason": outcome.detail, "resumed_on": next_model}
                )
                update = {
                    "status": "resuming",
                    "phase": f"stalled on {model}; resuming on {next_model}",
                    "current_stalls": list(round_record["stalls"]),
                }
                self.store.update(self.job_id, lambda r, u=update: r.update(u))
                model = next_model
                message = CONTINUE_AFTER_STALL
                continue
            break

        round_record.update(
            {
                "finished_at": now_iso(),
                "duration_s": round(time.monotonic() - started, 1),
                "final_model": model,
                "usage": usage_total or None,
            }
        )
        return self._finish(outcome, round_record, queued_before)

    # Completion -------------------------------------------------------------

    def _finish(self, outcome: TurnOutcome, round_record: dict[str, Any], queued_before: int) -> dict[str, Any]:
        result = outcome.result or {}
        queued = _read_lines(self.dir / "queued.ndjson")[queued_before:]
        commands = [q["command"] for q in queued] + self.denied_commands
        commands = list(dict.fromkeys(c for c in commands if c))
        round_record["queued_commands"] = commands

        # With --json-schema the reply may be only in structured_output; keep
        # it readable as text in case it fails the shape check below.
        response = result.get("response") or ""
        if not response and "structured_output" in result:
            response = json.dumps(result["structured_output"], indent=2)
        stored: dict[str, Any] = {
            "kind": self.job["kind"],
            "agy_status": result.get("status"),
            "response": response,
            "error": result.get("error"),
            "usage": result.get("usage"),
            "denied_actions": result.get("denied_actions"),
            "queued_commands": commands,
        }
        error = None
        if outcome.kind != "result":
            error = outcome.detail
            stderr_tail = _tail(self.dir / "agy.stderr.txt")
            if any(hint in stderr_tail.lower() for hint in AUTH_HINTS):
                error = "agy is not signed in. Run /agy:setup, then sign in with `agy` in a terminal."
            stored["stderr_tail"] = stderr_tail
        elif result.get("status") != "SUCCESS":
            error = result.get("error") or f"agy finished with status {result.get('status')}"

        kind = self.job["kind"]
        # agy can report an error (a stream idle timeout, seen live) and still
        # deliver the whole reply, so a review is parsed whenever there is one.
        if kind in ("review", "adversarial-review") and (error is None or response):
            schema = read_json(Path(self.job["json_schema"]), {}) if self.job.get("json_schema") else {}
            stored["review"], stored["parse_error"] = _parse_review(result, schema)
        if kind == "plan" and self.plan_paths:
            plan_file = Path(self.plan_paths[-1])
            if plan_file.is_file():
                stored["plan_path"] = str(plan_file)
                stored["plan_markdown"] = plan_file.read_text(encoding="utf-8", errors="replace")
        if self.job.get("writable"):
            patch_file = self.dir / "changes.patch"
            if patch_file.exists():
                patch_file.unlink()  # never leave an earlier round's patch for apply
            try:
                changes = gitctx.export_changes(Path(self.job["worktree"]), self.job["baseline"])
                patch_file.write_bytes(changes["patch"])
                stored["changed_files"] = changes["name_status"]
                stored["changed_paths"] = changes["paths"]
            except gitctx.GitError as exc:
                error = error or f"could not export agy's changes: {exc}"

        write_json(self.dir / "result.json", stored)
        if error is not None:
            status = "failed"
        elif commands:
            status = "needs-approval"
        else:
            status = "completed"

        def mutate(record: dict[str, Any]) -> None:
            record.setdefault("rounds", []).append(round_record)
            record.pop("pending_approval_wait_s", None)
            if record.get("status") in ("cancelled", "stopping") or record.get("generation") != self.job.get(
                "generation"
            ):
                return  # cancelled, or a newer worker owns the job
            record.update(
                {
                    "status": status,
                    "phase": "done" if status == "completed" else status,
                    "error": error,
                    "pid": None,
                    "agy_pid": None,
                    "model": round_record["final_model"],
                    "completed_at": now_iso(),
                    "queued_commands": commands,
                    "progress": dict(self.progress, text=self.progress["text"][-400:]),
                }
            )
            record.pop("current_stalls", None)

        record = self.store.update(self.job_id, mutate)
        # A review's or plan's working copy has no further use unless agy is
        # waiting on queued commands, so remove it rather than leave it
        # registered in the user's repository. Tasks keep theirs for
        # --resume-last and apply.
        if self.job.get("kind") != "task" and record.get("status") in ("completed", "failed", "cancelled"):
            gitctx.remove_worktree(self.job["repo_root"], Path(self.job["worktree"]))
        return record


def _parse_review(result: dict[str, Any], schema: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    structured = result.get("structured_output")
    if isinstance(structured, dict):
        return _checked_review(structured, schema)
    text = str(result.get("response", "")).strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None, "agy did not return JSON"
    try:
        value = json.loads(text[start : end + 1])
    except ValueError as exc:
        return None, f"agy returned invalid JSON: {exc}"
    if not isinstance(value, dict):
        return None, "agy returned JSON that is not an object"
    return _checked_review(value, schema)


def _checked_review(review: dict[str, Any], schema: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Accept a review only when it has the schema's required keys and types.

    Enumerations and bounds are not enforced: a review with an unexpected
    severity word is still worth showing, while a wrong type would make the
    rendering fail or print nonsense. On rejection the reply is shown raw.
    """
    problem = _type_mismatch(review, schema, "review")
    if problem:
        return None, f"agy returned a review that does not match the schema: {problem}"
    return review, None


JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def _type_mismatch(value: Any, schema: dict[str, Any], where: str) -> str | None:
    """Describe the first place value's types or required keys differ from schema, or None."""
    expected = schema.get("type")
    if expected in JSON_TYPES and (
        not isinstance(value, JSON_TYPES[expected]) or isinstance(value, bool) != (expected == "boolean")
    ):
        return f"{where} should be {'an' if expected[0] in 'aeiou' else 'a'} {expected}"
    if expected == "object":
        for key in schema.get("required", []):
            if key not in value:
                return f"{where} lacks {key}"
        for key, subschema in schema.get("properties", {}).items():
            if key in value:
                problem = _type_mismatch(value[key], subschema, f"{where}.{key}")
                if problem:
                    return problem
    if expected == "array" and "items" in schema:
        for index, item in enumerate(value):
            problem = _type_mismatch(item, schema["items"], f"{where}[{index}]")
            if problem:
                return problem
    return None


def _read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _count_lines(path: Path) -> int:
    return len(_read_lines(path))


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def _command_line(pid: int) -> str:
    """The process's command line, or "" when it cannot be read, so that it is never signalled."""
    try:
        proc = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True)
    except OSError:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def terminate_job_processes(jobs: list[dict[str, Any]], grace: float = STOP_GRACE_SECONDS) -> bool:
    """Stop the workers and agy process groups of several jobs at once,
    escalating to SIGKILL after one shared grace period."""
    groups = set()
    for job in jobs:
        for key in ("pid", "agy_pid"):
            pid = job.get(key)
            if not pid:
                continue
            try:
                group = os.getpgid(int(pid))
            except (ProcessLookupError, PermissionError, ValueError):
                continue
            # Both processes were started with start_new_session, so a live one
            # leads its own group and carries the job id on its command line
            # (the worker as an argument, agy in its --log-file path). A stale
            # record whose pid the system has since given to another process
            # fails these tests, and signalling that process's group would
            # stop the user's own programs.
            if group == int(pid) and str(job.get("id", "")) in _command_line(int(pid)):
                groups.add(group)
    if not groups:
        return False
    for group in groups:
        _signal_group(group, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not any(_signal_group(group, 0) for group in groups):
            return True
        time.sleep(0.2)
    for group in groups:
        _signal_group(group, signal.SIGKILL)
    return True


__all__ = ["JobRunner", "run_turn", "terminate_job_processes", "read_json"]
