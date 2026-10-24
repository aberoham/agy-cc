---
# Adapted from openai/codex-plugin-cc (Apache-2.0) and modified for agy. See NOTICE.
name: agy-cli-runtime
description: Internal contract for calling the agy companion script from the agy-rescue subagent
user-invocable: false
---

# agy runtime contract

Use only inside the `agy:agy-rescue` subagent.

Helper: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" task [--background] [--model high|medium|low] [--read-only] [--resume-last] "<request>"`

- One `task` call per handoff. Do not call `review`, `plan`, `status`, `result`, `cancel`, `resume` or `apply`.
- `--background` returns a job id at once. Without it the companion waits up to 100 seconds, then reports that the job is still running; check `/agy:status` after that.
- A task job edits a detached git worktree under the plugin's state directory. The user's checkout changes only when they run `/agy:apply`.
- `--resume-last` continues the newest finished task this Claude session started in this repository, in the same working copy and agy conversation. With `--read-only` that round cannot edit, but the task's earlier edits stay applicable.

## Gotchas

- Never call `agy` directly. In some shells `agy` is a shell function that the Bash tool cannot see, and the companion is what adds the worktree, the tool policy hook, the write sandbox and stall recovery.
- Never pass `--dangerously-skip-permissions`; the companion refuses it.
- Shell commands that are not read-only are not run by agy: the job ends as `needs-approval` with the commands listed. That is expected, not a failure.
