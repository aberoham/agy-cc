#!/usr/bin/env python3
"""Claude Code companion for Google's Antigravity CLI (agy).

Starts agy jobs in isolated git worktrees, tracks them, and reports results.
Standard library only; runs on the macOS system Python 3.9.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from agylib import agybin, container, gitctx, render, sandbox  # noqa: E402
from agylib.runner import JobRunner, terminate_job_processes  # noqa: E402
from agylib.state import (  # noqa: E402
    ACTIVE_STATUSES,
    SESSION_ID_ENV,
    STATE_DIR_ENV,
    TERMINAL_STATUSES,
    JobStore,
    new_job_id,
    now_iso,
    parse_iso,
    read_json,
    record_session_job,
    session_index,
    state_root,
    write_json,
)

KIND_MODES = {"review": None, "adversarial-review": None, "plan": "plan", "task": "accept-edits"}
DEFAULT_WAIT_SECONDS = 100  # stays under Claude Code's default two-minute Bash timeout
ISOLATION_CHOICES = ("auto", "container", "host")
HOST_SHELL_RULE = (
    "Shell commands that are not read-only are not run: they are queued for the colleague's "
    "assistant, which may run them and send you the output in a later turn."
)
CONTAINER_SHELL_RULE = (
    "You are inside a container that holds only this working copy, so run the commands you need, including tests."
)
HOST_REVIEW_GUIDANCE = (
    "Use the repository context below as primary evidence. Do not use shell commands in this "
    "job; most are blocked. Use your file tools (read, list, find and search files) to inspect "
    "the repository."
)
CONTAINER_REVIEW_GUIDANCE = (
    "Use the repository context below as primary evidence, and inspect the working copy with "
    "your file tools or read-only shell commands. Do not change files."
)
READ_ONLY_NOTE = (
    "\n\nThis round is read-only: the working copy is mounted read-only, so do not try to edit "
    "files or run commands that write there. Report what you find and what you would change.\n"
)


class UsageError(Exception):
    pass


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2))
    else:
        print(value)


def template(name: str, **values: str) -> str:
    text = (PLUGIN_ROOT / "prompts" / name).read_text(encoding="utf-8")
    if text.startswith("<!--"):  # licence notice for people, not for the model
        text = text[text.index("-->") + 3 :].lstrip("\n")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


# setup ------------------------------------------------------------------------


def setup_report() -> dict[str, Any]:
    binary = agybin.find_agy()
    version = agybin.agy_version(binary) if binary else None
    settings = agybin.read_settings()
    raw_gcp = settings.get("gcp")
    gcp: dict[str, Any] = raw_gcp if isinstance(raw_gcp, dict) else {}
    report: dict[str, Any] = {
        "agy_path": str(binary) if binary else None,
        "agy_version": ".".join(map(str, version)) if version else None,
        "version_ok": bool(version and version >= agybin.MIN_VERSION),
        "minimum_version": ".".join(map(str, agybin.MIN_VERSION)),
        "signed_in": agybin.token_present(),
        "gcp_project": gcp.get("project"),
        "gcp_location": gcp.get("location"),
        "permission_allow_rules": len((settings.get("permissions") or {}).get("allow") or []),
        "os_sandbox": sandbox.available(),
        "python": sys.version.split()[0],
        "default_model": agybin.DEFAULT_MODEL,
    }
    report.update(container.report())
    report["container_unavailable"] = container.unavailable_reason(report)
    report["isolation"] = "host" if report["container_unavailable"] else "container"
    problems = []
    if not binary:
        problems.append("agy is not installed or not on PATH. Install it, or set AGY_BIN to its path.")
    elif not report["version_ok"]:
        problems.append(f"agy {report['agy_version']} is older than {report['minimum_version']}; run `agy update`.")
    if binary and not report["signed_in"]:
        problems.append("agy has no saved sign-in. Run `agy` once in a terminal and sign in.")
    if binary and not report["gcp_project"]:
        problems.append("agy has no Google Cloud project set; choose one when you sign in.")
    report["ready"] = not problems
    report["problems"] = problems
    return report


def cmd_setup(args: argparse.Namespace) -> int:
    report = setup_report()
    if args.json:
        emit(report, True)
        return 0
    lines = [f"agy: {report['agy_path'] or 'not found'} (version {report['agy_version'] or '-'})"]
    lines.append(f"Signed in: {'yes' if report['signed_in'] else 'no'}")
    lines.append(f"Google Cloud project: {report['gcp_project'] or '-'} ({report['gcp_location'] or '-'})")
    lines.append(f"macOS write sandbox: {'available' if report['os_sandbox'] else 'not available'}")
    lines.append(f"Default model: {report['default_model']}")
    lines.append(f"Isolation: {report['isolation']}")
    reason = report["container_unavailable"]
    if reason:
        lines.append(f"Container mode is not available: {reason}.")
    else:
        lines.append(f"Container: {report['image']} with sign-in under {report['signin_home']}")
    lines += [f"Problem: {p}" for p in report["problems"]] or ["Ready."]
    print("\n".join(lines))
    return 0


# starting jobs ------------------------------------------------------------------


def start_job(
    args: argparse.Namespace, kind: str, message: str, *, writable: bool, extra: dict[str, Any] | None = None
) -> int:
    binary = agybin.find_agy()
    if not binary:
        raise UsageError("agy is not installed or not on PATH. Run /agy:setup.")
    root = gitctx.repo_root(os.getcwd())
    store = JobStore(root)
    isolation = choose_isolation(args)
    job_id = new_job_id(kind)
    job_dir = store.jobs / job_id
    worktree = store.dir / "worktrees" / job_id
    record: dict[str, Any] = {
        "id": job_id,
        "kind": kind,
        "status": "queued",
        "phase": "starting",
        "model": agybin.resolve_model(args.model),
        "mode": KIND_MODES[kind],
        "writable": writable,
        "worktree": str(worktree),
        "repo_root": str(root),
        "agy_bin": str(binary),
        "plugin_root": str(PLUGIN_ROOT),
        "session_id": os.environ.get(SESSION_ID_ENV),
        "isolation": isolation,
        "os_sandbox": False,
        "json_schema": str(PLUGIN_ROOT / "schemas" / "review-output.schema.json")
        if kind in ("review", "adversarial-review")
        else None,
        "next_round_kind": "initial",
        "read_dirs": [str(Path(d).expanduser().resolve()) for d in (args.read_dir or [])],
        "generation": 1,
    }
    if isolation == "container":
        record["baseline"] = gitctx.create_copy(root, worktree)
    else:
        record["baseline"] = gitctx.create_worktree(root, worktree, hook_command(job_dir))
        try:
            git_admin = Path(gitctx.git(worktree, "rev-parse", "--absolute-git-dir").strip())
        except Exception:
            gitctx.remove_worktree(root, worktree)
            raise
        record["os_sandbox"] = sandbox.available() and not args.no_os_sandbox
        record["writable_paths"] = [str(worktree), str(agybin.agy_home()), str(job_dir), str(git_admin)]
    try:
        tree = gitctx.baseline_tree(worktree, record["baseline"])
    except Exception:
        gitctx.remove_worktree(root, worktree)
        raise
    if args.stall_seconds:
        record["stall_seconds"] = args.stall_seconds
    record.update(extra or {})
    if record["read_dirs"]:
        message += "\n\nYou may also read files in: " + ", ".join(record["read_dirs"]) + "\n"
    store.create(record)
    write_json(job_dir / "baseline-tree.json", tree)
    if isolation == "host":
        write_json(job_dir / "policy.json", policy_for(record))
    record_session_job(record["session_id"], root, job_id)
    (job_dir / "next-message.md").write_text(message, encoding="utf-8")
    spawn_worker(store, job_id)
    return wait_or_report(store, job_id, args)


def choose_isolation(args: argparse.Namespace) -> str:
    """Container when it is available, otherwise the host-side policy and sandbox.

    The container check starts a container, so the answer is kept on args
    for the rest of the command."""
    chosen = getattr(args, "chosen_isolation", None)
    if chosen:
        return chosen
    wanted = getattr(args, "isolation", None) or "auto"
    if wanted == "host":
        chosen = "host"
    else:
        reason = container.unavailable_reason()
        if reason is not None and wanted == "container":
            raise UsageError(f"container isolation is not available: {reason}.")
        chosen = "host" if reason else "container"
    args.chosen_isolation = chosen
    return chosen


def hook_command(job_dir: Path) -> str:
    return " ".join(shlex.quote(p) for p in (sys.executable, str(SCRIPTS / "agy_hook.py"), str(job_dir)))


def policy_for(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": record["kind"],
        "writable": bool(record["writable"]) and not record.get("read_only_round"),
        "worktree": record["worktree"],
        "read_dirs": record.get("read_dirs") or [],
    }


def spawn_worker(store: JobStore, job_id: str) -> None:
    job_dir = store.job_dir(job_id)
    with open(job_dir / "worker.log", "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "_worker", job_id],
            cwd=str(store.root),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
    store.update(job_id, lambda r: r.update({"pid": proc.pid}))


def wait_or_report(store: JobStore, job_id: str, args: argparse.Namespace) -> int:
    if args.background:
        print(
            f"agy job {job_id} started in the background.\n"
            f"Check /agy:status {job_id}; read it with /agy:result {job_id}."
        )
        return 0
    deadline = time.monotonic() + (args.wait_seconds or DEFAULT_WAIT_SECONDS)
    while time.monotonic() < deadline:
        job = store.read(job_id) or {}
        if job.get("status") in TERMINAL_STATUSES:
            print(render.result_text(job, read_json(store.job_dir(job_id) / "result.json")))
            return 0 if job.get("status") != "failed" else 1
        time.sleep(2)
    job = store.read(job_id) or {}
    print(f"agy job {job_id} is still running.\n{render.job_detail(job)}")
    return 0


def cmd_review(args: argparse.Namespace, kind: str = "review") -> int:
    root = gitctx.repo_root(os.getcwd())
    in_container = choose_isolation(args) == "container"
    target = gitctx.resolve_target(root, args.base, args.scope)
    context = gitctx.collect_context(root, target)
    if not context["files"]:
        print(f"Nothing to review: no changes found for {target['label']}.")
        return 0
    focus = " ".join(getattr(args, "focus", []) or []).strip() or "(none)"
    name = "adversarial-review.md" if kind == "adversarial-review" else "review.md"
    message = template(
        name,
        TARGET_LABEL=target["label"],
        TARGET_SUMMARY=context["summary"],
        USER_FOCUS=focus,
        REVIEW_COLLECTION_GUIDANCE=CONTAINER_REVIEW_GUIDANCE if in_container else HOST_REVIEW_GUIDANCE,
        REVIEW_INPUT=context["content"],
    )
    return start_job(args, kind, message, writable=False, extra={"target": target})


def cmd_plan(args: argparse.Namespace) -> int:
    request = " ".join(args.request).strip()
    if not request:
        raise UsageError("Say what the plan is for, for example: /agy:plan add retry to the uploader")
    return start_job(args, "plan", template("plan.md", USER_REQUEST=request), writable=False)


def cmd_task(args: argparse.Namespace) -> int:
    request = " ".join(args.request).strip()
    if not request:
        raise UsageError("Say what agy should do.")
    store = JobStore(gitctx.repo_root(os.getcwd()))
    if args.resume_last:
        previous = resolve_own_job(
            store,
            args,
            lambda r: (
                r.get("kind") == "task" and r.get("status") in TERMINAL_STATUSES and bool(r.get("conversation_id"))
            ),
        )
        if not previous:
            raise UsageError("No earlier agy task from this session to resume in this repository.")
        if args.read_only and previous.get("isolation") == "container":
            request += READ_ONLY_NOTE
        return continue_job(store, previous, request, "follow-up", args, read_only=args.read_only)
    in_container = choose_isolation(args) == "container"
    message = template(
        "task.md", USER_REQUEST=request, SHELL_RULE=CONTAINER_SHELL_RULE if in_container else HOST_SHELL_RULE
    )
    if args.read_only and in_container:
        message += READ_ONLY_NOTE
    return start_job(args, "task", message, writable=not args.read_only)


def continue_job(
    store: JobStore,
    job: dict[str, Any],
    message: str,
    round_kind: str,
    args: argparse.Namespace,
    read_only: bool | None = None,
) -> int:
    """Queue another turn on a finished job. The status check and the change
    to "queued" happen under the job's lock, so two resumes cannot both run.

    read_only applies to this round's tool policy only; None keeps the
    previous round's setting, as an approval resume continues that round. The
    job's own "writable" flag is left alone, so the export after the round and
    /agy:apply still see the edits earlier rounds made."""
    job_id = job["id"]
    if not Path(job.get("worktree") or "").is_dir():
        raise UsageError(f"Job {job_id}'s working copy no longer exists; start a new job.")

    def mutate(record: dict[str, Any]) -> None:
        if record.get("status") in ACTIVE_STATUSES:
            raise UsageError(f"Job {job_id} is still running.")
        finished = parse_iso(record.get("completed_at"))
        record.update(
            {
                "status": "queued",
                "phase": f"resuming ({round_kind})",
                "next_round_kind": round_kind,
                "error": None,
                "queued_commands": [],
                "pending_approval_wait_s": round(time.time() - finished.timestamp(), 1) if finished else None,
            }
        )
        if getattr(args, "model", None):
            record["model"] = agybin.resolve_model(args.model)
        if read_only is not None:
            record["read_only_round"] = read_only
        record["generation"] = int(record.get("generation") or 1) + 1
        record.update({"pid": None, "agy_pid": None})

    record = store.update(job_id, mutate)
    job_dir = store.job_dir(job_id)
    if record.get("isolation") != "container":
        try:
            gitctx.install_hooks(Path(record["worktree"]), hook_command(job_dir))  # .agents holds only the hook again
        except Exception as exc:
            error = f"could not prepare the working copy: {exc}"
            store.update(job_id, lambda r: r.update({"status": "failed", "phase": "failed", "error": error}))
            raise
        write_json(job_dir / "policy.json", policy_for(record))
    (job_dir / "next-message.md").write_text(message, encoding="utf-8")
    spawn_worker(store, job_id)
    return wait_or_report(store, job_id, args)


def cmd_resume(args: argparse.Namespace) -> int:
    store = JobStore(gitctx.repo_root(os.getcwd()))
    job = store.resolve(args.job)
    if not job:
        raise UsageError("No matching agy job.")
    if not job.get("conversation_id"):
        raise UsageError(f"Job {job['id']} has no agy conversation to resume.")
    results = Path(args.results_file).read_text(encoding="utf-8") if args.results_file else ""
    note = " ".join(args.note or []).strip()
    if not results and not note:
        raise UsageError("Give the command output file, a note, or both.")
    body = results.strip()
    if note:
        body = f"{body}\n\nNote from Claude: {note}".strip()
    return continue_job(store, job, template("resume-with-results.md", COMMAND_RESULTS=body), "approval-resume", args)


# worker -------------------------------------------------------------------------


class _Stopped(Exception):
    pass


def _on_sigterm(signum: int, frame: Any) -> None:
    raise SystemExit(143)  # unwinds run_turn, whose finally stops agy


def cmd_worker(args: argparse.Namespace) -> int:
    signal.signal(signal.SIGTERM, _on_sigterm)
    store = JobStore(os.getcwd())

    def claim(record: dict[str, Any]) -> None:
        if record.get("status") != "queued":
            raise _Stopped(record.get("status"))
        record.update({"status": "running", "phase": "running", "pid": os.getpid()})

    try:
        store.update(args.job, claim)
    except _Stopped:
        return 0  # cancelled before the worker started
    try:
        JobRunner(store, args.job).run()
    except Exception as exc:  # noqa: BLE001 - record any failure on the job
        error = f"worker error: {exc}"

        def fail(record: dict[str, Any]) -> None:
            if record.get("status") != "cancelled":
                record.update(
                    {"status": "failed", "phase": "failed", "error": error, "pid": None, "completed_at": now_iso()}
                )

        store.update(args.job, fail)
        raise
    return 0


# status, result, cancel, apply, cleanup -------------------------------------------


def _session_filter(args: argparse.Namespace):
    session = os.environ.get(SESSION_ID_ENV)
    if getattr(args, "all", False) or not session:
        return None
    return lambda r: r.get("session_id") in (session, None)


def resolve_own_job(store: JobStore, args: argparse.Namespace, predicate) -> dict[str, Any] | None:
    """Resolve a job id, or with none given the newest match from this Claude session.

    Job state is shared per repository, so without the session filter a bare
    /agy:cancel or /agy:apply in one session would act on another session's
    job, one that /agy:status in this session does not even show.
    """
    reference = getattr(args, "job", None)
    if reference:
        return store.resolve(reference, predicate)
    own = _session_filter(args)
    if own is None:
        return store.resolve(None, predicate)
    return store.resolve(None, lambda r: own(r) and (predicate is None or predicate(r)))


def cmd_status(args: argparse.Namespace) -> int:
    store = JobStore(os.getcwd())
    if args.job:
        job = store.resolve(args.job)
        if not job:
            raise UsageError(f"No agy job matches {args.job}.")
        print(render.job_detail(job))
        return 0
    predicate = _session_filter(args)
    every = store.list_jobs()
    jobs = [j for j in every if predicate is None or predicate(j)][:10]
    print(render.status_table(jobs))
    hidden = len(every) - len([j for j in every if predicate is None or predicate(j)])
    if hidden:
        print(f"\n{hidden} job(s) from other Claude sessions are hidden; /agy:status --all shows them.")
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    store = JobStore(os.getcwd())
    job = resolve_own_job(store, args, None if args.job else (lambda r: r.get("status") in TERMINAL_STATUSES))
    if not job:
        raise UsageError("No finished agy job from this session found.")
    print(render.result_text(job, read_json(store.job_dir(job["id"]) / "result.json")))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    store = JobStore(os.getcwd())
    job = resolve_own_job(store, args, lambda r: r.get("status") in ACTIVE_STATUSES)
    if not job:
        raise UsageError("No running agy job from this session to cancel.")
    cancel_job(store, job, "cancelled")
    print(f"Cancelled {job['id']}.")
    return 0


def cancel_job(store: JobStore, job: dict[str, Any], phase: str, grace: float = 10) -> None:
    """Stop a job. It is marked "stopping" (still active, so nothing can resume
    it) while its processes are stopped, then "cancelled". Process ids are
    cleared only if no newer worker generation has started meanwhile."""
    stop_jobs([(store, job)], phase, grace)


def stop_jobs(jobs: list[tuple[JobStore, dict[str, Any]]], phase: str, grace: float) -> None:
    """Stop several jobs at once, so the total wait is one grace period."""
    claimed = []
    for store, job in jobs:

        def begin(record: dict[str, Any]) -> None:
            if record.get("status") in ACTIVE_STATUSES:
                record.update({"status": "stopping", "phase": phase})

        record = store.update(job["id"], begin)
        if record.get("status") == "stopping":
            claimed.append((store, record))
    terminate_job_processes([record for _, record in claimed], grace=grace)
    for store, record in claimed:
        generation = record.get("generation")

        def finish(current: dict[str, Any], generation: Any = generation) -> None:
            if current.get("generation") == generation:
                current.update(
                    {"status": "cancelled", "phase": phase, "completed_at": now_iso(), "pid": None, "agy_pid": None}
                )

        store.update(record["id"], finish)
        if record.get("kind") != "task" and record.get("worktree") and Path(record["worktree"]).exists():
            gitctx.remove_worktree(record["repo_root"], Path(record["worktree"]))


def cmd_apply(args: argparse.Namespace) -> int:
    store = JobStore(os.getcwd())
    job = resolve_own_job(store, args, lambda r: bool(r.get("writable")) and r.get("status") in TERMINAL_STATUSES)
    if not job:
        raise UsageError("No finished agy task with changes from this session found.")
    job_dir = store.job_dir(job["id"])
    patch_file = job_dir / "changes.patch"
    patch = patch_file.read_bytes() if patch_file.exists() else b""
    if not patch.strip():
        print(f"Job {job['id']} changed no files.")
        return 0
    stored = read_json(job_dir / "result.json", {}) or {}
    root = Path(job["repo_root"])
    tree = read_json(job_dir / "baseline-tree.json")
    if not isinstance(tree, dict):
        raise UsageError(
            f"Job {job['id']} has no baseline snapshot (it predates this version of the plugin), so the drift "
            f"check cannot run. Review the patch at {patch_file} and apply it with git apply, or start a new job."
        )
    moved = gitctx.baseline_mismatches(root, tree, stored.get("changed_paths") or [])
    if moved and not args.force:
        listed = "\n".join(f"- {path}" for path in moved)
        raise UsageError(
            "These files changed in your checkout since the job started, so applying could mix "
            f"agy's edits with different code:\n{listed}\nReview the patch at {patch_file}, "
            "or rerun with --force to apply it anyway."
        )
    gitctx.apply_patch(root, patch, check_only=True)
    if args.check:
        print(f"The changes from {job['id']} apply cleanly.")
        return 0
    gitctx.apply_patch(root, patch)
    store.update(job["id"], lambda r: r.update({"applied_at": now_iso()}))
    print(f"Applied the changes from {job['id']} to {root}. Review them with git diff.")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    store = JobStore(os.getcwd())
    removed = []
    for job in store.list_jobs():
        if args.job and not job["id"].startswith(args.job):
            continue
        if job.get("status") in ACTIVE_STATUSES:
            continue
        worktree = Path(job.get("worktree") or "")
        if worktree.exists():
            gitctx.remove_worktree(job["repo_root"], worktree)
            removed.append(job["id"])
    print(f"Removed {len(removed)} job working cop{'y' if len(removed) == 1 else 'ies'}.")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Write a container job's patch and file list. Runs inside the container."""
    out = Path(args.out_dir)
    changes = gitctx.export_changes(Path(args.worktree), args.baseline, gitctx.CONTAINER_EXPORT_EXCLUDES)
    (out / "changes.patch").write_bytes(changes["patch"])
    write_json(out / "changes.json", {"name_status": changes["name_status"], "paths": changes["paths"]})
    return 0


# Claude Code session hooks -----------------------------------------------------------


def cmd_session_start(args: argparse.Namespace) -> int:
    """Give the session's commands the same state root the hooks use.

    Claude Code exports CLAUDE_PLUGIN_DATA to hook processes but not to the
    Bash tool, so without this the session-end hook would look for the
    session's jobs in a directory the commands never wrote to.
    """
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    session = payload.get("session_id")
    if env_file and session:
        with open(env_file, "a", encoding="utf-8") as handle:
            handle.write(f"export {SESSION_ID_ENV}={shlex.quote(session)}\n")
            handle.write(f"export {STATE_DIR_ENV}={shlex.quote(str(state_root()))}\n")
    return 0


def cmd_session_end(args: argparse.Namespace) -> int:
    """Stop every active job the ending Claude session started, in any repository."""
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    session = payload.get("session_id")
    if not session:
        return 0
    try:
        entries = read_json(session_index(session), []) or []
        targets = []
        for entry in entries:
            store = JobStore(entry["repo_root"])
            job = store.read(entry["job_id"])
            if job and job.get("session_id") == session and job.get("status") in ACTIVE_STATUSES:
                targets.append((store, job))
        stop_jobs(targets, "session ended", grace=3)  # the hook allows ten seconds in total
        session_index(session).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - never block session shutdown
        return 0
    return 0


# argument parsing -----------------------------------------------------------------------


def add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", help="high, medium, low, or a full agy model name")
    parser.add_argument("--background", action="store_true", help="return at once with the job id")
    parser.add_argument("--wait", action="store_true", help="wait for the result (the default)")
    parser.add_argument("--wait-seconds", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--stall-seconds", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--no-os-sandbox", action="store_true", help="host mode: do not wrap agy in sandbox-exec")
    parser.add_argument(
        "--isolation",
        choices=ISOLATION_CHOICES,
        default="auto",
        help="container when available (the default), or host for the hook policy and sandbox-exec",
    )
    parser.add_argument(
        "--read-dir", action="append", help="another directory agy may read (repeatable); reads are otherwise confined"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agy_companion.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup")
    setup.add_argument("--json", action="store_true")
    setup.set_defaults(func=cmd_setup)

    for name in ("review", "adversarial-review"):
        review = sub.add_parser(name)
        add_run_options(review)
        review.add_argument("--base")
        review.add_argument("--scope", default="auto")
        if name == "adversarial-review":
            review.add_argument("focus", nargs="*")
        review.set_defaults(func=lambda a, kind=name: cmd_review(a, kind))

    plan = sub.add_parser("plan")
    add_run_options(plan)
    plan.add_argument("request", nargs="*")
    plan.set_defaults(func=cmd_plan)

    task = sub.add_parser("task")
    add_run_options(task)
    task.add_argument("--read-only", action="store_true")
    task.add_argument("--resume-last", action="store_true")
    task.add_argument("request", nargs="*")
    task.set_defaults(func=cmd_task)

    resume = sub.add_parser("resume")
    add_run_options(resume)
    resume.add_argument("job")
    resume.add_argument("results_file", nargs="?")
    resume.add_argument("--note", nargs="*")
    resume.set_defaults(func=cmd_resume)

    for name, func in (("status", cmd_status), ("result", cmd_result), ("cancel", cmd_cancel)):
        p = sub.add_parser(name)
        p.add_argument("job", nargs="?")
        if name == "status":
            p.add_argument("--all", action="store_true")
        p.set_defaults(func=func)

    apply = sub.add_parser("apply")
    apply.add_argument("job", nargs="?")
    apply.add_argument("--check", action="store_true")
    apply.add_argument("--force", action="store_true")
    apply.set_defaults(func=cmd_apply)

    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("job", nargs="?")
    cleanup.set_defaults(func=cmd_cleanup)

    worker = sub.add_parser("_worker")
    worker.add_argument("job")
    worker.set_defaults(func=cmd_worker)

    export = sub.add_parser("_export")
    export.add_argument("worktree")
    export.add_argument("baseline")
    export.add_argument("out_dir")
    export.set_defaults(func=cmd_export)

    sub.add_parser("session-start").set_defaults(func=cmd_session_start)
    sub.add_parser("session-end").set_defaults(func=cmd_session_end)
    return parser


def split_arguments(argv: list[str]) -> list[str]:
    """Expand `--raw <string>` into the words of the string.

    Slash commands pass the user's whole argument line as one string after
    --raw, and it is split like a shell would split it. A stray apostrophe
    falls back to splitting on whitespace. Every other argument is kept as it
    is, so a quoted path with spaces stays one argument.
    """
    out: list[str] = []
    items = iter(argv)
    for arg in items:
        if arg != "--raw":
            out.append(arg)
            continue
        raw = next(items, "")
        try:
            out += shlex.split(raw)
        except ValueError:
            out += raw.split()
    return out


def main(argv: list[str] | None = None) -> int:
    argv = split_arguments(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (UsageError, gitctx.GitError) as exc:
        print(f"agy: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
