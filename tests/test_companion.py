"""End-to-end tests of the companion against the fake agy."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FAKE_AGY, git

NO_SANDBOX = "--no-os-sandbox"


def test_review_runs_in_worktree_and_parses_structured_output(companion, repo):
    before = git(repo, "status", "--porcelain")
    proc = companion.run("review", "--model", "low", NO_SANDBOX, scenario="review")
    assert "Verdict: needs-attention" in proc.stdout
    assert "[high] Off by one" in proc.stdout
    job = companion.job()
    assert job["status"] == "completed"
    assert job["writable"] is False
    assert Path(job["worktree"]) != repo
    assert git(repo, "status", "--porcelain") == before
    (call,) = companion.agy_calls()
    assert call["argv"][-1] == "--print="
    assert "-p" not in call["argv"]
    assert "--dangerously-skip-permissions" not in call["argv"]
    assert call["argv"][call["argv"].index("--model") + 1] == "gemini-4-argon-low"
    assert "--json-schema" in call["argv"]
    assert "--mode" not in call["argv"]
    message = json.loads(call["stdin"])
    assert message["event"] == "user"
    assert "uncommitted changes" in message["message"]["content"]
    assert "sum(v[1:])" in message["message"]["content"]


def test_review_with_nothing_to_review(companion, repo):
    git(repo, "checkout", "--", "stats.py")
    (repo / "notes.txt").unlink()
    proc = companion.run("review", NO_SANDBOX, scenario="review")
    assert "Nothing to review" in proc.stdout
    assert companion.agy_calls() == []


def test_arguments_arrive_as_one_string(companion):
    proc = companion.run("review", "--raw", "--model low --no-os-sandbox", scenario="review")
    assert "Verdict" in proc.stdout


def test_plan_reads_the_plan_from_the_brain_artifact(companion):
    proc = companion.run("plan", NO_SANDBOX, "add a median function", scenario="plan")
    assert "1. Do the thing." in proc.stdout
    (call,) = companion.agy_calls()
    assert call["argv"][call["argv"].index("--mode") + 1] == "plan"
    assert "add a median function" in json.loads(call["stdin"])["message"]["content"]


def test_task_edits_worktree_only_queues_commands_and_resumes(companion, repo, tmp_path):
    proc = companion.run("task", NO_SANDBOX, "fix the bug", scenario="task")
    assert "needs-approval" in proc.stdout
    assert "`make test`" in proc.stdout
    job = companion.job()
    worktree = Path(job["worktree"])
    assert (worktree / "stats.py").read_text() == "fixed\n"
    assert "sum(v[1:])" in (repo / "stats.py").read_text()
    assert not (worktree.parent / "escaped.txt").exists()
    assert job["queued_commands"] == ["make test"]

    results = tmp_path / "results.txt"
    results.write_text("$ make test\nok\nexit status: 0\n")
    proc = companion.run("resume", job["id"], str(results), NO_SANDBOX, scenario="task")
    assert "Tests pass" in proc.stdout
    job = companion.job()
    assert job["status"] == "completed"
    assert [r["kind"] for r in job["rounds"]] == ["initial", "approval-resume"]
    assert job["rounds"][1]["approval_wait_s"] is not None
    second = companion.agy_calls()[1]
    assert second["argv"][second["argv"].index("--conversation") + 1] == job["conversation_id"]
    assert "exit status: 0" in json.loads(second["stdin"])["message"]["content"]

    proc = companion.run("apply", job["id"])
    assert "Applied" in proc.stdout
    assert (repo / "stats.py").read_text() == "fixed\n"


def test_agy_permission_denials_are_queued(companion):
    proc = companion.run("task", NO_SANDBOX, "run the tests", scenario="permission-denied")
    assert "`npm test`" in proc.stdout
    assert companion.job()["status"] == "needs-approval"


@pytest.mark.parametrize("scenario", ["stall", "stall-log"])
def test_stall_resumes_same_conversation_on_next_model(companion, scenario):
    proc = companion.run("task", NO_SANDBOX, "--model", "high", "--stall-seconds", "3", "do it", scenario=scenario)
    assert "reply from gemini-4-argon-medium" in proc.stdout
    job = companion.job()
    assert job["status"] == "completed"
    (stall,) = job["rounds"][0]["stalls"]
    assert stall["model"] == "gemini-4-argon-high"
    assert stall["resumed_on"] == "gemini-4-argon-medium"
    if scenario == "stall-log":
        assert "stream idle timeout" in stall["reason"]
    first, second = companion.agy_calls()
    conversation = (
        first["argv"][first["argv"].index("--conversation") + 1] if "--conversation" in first["argv"] else None
    )
    assert conversation is None
    assert second["argv"][second["argv"].index("--model") + 1] == "gemini-4-argon-medium"
    assert second["argv"][second["argv"].index("--conversation") + 1] == job["conversation_id"]


def test_background_then_cancel(companion):
    proc = companion.run(
        "task", NO_SANDBOX, "--background", "--model", "high", "--stall-seconds", "600", "do it", scenario="stall"
    )
    assert "started in the background" in proc.stdout
    companion.wait_for(lambda j: j["status"] == "running" and j.get("conversation_id"))
    status = companion.run("status")
    assert "running" in status.stdout
    proc = companion.run("cancel")
    assert "Cancelled" in proc.stdout
    assert companion.job()["status"] == "cancelled"


def test_auth_failure_points_to_setup(companion):
    proc = companion.run("review", NO_SANDBOX, scenario="auth")
    assert proc.returncode == 1
    assert "/agy:setup" in proc.stdout
    assert companion.job()["status"] == "failed"


def test_setup_reports_fake_agy(companion):
    proc = companion.run("setup", "--json")
    report = json.loads(proc.stdout)
    assert report["agy_version"] == "1.4.7"
    assert report["signed_in"] is False
    assert report["ready"] is False


def test_fake_agy_rejects_p_followed_by_a_flag():
    proc = subprocess.run(
        [sys.executable, str(FAKE_AGY), "-p", "--model", "x"], input="", capture_output=True, text=True
    )
    assert proc.returncode == 2
    assert 'took "--model" as its prompt' in proc.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
def test_review_under_sandbox_exec(companion):
    proc = companion.run("review", scenario="review")
    assert "Verdict" in proc.stdout
    assert companion.job()["os_sandbox"] is True


def test_relative_state_dir_is_made_absolute(companion, repo):
    proc = companion.run("review", NO_SANDBOX, scenario="review", AGY_COMPANION_STATE_DIR="../relative-state")
    assert "Verdict" in proc.stdout
    jobs = list((repo.parent / "relative-state").glob("*/jobs/*/job.json"))
    assert len(jobs) == 1
    assert Path(json.loads(jobs[0].read_text())["worktree"]).is_absolute()
