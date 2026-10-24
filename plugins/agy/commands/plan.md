---
description: Ask agy (Gemini) to write an implementation plan without changing anything
argument-hint: '[--wait|--background] [--model high|medium|low] <what the plan is for>'
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash(python3:*), AskUserQuestion
---

Ask agy for an implementation plan. agy runs read-only on a separate copy of the repository and writes the plan as its own artifact.

Raw slash-command arguments:
`$ARGUMENTS`

Rules:
- If no request text was given, ask what the plan should cover and stop.
- Plans usually take minutes. Unless `--wait` is present, run with `--background` and tell the user to check `/agy:status`.
- Do not start implementing the plan. Present it and stop.

Run:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" plan --background --raw "$ARGUMENTS"
```
(Drop `--background` when the user passed `--wait`.)

When the plan is read back with `/agy:result`, follow the `agy-result-handling` skill. Plans are where agy has invented file contents before: check every file, function and quoted text it names against the repository, and say which you checked.
