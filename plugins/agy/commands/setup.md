---
# Adapted from openai/codex-plugin-cc (Apache-2.0) and modified for agy. See NOTICE.
description: Check whether agy is installed, signed in and ready for the agy plugin
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py" setup`

Present the output, including the isolation line. If container mode is not available, pass on the reason as it is; host mode still works. If agy is missing, say: install the Antigravity CLI (https://antigravity.google/docs/cli), or set `AGY_BIN` to its path. If it is not signed in, tell the user to open a terminal window, run `agy`, and complete the sign-in there. The sign-in is interactive, so `!` in Claude Code cannot do it. Do not try other sign-in methods.
