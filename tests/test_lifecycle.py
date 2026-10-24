"""Job lifecycle, isolation and recovery, against the fake agy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agylib import gitctx
from conftest import git

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "real"
NO_SANDBOX = "--no-os-sandbox"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie has exited; ps reports its state as Z.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_real_permission_denial_replay_is_queued(companion):
    fixture = FIXTURES / "headless-permission-auto-deny.ndjson"
    proc = companion.run("task", NO_SANDBOX, "touch a file", scenario=f"replay:{fixture}")
    job = companion.job()
    assert job["status"] == "needs-approval"
    assert job["queued_commands"] == ["touch /tmp/agy-probe/perm-touch"]
    assert "touch /tmp/agy-probe/perm-touch" in proc.stdout


def test_real_plan_replay_reads_conversation_and_completes(companion):
    fixture = FIXTURES / "pong-plan.ndjson"
    proc = companion.run("plan", NO_SANDBOX, "say pong", scenario=f"replay:{fixture}")
    job = companion.job()
    assert job["status"] == "completed"
    assert job["conversation_id"] == "3dd7d243-ef1b-40aa-83bc-5ef1fce93335"
    assert "PONG" in proc.stdout


def test_read_only_job_worktree_is_removed_when_done(companion, repo):
    companion.run("review", NO_SANDBOX, scenario="review")
    job = companion.job()
    assert not Path(job["worktree"]).exists()
    assert job["worktree"] not in git(repo, "worktree", "list")


def test_worktree_reproduces_staged_unstaged_deleted_renamed_and_symlinks(repo, tmp_path):
    (repo / "keep.txt").write_text("keep\n")
    (repo / "gone.txt").write_text("gone\n")
    (repo / "old name.txt").write_text("rename me\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "more")
    (repo / "keep.txt").write_text("staged\n")
    git(repo, "add", "keep.txt")
    (repo / "keep.txt").write_text("staged then unstaged\n")
    git(repo, "rm", "-q", "gone.txt")
    git(repo, "mv", "old name.txt", "new name.txt")
    (repo / "link").symlink_to("keep.txt")
    dest = tmp_path / "wt"
    gitctx.create_worktree(repo, dest)
    assert (dest / "keep.txt").read_text() == "staged then unstaged\n"
    assert not (dest / "gone.txt").exists()
    assert not (dest / "old name.txt").exists()
    assert (dest / "new name.txt").read_text() == "rename me\n"
    assert (dest / "link").is_symlink() and os.readlink(dest / "link") == "keep.txt"
    assert (dest / "notes.txt").read_text() == "untracked\n"
    gitctx.remove_worktree(repo, dest)


def test_symlinked_agents_directory_refuses_to_start(companion, repo, tmp_path):
    outside = tmp_path / "outside-agents"
    outside.mkdir()
    (repo / ".agents").symlink_to(outside)
    git(repo, "add", ".agents")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "link")
    proc = companion.run("review", NO_SANDBOX, scenario="review", check=False)
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    assert not (outside / "hooks.json").exists()
    assert "agy-companion" not in git(repo, "worktree", "list")


def test_resume_last_read_only_blocks_writes(companion, repo):
    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    job = companion.job()
    worktree = Path(job["worktree"])
    (worktree / "stats.py").write_text("reset\n")
    companion.run("task", NO_SANDBOX, "--resume-last", "--read-only", "look again", scenario="task")
    job = companion.job()
    assert job["writable"] is True and job["read_only_round"] is True
    assert (worktree / "stats.py").read_text() == "reset\n"


def test_second_resume_is_refused_while_first_runs(companion, tmp_path):
    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    job = companion.job()
    results = tmp_path / "results.txt"
    results.write_text("output is here\n")
    first = companion.run(
        "resume", job["id"], str(results), NO_SANDBOX, "--background", "--model", "high", scenario="stall"
    )
    assert "started in the background" in first.stdout
    second = companion.run("resume", job["id"], str(results), NO_SANDBOX, scenario="stall", check=False)
    assert second.returncode == 2
    assert "still running" in second.stderr
    companion.run("cancel", job["id"])


def test_resume_accepts_a_results_path_with_spaces(companion, tmp_path):
    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    job = companion.job()
    results = tmp_path / "results with spaces.txt"
    results.write_text("$ make test\noutput is fine\n")
    proc = companion.run("resume", job["id"], str(results), NO_SANDBOX, scenario="task")
    assert "Tests pass" in proc.stdout


def test_apply_refuses_when_the_checkout_moved_on(companion, repo):
    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    (repo / "stats.py").write_text("someone else changed this\n")
    proc = companion.run("apply", check=False)
    assert proc.returncode == 2
    assert "stats.py" in proc.stderr
    assert (repo / "stats.py").read_text() == "someone else changed this\n"
    proc = companion.run("apply", "--force", check=False)
    assert proc.returncode == 2  # the patch itself no longer applies


def test_stall_retries_are_capped(companion):
    proc = companion.run(
        "task",
        NO_SANDBOX,
        "--model",
        "high",
        "--stall-seconds",
        "2",
        "do it",
        scenario="stall",
        FAKE_AGY_STALL_MODEL="gemini-4-argon-high,gemini-4-argon-medium,gemini-4-argon-low",
    )
    job = companion.job()
    assert job["status"] == "failed"
    assert len(job["rounds"][0]["stalls"]) == 2
    assert "no agy event" in proc.stdout


def test_cancel_stops_worker_and_agy(companion, tmp_path):
    pid_file = tmp_path / "agy.pids"
    companion.run(
        "task",
        NO_SANDBOX,
        "--background",
        "--model",
        "high",
        "--stall-seconds",
        "600",
        "do it",
        scenario="stall",
        FAKE_AGY_PIDFILE=str(pid_file),
    )
    job = companion.wait_for(lambda j: j["status"] == "running" and j.get("agy_pid"))
    worker_pid = job["pid"]
    agy_pid = int(pid_file.read_text().split()[0])
    companion.run("cancel")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and (_alive(worker_pid) or _alive(agy_pid)):
        time.sleep(0.2)
    assert not _alive(agy_pid)
    assert not _alive(worker_pid)
    assert companion.job()["status"] == "cancelled"


def test_session_end_cancels_that_sessions_jobs(companion, repo, tmp_path):
    pid_file = tmp_path / "agy.pids"
    companion.run(
        "task",
        NO_SANDBOX,
        "--background",
        "--model",
        "high",
        "--stall-seconds",
        "600",
        "do it",
        scenario="stall",
        FAKE_AGY_PIDFILE=str(pid_file),
        AGY_COMPANION_SESSION_ID="session-1",
    )
    companion.wait_for(lambda j: j["status"] == "running" and j.get("agy_pid"))
    from conftest import COMPANION

    subprocess.run(
        [sys.executable, str(COMPANION), "session-end"],
        input=json.dumps({"session_id": "session-1", "cwd": str(repo)}),
        env=companion.env,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert companion.job()["status"] == "cancelled"
    agy_pid = int(pid_file.read_text().split()[0])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _alive(agy_pid):
        time.sleep(0.2)
    assert not _alive(agy_pid)


def test_prune_removes_worktrees(companion, repo, monkeypatch):
    from agylib import state

    monkeypatch.setattr(state, "MAX_JOBS", 0)
    monkeypatch.setenv("AGY_COMPANION_STATE_DIR", companion.env["AGY_COMPANION_STATE_DIR"])
    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    job = companion.job()
    assert Path(job["worktree"]).exists()
    state.JobStore(repo).prune()
    assert not Path(job["worktree"]).exists()
    assert job["worktree"] not in git(repo, "worktree", "list")


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
def test_sandbox_blocks_writes_to_the_checkout_that_bypass_the_hook(companion, repo):
    target = repo / "escaped.txt"
    proc = companion.run("task", "do it", scenario="escape", FAKE_AGY_ESCAPE_PATH=str(target))
    assert companion.job()["os_sandbox"] is True
    assert not target.exists()
    assert "blocked" in proc.stdout


def test_without_sandbox_the_same_write_succeeds(companion, repo):
    target = repo / "escaped.txt"
    companion.run("task", NO_SANDBOX, "do it", scenario="escape", FAKE_AGY_ESCAPE_PATH=str(target))
    assert target.exists()  # shows the sandbox test above is meaningful


def test_worktree_setup_runs_no_repository_hooks_or_textconv(repo, tmp_path):
    marker = repo / "hook-ran"
    hooks = repo / ".git" / "hooks"
    for name in ("post-checkout", "post-commit"):
        script = hooks / name
        script.write_text(f"#!/bin/sh\ntouch {marker}\n")
        script.chmod(0o755)
    (repo / ".gitattributes").write_text("*.txt diff=same\n")
    git(repo, "config", "diff.same.textconv", "sh -c 'echo same'")
    (repo / "notes.txt").unlink()
    (repo / "data.txt").write_text("one\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "attrs")
    (repo / "data.txt").write_text("two\n")
    dest = tmp_path / "wt"
    gitctx.create_worktree(repo, dest)
    assert not marker.exists()
    assert (dest / "data.txt").read_text() == "two\n"
    gitctx.remove_worktree(repo, dest)


def test_cancel_marks_stopping_so_resume_is_refused(companion, tmp_path):
    from agylib.state import JobStore

    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    job = companion.job()
    store = JobStore(companion.repo)
    import os as _os

    _os.environ["AGY_COMPANION_STATE_DIR"] = companion.env["AGY_COMPANION_STATE_DIR"]
    try:
        store = JobStore(companion.repo)
        store.update(job["id"], lambda r: r.update({"status": "stopping"}))
        results = tmp_path / "r.txt"
        results.write_text("x\n")
        proc = companion.run("resume", job["id"], str(results), NO_SANDBOX, scenario="task", check=False)
        assert proc.returncode == 2
        assert "still running" in proc.stderr
    finally:
        _os.environ.pop("AGY_COMPANION_STATE_DIR", None)


def test_session_end_stops_jobs_in_every_repository(companion, repo, tmp_path):
    from conftest import COMPANION, Companion

    other_repo = tmp_path / "other"
    other_repo.mkdir()
    git(other_repo, "init", "-q", "-b", "main")
    (other_repo / "a.txt").write_text("a\n")
    git(other_repo, "add", ".")
    git(other_repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    other = Companion(other_repo, tmp_path / "second")
    other.env = dict(companion.env)
    pids = tmp_path / "pids"
    for c in (companion, other):
        c.run(
            "task",
            NO_SANDBOX,
            "--background",
            "--model",
            "high",
            "--stall-seconds",
            "600",
            "do it",
            scenario="stall",
            FAKE_AGY_PIDFILE=str(pids),
            AGY_COMPANION_SESSION_ID="session-x",
        )
        c.wait_for(lambda j: j["status"] == "running" and j.get("agy_pid"))
    started = time.monotonic()
    subprocess.run(
        [sys.executable, str(COMPANION), "session-end"],
        input=json.dumps({"session_id": "session-x", "cwd": str(repo)}),
        env=companion.env,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert time.monotonic() - started < 10
    statuses = sorted(j["status"] for j in companion.jobs())
    assert statuses == ["cancelled", "cancelled"]
    for pid in map(int, pids.read_text().split()):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _alive(pid):
            time.sleep(0.2)
        assert not _alive(pid)


def test_apply_refuses_a_mode_change_since_the_job(companion, repo):
    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    job = companion.job()
    (repo / "stats.py").chmod(0o755)
    proc = companion.run("apply", check=False)
    assert proc.returncode == 2 and "stats.py" in proc.stderr
    assert job["status"] == "needs-approval"


def test_runner_enforces_the_deadline_after_stdout_closes(tmp_path):
    from agylib.runner import run_turn

    script = tmp_path / "closes_stdout.py"
    script.write_text("import os, sys, time\nsys.stdin.read()\nos.close(1)\ntime.sleep(60)\n")
    started = time.monotonic()
    outcome = run_turn(
        [sys.executable, str(script)],
        cwd=tmp_path,
        message="hi",
        events_path=tmp_path / "e",
        log_path=tmp_path / "l",
        stderr_path=tmp_path / "s",
        stall_seconds=1,
        deadline=time.monotonic() + 30,
        on_event=lambda e: None,
    )
    assert time.monotonic() - started < 15
    assert outcome.kind == "exited"


def test_stalled_attempt_keeps_its_step_usage(companion):
    companion.run(
        "task",
        NO_SANDBOX,
        "--model",
        "high",
        "--stall-seconds",
        "3",
        "do it",
        scenario="stall-usage",
    )
    usage = companion.job()["rounds"][0]["usage"]
    assert usage["total_tokens"] == 50 + 120
