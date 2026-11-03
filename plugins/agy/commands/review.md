---
# Adapted from openai/codex-plugin-cc (Apache-2.0) and modified for agy. See NOTICE.
description: Run an agy (Gemini) code review of local git changes, read-only
argument-hint: '[--wait|--background] [--base <ref>] [--scope auto|working-tree|branch] [--model high|medium|low]'
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash(python3:*), Bash(git:*), AskUserQuestion
---

Run an agy review through the agy companion.

Raw slash-command arguments:
`$ARGUMENTS`

Core constraint:
- This command is review-only. agy works on a separate copy of the repository and cannot change the user's files.
- Do not fix issues, apply patches, or suggest that you are about to make changes.

Execution mode:
- If the arguments include `--wait`, run in the foreground. If they include `--background`, run in the background. Do not ask in either case.
- Otherwise estimate the size first: `git status --short --untracked-files=all` and `git diff --shortstat` for working-tree changes, or `git diff --shortstat <base>...HEAD` for a branch.
- Recommend waiting only for a tiny change (one or two files). Otherwise recommend background. Use `AskUserQuestion` once with `Wait for results` and `Run in background`, the recommended one first with `(Recommended)`.

Foreground:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" review --raw "$ARGUMENTS"
```
Then follow the `agy-result-handling` skill: present the findings, verifying agy's quotes and file:line claims before you relay them.

Background:
```typescript
Bash({ command: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" review --wait --wait-seconds 3600 --raw "<ARGUMENTS without --background>"`, description: "agy review", run_in_background: true })
```
Remove `--background` from the arguments before passing them (the companion would otherwise detach and return at once). Claude Code keeps the companion running as a background shell, so the user can watch it in the shell details view, and you are notified when it exits. Do not poll the output file. Tell the user: "agy review started in the background. Check `/agy:status` for progress." When the notification arrives, read the output file and follow the `agy-result-handling` skill.
