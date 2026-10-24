---
# Adapted from openai/codex-plugin-cc (Apache-2.0) and modified for agy. See NOTICE.
description: Show the stored output of a finished agy job
argument-hint: '[job-id]'
disable-model-invocation: true
allowed-tools: Bash(python3:*), Read, Grep
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" result --raw "$ARGUMENTS"`

Present the output following the `agy-result-handling` skill. Keep verdicts, findings, file paths, line numbers, errors and the follow-up commands. Verify quotes and file:line claims before relaying them.
