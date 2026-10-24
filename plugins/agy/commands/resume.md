---
description: Send the output of commands agy queued back to its job and let it continue
argument-hint: '<job-id> [results-file] [--note <text>]'
disable-model-invocation: true
allowed-tools: Bash(python3:*), Bash, Read, Write
---

Resume an agy job that ended in `needs-approval`.

Raw arguments:
`$ARGUMENTS`

If no results file was given:
1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" status <job-id>` to list the queued commands and the job's working copy.
2. Run each queued command you would run yourself in that working copy, using your own Bash tool, one command per call. Your normal permission checks apply. Skip any command you would not run, and say why in the note.
3. Write the commands and their output (including exit status) to a file in your scratchpad.

Then run:
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" resume --background <job-id> <results-file> --note "<what you skipped and why, if anything>"
```
Tell the user the job is resuming and to check `/agy:status <job-id>`.
