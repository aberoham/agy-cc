"""Failure classes and the companion's exit codes, against the fake agy."""

from __future__ import annotations

import time

import pytest
from agylib import failure

NO_SANDBOX = "--no-os-sandbox"


@pytest.mark.parametrize(
    ("kind", "texts", "expected"),
    [
        (
            "result",
            ['"reason": "GEMINI_ENTERPRISE_LICENSE_NOT_VALID"', "is not recognized as a known model"],
            "licence",
        ),
        ("exited", ["error: authentication required"], "signin"),
        ("result", ["Error: Please sign in to view available models."], "signin"),
        ("signin", ['oauth2: "invalid_grant" "Refresh token has expired"'], "signin"),
        ("result", ["429 RESOURCE_EXHAUSTED"], "quota"),
        ("result", ["invalid model selection: model x is not recognized as a known model"], "model"),
        ("exited", ["failed to connect to the docker API at unix:///x/docker.sock"], "container"),
        ("exited", ["Unable to find image 'agy-sandbox:experiment' locally"], "container"),
        ("timeout", ["job exceeded its time limit"], "timeout"),
        ("stall", ["no agy event for 330 s"], "stall"),
        ("exited", ["agy exited with code 3 and no result"], "agy-failed"),
        ("exited", [], "agy-failed"),
    ],
)
def test_classify(kind, texts, expected):
    assert failure.classify(kind, *texts) == expected


def test_every_class_has_a_distinct_code_and_none_collides_with_usage_errors():
    codes = list(failure.EXIT_CODES.values())
    assert len(codes) == len(set(codes))
    assert 0 not in codes and 2 not in codes
    assert failure.exit_code(None) == 1 and failure.exit_code("no-such-class") == 1


def test_sign_in_failure_exits_11_and_is_recorded(companion):
    proc = companion.run("review", NO_SANDBOX, scenario="auth", check=False)
    assert proc.returncode == 11
    job = companion.job()
    assert job["failure"] == "signin"
    assert "Failure: signin (exit 11)" in proc.stdout


def test_reclaimed_licence_exits_15_not_as_an_unknown_model(companion):
    proc = companion.run("review", NO_SANDBOX, scenario="licence", check=False)
    assert proc.returncode == 15
    assert companion.job()["failure"] == "licence"


def test_an_expired_session_fails_at_once_instead_of_waiting_for_the_stall_timer(companion):
    started = time.monotonic()
    proc = companion.run("review", NO_SANDBOX, "--model", "high", scenario="session-expired", check=False)
    assert proc.returncode == 11
    assert time.monotonic() - started < 60  # the stall timer alone is 330 s
    job = companion.job()
    assert job["failure"] == "signin"
    assert job["rounds"][0]["stalls"] == []  # no resume on another model
    assert len(companion.agy_calls()) == 1


def test_missing_agy_exits_13(companion_module, monkeypatch, capsys):
    # In-process, so no real agy elsewhere on this machine can be found and run.
    monkeypatch.setattr(companion_module.agybin, "find_agy", lambda: None)
    assert companion_module.main(["review", NO_SANDBOX]) == 13
    assert "not installed" in capsys.readouterr().err


def test_forced_container_without_docker_exits_17(companion):
    proc = companion.run("review", "--isolation", "container", scenario="reply", check=False)
    assert proc.returncode == 17
    assert "container isolation is not available" in proc.stderr


def test_a_resumed_job_drops_its_old_failure(companion, repo):
    companion.run("task", NO_SANDBOX, "fix it", scenario="auth", check=False)
    job = companion.job()
    assert job["failure"] == "signin"
    results = repo.parent / "results.md"
    results.write_text("signed in again\n")
    companion.run("resume", job["id"], str(results), NO_SANDBOX, scenario="task")
    job = companion.job()
    assert job["status"] == "completed" and job.get("failure") is None


def test_an_expired_session_split_across_log_reads_is_still_caught(companion):
    proc = companion.run("review", NO_SANDBOX, scenario="session-expired-split", check=False)
    assert proc.returncode == 11
    assert companion.job()["failure"] == "signin"


def test_a_result_already_on_stdout_wins_over_a_later_expired_session(companion):
    proc = companion.run("review", NO_SANDBOX, scenario="result-then-session-expired", check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    job = companion.job()
    assert job["status"] == "completed" and job.get("failure") is None


def test_an_expired_session_ends_the_turn_even_while_agy_keeps_emitting_events(companion):
    started = time.monotonic()
    proc = companion.run("review", NO_SANDBOX, scenario="session-expired-chatty", check=False)
    assert proc.returncode == 11
    assert time.monotonic() - started < 60


def test_a_worker_crash_records_its_failure_class(companion, companion_module, repo, monkeypatch):
    from agylib import state

    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    job = companion.job()
    state.JobStore(repo).update(job["id"], lambda r: r.update({"status": "queued"}))

    def crash(self):
        raise RuntimeError("Cannot connect to the Docker daemon at unix:///x/docker.sock")

    monkeypatch.setattr(companion_module.JobRunner, "run", crash)
    with pytest.raises(RuntimeError):
        companion_module.main(["_worker", job["id"]])
    job = companion.job()
    assert job["status"] == "failed" and job["failure"] == "container"


def test_a_failed_working_copy_preparation_records_its_failure_class(companion, companion_module, repo, monkeypatch):
    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    job = companion.job()

    def refuse(*_args):
        raise OSError("read-only file system")

    monkeypatch.setattr(companion_module.gitctx, "install_hooks", refuse)
    results = repo.parent / "results.md"
    results.write_text("more\n")
    with pytest.raises(OSError):
        companion_module.main(["resume", job["id"], str(results), NO_SANDBOX])
    job = companion.job()
    assert job["status"] == "failed" and job["failure"] == "agy-failed"
