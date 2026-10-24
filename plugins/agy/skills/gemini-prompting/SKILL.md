---
# Adapted from openai/codex-plugin-cc (Apache-2.0) and modified for agy. See NOTICE.
name: gemini-prompting
description: Guidance for turning a user's request into a clear prompt for agy (Gemini 4 Argon) in the agy-rescue subagent
user-invocable: false
---

# Prompting agy

Use this skill only to tighten the request text that `agy-rescue` forwards. Do not inspect the repository or solve the task.

The companion already wraps the request in a template that tells agy it works in a separate copy, that commands are queued, and how to report. Add only what the request is missing:

- **One task per job.** Split unrelated asks.
- **Done condition.** Say what finished looks like: "the failing test in tests/test_upload.py passes", not "fix uploads".
- **Scope.** Name the files or directories in play when the user did, and say what must not change.
- **Evidence.** For diagnosis, ask agy to separate what it observed in files from what it infers.
- **Exact copies.** If the user wants text copied from a file, say "copy it from <path> character for character, or say you could not open it".

Keep the user's wording where it is already clear. Do not raise the model or add effort instructions unless the user asked.

## Gotchas

- agy cannot run tests itself: a test command is queued for Claude and costs a resume round. If the done condition needs a test run, name the exact command so it is queued once, not guessed at several times.
- Long pasted context costs tokens on every resume, because a resumed conversation carries its history. Point agy at files instead of pasting them.
- "Verbatim" in a prompt does not make agy copy; it has written invented text under that label. The result-handling skill's verification step is what catches it.
