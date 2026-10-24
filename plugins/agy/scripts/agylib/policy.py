"""Decide each agy tool call for a job.

agy's `--mode plan` only adds an instruction to the prompt, so this policy is
the enforcement. It runs inside agy's PreToolUse hook and can only deny: in
print mode agy ignores a hook's "allow" for anything its own permission
settings would refuse, so "allow" here means "do not object".

Commands this policy will not pass are queued for Claude, which runs them
with its own Bash tool and resumes the agy conversation with the output.
"""

from __future__ import annotations

import os
import shlex
from typing import Any, NamedTuple

# The CLI reports some tools under names that differ from its docs and its
# own init event (write_file vs write_to_file, read_file vs view_file).
# Tools that read a path. Reads are confined to the job's working copy, agy's
# artifact directory and any directory the caller passed with --read-dir, so a
# job cannot read and upload files such as ~/.aws/credentials.
PATH_READ_TOOLS = frozenset({"read_file", "view_file", "list_dir", "find_by_name", "grep_search"})
# Tools that read no files.
PATHLESS_TOOLS = frozenset({"list_permissions", "command_status", "finish", "wait", "wait_5_seconds"})
READ_TOOLS = PATH_READ_TOOLS | PATHLESS_TOOLS
WRITE_TOOLS = frozenset(
    {
        "write_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "sed_file",
        "notebook_edit",
    }
)
COMMAND_TOOLS = frozenset({"run_command"})
PATH_KEYS = ("Path", "TargetFile", "AbsolutePath", "FilePath", "File")
READ_PATH_KEYS = ("Path", "AbsolutePath", "FilePath", "File", "DirectoryPath", "SearchDirectory", "SearchPath")

# A command is passed without Claude only when it has no shell syntax, names
# its program without a path (so ./ls or /tmp/ls cannot pose as ls), and the
# program and its options can only read. Programs that can start another
# program through an option (rg --pre, file -C, which) are left out. Anything
# else is queued for Claude, so being strict here costs a round, not safety.
SHELL_SYNTAX = set(";|&<>`$(){}\n\\*?")
READ_ONLY_PROGRAMS = frozenset({"ls", "cat", "head", "tail", "wc", "pwd", "grep", "stat", "echo"})
# git is not on the list: repository configuration can make even `git diff`
# run a program (diff.external, textconv, fsmonitor), so git commands go to
# Claude. Reviews already carry the diff in their prompt.
FIND_UNSAFE_PREFIXES = ("-exec", "-ok", "-delete", "-fprint", "-fls")


class Decision(NamedTuple):
    decision: str  # "allow" or "deny"
    reason: str
    queued_command: str | None = None


def _inside(path: str, root: str) -> bool:
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(root)
    except (OSError, ValueError):
        return False
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def _target_path(args: dict[str, Any]) -> str | None:
    for key in PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def is_read_only_command(command: str) -> bool:
    if not command.strip() or any(ch in SHELL_SYNTAX for ch in command):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens or "/" in tokens[0]:
        return False
    program, args = tokens[0], tokens[1:]
    if program in READ_ONLY_PROGRAMS:
        return True
    if program == "find":
        return not any(token.startswith(FIND_UNSAFE_PREFIXES) for token in args)
    return False


def command_paths_inside(command: str, cwd: str, roots: list[str]) -> bool:
    """True when every argument that is not an option resolves inside one of roots.

    Arguments that are patterns rather than paths resolve inside cwd, which is
    itself inside the working copy, so they pass; an absolute or ../ argument
    that leaves the roots does not.
    """
    tokens = shlex.split(command)
    if tokens and tokens[0] == "echo":
        return True
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        path = token if os.path.isabs(token) else os.path.join(cwd, token)
        if not any(_inside(path, root) for root in roots):
            return False
    return True


def decide(payload: dict[str, Any], policy: dict[str, Any]) -> Decision:
    call = payload.get("toolCall") or {}
    name = str(call.get("name", ""))
    args = call.get("args") or {}
    worktree = str(policy.get("worktree", ""))
    writable = bool(policy.get("writable"))
    brain = payload.get("artifactDirectoryPath")
    read_roots = [r for r in [worktree, brain if isinstance(brain, str) else "", *policy.get("read_dirs", [])] if r]

    if name in PATHLESS_TOOLS:
        return Decision("allow", "tool reads no files")

    if name in PATH_READ_TOOLS:
        targets = [v for k in READ_PATH_KEYS if isinstance(v := args.get(k), str) and v]
        if not targets:
            return Decision("deny", f"{name}: no path given")
        if all(any(_inside(target, root) for root in read_roots) for target in targets):
            return Decision("allow", "read inside the job's allowed directories")
        return Decision(
            "deny",
            "Reads are limited to the job's working copy, your artifact directory and the "
            "directories the caller named. Ask for the file in your reply if you need it.",
        )

    if name in WRITE_TOOLS:
        target = _target_path(args)
        if target is None:
            return Decision("deny", f"{name}: no target path given")
        if isinstance(brain, str) and brain and _inside(target, brain):
            return Decision("allow", "agy's own artifact directory")
        if not writable:
            return Decision(
                "deny",
                "This is a read-only agy job: do not change files. Put your answer in your reply.",
            )
        if not worktree or not _inside(target, worktree):
            return Decision("deny", "Writes are allowed only inside the job's working copy.")
        relative = os.path.relpath(os.path.realpath(target), os.path.realpath(worktree))
        parts = relative.split(os.sep)
        if parts[0] == ".git" or relative == os.path.join(".agents", "hooks.json"):
            return Decision("deny", "That file controls the job and cannot be changed.")
        return Decision("allow", "write inside the job's working copy")

    if name in COMMAND_TOOLS:
        command = str(args.get("CommandLine", ""))
        cwd = args.get("Cwd")
        if isinstance(cwd, str) and cwd and worktree and not _inside(cwd, worktree):
            return Decision("deny", "Commands must run inside the job's working copy.")
        if is_read_only_command(command) and command_paths_inside(command, cwd or worktree, read_roots):
            return Decision("allow", "read-only command")
        return Decision(
            "deny",
            "Queued for Claude: this command was not run. Claude may run it and send you "
            "the output in a later turn. Continue with what you can do without it, and list "
            "any command you still need at the end of your reply.",
            queued_command=command,
        )

    return Decision("deny", f"The {name} tool is not available in agy plugin jobs.")
