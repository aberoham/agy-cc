#!/usr/bin/env python3
"""A stand-in for the agy binary, driven by FAKE_AGY_SCENARIO.

It copies the agy 1.4.7 behaviour the plugin depends on: `-p` swallowing the
next flag is an error, the prompt arrives on stdin as stream-json, output is
stream-json events, tool calls go through the workspace PreToolUse hook, and a
stalled model produces no events while its log records an idle timeout.
Every invocation's argv and stdin are appended to FAKE_AGY_CALLS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def arg_value(argv: list[str], flag: str) -> str | None:
    if flag in argv:
        index = argv.index(flag)
        return argv[index + 1] if index + 1 < len(argv) else None
    return None


def check_print_flag(argv: list[str]) -> None:
    for i, arg in enumerate(argv):
        if arg in ("-p", "--print", "--prompt") and i + 1 < len(argv) and argv[i + 1].startswith("-"):
            sys.stderr.write(
                f'Error: -p took "{argv[i + 1]}" as its prompt, so the intended prompt was left as '
                "an argument and ignored.\n"
            )
            sys.exit(2)


def run_hook(cwd: Path, conversation: str, brain: Path, name: str, args: dict) -> dict:
    hooks_file = cwd / ".agents" / "hooks.json"
    if not hooks_file.exists():
        return {"decision": "allow"}
    payload = {
        "toolCall": {"name": name, "args": args},
        "stepIdx": 2,
        "conversationId": conversation,
        "workspacePaths": [str(cwd)],
        "artifactDirectoryPath": str(brain),
        "modelName": "fake",
    }
    for config in json.loads(hooks_file.read_text()).values():
        for matcher in config.get("PreToolUse", []):
            for hook in matcher.get("hooks", []):
                out = subprocess.run(
                    hook["command"], shell=True, input=json.dumps(payload), capture_output=True, text=True
                )
                decision = json.loads(out.stdout)
                if decision.get("decision") == "deny":
                    return decision
    return {"decision": "allow"}


def tool_step(conversation: str, index: int, name: str, params: dict, decision: dict, output: str = "ok") -> None:
    base = {"conversation_id": conversation, "step_index": index, "step_type": "tool", "tool_name": name}
    emit(
        {
            "event": "step_update",
            "step_update": dict(base, state="ACTIVE", tool_info={"name": name, "parameters": params}),
        }
    )
    if decision.get("decision") == "deny":
        error = {"type": "TOOL_ERROR", "message": f"tool call denied by pre-tool hook: {decision.get('reason')}"}
        emit(
            {
                "event": "step_update",
                "step_update": dict(
                    base, state="ERROR", tool_info={"name": name, "parameters": params, "error": error}
                ),
            }
        )
    else:
        emit(
            {
                "event": "step_update",
                "step_update": dict(
                    base, state="DONE", tool_info={"name": name, "parameters": params, "output": output}
                ),
            }
        )


def result(conversation: str, response: str, status: str = "SUCCESS", **extra) -> None:
    usage = {
        "input_tokens": 100,
        "output_tokens": 20,
        "thinking_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 120,
    }
    emit(
        {
            "event": "result",
            "result": dict(
                {
                    "conversation_id": conversation,
                    "status": status,
                    "response": response,
                    "duration_seconds": 0.1,
                    "num_turns": 1,
                    "usage": usage,
                },
                **extra,
            ),
        }
    )


def main() -> int:
    argv = sys.argv[1:]
    calls = os.environ.get("FAKE_AGY_CALLS")
    if argv == ["--version"]:
        print("1.4.7")
        return 0
    check_print_flag(argv)
    stdin = sys.stdin.read()
    if calls:
        with open(calls, "a") as handle:
            handle.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")
    if "--dangerously-skip-permissions" in argv:
        sys.stderr.write("fake agy: the plugin must never pass --dangerously-skip-permissions\n")
        return 3
    message = json.loads(stdin.splitlines()[0])["message"]["content"]
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "reply")
    pid_file = os.environ.get("FAKE_AGY_PIDFILE")
    if pid_file:
        with open(pid_file, "a") as handle:
            handle.write(f"{os.getpid()}\n")
    if scenario.startswith("replay:"):
        # Replay a stream recorded from the real agy, unchanged.
        sys.stdout.write(Path(scenario[len("replay:") :]).read_text())
        return 0
    model = arg_value(argv, "--model") or "default"
    conversation = arg_value(argv, "--conversation") or str(uuid.uuid4())
    log_file = arg_value(argv, "--log-file")
    cwd = Path.cwd()
    brain = Path.home() / ".gemini" / "antigravity-cli" / "brain" / conversation
    emit(
        {
            "event": "init",
            "conversation_id": conversation,
            "init": {
                "model": model,
                "cwd": str(cwd),
                "tools": ["read_file", "write_file", "run_command"],
                "permission_mode": "request-review",
            },
        }
    )
    emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": conversation,
                "step_index": 0,
                "state": "DONE",
                "step_type": "user_input",
            },
        }
    )

    if scenario == "escape":
        # Write past the hook, as a buggy or hostile agy might; only the
        # operating-system sandbox can stop this.
        target = Path(os.environ["FAKE_AGY_ESCAPE_PATH"])
        try:
            target.write_text("escaped\n")
            outcome = "written"
        except OSError as exc:
            outcome = f"blocked: {exc.strerror}"
        result(conversation, outcome)
        return 0

    if scenario == "auth":
        sys.stderr.write("error: authentication required\n")
        return 1

    stall_models = os.environ.get("FAKE_AGY_STALL_MODEL", "gemini-4-argon-high").split(",")
    if scenario.startswith("stall") and model in stall_models:
        if scenario == "stall-usage":
            usage = {
                "input_tokens": 40,
                "output_tokens": 10,
                "thinking_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 50,
            }
            emit(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": conversation,
                        "step_index": 1,
                        "state": "DONE",
                        "step_type": "agent_response",
                        "usage": usage,
                    },
                }
            )
        if scenario == "stall-log" and log_file:
            with open(log_file, "a") as log:
                log.write(
                    "Run: attempt 1 failed (request failed: stream idle timeout (5m0s): context deadline exceeded)\n"
                )
        time.sleep(3600)
        return 0

    if scenario == "review-empty-structured":
        result(conversation, "", structured_output={})
        return 0

    if scenario == "review":
        review = {
            "verdict": "needs-attention",
            "summary": "One bug.",
            "findings": [
                {
                    "severity": "high",
                    "title": "Off by one",
                    "body": "Skips the first item.",
                    "file": "stats.py",
                    "line_start": 3,
                    "line_end": 3,
                    "confidence": 0.9,
                    "recommendation": "Start at 0.",
                }
            ],
            "next_steps": ["Fix it."],
        }
        result(conversation, json.dumps(review), structured_output=review)
        return 0

    if scenario == "plan":
        brain.mkdir(parents=True, exist_ok=True)
        plan = brain / "implementation_plan.md"
        decision = run_hook(cwd, conversation, brain, "write_file", {"Path": str(plan), "Content": "# Plan\n"})
        if decision.get("decision") != "deny":
            plan.write_text("# Plan\n\n1. Do the thing.\n")
        tool_step(conversation, 1, "write_file", {"Path": str(plan)}, decision)
        result(conversation, "I wrote the plan and am waiting for approval.")
        return 0

    if scenario == "task":
        if "--conversation" in argv and "output is" in message:
            result(conversation, "Tests pass. Done.")
            return 0
        target = cwd / "stats.py"
        decision = run_hook(cwd, conversation, brain, "write_file", {"Path": str(target), "Content": "fixed\n"})
        if decision.get("decision") != "deny":
            target.write_text("fixed\n")
        tool_step(conversation, 1, "write_file", {"Path": str(target)}, decision)
        outside = cwd.parent / "escaped.txt"
        decision = run_hook(cwd, conversation, brain, "write_file", {"Path": str(outside), "Content": "x"})
        if decision.get("decision") != "deny":
            outside.write_text("x")
        tool_step(conversation, 2, "write_file", {"Path": str(outside)}, decision)
        decision = run_hook(cwd, conversation, brain, "run_command", {"CommandLine": "make test", "Cwd": str(cwd)})
        tool_step(conversation, 3, "run_command", {"CommandLine": "make test"}, decision)
        result(conversation, "Fixed stats.py. I still need `make test`.")
        return 0

    if scenario == "permission-denied":
        params = {"CommandLine": "npm test"}
        base = {"conversation_id": conversation, "step_index": 1, "step_type": "tool", "tool_name": "run_command"}
        error = {
            "type": "TOOL_ERROR",
            "message": 'permission check failed for command "npm test": user denied permission to run command',
        }
        emit(
            {
                "event": "step_update",
                "step_update": dict(
                    base, state="ERROR", tool_info={"name": "run_command", "parameters": params, "error": error}
                ),
            }
        )
        result(conversation, "", denied_actions=[{"action": "command", "display_name": "RunCommand"}])
        return 0

    result(conversation, f"reply from {model}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
