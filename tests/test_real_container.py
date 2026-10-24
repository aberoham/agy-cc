"""The container boundary itself, with the fake agy inside a real Linux container.

Skipped when no Docker daemon is reachable. The scratch directory is under
the home directory, because a Docker virtual machine on macOS (Colima) shares
that and not the system temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from conftest import COMPANION, git

TESTS = Path(__file__).resolve().parent
IMAGE = "agy-cc-test-fake-agy"


def _docker_reachable() -> bool:
    if not shutil.which("docker"):
        return False
    proc = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=30)
    return proc.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_reachable(), reason="no Docker daemon")


@pytest.fixture(scope="module")
def image() -> str:
    subprocess.run(
        ["docker", "build", "--quiet", "--tag", IMAGE, "--file", str(TESTS / "containers" / "Dockerfile"), str(TESTS)],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return IMAGE


@pytest.fixture
def scratch():
    root = Path.home() / ".cache" / "agy-cc-tests" / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _companion(scratch: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    token = scratch / "container-home" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("{}")
    env = dict(
        os.environ,
        AGY_BIN=str(TESTS / "fake_agy.py"),
        AGY_IMAGE=IMAGE,
        AGY_COMPANION_STATE_DIR=str(scratch / "state"),
        AGY_CONTAINER_HOME=str(scratch / "container-home"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.pop("AGY_COMPANION_SESSION_ID", None)
    return subprocess.run(
        [sys.executable, str(COMPANION), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=600
    )


def test_task_in_a_real_container_reaches_only_the_working_copy(image, scratch):
    repo = scratch / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "stats.py").write_text("def average(v):\n    return sum(v[1:]) / len(v)\n")
    hook_marker = f"/tmp/agy-hook-marker-{uuid.uuid4().hex}"
    hooks = {
        "repo": {
            "PreToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": f"touch {hook_marker}; touch hook-marker", "timeout": 5}],
                }
            ]
        }
    }
    (repo / ".agents").mkdir()
    (repo / ".agents" / "hooks.json").write_text(json.dumps(hooks))
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    secret = scratch / "credentials"
    secret.write_text("aws_secret_access_key = not-for-agy\n")
    filter_marker = scratch / "filter-ran-on-host"

    proc = _companion(
        scratch,
        repo,
        "task",
        "--isolation",
        "container",
        f"fix the bug\nprobe-read {secret}\nprobe-filter {filter_marker}",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (job_file,) = (scratch / "state").glob("*/jobs/*/job.json")
    job = json.loads(job_file.read_text())
    assert job["status"] == "completed", job
    assert job["isolation"] == "container"
    stored = json.loads((job_file.parent / "result.json").read_text())
    assert f"read {secret}: missing" in stored["response"]
    assert stored["changed_paths"] == [["A", ".gitattributes"], ["A", "hook-marker"], ["M", "stats.py"]]
    assert not Path(hook_marker).exists()  # the repository hook ran inside the container
    assert not filter_marker.exists()  # the export's git add ran inside the container too
    worktree = Path(job["worktree"])
    assert (worktree / "hook-marker").exists()
    assert "filter" in (worktree / ".git" / "config").read_text()

    proc = _companion(scratch, repo, "apply", job["id"])
    assert "Applied" in proc.stdout, proc.stdout + proc.stderr
    assert (repo / "stats.py").read_text() == "fixed\n"
    assert (repo / "hook-marker").exists()
    assert not filter_marker.exists()
