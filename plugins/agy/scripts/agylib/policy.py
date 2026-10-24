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
# program and every option on the line are on the allow-list below. Programs
# that can start another program through an option (rg --pre, file -C, which)
# and git (diff.external, textconv and fsmonitor can run programs) are left
# out; reviews already carry the diff in their prompt. Anything else is queued
# for Claude, so being strict here costs a round, not safety.
#
# agy runs the line through a shell, which expands `~` and `[...]` as it does
# `$` and `*`, and zsh also `^`, `#` and a word-leading `=`, so they count as
# shell syntax too. Inside single quotes nothing is special and inside double
# quotes only `$`, backquote, backslash and `!` are, so a quoted pattern such
# as '*.py' or 'foo(bar)' passes. A carriage return is refused everywhere,
# because shlex splits on it and the shell does not.
SHELL_SYNTAX = set(";|&<>`$(){}[]\n\r\\*?~!^#")  # ^ and # are zsh extended globbing
DOUBLE_QUOTED_SYNTAX = set("$`\\!\n\r")  # a backslash is refused rather than modelled


# The grammar is kept strict enough that GNU and BSD programs read a line the
# same way, so that every word a program could open as a file is an operand
# the policy path-checks:
#   - options come before operands; a dash word after an operand refuses the
#     command, because BSD programs would open it as a file;
#   - an option's value is attached (-A3, --context=3); a separate word is
#     an operand to the policy whatever the program makes of it, so it is
#     path-checked, and the policy never has to guess whether `ls -T 3`
#     means a tab size or a file called 3;
#   - options that name a file to read (grep -f, --file, --files0-from) are
#     not on the list at all, and an attached value never contains a slash;
#   - a pattern or format value, and a long option's value, must be attached,
#     because the program would otherwise consume the next word, whatever it
#     is, and the policy would misread what follows (`-e -- -fsecret`);
#   - words after `--` are operands.
# Letters whose value is a count, a pattern or a format may carry it
# attached; a flag letter takes none. Left out on purpose: anything that
# follows symlinks met while recursing (grep -R/-S/-O, ls -L, find -L/-H),
# anything that reads a list of further files, and tail's follow modes (a job
# would hang, and -F reopens by name). GNU programs accept unambiguous
# abbreviations of long options; an abbreviation is not on the list, so it is
# refused rather than mistaken for something else.
class Program(NamedTuple):
    flags: frozenset  # letters that take no value and may appear in a cluster such as -rn
    numeric: frozenset = frozenset()  # letters whose value is a count, attached (-A3) or the next word
    free: frozenset = frozenset()  # letters whose value is a pattern or format and must be attached (-efoo)
    long: frozenset = frozenset()  # long options that take no value
    long_value: frozenset = frozenset()  # long options whose value must be attached with = (--context=3)
    bare_number: bool = False  # accepts -N as a count (head -5, grep -3)


PROGRAMS = {
    # ls -w and -T take a number on GNU but are flags on BSD, so `-w1L` would be
    # -w -1 -L to BSD ls; both letters are left out rather than modelled.
    "ls": Program(
        frozenset("1AaBbCcdFfGghiklmnopqRrSstuUvxX%@"),
        long=frozenset(
            {
                "--all",
                "--almost-all",
                "--recursive",
                "--human-readable",
                "--reverse",
                "--size",
                "--no-color",
                "--color",
            }
        ),
        long_value=frozenset({"--sort", "--time", "--color", "--width", "--tabsize"}),
    ),
    "cat": Program(
        frozenset("AbeEnstTuv"),
        long=frozenset({"--number", "--show-all", "--squeeze-blank", "--show-ends", "--number-nonblank"}),
    ),
    "head": Program(
        frozenset("qvz"),
        numeric=frozenset("nc"),
        long=frozenset({"--quiet", "--verbose"}),
        long_value=frozenset({"--lines", "--bytes"}),
        bare_number=True,
    ),
    "tail": Program(
        frozenset("qvzr"),
        numeric=frozenset("nc"),
        long=frozenset({"--quiet", "--verbose"}),
        long_value=frozenset({"--lines", "--bytes"}),
        bare_number=True,
    ),
    "wc": Program(
        frozenset("clmwL"), long=frozenset({"--bytes", "--chars", "--lines", "--words", "--max-line-length"})
    ),
    "pwd": Program(frozenset("LP")),
    # stat -f and -t take a format on BSD but are flags on GNU, so `-tL` would be
    # -t -L to GNU stat; only -c, a format on GNU and unknown to BSD, is listed.
    "stat": Program(
        frozenset("Flnqrsx"),
        free=frozenset("c"),
        long=frozenset({"--terse", "--file-system"}),
        long_value=frozenset({"--format", "--printf"}),
    ),
    "grep": Program(
        frozenset("abcEFGhHiIlLnoPqrsUvwxyzZ"),
        numeric=frozenset("mABC"),
        free=frozenset("ed"),
        bare_number=True,
        long=frozenset(
            {
                "--count",
                "--files-with-matches",
                "--files-without-match",
                "--ignore-case",
                "--no-ignore-case",
                "--line-number",
                "--recursive",
                "--extended-regexp",
                "--fixed-strings",
                "--basic-regexp",
                "--perl-regexp",
                "--invert-match",
                "--word-regexp",
                "--line-regexp",
                "--only-matching",
                "--quiet",
                "--silent",
                "--no-messages",
                "--with-filename",
                "--no-filename",
                "--text",
                "--null",
                "--no-color",
                "--color",
                "--colour",
            }
        ),
        long_value=frozenset(
            {
                "--max-count",
                "--after-context",
                "--before-context",
                "--context",
                "--include",
                "--exclude",
                "--exclude-dir",
                "--color",
                "--colour",
                "--regexp",
            }
        ),
    ),
}
# find's grammar is vetted apart: leading option clusters (BSD find takes
# -PL as well as -P -L), then paths, then expression words. Only these
# leading letters pass; -L, -H and -f (a path) are not among them. In the
# expression every dash word is vetted, whether find would read it as a
# predicate or as the value of the one before (`-name -delete`), and every
# other word is an operand, so a name is path-checked wherever it falls.
# Refused: the prefixes that act or write, the words that follow symlinks or
# read a list of further paths, and the predicates that name a reference
# file, because `-name -newer x` would hide that file from the check.
# Predicates that read only a symlink target's metadata (-printf %Y) are
# tolerated: the guarantee is that no file outside the roots is read or
# traversed.
FIND_LEADING_LETTERS = frozenset("EPXdsx")
FIND_UNSAFE_PREFIXES = ("-exec", "-ok", "-delete", "-fprint", "-fls", "-files0-from", "-newer")
FIND_UNSAFE_WORDS = frozenset({"-L", "-H", "-follow", "-xtype", "-samefile"})


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


def _controls_job(target: str, worktree: str) -> bool:
    """True when target is, or lies under, the worktree's .git entry or its .agents directory.

    .agents holds the plugin's hook, and agy would also load hooks from a
    workspace plugin written there. The names are compared exactly and then
    by file identity, because a case-insensitive filesystem (the macOS
    default) also maps .GIT, .Agents or a spelling with a look-alike Unicode
    letter to the same entry, and realpath does not restore the name on disk.
    """
    relative = os.path.relpath(os.path.realpath(target), os.path.realpath(worktree))
    head = relative.split(os.sep)[0]
    for protected in (".git", ".agents"):
        if head == protected:
            return True
        try:
            if os.path.samefile(os.path.join(worktree, head), os.path.join(worktree, protected)):
                return True
        except OSError:
            continue
    return False


def _target_path(args: dict[str, Any]) -> str | None:
    for key in PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class Parsed(NamedTuple):
    program: str
    options: list[str]
    operands: list[str]  # every word the program could open as a file or treat as a pattern


def parse_command(command: str) -> Parsed | None:
    """Split a command into program, options and operands, or None when it cannot be judged.

    See the grammar above SHELL_SYNTAX. find has its own grammar, parsed by
    _parse_find, which returns None for an unsafe form.
    """
    if not command.strip() or _has_unquoted_shell_syntax(command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or "/" in tokens[0]:
        return None
    program, words = tokens[0], tokens[1:]
    if program == "find":
        return _parse_find(words)
    options: list[str] = []
    operands: list[str] = []
    for index, word in enumerate(words):
        if word == "--":
            operands += words[index + 1 :]
            break
        if word.startswith("=") or word == "-":
            return None  # zsh expands =name to a program's path; `-` reads stdin, which never closes
        if word.startswith("-"):
            if operands:
                return None  # BSD programs would open a later dash word as a file
            options.append(word)
        else:
            operands.append(word)
    return Parsed(program, options, operands)


def _has_unquoted_shell_syntax(command: str) -> bool:
    """True when a character the shell would act on appears outside the quotes that neutralise it."""
    quote = ""
    for ch in command:
        if quote == "'":
            if ch == "'":
                quote = ""
            elif ch == "\r":
                return True  # shlex and the shell disagree on where the word ends
            continue
        if quote == '"':
            if ch == '"':
                quote = ""
            elif ch in DOUBLE_QUOTED_SYNTAX:
                return True
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "\\":
            return True  # an unquoted backslash escapes the next character; refuse rather than model it
        elif ch in SHELL_SYNTAX:
            return True
    return bool(quote)  # an unclosed quote cannot be judged


def _parse_find(words: list[str]) -> Parsed | None:
    """Parse a find command's leading options, paths and expression; None if any part is unsafe."""
    options: list[str] = []
    operands: list[str] = []
    index = 0
    while index < len(words) and words[index].startswith("-"):
        cluster = words[index][1:]
        if not cluster or any(letter not in FIND_LEADING_LETTERS for letter in cluster):
            return None
        options.append(words[index])
        index += 1
    while index < len(words):
        word = words[index]
        index += 1
        if word.startswith("="):
            return None  # zsh expands =name to a program's path
        if not word.startswith("-"):
            operands.append(word)
            continue
        unsafe = word.startswith(FIND_UNSAFE_PREFIXES) or word.endswith("newer") or word in FIND_UNSAFE_WORDS
        if unsafe or "/" in word:
            return None
        options.append(word)
    return Parsed("find", options, operands)


def _options_allowed(parsed: Parsed) -> bool:
    if parsed.program in ("echo", "find"):  # find was vetted while parsing
        return True
    spec = PROGRAMS.get(parsed.program)
    if spec is None or not all(_option_allowed(spec, word) for word in parsed.options):
        return False
    return not _reads_stdin(parsed)


def _reads_stdin(parsed: Parsed) -> bool:
    """True when the program would wait on standard input, which agy's pseudo-terminal never closes.

    cat, head, tail, wc and stat read stdin with no file operand. grep does so
    with no file operand: its first operand is the pattern unless one came
    through -e or --regexp, and a recursive search without a path searches
    cwd. A count given as the next word (head -n 5) is an operand to the
    policy but is consumed by the program, so it does not count as a file.
    """
    if parsed.program in ("ls", "pwd"):
        return False
    spec = PROGRAMS[parsed.program]
    consumed = sum(1 for o in parsed.options if not o.startswith("--") and o[-1] in spec.numeric)
    files = len(parsed.operands) - consumed
    if parsed.program != "grep":
        return files < 1
    options = parsed.options
    recursive = any(o == "--recursive" or (not o.startswith("--") and "r" in o) for o in options)
    if recursive:
        return False
    pattern_given = any(o.startswith("--regexp=") or (not o.startswith("--") and "e" in o) for o in options)
    return files < (1 if pattern_given else 2)


def _option_allowed(spec: Program, token: str) -> bool:
    if "/" in token:
        return False
    if token.startswith("--"):
        # A long option that takes a value must carry it after `=`; without
        # one the program would consume the next word (--regexp -- -fsecret).
        # Options listed in both sets take an optional value (--color).
        name, _, value = token.partition("=")
        return (name in spec.long_value and bool(value)) or (name in spec.long and not value)
    letters = token[1:]
    if letters.isdigit():  # head -5, grep -3
        return spec.bare_number
    for index, letter in enumerate(letters):
        attached = letters[index + 1 :]
        if letter in spec.numeric:
            # A separate count (head -n 5) is an operand to the policy and is
            # path-checked, which is harmless; the program reads it as a count
            # and rejects anything else, so no word can hide behind it.
            return attached == "" or _is_count(attached)
        if letter in spec.free:
            # A pattern or format must be attached (-efoo): a separate word
            # would be consumed whatever it is, so `-e -- -fsecret` could
            # smuggle options past the `--` rule.
            return attached != ""
        if letter not in spec.flags:
            return False
    return True


COUNT_UNITS = set("bkKmMgG")


def _is_count(value: str) -> bool:
    """A count as head, tail and grep accept it: digits, optional sign, optional unit (-n+20, -c1K).

    Only the units head and tail define are accepted, so a trailing letter
    cannot be a smuggled flag such as the L of `-w1L`.
    """
    body = value.lstrip("+-")
    if body[-1:] in COUNT_UNITS:
        body = body[:-1]
    return body.isdigit()


def _operands_inside(parsed: Parsed, cwd: str, roots: list[str]) -> bool:
    """True when every operand resolves inside one of roots.

    Operands that are patterns rather than paths resolve inside cwd, which is
    itself inside the working copy, so they pass; an absolute or ../ operand,
    or a symlink whose target leaves the roots, does not.
    """
    if parsed.program == "echo":
        return True
    for token in parsed.operands:
        path = token if os.path.isabs(token) else os.path.join(cwd, token)
        if not any(_inside(path, root) for root in roots):
            return False
    return True


def is_read_only_command(command: str) -> bool:
    parsed = parse_command(command)
    return parsed is not None and _options_allowed(parsed)


def command_paths_inside(command: str, cwd: str, roots: list[str]) -> bool:
    """True when the command's options are allowed and its operands stay inside roots.

    Both checks run, so a path attached to an option (-f/etc/x) fails here too.
    """
    parsed = parse_command(command)
    return parsed is not None and _options_allowed(parsed) and _operands_inside(parsed, cwd, roots)


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
        if _controls_job(target, worktree):
            return Decision("deny", "That file controls the job and cannot be changed.")
        return Decision("allow", "write inside the job's working copy")

    if name in COMMAND_TOOLS:
        command = str(args.get("CommandLine", ""))
        cwd = args.get("Cwd")
        if isinstance(cwd, str) and cwd and worktree and not _inside(cwd, worktree):
            return Decision("deny", "Commands must run inside the job's working copy.")
        parsed = parse_command(command)
        if parsed and _options_allowed(parsed) and _operands_inside(parsed, cwd or worktree, read_roots):
            return Decision("allow", "read-only command")
        return Decision(
            "deny",
            "Queued for Claude: this command was not run. Claude may run it and send you "
            "the output in a later turn. Continue with what you can do without it, and list "
            "any command you still need at the end of your reply.",
            queued_command=command,
        )

    return Decision("deny", f"The {name} tool is not available in agy plugin jobs.")
