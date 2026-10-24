"""Regression tests for the findings of the first whole-repository review."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from agylib import gitctx
from agylib.policy import decide, is_read_only_command
from agylib.runner import _parse_review, terminate_job_processes
from conftest import COMPANION, REPO, git

SCHEMA = json.loads((REPO / "plugins" / "agy" / "schemas" / "review-output.schema.json").read_text())

NO_SANDBOX = "--no-os-sandbox"


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


# Hooks ----------------------------------------------------------------------------


def test_repository_hooks_are_replaced_not_merged(companion, repo):
    foreign = {"repo": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": "touch pwned"}]}]}}
    plugin = repo / ".agents" / "plugins" / "probe"
    plugin.mkdir(parents=True)
    (repo / ".agents" / "hooks.json").write_text(json.dumps(foreign))
    (plugin / "plugin.json").write_text('{"name": "probe"}')
    (plugin / "hooks.json").write_text(json.dumps(foreign))
    _commit(repo, "hooks")
    companion.run("task", NO_SANDBOX, "--background", "fix it", scenario="task")
    job = companion.wait_for(lambda j: j["status"] != "queued")
    agents = Path(job["worktree"]) / ".agents"
    assert sorted(p.name for p in agents.iterdir()) == ["hooks.json"]
    assert list(json.loads((agents / "hooks.json").read_text())) == ["agy-plugin-policy"]
    assert json.loads((repo / ".agents" / "hooks.json").read_text()) == foreign
    assert (plugin / "hooks.json").exists()
    companion.wait_for(lambda j: j["status"] == "needs-approval")
    patch = (Path(companion.env["AGY_COMPANION_STATE_DIR"]) / "*").parent
    patch_text = next(patch.glob("*/jobs/*/changes.patch")).read_text()
    assert ".agents" not in patch_text and "stats.py" in patch_text


def _aliases_long_s(path: Path) -> bool:
    probe = path / "probe\u017f"
    probe.write_text("")
    try:
        return (path / "probes").exists()
    finally:
        probe.unlink()


def test_removing_a_look_alike_agents_directory_never_reaches_the_export(repo, tmp_path):
    if not _aliases_long_s(repo):
        pytest.skip("needs a filesystem that folds the long s")
    lookalike = repo / ".agent\u017f"
    lookalike.mkdir()
    (lookalike / "notes").write_text("keep\n")
    _commit(repo, "look-alike agents directory")
    dest = tmp_path / "wt"
    baseline = gitctx.create_worktree(repo, dest, "true")
    assert sorted(p.name for p in (dest / ".agents").iterdir()) == ["hooks.json"]
    (dest / "stats.py").write_text("changed\n")
    changes = gitctx.export_changes(dest, baseline)
    assert [path for _, path in changes["paths"]] == ["stats.py"]
    assert (lookalike / "notes").read_text() == "keep\n"
    gitctx.remove_worktree(repo, dest)


def test_export_excludes_the_agents_directory_in_any_case(repo, tmp_path):
    dest = tmp_path / "wt"
    baseline = gitctx.create_worktree(repo, dest)
    (dest / ".Agents").mkdir()
    (dest / ".Agents" / "Hooks.json").write_text("{}")
    (dest / "stats.py").write_text("changed\n")
    changes = gitctx.export_changes(dest, baseline)
    assert [path for _, path in changes["paths"]] == ["stats.py"]
    gitctx.remove_worktree(repo, dest)


# Read confinement ---------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.aws/credentials",
        "cat ~bob/.ssh/id_ed25519",
        "grep --file=/etc/passwd -r .",
        "grep --file=patterns .",
        "grep -fpatterns .",
        "grep -f1 .",
        "wc --files0-f=list",
        "grep --dereference-r key .",
        "cat [l]ink",
        "grep --recurs key .",
        "grep -f patterns .",
        "grep --file patterns src",
        "cat input -n",
        "head input -n 3",
        "ls --color -LR .",
        "grep --color -R key .",
        "grep -r --color -S key .",
        "grep --regexp= -fsecret input",
        "grep --color -fsecret input",
        "grep -e -- -fsecret input",
        "grep --regexp -- --file=secret input",
        "grep -e -- -eMARKER -rS .",
        "grep -e pattern input",
        "grep --regexp pattern input",
        "grep --max-count 3 input",
        "find . -name -newer -delete",
        "find . -name -newer -follow -o -print",
        "find . -name x -newer y",
        "find -P . -newer stats.py -name x",
        "find . -samefile x",
        'grep "$(id)" input',
        'grep "TODO: $" input',
        "cat =sh",
        "head -c4 =sh",
        "find =x -name y",
        "find . ^input",
        "cat ^input",
        "cat s#ecret",
        'cat "\\$secret"',
        'cat "a\\\nb"',
        "cat a\rb",
        "cat 'a\rb'",
        "cat 'unclosed input",
        'grep "a`b" input',
        "cat \\$HOME/x",
        "find -- . -delete",
        "find -PL .",
        "find -EL .",
        "find -f x .",
        "find -fx .",
        "find -L",
        "find . -- -delete",
        "cat -123 input",
        "tail -F log",
        "tail -f log",
        "tail --follow=name log",
        "grep -f/etc/passwd .",
        "grep -R key .",
        "grep -rS key .",
        "grep -O -r key .",
        "grep --dereference-recursive key .",
        "ls -LR .",
        "ls --dereference .",
        "find -L . -name x",
        "find . -follow -name x",
        "find -H . -name x",
        "find . -files0-from list",
        "wc --files0-from=list",
        "wc --files0-from list",
    ],
)
def test_options_that_read_elsewhere_need_claude(command):
    assert not is_read_only_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "grep -rn key .",
        "grep -eSOURCE input",
        "grep --regexp=SOURCE input",
        "grep --color=always key .",
        "grep --context=2 key .",
        "grep --color key .",
        "grep --max-count=3 input",
        "grep -P 'key(s)?' input",
        "grep -E 'foo(bar)' input",
        'grep -rn "TODO: fix" src',
        "find . -name '*.py'",
        "find . -name '*.py' -not -path '*/node_modules/*'",
        "head -c1K input",
        "tail -n+20 input",
        "grep -A3 -C2 -n key .",
        "grep -A 3 key .",
        "grep -3 key .",
        "head -n 5 a.txt",
        "find -E . -regex x",
        "ls -w80 -T4 .",
        "ls -w 80 .",
        "head -5 a.txt",
        "tail -n 20 a.txt",
        "ls -la",
        "cat - a.txt",
    ],
)
def test_plain_options_stay_read_only(command):
    assert is_read_only_command(command)


def test_operands_after_double_dash_are_checked_as_paths(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "-secret").symlink_to(tmp_path / "outside.txt")
    policy = {"worktree": str(worktree), "writable": False, "read_dirs": []}
    call = {"toolCall": {"name": "run_command", "args": {"CommandLine": "cat -- -secret", "Cwd": str(worktree)}}}
    assert decide(call, policy).decision == "deny"
    (worktree / "inside.txt").write_text("x")
    call["toolCall"]["args"]["CommandLine"] = "cat -- inside.txt"
    assert decide(call, policy).decision == "allow"


def test_separate_words_are_path_checked_whatever_the_program_makes_of_them(tmp_path):
    """Under GNU a word after -A or -T is a value; under BSD it is a file. The policy checks it as a path."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    for name in ("-n", "3", "-ref"):
        (worktree / name).symlink_to(outside)
    (worktree / "input").write_text("x\n")
    (worktree / "5").write_text("x\n")
    policy = {"worktree": str(worktree), "writable": False, "read_dirs": []}

    def decision(command: str) -> str:
        call = {"toolCall": {"name": "run_command", "args": {"CommandLine": command, "Cwd": str(worktree)}}}
        return decide(call, policy).decision

    for command in (
        "grep -e -A 3 input",
        "grep --regexp -A 3 input",
        "ls -T 3",
        "ls -w 3",
        "head -n 3 input",
        "grep -A 3 key input",
    ):
        assert decision(command) == "deny", command
    for command in ("head -n 5 input", "grep -A 5 key input", "ls -T 5", "grep -A5 key input"):
        assert decision(command) == "allow", command


def test_tilde_command_is_queued_not_allowed(tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    policy = {"worktree": str(worktree), "writable": False, "read_dirs": []}
    call = {
        "toolCall": {"name": "run_command", "args": {"CommandLine": "cat ~/.aws/credentials", "Cwd": str(worktree)}}
    }
    result = decide(call, policy)
    assert result.decision == "deny"
    assert result.queued_command == "cat ~/.aws/credentials"


# Protected paths ----------------------------------------------------------------


def _case_insensitive(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.write_text("")
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.unlink()


@pytest.mark.parametrize(
    "target",
    [".GIT/config", ".Git/hooks/pre-commit", ".Agents/Hooks.json", ".AGENTS/hooks.json", ".agents/hook\u017f.json"],
)
def test_protected_paths_are_matched_by_identity_on_case_insensitive_filesystems(tmp_path, target):
    worktree = tmp_path / "wt"
    (worktree / ".agents").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n")
    (worktree / ".agents" / "hooks.json").write_text("{}")
    if not _case_insensitive(worktree):
        pytest.skip("needs a case-insensitive filesystem")
    policy = {"worktree": str(worktree), "writable": True, "read_dirs": []}
    call = {"toolCall": {"name": "write_file", "args": {"Path": str(worktree / target)}}}
    result = decide(call, policy)
    assert result.decision == "deny"
    assert "controls the job" in result.reason


def test_distinct_paths_that_only_resemble_protected_ones_are_allowed(tmp_path):
    worktree = tmp_path / "wt"
    (worktree / ".agents").mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n")
    (worktree / ".agents" / "hooks.json").write_text("{}")
    policy = {"worktree": str(worktree), "writable": True, "read_dirs": []}
    for target in (".gitignore", ".agentsx/other.json", "src/.git-notes", ".github/workflows/ci.yml"):
        call = {"toolCall": {"name": "write_file", "args": {"Path": str(worktree / target)}}}
        assert decide(call, policy).decision == "allow", target
    for target in (".agents/plugins/probe/plugin.json", ".agents/plugins/probe/hooks.json", ".agents/other.json"):
        call = {"toolCall": {"name": "write_file", "args": {"Path": str(worktree / target)}}}
        assert decide(call, policy).decision == "deny", target


# Review context -----------------------------------------------------------------


def test_untracked_symlink_contributes_only_its_link_text(repo, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text('{"token": "hunter2"}')
    (repo / "local-config.json").symlink_to(secret)
    context = gitctx.collect_context(repo, {"mode": "working-tree"})
    assert "hunter2" not in context["content"]
    assert f"(symlink to {secret})" in context["content"]


# Git configuration ------------------------------------------------------------------


def test_worktree_and_export_survive_colour_noprefix_and_signing(repo, tmp_path):
    git(repo, "config", "color.ui", "always")
    git(repo, "config", "color.diff", "always")
    git(repo, "config", "color.status", "always")
    git(repo, "config", "diff.noprefix", "true")
    git(repo, "config", "diff.srcPrefix", "x/y/")
    git(repo, "config", "diff.context", "0")
    git(repo, "config", "diff.orderFile", "/missing/order-file")
    git(repo, "config", "core.autocrlf", "true")
    git(repo, "config", "core.safecrlf", "true")
    git(repo, "config", "commit.cleanup", "strip")
    git(repo, "config", "core.commentChar", "a")
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "/bin/false")
    dest = tmp_path / "wt"
    baseline = gitctx.create_worktree(repo, dest)
    assert (dest / "stats.py").read_text() == (repo / "stats.py").read_text()
    (dest / "stats.py").write_text("changed\n")
    changes = gitctx.export_changes(dest, baseline)
    assert b"\x1b[" not in changes["patch"]
    assert b"--- a/stats.py" in changes["patch"]
    context = gitctx.collect_context(repo, {"mode": "working-tree"})
    assert "\x1b[" not in context["content"]
    gitctx.remove_worktree(repo, dest)


# Session state ------------------------------------------------------------------------


def test_session_start_exports_the_state_root_for_commands(companion, tmp_path):
    env_file = tmp_path / "claude.env"
    env_file.touch()
    plugin_data = tmp_path / "plugin-data"
    env = dict(companion.env, CLAUDE_ENV_FILE=str(env_file), CLAUDE_PLUGIN_DATA=str(plugin_data))
    env.pop("AGY_COMPANION_STATE_DIR")
    subprocess.run(
        [sys.executable, str(COMPANION), "session-start"],
        input=json.dumps({"session_id": "s-1"}),
        env=env,
        cwd=companion.repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    exported = env_file.read_text()
    assert "export AGY_COMPANION_SESSION_ID=s-1\n" in exported
    assert f"export AGY_COMPANION_STATE_DIR={(plugin_data / 'state').resolve()}\n" in exported


def test_bare_cancel_result_and_apply_act_only_on_this_sessions_jobs(companion, repo):
    companion.run("task", NO_SANDBOX, "fix it", scenario="task", AGY_COMPANION_SESSION_ID="session-a")
    for command, phrase in (("cancel", "to cancel"), ("result", "No finished"), ("apply", "with changes")):
        proc = companion.run(command, check=False, AGY_COMPANION_SESSION_ID="session-b")
        assert proc.returncode == 2, proc.stdout
        assert "from this session" in proc.stderr and phrase in proc.stderr
    proc = companion.run(
        "task", NO_SANDBOX, "--resume-last", "again", scenario="task", check=False, AGY_COMPANION_SESSION_ID="session-b"
    )
    assert proc.returncode == 2
    assert "from this session" in proc.stderr
    job_id = companion.job()["id"]
    assert job_id in companion.run("result", job_id, AGY_COMPANION_SESSION_ID="session-b").stdout
    assert "Fixed stats.py" in companion.run("result", AGY_COMPANION_SESSION_ID="session-a").stdout
    assert companion.job()["status"] == "needs-approval"


def test_read_only_follow_up_keeps_the_task_applicable(companion, repo):
    companion.run("task", NO_SANDBOX, "fix it", scenario="task")
    first = companion.job()
    companion.run("task", NO_SANDBOX, "--resume-last", "--read-only", "explain", scenario="task")
    job = companion.job()
    assert job["writable"] is True and job["read_only_round"] is True
    policy_file = next(Path(companion.env["AGY_COMPANION_STATE_DIR"]).glob("*/jobs/*/policy.json"))
    policy = json.loads(policy_file.read_text())
    assert policy["writable"] is False
    assert (Path(job["worktree"]) / "stats.py").read_text() == "fixed\n"
    proc = companion.run("apply", "--check")
    assert "apply cleanly" in proc.stdout
    companion.run("task", NO_SANDBOX, "--resume-last", "write more", scenario="task")
    assert companion.job()["read_only_round"] is False
    assert companion.job()["id"] == first["id"]


# Process handling ---------------------------------------------------------------------


def test_pids_that_no_longer_belong_to_the_job_are_not_signalled(tmp_path):
    """A child inside another group, and an unrelated group leader, both keep running."""
    pid_file = tmp_path / "child.pid"
    child_code = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    leader_code = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {child_code!r}])"
    leader = subprocess.Popen([sys.executable, "-c", leader_code], start_new_session=True)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        child = int(pid_file.read_text())
        assert os.getpgid(child) == leader.pid
        jobs = [{"id": "task-20260101-000000-abcd", "pid": child, "agy_pid": bystander.pid}]
        assert terminate_job_processes(jobs, grace=0.5) is False
        time.sleep(0.3)
        assert leader.poll() is None and bystander.poll() is None
        os.kill(child, 0)
    finally:
        os.killpg(leader.pid, signal.SIGKILL)
        os.killpg(bystander.pid, signal.SIGKILL)
        leader.wait()
        bystander.wait()


# Review parsing --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "review",
    [
        {"verdict": "approve", "summary": "ok", "findings": "none", "next_steps": []},
        {"verdict": "approve", "summary": "ok", "findings": ["a string"], "next_steps": []},
        {"verdict": "approve", "summary": "ok", "findings": {"stats.py": {}}, "next_steps": []},
        {"verdict": "approve", "summary": "ok", "findings": [], "next_steps": "fix it"},
        {"verdict": "approve", "summary": "ok", "findings": []},
        {"verdict": "approve", "summary": "ok", "findings": [{"severity": {}, "line_start": [], "title": False}]},
        {"verdict": 1, "summary": "ok", "findings": [], "next_steps": []},
    ],
)
def test_misshapen_reviews_are_reported_not_rendered(review):
    for result in ({"structured_output": review}, {"response": json.dumps(review)}):
        parsed, error = _parse_review(result, SCHEMA)
        assert parsed is None
        assert error and "does not match the schema" in error


def test_empty_structured_output_is_kept_as_the_raw_reply(companion):
    proc = companion.run("review", NO_SANDBOX, scenario="review-empty-structured", check=False)
    assert "does not match the schema" in proc.stdout
    assert "{}" in proc.stdout


def test_well_formed_review_is_accepted():
    finding = {
        "severity": "low",
        "title": "t",
        "body": "b",
        "file": "f",
        "line_start": 1,
        "line_end": 2,
        "confidence": 0.5,
        "recommendation": "r",
    }
    review = {"verdict": "approve", "summary": "ok", "findings": [finding], "next_steps": ["x"]}
    assert _parse_review({"structured_output": review}, SCHEMA) == (review, None)
    assert _parse_review({"response": f"```json\n{json.dumps(review)}\n```"}, SCHEMA) == (review, None)
    lenient = dict(review, findings=[dict(finding, severity="urgent", extra=1)])
    assert _parse_review({"structured_output": lenient}, SCHEMA) == (lenient, None)
