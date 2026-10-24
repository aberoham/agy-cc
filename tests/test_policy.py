from __future__ import annotations

from pathlib import Path

import pytest
from agylib.policy import decide, is_read_only_command


def payload(name: str, brain: Path, **args) -> dict:
    return {"toolCall": {"name": name, "args": args}, "artifactDirectoryPath": str(brain)}


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    worktree = tmp_path / "wt"
    brain = tmp_path / "brain"
    worktree.mkdir()
    brain.mkdir()
    return worktree, brain


def test_reads_inside_the_worktree_and_brain_pass(dirs):
    worktree, brain = dirs
    policy = {"worktree": str(worktree)}
    assert decide(payload("read_file", brain, Path=str(worktree / "a.py")), policy).decision == "allow"
    assert decide(payload("view_file", brain, AbsolutePath=str(brain / "plan.md")), policy).decision == "allow"
    assert decide(payload("list_dir", brain, DirectoryPath=str(worktree)), policy).decision == "allow"
    assert decide(payload("find_by_name", brain, SearchDirectory=str(worktree / "src")), policy).decision == "allow"
    assert decide(payload("grep_search", brain, SearchPath=str(worktree)), policy).decision == "allow"
    assert decide(payload("finish", brain), policy).decision == "allow"


def test_reads_outside_are_denied_unless_named(dirs, tmp_path):
    worktree, brain = dirs
    secret = tmp_path / "home" / ".aws" / "credentials"
    policy = {"worktree": str(worktree)}
    assert decide(payload("read_file", brain, Path=str(secret)), policy).decision == "deny"
    assert decide(payload("grep_search", brain, SearchPath="/"), policy).decision == "deny"
    assert decide(payload("read_file", brain, Path=str(worktree / ".." / "x")), policy).decision == "deny"
    assert decide(payload("read_file", brain), policy).decision == "deny"  # no path at all
    docs = tmp_path / "docs"
    allowed = {"worktree": str(worktree), "read_dirs": [str(docs)]}
    assert decide(payload("read_file", brain, Path=str(docs / "spec.md")), allowed).decision == "allow"
    assert decide(payload("read_file", brain, Path=str(secret)), allowed).decision == "deny"


def test_read_only_command_reading_outside_is_queued(dirs, tmp_path):
    worktree, brain = dirs
    policy = {"worktree": str(worktree)}
    outside = decide(payload("run_command", brain, CommandLine=f"cat {tmp_path}/secret", Cwd=str(worktree)), policy)
    assert outside.decision == "deny" and outside.queued_command
    inside = decide(payload("run_command", brain, CommandLine="cat src/a.py", Cwd=str(worktree)), policy)
    assert inside.decision == "allow"


def test_read_only_job_blocks_workspace_writes_under_either_tool_name(dirs):
    worktree, brain = dirs
    policy = {"worktree": str(worktree), "writable": False}
    for name in ("write_file", "write_to_file", "replace_file_content", "sed_file"):
        result = decide(payload(name, brain, Path=str(worktree / "a.py")), policy)
        assert result.decision == "deny", name


def test_read_only_job_may_write_its_brain_artifacts(dirs):
    worktree, brain = dirs
    result = decide(
        payload("write_file", brain, Path=str(brain / "implementation_plan.md")), {"worktree": str(worktree)}
    )
    assert result.decision == "allow"


def test_writable_job_is_confined_to_its_worktree(dirs, tmp_path):
    worktree, brain = dirs
    policy = {"worktree": str(worktree), "writable": True}
    assert decide(payload("write_file", brain, Path=str(worktree / "src" / "a.py")), policy).decision == "allow"
    assert decide(payload("write_file", brain, TargetFile=str(tmp_path / "escape.py")), policy).decision == "deny"
    assert decide(payload("write_file", brain, Path=str(worktree / ".." / "escape.py")), policy).decision == "deny"
    assert decide(payload("write_file", brain, Path=str(worktree / ".git" / "config")), policy).decision == "deny"
    assert (
        decide(payload("write_file", brain, Path=str(worktree / ".agents" / "hooks.json")), policy).decision == "deny"
    )


def test_symlink_out_of_worktree_is_denied(dirs, tmp_path):
    worktree, brain = dirs
    (worktree / "link").symlink_to(tmp_path)
    policy = {"worktree": str(worktree), "writable": True}
    assert decide(payload("write_file", brain, Path=str(worktree / "link" / "x")), policy).decision == "deny"


def test_unknown_tools_are_denied(dirs):
    worktree, brain = dirs
    for name in (
        "read_url_content",
        "search_web",
        "browser_click_element",
        "invoke_subagent",
        "call_mcp_tool",
        "made_up",
    ):
        assert decide(payload(name, brain), {"worktree": str(worktree), "writable": True}).decision == "deny"


def test_commands_that_are_not_read_only_are_queued(dirs):
    worktree, brain = dirs
    result = decide(
        payload("run_command", brain, CommandLine="npm test", Cwd=str(worktree)), {"worktree": str(worktree)}
    )
    assert result.decision == "deny"
    assert result.queued_command == "npm test"


def test_read_only_command_passes(dirs):
    worktree, brain = dirs
    result = decide(
        payload("run_command", brain, CommandLine="ls -la src", Cwd=str(worktree)), {"worktree": str(worktree)}
    )
    assert result.decision == "allow"
    assert result.queued_command is None


def test_command_outside_worktree_is_denied_not_queued(dirs, tmp_path):
    worktree, brain = dirs
    result = decide(payload("run_command", brain, CommandLine="ls", Cwd=str(tmp_path)), {"worktree": str(worktree)})
    assert result.decision == "deny"
    assert result.queued_command is None


@pytest.mark.parametrize(
    "command",
    ["ls -la", "grep -rn foo src", "find . -name x.py", "cat a.txt", "wc -l a b", "head -n 5 a.txt"],
)
def test_read_only_commands(command):
    assert is_read_only_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "ls; rm x",
        "cat a > b",
        "echo $(id)",
        "git commit -m x",
        "git status",
        "git diff HEAD",
        "git -c core.pager=sh status",
        "git diff --output=/tmp/x",
        "git grep -O vim foo",
        "rg --pre ./run foo",
        "rg foo",
        "/tmp/ls",
        "./ls",
        "git diff --textconv",
        "git log --show-signature",
        "git log --format=%G?",
        "find . -fprint0 /tmp/x",
        "file -C -m magic",
        "which ls",
        "find . -delete",
        "find . -exec rm {} +",
        "python3 -m pytest",
        "",
        "ls 'unclosed",
    ],
)
def test_commands_that_need_claude(command):
    assert not is_read_only_command(command)
