"""Text output for status, results and job details."""

from __future__ import annotations

import os
from typing import Any

from .state import now_iso, parse_iso

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def elapsed(job: dict[str, Any]) -> str:
    start = parse_iso(job.get("created_at"))
    end = parse_iso(job.get("completed_at")) if job.get("status") not in ("running", "resuming", "queued") else None
    end = end or parse_iso(now_iso())
    if not start or not end:
        return "-"
    seconds = int((end - start).total_seconds())
    minutes, seconds = divmod(max(seconds, 0), 60)
    return f"{minutes}m{seconds:02d}s"


def _stall_count(job: dict[str, Any]) -> int:
    rounds = job.get("rounds") or []
    return sum(len(r.get("stalls") or []) for r in rounds) + len(job.get("current_stalls") or [])


def status_table(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "No agy jobs for this repository."
    header = "| Job | Kind | Status | Phase | Model | Elapsed | Rounds | Stalls | Queued commands |\n"
    header += "|---|---|---|---|---|---|---|---|---|\n"
    rows = []
    for job in jobs:
        rows.append(
            "| {id} | {kind} | {status} | {phase} | {model} | {elapsed} | {rounds} | {stalls} | {queued} |".format(
                id=job.get("id"),
                kind=job.get("kind"),
                status=job.get("status"),
                phase=str(job.get("phase") or "").replace("|", "/"),
                model=job.get("model"),
                elapsed=elapsed(job),
                rounds=len(job.get("rounds") or []),
                stalls=_stall_count(job),
                queued=len(job.get("queued_commands") or []),
            )
        )
    return header + "\n".join(rows)


def rounds_table(job: dict[str, Any]) -> str:
    rounds = job.get("rounds") or []
    if not rounds:
        return ""
    lines = [
        "| Round | Kind | Model | Duration | Wait for Claude | Tokens | Stalls | Queued |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(rounds, 1):
        usage = item.get("usage") or {}
        wait = item.get("approval_wait_s")
        lines.append(
            f"| {index} | {item.get('kind')} | {item.get('final_model')} | {item.get('duration_s')} s | "
            f"{'-' if wait is None else str(wait) + ' s'} | {usage.get('total_tokens', '-')} | "
            f"{len(item.get('stalls') or [])} | {len(item.get('queued_commands') or [])} |"
        )
    return "\n".join(lines)


def job_detail(job: dict[str, Any]) -> str:
    lines = [
        f"Job: {job.get('id')}",
        f"Kind: {job.get('kind')}",
        f"Status: {job.get('status')} ({job.get('phase')})",
        f"Model: {job.get('model')}",
        f"Elapsed: {elapsed(job)}",
        f"Conversation: {job.get('conversation_id') or '-'}",
        f"Working copy: {job.get('worktree')}",
    ]
    progress = job.get("progress") or {}
    if progress:
        lines.append(
            f"Progress: step {progress.get('steps')}, {progress.get('tools')} tool calls, "
            f"last tool {progress.get('last_tool') or '-'}"
        )
    for stall in job.get("current_stalls") or []:
        lines.append(
            f"Stall: {stall.get('model')} at {stall.get('at')}: {stall.get('reason')}; "
            f"resumed on {stall.get('resumed_on')}"
        )
    if job.get("error"):
        lines.append(f"Error: {job['error']}")
    table = rounds_table(job)
    if table:
        lines += ["", table]
    lines += ["", next_steps(job)]
    return "\n".join(lines)


def next_steps(job: dict[str, Any]) -> str:
    job_id = job.get("id")
    status = job.get("status")
    if status in ("queued", "running", "resuming"):
        return f"Next: /agy:status {job_id} for progress, /agy:cancel {job_id} to stop."
    if status == "needs-approval":
        commands = "\n".join(f"- `{c}`" for c in job.get("queued_commands") or [])
        return (
            "agy asked for these commands, which it was not allowed to run:\n"
            f"{commands}\n"
            f"Run the ones you judge safe from the working copy {job.get('worktree')}, save their output to a file, "
            f"then `/agy:resume {job_id} <file>`. Or read the result as it is with /agy:result {job_id}."
        )
    if status == "completed" and job.get("writable"):
        return (
            f"Next: /agy:result {job_id} to read the report and changes; "
            f"/agy:apply {job_id} to apply them to your checkout."
        )
    return f"Next: /agy:result {job_id}"


def _review_text(review: dict[str, Any]) -> str:
    lines = [f"Verdict: {review.get('verdict')}", "", str(review.get("summary", "")).strip(), ""]
    findings = sorted(review.get("findings") or [], key=lambda f: SEVERITY_ORDER.get(str(f.get("severity")), 9))
    if not findings:
        lines.append("No findings.")
    for item in findings:
        lines += [
            f"[{item.get('severity')}] {item.get('title')}",
            f"  {item.get('file')}:{item.get('line_start')}-{item.get('line_end')} "
            f"(confidence {item.get('confidence')})",
            f"  {str(item.get('body', '')).strip()}",
            f"  Recommendation: {str(item.get('recommendation', '')).strip()}",
            "",
        ]
    steps = review.get("next_steps") or []
    if steps:
        lines.append("Next steps:")
        lines += [f"- {s}" for s in steps]
    return "\n".join(lines).rstrip()


def result_text(job: dict[str, Any], stored: dict[str, Any] | None) -> str:
    """Render a result, with paths in the job's working copy mapped back to the repository."""
    text = _result_text(job, stored)
    worktree, root = job.get("worktree"), job.get("repo_root")
    # A read-only job's working copy matches the repository, so its paths can
    # point at the user's files. A task's edits exist only in its working copy
    # until applied, so its paths stay as they are.
    if worktree and root and not job.get("writable"):
        for variant in {worktree, os.path.realpath(worktree)}:
            text = text.replace(variant, root)
    return text


def _result_text(job: dict[str, Any], stored: dict[str, Any] | None) -> str:
    head = f"Job {job.get('id')} ({job.get('kind')}, {job.get('status')}, model {job.get('model')})"
    if not stored:
        return f"{head}\nNo result yet.\n{next_steps(job)}"
    parts = [head, ""]
    if job.get("error"):
        parts += [f"Error: {job['error']}", ""]
        if stored.get("stderr_tail"):
            parts += ["agy stderr (last lines):", stored["stderr_tail"].strip(), ""]
    kind = job.get("kind")
    if kind in ("review", "adversarial-review") and stored.get("review"):
        parts.append(_review_text(stored["review"]))
    elif kind == "plan" and stored.get("plan_markdown"):
        parts += [f"Plan file: {stored.get('plan_path')}", "", stored["plan_markdown"].strip()]
        if stored.get("response"):
            parts += ["", "agy's reply:", stored["response"].strip()]
    else:
        if stored.get("parse_error"):
            parts += [f"Could not read a structured review: {stored['parse_error']}. Raw reply:", ""]
        parts.append(str(stored.get("response") or "(empty reply)").strip())
    if stored.get("changed_files"):
        parts += ["", "Files changed in the job's working copy:", stored["changed_files"]]
    elif job.get("writable") and not job.get("error"):
        parts += ["", "agy changed no files."]
    parts += ["", next_steps(job)]
    return "\n".join(parts)
