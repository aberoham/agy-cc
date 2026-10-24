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
