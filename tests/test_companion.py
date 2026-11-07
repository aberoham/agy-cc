"""End-to-end tests of the companion against the fake agy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from agylib import agybin
from agylib.runner import _tail
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


def test_auth_failure_points_to_the_host_sign_in(companion):
    proc = companion.run("review", NO_SANDBOX, scenario="auth")
    assert proc.returncode == 11
    assert "Sign in with `agy` in a terminal" in proc.stdout
    assert companion.job()["status"] == "failed"


def test_invalid_licence_asks_for_a_new_sign_in(companion):
    proc = companion.run("review", NO_SANDBOX, scenario="licence")
    assert proc.returncode == 15
    job = companion.job()
    assert job["status"] == "failed"
    assert "GEMINI_ENTERPRISE_LICENSE_NOT_VALID" in job["error"]
    assert "Run `agy` in a terminal" in job["error"]


def test_invalid_licence_in_a_container_points_to_the_host_sign_in(contained):
    contained.run("review", scenario="licence")
    job = contained.job()
    assert "GEMINI_ENTERPRISE_LICENSE_NOT_VALID" in job["error"]
    assert "Run `agy` in a terminal" in job["error"]


def test_licence_error_from_an_earlier_round_is_not_reused(companion):
    companion.run("task", NO_SANDBOX, "fix it", scenario="licence")
    companion.run("task", NO_SANDBOX, "--resume-last", "try again", scenario="auth")
    job = companion.job()
    assert "LICENSE_NOT_VALID" not in job["error"]
    assert "not signed in" in job["error"]


def test_sign_in_error_from_an_earlier_round_is_not_reused(companion):
    companion.run("task", NO_SANDBOX, "fix it", scenario="auth")
    companion.run("task", NO_SANDBOX, "--resume-last", "try again", scenario="review-error-with-response")
    error = companion.job()["error"]
    assert "not signed in" not in error
    assert "stream idle timeout" in error


def test_setup_reports_fake_agy(companion):
    proc = companion.run("setup", "--json")
    report = json.loads(proc.stdout)
    assert report["agy_version"] == "1.4.7"
    assert report["signed_in"] is True
    assert report["token_file"] is False


def test_setup_counts_a_sign_in_kept_outside_the_token_file(companion):
    proc = companion.run("setup")
    assert "Signed in: yes (not in the token file" in proc.stdout
    assert "not signed in" not in proc.stdout


def test_setup_reports_signed_out_when_agy_asks_for_a_sign_in(companion):
    proc = companion.run("setup", "--json", scenario="auth")
    report = json.loads(proc.stdout)
    assert report["signed_in"] is False
    assert report["ready"] is False
    assert any("not signed in" in p for p in report["problems"])


def test_sign_in_check_is_unknown_when_agy_lists_nothing(tmp_path):
    silent = tmp_path / "agy"
    silent.write_text("#!/bin/sh\necho 'Fetching available models...'\n")
    silent.chmod(0o755)
    signed_in, detail = agybin.sign_in_check(silent)
    assert signed_in is None
    assert "printed no models" in detail


def test_sign_in_check_reports_another_error_as_unknown(tmp_path):
    failing = tmp_path / "agy"
    failing.write_text("#!/bin/sh\necho 'Error: PERMISSION_DENIED (code 403)'\n")
    failing.chmod(0o755)
    signed_in, detail = agybin.sign_in_check(failing)
    assert signed_in is None
    assert detail == "Error: PERMISSION_DENIED (code 403)"


@pytest.mark.parametrize(
    ("script", "detail"),
    [
        ("echo 'gemini-client: request failed'", "gemini-client: request failed"),
        (
            "printf 'gemini-4-argon-low\\tGemini 4 Argon (Low)\\n'; echo 'PERMISSION_DENIED' >&2; exit 1",
            "PERMISSION_DENIED",
        ),
        ("printf 'gemini-4-argon-low\\tGemini 4 Argon (Low)\\n'; exit 1", "`agy models` printed no models (exit 1)"),
        ("echo '403 PERMISSION_DENIED: licence revoked'", "403 PERMISSION_DENIED: licence revoked"),
    ],
)
def test_sign_in_check_never_says_yes_on_a_failure(tmp_path, script, detail):
    agy = tmp_path / "agy"
    agy.write_text(f"#!/bin/sh\n{script}\n")
    agy.chmod(0o755)
    assert agybin.sign_in_check(agy) == (None, detail)


def test_sign_in_check_survives_output_that_is_not_utf8(tmp_path):
    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\nprintf 'Error: \\377\\376\\n'\n")
    agy.chmod(0o755)
    signed_in, detail = agybin.sign_in_check(agy)
    assert signed_in is None
    assert detail.startswith("Error:")


def test_sign_in_check_reports_an_agy_that_cannot_run(tmp_path):
    signed_in, detail = agybin.sign_in_check(tmp_path / "missing")
    assert signed_in is None
    assert "could not run" in detail


def test_setup_shows_an_unknown_sign_in_without_telling_the_user_to_sign_in(companion, tmp_path):
    agy = tmp_path / "agy"
    agy.write_text(
        "#!/bin/sh\n"
        "[ \"$1\" = models ] && { echo 'Error: quota service unavailable.'; exit 0; }\n"
        f'exec {sys.executable} {FAKE_AGY} "$@"\n'
    )
    agy.chmod(0o755)
    proc = companion.run("setup", AGY_BIN=str(agy))
    assert "Signed in: unknown" in proc.stdout
    assert "Could not confirm agy's sign-in: Error: quota service unavailable." in proc.stdout
    assert "unavailable.." not in proc.stdout
    assert "not signed in" not in proc.stdout


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


def test_stderr_tail_refuses_links_and_pipes(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("not agy's output\n")
    link = tmp_path / "linked.txt"
    link.symlink_to(secret)
    pipe = tmp_path / "pipe.txt"
    os.mkfifo(pipe)
    assert _tail(link) == ""
    assert _tail(pipe) == ""  # returns at once rather than waiting for a writer


def test_stderr_tail_reads_only_after_start(tmp_path):
    path = tmp_path / "agy.stderr.txt"
    path.write_text("earlier round\nthis round\n")
    assert _tail(path, start=len("earlier round\n")) == "this round\n"
    assert _tail(path, limit=6) == "round\n"
