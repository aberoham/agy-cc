"""A plan artifact is read back only from the brain directory itself, never through a link."""

from __future__ import annotations

import os
from pathlib import Path


def _runner(tmp_path: Path, monkeypatch):
    from agylib.runner import JobRunner
    from agylib.state import JobStore

    monkeypatch.setenv("AGY_COMPANION_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    store = JobStore(tmp_path)
    store.create({"id": "plan-1", "kind": "plan", "status": "queued", "isolation": "host"})
    return JobRunner(store, "plan-1")


def test_plan_under_the_brain_directory_is_read(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    brain = tmp_path / "home" / ".gemini" / "antigravity-cli" / "brain" / "abc"
    brain.mkdir(parents=True)
    (brain / "implementation_plan.md").write_text("# Plan\n")
    assert runner._read_plan(brain / "implementation_plan.md") == "# Plan\n"


def test_plan_links_out_of_the_brain_directory_are_refused(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    brain = tmp_path / "home" / ".gemini" / "antigravity-cli" / "brain" / "abc"
    brain.mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("not for the plan\n")
    os.symlink(secret, brain / "plan.md")  # a file link
    assert runner._read_plan(brain / "plan.md") is None
    os.symlink(tmp_path, brain / "dir")  # a directory link in a parent component
    assert runner._read_plan(brain / "dir" / "secret.md") is None
    assert runner._read_plan(tmp_path / "secret.md") is None  # outside altogether


def test_a_brain_directory_replaced_by_a_link_is_refused(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    cli = tmp_path / "home" / ".gemini" / "antigravity-cli"
    cli.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "abc").mkdir(parents=True)
    (outside / "abc" / "implementation_plan.md").write_text("stolen\n")
    os.symlink(outside, cli / "brain")  # the brain directory itself is the link
    assert runner._read_plan(cli / "brain" / "abc" / "implementation_plan.md") is None


def test_a_home_behind_a_link_still_works(tmp_path, monkeypatch):
    real_home = tmp_path / "real-home"
    (real_home / ".gemini" / "antigravity-cli" / "brain" / "abc").mkdir(parents=True)
    (real_home / ".gemini" / "antigravity-cli" / "brain" / "abc" / "plan.md").write_text("# Plan\n")
    os.symlink(real_home, tmp_path / "home")  # HOME is a link, as /home can be on Linux
    runner = _runner(tmp_path, monkeypatch)
    assert (
        runner._read_plan(tmp_path / "home" / ".gemini" / "antigravity-cli" / "brain" / "abc" / "plan.md") == "# Plan\n"
    )


def test_a_named_pipe_in_place_of_the_plan_does_not_hang(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch)
    brain = tmp_path / "home" / ".gemini" / "antigravity-cli" / "brain" / "abc"
    brain.mkdir(parents=True)
    os.mkfifo(brain / "plan.md")
    assert runner._read_plan(brain / "plan.md") is None


def test_a_named_pipe_in_place_of_the_log_does_not_hang(tmp_path):
    from agylib.runner import _read_log_chunk

    os.mkfifo(tmp_path / "agy.log")
    assert _read_log_chunk(tmp_path / "agy.log", 0) == ("", 0)
    (tmp_path / "real.log").write_text("one\ntwo\n")
    assert _read_log_chunk(tmp_path / "real.log", 4) == ("two\n", 8)
