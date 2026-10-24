---
# Adapted from openai/codex-plugin-cc (Apache-2.0) and modified for agy. See NOTICE.
description: Show active and recent agy jobs for this repository
argument-hint: '[job-id] [--all]'
disable-model-invocation: true
allowed-tools: Bash(python3:*), Read, Grep
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" status --raw "$ARGUMENTS"`

If no job id was given, show the table as it is: one row per job. If a job id was given, show the full detail, including the rounds table (each round's duration, Claude's wait, tokens, stalls and queued commands). Do not summarise.
