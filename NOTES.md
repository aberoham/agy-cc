# agy behaviour notes and open questions

Working notes for the plugin design. Each fact below was observed on Abe's Mac
on 24 October 2026 with agy 1.4.7, unless marked otherwise.

## Observed

- `/usr/local/bin/agy` is a Jamf-installed Bash wrapper around
  `~/.local/share/agy/agy`. If the OAuth token file is missing, it puts the
  organisation's workforce sign-in provider string on the clipboard and prints a hint.
- `-p` takes the next argument as the prompt, so `-p --model x` fails. agy
  says so: `-p took "--input-format" as its prompt`.
- Prompts can be sent on stdin without shell quoting:
  `--input-format stream-json --output-format stream-json --print=` with one
  NDJSON line per turn, shaped
  `{"event":"user","message":{"role":"user","content":"..."}}`.
  Other event names (`user_message`, `user_input`, `message`, `prompt`) are
  ignored with `warning: ignoring unsupported stream input message event`.
  A line with no `event` field fails with
  `stream input message is missing the "event" field`.
- Output events: `init` (conversation_id, model, cwd, tools), `step_update`
  (step_index, state ACTIVE|DONE, step_type user_input|agent_response|tool,
  tool_name, tool_info.parameters, tool_info.output, text_delta, usage), and
  `result` (status SUCCESS|ERROR, response, error, duration_seconds,
  num_turns, usage).
- `--mode accept-edits` without `--dangerously-skip-permissions` ran
  `run_command` (`git status --short`) and `write_file` with no prompt and
  no stall.
- `--mode plan` without the skip flag: in two probes Argon (low) declined to
  write workspace files or run `touch`, even when told plan mode was
  approved. Instead it wrote `implementation_plan.md` under
  `~/.gemini/antigravity-cli/brain/<id>/` and said it was waiting for
  approval. The brain `<id>` is not the `conversation_id`. The `write_file`
  tool itself worked in plan mode, on the brain path.
- So the plan text for a plan command is in the brain file. The `response`
  field holds only a short summary. The runner must take the path from the
  `write_file` step_update.
- `--conversation <id>` with stdin stream-json resumed a low-model
  conversation on `gemini-4-argon-medium` and kept its context. The one-word
  reply took 184 seconds.
- `--json-schema` exists (string or file path; for stream-json it applies to
  the final result only). It may replace prompt-only JSON instructions for
  reviews. Not yet tested.
- `--sandbox` exists ("terminal restrictions"). Not yet tested.

- Headless permission handling: a command not in `permissions.allow` of
  `~/.gemini/antigravity-cli/settings.json` is auto-denied, not prompted.
  The tool step_update has `state: "ERROR"` and
  `error.message: permission check failed for command "...": user denied ...`.
  The `result` carries `denied_actions: [{"action":"command",...}]`, and
  stderr says to add `command(<target>)` to `permissions.allow`. No hang.
  Abe's settings allow `git status` and others, which is why the earlier
  accept-edits probe ran without a prompt.
- Runtimes: agy is a Go binary (Mach-O arm64), and the Claude Code native
  install is a compiled Mach-O. Neither exposes a script runtime.
  `BUN_BE_BUN=1 claude <file>` does not work: it starts a Claude session with
  the file name as the prompt. `/usr/bin/python3` (3.9.6) ships with the
  Command Line Tools, which also supply git on macOS. `/usr/bin/jq`,
  `/usr/bin/perl` and `/usr/bin/sandbox-exec` are present on macOS 26.

- **PreToolUse hooks work in headless mode and enforce at the tool layer.**
  A workspace `.agents/hooks.json` with a `PreToolUse` matcher `.*` ran a
  command hook for every tool call. Returning `{"decision":"deny"}` for
  `run_command` produced a tool step with `state: "ERROR"` and
  `tool call denied by pre-tool hook: <reason>`. The CLI's tool names differ
  from the docs (https://antigravity.google/docs/hooks): the CLI sent
  `write_file` with args `Path`, `Content`, `Overwrite`, not
  `write_to_file`/`TargetFile`. The hook input also carries
  `lastUserInput`, `artifactDirectoryPath` and `conversationId`; in this run
  the brain directory id equalled the conversation id.
- Docs list hook decisions `allow`, `deny`, `ask`, `force_ask`,
  `deny_unless_prior_grant`, and a per-handler `timeout` in seconds
  (default 30). A hook could hold a tool call while the Claude session
  decides, which would give approval during the run, without a resume.

- Hook deny of `write_file` confirmed: no file was created, and the step had
  `state: "ERROR"`.
- **A hook can hold a tool call.** A handler with `timeout: 600` slept 90
  seconds and then allowed `run_command`. agy waited, the tool step stayed
  `ACTIVE` with no stream events for 90 seconds, then finished `DONE` with
  `duration_seconds: 90.2`, and the turn completed normally. So approval can
  happen during the run. Status must read pending approvals from the hook's
  own state, because the stream is silent while a hook holds a call; the
  stall detector must not count that silence.

- **A hook cannot allow a command in headless mode.** A hook returning
  `{"decision":"allow"}`, and again with
  `permissionOverrides: ["command(<exact command>)"]`, did not stop the
  headless auto-deny of a `touch` command that was not in
  `permissions.allow`. Hooks can deny or delay; only the global allow-list
  or `--dangerously-skip-permissions` lets an unlisted command run. Abe
  chose the rerun-and-resume design on 24 October 2026: the job queues the
  command, Claude runs it with its own Bash tool under auto mode, and the
  output goes back to agy with `--conversation`.
- Tool names differ by surface: `init.tools` lists `write_to_file` and
  `view_file`; step_update and hook payloads use `write_file` and
  `read_file`. The policy accepts both.

- **Release criterion (Abe, 24 October 2026): rerun-and-resume must prove
  efficient in real use.** Each job records every approval round: commands
  queued, time until Claude supplied results, resume duration and tokens.
  Status shows them. Review the numbers from real rescue runs before
  publishing.

- First end-to-end runs on gemini-4-argon-low (24 October 2026): review 15 s;
  plan job wrote its plan to the brain directory; task job edited only its
  worktree and queued `python3 -m unittest`. Claude ran it and resumed:
  round 1 took 41 s and 17,380 tokens; Claude took 22.5 s; the resume took 19 s
  and 21,354 tokens. Under `sandbox-exec`, agy needs write access to
  `/dev/ptmx` to run any command.

- Live adversarial review of the plugin's own branch (24 October 2026,
  `--model high`): `gemini-4-argon-high` stalled with "stream idle timeout"
  after six minutes and 14 tool calls; the runner resumed the conversation on
  `gemini-4-argon-medium`, which worked for 18 minutes and then reported
  `API error: stream idle timeout (5m0s)` while still delivering the whole
  review as text (no `structured_output` on an error result). 282k tokens
  in all. The review was real: three policy findings, one a genuine
  symlink-follow bypass (`ls -w1L` on BSD ls). So a result with an error
  and a response is parsed as a review anyway.
- In a Claude Code session that has another plugin's hooks active,
  `CLAUDE_PLUGIN_DATA` can be set in the Bash tool's environment to that
  plugin's data directory; the job state then lands there. Harmless, but
  `/agy:status` from a plain terminal will not see those jobs.

## Container mode, verified live (24 October 2026)

- Colima (virtiofs) shares the home directory and not `/var/folders`: a
  `touch` through a bind mount of a `mktemp -d` directory failed with
  "Permission denied", and the same under `~/.claude` worked. So the state
  root must be under the home directory for container jobs; the companion
  probes this with a `touch` from inside the container.
- With `--user 501:20` the files the host created appear owned by `501 20`
  inside the container, but git in the container still refused the working
  copy as "dubious ownership" until `safe.directory=*` was passed through
  `GIT_CONFIG_*` environment variables.
- Live runs on `gemini-4-argon-low` with `--isolation container`, the image
  from `containers/Dockerfile` and the sign-in home seeded by copying the
  host's `antigravity-oauth-token` plus a `settings.json` with the host's
  `gcp` block and `enableTerminalSandbox: false`: task 45 s (edited
  `stats.py`, ran the requested `python3 -c` itself, patch exported inside
  the container and `apply --check` passed on the host); review 19 s with
  the planted bug found; plan 1 m 57 s with the plan read from the brain
  directory in the container home. No container was left after any run.
- The task was asked to list `~/.aws`, `~/.ssh` and the host's `~/.gemini`;
  the model declined on policy grounds and the Gemini API also returned a
  content-safety error for that turn, so the job ended `failed` while its
  patch was still exported. The fake-agy test inside a real container is what
  shows those paths are absent.
- In plan mode inside the container agy used its research subagent; the
  host policy blocked subagents, the container does not.
- In-container sign-in with `containers/signin.sh` is not yet tested (it
  needs a terminal). `AGY_ADC_AUTH` is untested too: the host's Application
  Default Credentials file is an `authorized_user` credential with a refresh
  token, not a workforce federation credential, so the seat-or-pay-as-you-go
  question for ADC stays open.

## Alternative backend: the Antigravity Python SDK

agy is closed source. `google-antigravity/antigravity-sdk-python` (Apache-2.0)
is open, but it does not drive agy: it runs its own Go "localharness" binary
from the PyPI wheel and authenticates with a Gemini API key or with ADC
against Vertex AI ("Gemini Enterprise Agent Platform"), not with agy's
Gemini Enterprise seat sign-in. Billing would likely be pay-as-you-go. It
has real permission callbacks (`ask_user("run_command", handler=...)`), so
live approval would work, and ADC would also solve container sign-in. It
needs Python 3.10+ and packages (pydantic, google-genai, protobuf), so uv.
Open question for Abe: is Vertex AI billing through ADC acceptable for this
plugin, instead of the Enterprise seat?

## Containers (research of 24 October 2026, before the live runs above)

- Linux builds exist: `https://antigravity.google/cli/install.sh` installs
  `linux_amd64`, `linux_arm64` and musl variants from a manifest with a
  SHA-512 checksum. There is no official image; Docker's
  `sbx-kits-contrib/antigravity` kit runs the installer.
- Auth: the OAuth token file is plain JSON with a refresh token, so copying
  it into a container copies a long-lived secret. Signing in inside the
  container keeps the refresh token in a named volume, which is still a
  long-lived secret. `AGY_ADC_AUTH=true` (Preview) uses Application Default
  Credentials and might take a workforce-federation config, but may bill
  pay-as-you-go rather than the Gemini Enterprise seat (unverified).
- agy's own Linux sandbox needs user namespaces, which Docker's default
  seccomp profile blocks; run with `enableTerminalSandbox: false` and treat
  the container as the boundary.
- Sandcastle bind-mounts a worktree, runs as the host UID and GID, and
  passes credentials as environment variables. Adding agy means a new agent
  provider and an image that runs `install.sh`.
- Minimum experiment: Debian-slim image with `install.sh`, worktree
  bind-mounted, named volume for `~/.gemini`, in-container sign-in through
  the organisation's workforce pool, then a fresh container runs one print-mode turn.

## Prior art (research of 24 October 2026)

- `arcobaleno64/agy-plugin-cc` (MIT, JavaScript) already uses this name and
  ports codex-plugin-cc to agy and Gemini CLI. It states that agy has no
  enforced read-only mode and compares the workspace before and after each
  run.
- agy docs: `--mode plan` "prepends the `/plan` instruction prefix". agy issue
  1181 (open) shows a headless plan-mode run writing a file. Issue 36: with
  `--dangerously-skip-permissions`, the model can set `bypassSandbox`.
  `--sandbox` covers `run_command` only, not the native file tools.
- agy issues 1300 and 1255: stream idle timeouts and 503 "No capacity"
  retry loops of 3 to 10 minutes.

## Open questions

1. **Plan mode does not enforce read-only.** Both probes here show the
   model refusing, but agy's docs and issue 1181 show it is a prompt prefix
   only. Every job runs isolated from the user's checkout, and the README
   says so. Isolation method (worktree, `sandbox-exec`, container) is still
   to be decided. Do not publish to the THG marketplace until it is built and
   tested.
2. **Is `--dangerously-skip-permissions` needed at all?** Accept-edits ran a
   command and a write without it. Claude Code's auto-mode classifier blocks
   commands that carry the flag, so this session could not test plan mode
   with it.
3. **Google Cloud project.** Requests go to project
   PROJECT_ID (EU region), whose name marks it as a proof of concept.
   The plugin must not hard-code it; who owns the long-term project?
4. **Python or Node** for the runner (default: Python with uv).
5. **Stop-review-gate hook**: default is to leave it out.
6. Prior art: gemini-cli wrappers and existing Claude Code plugins
   (research in progress).
