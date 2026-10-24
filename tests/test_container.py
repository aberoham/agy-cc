"""Container isolation: job setup, the docker command, the in-container export, and the fallback.

The fake docker runs containers as host processes, so these tests cover the
plugin's side of the boundary. test_real_container.py runs the same jobs in a
real container when Docker is reachable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agylib import container
from conftest import git


def _run_options(call: list[str]) -> list[str]:
    return [arg for arg in call[1:] if arg.startswith("-")]


def test_without_docker_jobs_fall_back_to_host_mode(companion):
    companion.run("review", "--no-os-sandbox", scenario="review")
    job = companion.job()
    assert job["isolation"] == "host"
    assert (Path(job["worktree"]).parent.parent / "jobs" / job["id"] / "policy.json").exists()
    assert companion.docker_runs() == []
    (call,) = companion.agy_calls()
    assert "--dangerously-skip-permissions" not in call["argv"]


def test_forcing_container_without_docker_is_an_error(companion):
    proc = companion.run("review", "--isolation", "container", scenario="review", check=False)
    assert proc.returncode == 2
    assert "not available" in proc.stderr
    assert companion.jobs() == []


def test_setup_reports_the_container_state(companion, contained):
    report = json.loads(companion.run("setup", "--json").stdout)
    assert report["docker_reachable"] is False and report["isolation"] == "host"
    assert "Docker daemon" in report["container_unavailable"]
    report = json.loads(contained.run("setup", "--json").stdout)
    assert report["docker_reachable"] and report["image_present"] and report["state_mountable"]
    assert report["container_signed_in"] is True
    assert report["isolation"] == "container" and report["container_unavailable"] is None


def test_an_image_built_for_another_user_is_refused_with_the_rebuild_command(contained):
    other = f"{os.getuid() + 1}:{os.getgid() + 1}"
    proc = contained.run(
        "review", "--isolation", "container", scenario="review", check=False, FAKE_DOCKER_IMAGE_USER=other
    )
    assert proc.returncode == 2
    assert f"built for user {other}" in proc.stderr and "--build-arg UID=$(id -u)" in proc.stderr
    report = json.loads(contained.run("setup", "--json", FAKE_DOCKER_IMAGE_USER=other).stdout)
    assert report["image_user_matches"] is False and report["isolation"] == "host"
    assert f"built for user {other}" in report["container_unavailable"]
    # A named user cannot be checked and passes; a failed inspection does not.
    assert json.loads(contained.run("setup", "--json", FAKE_DOCKER_IMAGE_USER="agent").stdout)["image_user_matches"]
    from agylib import container

    assert container.image_user_matches(None) is False
    assert container.image_user_matches("") is True


def test_review_runs_in_a_container_with_only_the_job_paths_mounted(contained, repo):
    proc = contained.run("review", "--model", "low", scenario="review")
    assert "Verdict: needs-attention" in proc.stdout
    job = contained.job()
    assert job["isolation"] == "container" and job["os_sandbox"] is False
    worktree = Path(job["worktree"])
    state = Path(contained.env["AGY_COMPANION_STATE_DIR"])
    signin = Path(contained.env["AGY_CONTAINER_HOME"])
    job_mount = worktree.parent.parent / "jobs" / job["id"] / "container"
    probe, run = contained.docker_runs()
    assert "touch" in probe and str(state) in " ".join(probe)
    options = _run_options(run)
    volumes = [o[len("--volume=") :] for o in options if o.startswith("--volume=")]
    # Exactly these, and nothing else: the job record, the result and the
    # sign-in home stay outside.
    assert volumes == [
        f"{worktree}:{worktree}:ro",  # a review never edits the copy
        f"{job_mount}:{job_mount}",
        f"{job['plugin_root']}:{job['plugin_root']}:ro",
    ]
    assert not any(str(repo) in v or str(signin) in v for v in volumes)
    assert run[run.index("--log-file") + 1] == str(job_mount / "agy.log")
    assert "--init" in options and f"--env=HOME={job_mount / 'home'}" in options
    seeded = job_mount / "home" / ".gemini" / "antigravity-cli"
    assert (seeded / "antigravity-oauth-token").read_text() == "{}"
    assert (seeded / "settings.json").read_text() == '{"enableTerminalSandbox": false}\n'
    env = dict(o[len("--env=") :].split("=", 1) for o in options if o.startswith("--env="))
    keys = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert keys == {
        "safe.directory": "*",
        "user.name": "agy plugin",
        "user.email": "agy-plugin@localhost",
        "commit.gpgsign": "false",
    }
    assert any(o.startswith("--name=agy-review-") for o in options)
    assert run[-1] == "--dangerously-skip-permissions"
    (call,) = contained.agy_calls()
    assert call["argv"][-1] == "--dangerously-skip-permissions"
    assert "--json-schema" in call["argv"]
    assert "read-only shell commands" in json.loads(call["stdin"])["message"]["content"]
    assert not worktree.exists()


def test_container_task_keeps_repository_hooks_and_exports_from_inside(contained, repo, tmp_path):
    marker = tmp_path / "hook-ran"
    hooks = {
        "repo": {
            "PreToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": f"touch {marker}; touch hook-marker", "timeout": 5}],
                }
            ]
        }
    }
    (repo / ".agents").mkdir()
    (repo / ".agents" / "hooks.json").write_text(json.dumps(hooks))
    git(repo, "add", ".agents")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "hooks")
    proc = contained.run("task", "fix the bug", scenario="container")
    job = contained.job()
    assert job["status"] == "completed" and job["isolation"] == "container"
    assert "Fixed stats.py" in proc.stdout
    worktree = Path(job["worktree"])
    assert (worktree / ".agents" / "hooks.json").exists()  # not emptied in container mode
    assert (worktree / ".git").is_dir()  # a self-contained copy, not a linked worktree
    assert str(worktree) not in git(repo, "worktree", "list")
    assert marker.exists()  # the fake docker runs the hook on the host; a real container keeps it inside
    message = json.loads(contained.agy_calls()[0]["stdin"])["message"]["content"]
    assert "inside a container" in message and "queued" not in message
    # The export ran as a second container, and the host read its output.
    export = contained.docker_runs()[-1]
    assert "_export" in export and "--entrypoint" in export
    assert f"--name=agy-{job['id']}-1-export" in export and "--init" in export
    assert [o for o in export if o.startswith("--volume=")] == [
        o for o in contained.docker_runs()[1] if o.startswith("--volume=")
    ]
    assert export[-1] == str(worktree.parent.parent / "jobs" / job["id"] / "container")
    calls = [json.loads(line) for line in contained.docker_calls.read_text().splitlines()]
    assert ["rm", "--force", f"agy-{job['id']}-1-export"] in calls
    assert job["container"] is None
    stored = json.loads((Path(job["worktree"]).parent.parent / "jobs" / job["id"] / "result.json").read_text())
    assert stored["changed_paths"] == [["A", "hook-marker"], ["M", "stats.py"]]
    proc = contained.run("apply", job["id"])
    assert "Applied" in proc.stdout
    assert (repo / "stats.py").read_text() == "fixed\n"
    assert (repo / "hook-marker").exists()


def test_container_task_resumes_in_the_same_copy_and_conversation(contained, repo):
    contained.run("task", "fix the bug", scenario="container")
    first = contained.job()
    contained.run("task", "--resume-last", "--read-only", "carry on", scenario="container")
    job = contained.job()
    assert job["id"] == first["id"] and job["status"] == "completed"
    assert [r["kind"] for r in job["rounds"]] == ["initial", "follow-up"]
    calls = contained.agy_calls()
    assert calls[1]["argv"][calls[1]["argv"].index("--conversation") + 1] == job["conversation_id"]
    assert "mounted read-only" in json.loads(calls[1]["stdin"])["message"]["content"]
    worktree = job["worktree"]
    runs = [c for c in contained.docker_runs() if any(o.startswith("--name=agy-") for o in c)]
    names = [o for c in runs for o in c if o.startswith("--name=")]
    assert names == [
        f"--name=agy-{job['id']}-1",
        f"--name=agy-{job['id']}-1-export",
        f"--name=agy-{job['id']}-2",
        f"--name=agy-{job['id']}-2-export",
    ]
    assert f"--volume={worktree}:{worktree}" in runs[0]  # the task round edits
    assert f"--volume={worktree}:{worktree}:ro" in runs[2]  # the read-only round does not


def test_each_round_starts_from_a_fresh_home(contained, repo):
    contained.run("task", "fix the bug", scenario="container")
    job = contained.job()
    home = Path(job["worktree"]).parent.parent / "jobs" / job["id"] / "container" / "home"
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    assert settings.is_file()
    settings.write_text('{"hooks": "planted by a job"}')  # what a hostile round could leave
    (home / ".cache" / "uv").mkdir(parents=True)
    contained.run("task", "--resume-last", "carry on", scenario="container")
    assert settings.read_text() == '{"enableTerminalSandbox": false}\n'
    assert not (home / ".cache").exists()


def test_plans_are_read_from_the_job_home(contained):
    proc = contained.run("plan", "add a median function", scenario="plan")
    assert "1. Do the thing." in proc.stdout
    job = contained.job()
    assert job["status"] == "completed"
    assert "/container/home/.gemini/antigravity-cli/brain/" in proc.stdout


def test_cancel_removes_the_container(contained):
    contained.run("task", "--background", "--model", "high", "--stall-seconds", "600", "do it", scenario="stall")
    job = contained.wait_for(lambda j: j["status"] == "running" and j.get("container"))
    assert job["container"] == f"agy-{job['id']}-1"
    contained.run("cancel")
    assert contained.job()["status"] == "cancelled"
    calls = [json.loads(line) for line in contained.docker_calls.read_text().splitlines()]
    assert ["rm", "--force", job["container"]] in calls


def test_apply_uses_the_baseline_snapshot_not_git_in_the_copy(contained, repo):
    contained.run("task", "fix the bug", scenario="container")
    job = contained.job()
    job_dir = Path(job["worktree"]).parent.parent / "jobs" / job["id"]
    tree = json.loads((job_dir / "baseline-tree.json").read_text())
    assert set(tree) == {"stats.py", "notes.txt"}
    (repo / "stats.py").write_text("someone else changed this\n")
    proc = contained.run("apply", check=False)
    assert proc.returncode == 2 and "stats.py" in proc.stderr


def test_wrap_puts_every_mount_at_its_host_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_COMPANION_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGY_CONTAINER_HOME", str(tmp_path / "container-home"))
    monkeypatch.setenv("PATH", str(tmp_path))
    (tmp_path / "docker").write_text("")
    (tmp_path / "docker").chmod(0o755)
    (tmp_path / "container-home" / ".gemini" / "antigravity-cli").mkdir(parents=True)
    container.seed_home(tmp_path / "job")
    mounts = container.Mounts(tmp_path / "wt", tmp_path / "job", tmp_path / "plugin", [tmp_path / "docs"], True)
    command = container.wrap(["agy", "--model", "x", "--print="], name="agy-job-1", mounts=mounts)
    assert command[0] == str(tmp_path / "docker")
    assert command[1] == "run" and "--rm" in command[:5] and "--interactive" in command[:5]
    assert f"--volume={tmp_path / 'docs'}:{tmp_path / 'docs'}:ro" in command
    assert f"--volume={tmp_path / 'job' / 'container'}:{tmp_path / 'job' / 'container'}" in command
    assert not any(o == f"--volume={tmp_path / 'job'}:{tmp_path / 'job'}" for o in command)
    assert not any(str(tmp_path / "container-home") in o for o in command)
    assert f"--env=HOME={tmp_path / 'job' / 'container' / 'home'}" in command
    assert command[-4:] == ["--model", "x", "--print=", "--dangerously-skip-permissions"]
    assert "agy" not in command[command.index(container.DEFAULT_IMAGE) + 1 :]


def test_apply_refuses_a_job_without_a_baseline_snapshot(contained, repo):
    contained.run("task", "fix the bug", scenario="container")
    job = contained.job()
    (Path(job["worktree"]).parent.parent / "jobs" / job["id"] / "baseline-tree.json").unlink()
    proc = contained.run("apply", check=False)
    assert proc.returncode == 2 and "no baseline snapshot" in proc.stderr
    assert "sum(v[1:])" in (repo / "stats.py").read_text()
