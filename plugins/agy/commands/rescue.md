---
# Adapted from openai/codex-plugin-cc (Apache-2.0) and modified for agy. See NOTICE.
description: Hand a debugging, implementation or second-opinion task to agy (Gemini) through the agy-rescue subagent
argument-hint: "[--background|--wait] [--resume|--fresh] [--read-only] [--model high|medium|low] [what agy should investigate or do]"
allowed-tools: Bash(python3:*), AskUserQuestion, Agent
---

Invoke the `agy:agy-rescue` subagent with the `Agent` tool (`subagent_type: "agy:agy-rescue"`), forwarding the raw user request as the prompt. It is a subagent, not a skill: do not call `Skill(agy:agy-rescue)` or `Skill(agy:rescue)`.

Raw user request:
$ARGUMENTS

Execution mode:
- `--background` runs the subagent in the background; `--wait` or neither runs it in the foreground.
- `--resume` means continue the last agy task in this repository; `--fresh` means start a new one. Without either, if the request is clearly a follow-up ("continue", "keep going", "apply the top fix"), add `--resume`; otherwise start fresh.
- Keep `--model` and `--read-only` in the forwarded request.

If no request was given, ask what agy should investigate or fix.

Return the subagent's output to the user as it is. If the job ends in `needs-approval`, agy asked for shell commands it could not run. Follow the "Queued commands" section of the `agy-result-handling` skill.
