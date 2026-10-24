from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins" / "agy" / "scripts"
COMPANION = SCRIPTS / "agy_companion.py"
FAKE_AGY = Path(__file__).resolve().parent / "fake_agy.py"
FAKE_DOCKER = Path(__file__).resolve().parent / "fake_docker.py"
sys.path.insert(0, str(SCRIPTS))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with one commit and an uncommitted bug in stats.py."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "stats.py").write_text("def average(v):\n    return sum(v) / len(v)\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    (root / "stats.py").write_text("def average(v):\n    return sum(v[1:]) / len(v)\n")
    (root / "notes.txt").write_text("untracked\n")
    return root


class Companion:
    """Runs the companion with the fake agy. The fake docker is first on PATH;
    it reports no daemon unless container_mode is set, in which case it runs
    containers as host processes and the container sign-in is present."""

    def __init__(self, repo: Path, tmp: Path, container_mode: bool = False):
        self.repo = repo
        self.calls = tmp / "agy-calls.ndjson"
        self.docker_calls = tmp / "docker-calls.ndjson"
        home = tmp / "home"
        (home / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
        shim = tmp / "bin"
        shim.mkdir(exist_ok=True)
        if not (shim / "docker").exists():
            (shim / "docker").symlink_to(FAKE_DOCKER)
        self.env = dict(
            os.environ,
            HOME=str(home),
            PATH=f"{shim}{os.pathsep}{os.environ.get('PATH', '')}",
            AGY_BIN=str(FAKE_AGY),
            AGY_COMPANION_STATE_DIR=str(tmp / "state"),
            FAKE_AGY_CALLS=str(self.calls),
            FAKE_DOCKER_CALLS=str(self.docker_calls),
            FAKE_DOCKER_MODE="present" if container_mode else "absent",
            AGY_CONTAINER_HOME=str(tmp / "container-home"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.env.pop("AGY_COMPANION_SESSION_ID", None)
        if container_mode:
            token = tmp / "container-home" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            token.parent.mkdir(parents=True, exist_ok=True)
            token.write_text("{}")

    def run(self, *args: str, scenario: str = "reply", check: bool = True, **env: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, str(COMPANION), *args],
            cwd=self.repo,
            env=dict(self.env, FAKE_AGY_SCENARIO=scenario, **env),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if check and proc.returncode not in (0, 1):
            raise AssertionError(f"companion failed ({proc.returncode}): {proc.stdout}\n{proc.stderr}")
        return proc

    def agy_calls(self) -> list[dict]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def docker_runs(self) -> list[list[str]]:
        if not self.docker_calls.exists():
            return []
        calls = [json.loads(line) for line in self.docker_calls.read_text().splitlines()]
        return [c for c in calls if c[:1] == ["run"]]

    def jobs(self) -> list[dict]:
        state = Path(self.env["AGY_COMPANION_STATE_DIR"])
        return [json.loads(p.read_text()) for p in state.glob("*/jobs/*/job.json")]

    def job(self) -> dict:
        jobs = self.jobs()
        assert len(jobs) == 1, jobs
        return jobs[0]

    def wait_for(self, predicate, timeout: float = 60) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = self.jobs()
            if jobs and predicate(jobs[0]):
                return jobs[0]
            time.sleep(0.3)
        raise AssertionError(f"timed out; jobs: {self.jobs()}")


@pytest.fixture
def companion(repo: Path, tmp_path: Path) -> Companion:
    return Companion(repo, tmp_path)


@pytest.fixture
def contained(repo: Path, tmp_path: Path) -> Companion:
    return Companion(repo, tmp_path, container_mode=True)
