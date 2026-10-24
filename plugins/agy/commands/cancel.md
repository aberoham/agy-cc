---
# Adapted from openai/codex-plugin-cc (Apache-2.0) and modified for agy. See NOTICE.
description: Cancel a running agy job in this repository
argument-hint: '[job-id]'
disable-model-invocation: true
allowed-tools: Bash(python3:*), Read, Grep
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" cancel --raw "$ARGUMENTS"`

Report what the command printed.
