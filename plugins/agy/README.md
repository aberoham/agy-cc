# agy plugin for Claude Code

Hand work from a Claude Code session to Google's Antigravity CLI (`agy`),
which runs Gemini 4 Argon. The plugin runs code reviews, adversarial reviews,
implementation plans and rescue tasks as tracked background jobs. Each job
works on its own copy of your repository, so agy never edits your checkout.

## Prerequisites

- **agy 1.1.12 or later**, installed and on `PATH`, or `AGY_BIN` set to its
  path. See https://antigravity.google/docs/cli. The plugin finds the binary
  itself: an `agy` shell function in your interactive shell is not visible to
  Claude Code and is not used.
- **A Gemini Enterprise seat**, and access to the Google Cloud project that
  agy is configured to use. Sign in once by running `agy` in a terminal
  window. `/agy:setup` shows the project agy will use.
- **git**, and `python3` 3.9 or later. The macOS system Python from the
  Command Line Tools is enough; the plugin needs no packages.
- **Docker or Colima, and the agy image**, for container isolation. Build the
  image from `containers/` in the source repository with your own user ids
  (`--build-arg UID=$(id -u) --build-arg GID=$(id -g)`) and sign in once with
  `containers/signin.sh`. Without them the plugin falls back to host mode,
  described below. The image carries `git`, `curl`, `jq`, `python3` and `uv`,
  so agy can run a Python repository's tests; add what your repositories
  need to the Dockerfile. Each job starts with an empty uv cache, so a job
  downloads what it needs.

## Install

This repository is its own marketplace. Add it, then install the plugin:

```
/plugin marketplace add aberoham/agy-cc
/plugin install agy@agy-cc
```

From another marketplace that lists this plugin:

```
/plugin install agy@<marketplace>
```

To update later: `/plugin marketplace update agy-cc`.

To try a local checkout: `claude --plugin-dir plugins/agy`.

Then run `/agy:setup`.

## Commands

| Command | What it does |
| --- | --- |
| `/agy:review` | Reviews uncommitted changes, or the branch against its base (`--base <ref>`). Read-only. |
| `/agy:adversarial-review` | Review that challenges the design and assumptions. Takes focus text. Read-only. |
| `/agy:plan <request>` | Writes an implementation plan. Read-only. |
| `/agy:rescue <request>` | Hands a debugging or implementation task to agy through the `agy-rescue` subagent. agy edits its own working copy. |
| `/agy:status [job]` | Lists jobs, or shows one job with a table of its rounds: duration, time waiting for Claude, tokens, stalls and queued commands. |
| `/agy:result [job]` | Shows a finished job's output. |
| `/agy:resume <job> [file]` | Sends the output of commands agy queued back to agy, and lets it continue. |
| `/agy:apply [job]` | Applies a finished task's changes to your checkout. Use `--check` to test first. |
| `/agy:cancel [job]` | Stops a running job. |
| `/agy:setup` | Checks that agy is installed and signed in. |

Most commands accept `--model high|medium|low` (Gemini 4 Argon High, Medium or
Low), `--background` and `--isolation container|host`. The default model is
Medium. The default isolation is the container when Docker, the image and the
container sign-in are all present, otherwise the host; `/agy:setup` says
which, and `/agy:status` shows it per job.

## How a job is contained

agy has no enforced read-only mode: `--mode plan` only adds an instruction to
the prompt. The plugin does not rely on it. Every job starts from a separate
working copy in the plugin's state directory, not in your repository, holding
your `HEAD` plus your uncommitted and untracked changes. The plugin's own git
calls turn off repository hooks, the fsmonitor helper and commit signing, so
setting up the copy runs no program your repository configures, and they pin
the patch format so your diff and colour settings cannot break it.

### Container mode

agy runs in a Linux container (`containers/Dockerfile`) that can see three
things, each mounted at its host path: the working copy, one subdirectory of
the job's state directory (agy's log, the exported patch and the job's own
home) and the plugin's own files (read-only). Nothing else from your machine
is mounted, so there is no `~/.aws`, `~/.ssh` or keychain to read, and the
job record, the baseline snapshot and the result, which the host trusts, are
out of the container's reach. The job's home belongs to that job alone: it
starts empty, receives a copy of the container sign-in and settings from
`~/.gemini/agy-container-home` (which is never mounted into a job) at the
start of every round, and keeps agy's conversation between rounds so a
resume works. A job therefore cannot leave a setting, a hook or a cached
package behind for the next job, whatever repository it was reviewing. The
patch export runs with an empty home, python in isolated mode and no user
git configuration, so nothing a round planted in the job's home runs there.
A `--read-dir` that overlaps the sign-in or the state directory is refused.
Inside the container agy runs with `--dangerously-skip-permissions`: it runs
the commands it needs, including your tests, and edits what it likes, and
all of that lands in the working copy. For a review, a plan or a task round
with `--read-only`, the working copy is mounted read-only, so agy cannot edit
it at all.

- The working copy is a self-contained repository (your `HEAD` tree plus your
  changes as its only commit), not a linked worktree, so nothing inside the
  container reaches your `.git`.
- The patch is computed inside the container too. The host runs no git over
  files agy has written; it only runs `git apply` into your checkout, and
  compares your files against a snapshot of the baseline taken before agy
  started.
- Your repository's own `.agents` hooks and workspace plugins run, inside the
  container, as they would for a developer.
- agy's own terminal sandbox is off inside the container (Docker blocks the
  user namespaces it needs); the container is the boundary.
- Jobs run as your user id, and `/agy:setup` refuses an image built for a
  different one, so files the image created and files on the bind mounts
  have one owner without any ownership fix-up at start. Inside the container
  git trusts the working copy and has an identity (`agy plugin`), so agy may
  commit there; only the patch leaves.

The container design follows Matt Pocock's
[Sandcastle](https://github.com/mattpocock/sandcastle) (MIT): a bind-mounted
working copy, the host user's ids baked into the image at build time with a
pre-flight check, git trust and identity set inside the sandbox, caches in the
sandbox home, and container removal on every exit path. No Sandcastle code is
used, so `NOTICE` is unchanged.

What the container does not change: agy still has the network, because it
must reach Google, so a prompt injection in the repository can send out
anything the container can read: the repository, any `--read-dir`
directory, and the job's copy of the container sign-in. The repository
already goes to Google for the review, so that part is not new, but it is
not nothing. The container sign-in is a credential like the one in your own
`~/.gemini`; with workforce identity federation it expires with the pool's
session duration (see `NOTES.md`). And a Docker virtual machine on
macOS shares only some directories: the plugin's state directory must be
under your home (Colima's default share), which `/agy:setup` checks.

### Host mode

Without a container, each job gets three layers:

1. **A linked worktree** of your repository as the working copy.
2. **A tool policy.** The plugin installs an agy `PreToolUse` hook in that
   working copy. The copy's `.agents` directory is emptied first and holds
   only that hook, because agy would otherwise run any hooks and workspace
   plugins the repository tracks there on every tool call, outside the policy
   and the sandbox. Your own `.agents` directory is not touched, and edits
   agy makes under `.agents` are not exported.
   - Reads are limited to the working copy and agy's artifact directory. To
     let agy read another directory, pass `--read-dir <path>` (repeatable);
     the prompt tells agy it may read there. A shell command runs without
     Claude only when its program and every option are on a short
     allow-list (`ls`, `cat`, `head`, `tail`, `wc`, `grep`, `find`, `stat`,
     `pwd`, `echo`) and every path it names stays inside those directories.
     A `~` or glob, an option that follows symlinks or reads a list of
     further files, or a file name attached to an option sends the command
     to Claude instead.
   - Reviews and plans may write only agy's own plan artifact. Tasks may write
     only inside the working copy, and never its `.git` or `.agents`
     directory, under any spelling the filesystem maps to them.
   - Web, browser, MCP and subagent tools are blocked in every job.
   - A symlinked `.agents` directory stops the job before it starts, so the
     plugin never removes or writes through a link into your checkout.
3. **A write sandbox on macOS.** agy runs under `sandbox-exec`. It may write
   only to the working copy, `~/.gemini`, the job's state directory and
   temporary directories, and never to your checkout, even when your checkout
   is itself under a temporary directory. Pass `--no-os-sandbox` to turn this
   off. On Linux there is no equivalent layer yet.

In host mode the plugin never passes `--dangerously-skip-permissions`.

## Commands agy wants to run (host mode)

In a container agy runs its own commands. On the host, in print mode agy
refuses any shell command that is not in its own
`permissions.allow` list (in `~/.gemini/antigravity-cli/settings.json`), and
an agy hook cannot override that. The plugin's policy is stricter still. It
lets a command through only when the program is one of `ls`, `cat`, `head`,
`tail`, `wc`, `pwd`, `grep`, `stat`, `echo` or `find` (without `-exec`,
`-delete` or `-fprint`), is named without a path, uses no shell syntax, and
reads only inside the job's allowed directories. git is not on that list,
because repository configuration can make even `git diff` run a program.

Every other command is queued, and the job ends as `needs-approval`. Claude
runs each queued command it judges safe with its own Bash tool, under your
normal Claude Code permission checks, and sends the output back with
`/agy:resume`. agy continues the same conversation in the same working copy.
`/agy:status <job>` shows each round's duration, the time agy waited for
Claude, and the tokens used, so you can see what the approval loop costs.

## Stalls

agy sometimes stalls: its log shows `stream idle timeout (5m0s): context
deadline exceeded` while it retries the model without output. The job runner
treats that log line, or about five and a half minutes without a stream
event, as a stall. While a tool step is running, the stream is silent until
the tool finishes, so the runner then waits up to 30 minutes. On a stall it
stops agy and everything agy started, and resumes the same conversation on
the next model down (High to Medium, Medium to Low), up to two times.
`/agy:status` shows each stall and the model that finished the job.

## Using a Gemini Enterprise licence

See [docs/gemini-enterprise.md](docs/gemini-enterprise.md) for what an
organisation needs before agy works with its Gemini Enterprise licence, and
for the sign-in quirks we hit.

## Testing

`tests/` in the source repository runs the companion against a fake agy that
copies the behaviour recorded from agy 1.4.7, including permission denials,
hook calls, stalls and recorded streams replayed unchanged, and against a
fake docker that runs containers as host processes. CI runs it on Linux and
macOS with Python 3.9 and 3.13; the macOS run exercises the real
`sandbox-exec` profile, and where a Docker daemon is reachable
`test_real_container.py` runs the fake agy inside a real container and checks
that a repository hook, a file outside the working copy and a git clean
filter all stay inside it. The tests use no Gemini licence and no Claude
account. They show the plugin handles agy's recorded behaviour, not that a
live model still behaves that way.

For that, run `scripts/live-smoke.sh --apply` from the source repository on a
machine where agy is signed in (`AGY_SMOKE_ISOLATION=container` or `host`
picks the mode; the default is whatever `/agy:setup` would choose). In a
throwaway repository it runs the
companion's setup, review, adversarial review, plan, task, resume (or, when
agy queued nothing, a `--resume-last` follow-up that must recall a code word
from the first round), result, apply check and cancel against the real agy,
checks each result, and prints the approval-round table. `/agy:rescue` itself is not run: it is a Claude
subagent that calls the same `task` command. Code agy wrote runs only under a
write sandbox, so on Linux those checks are skipped. It uses your Gemini
licence, so without `--apply` it only lists the steps. `--with-claude` also
drives `/agy:setup` and `/agy:review` through `claude --plugin-dir`.

## Gotchas

- **Check what agy quotes.** agy has presented invented text as an exact copy
  of a real file. Claude verifies agy's quotes and `file:line` claims before
  relaying them, and says which it checked.
- **Plans go to agy's artifact directory.** In plan mode agy writes
  `implementation_plan.md` under `~/.gemini/antigravity-cli/brain/<id>/` (for
  a container job, under the job's own home in its state directory); its
  reply only summarises it. `/agy:result` shows the file.
- **Resumes cost tokens.** A resumed conversation carries its history, so
  each approval round costs more input tokens than the last.
- **`--wait` returns after 100 seconds** with the job still running in the
  background, so the Bash call stays inside Claude Code's default timeout.
  Follow it with `/agy:status`.
- **`Clearcut responded with HTTP code: 401`** lines in agy's logs are
  telemetry noise.
- **Working copies.** A review's or plan's working copy is removed when the
  job finishes, unless it is waiting on queued commands. A task's working copy
  stays for `/agy:resume` and `/agy:apply`, until the job is pruned (the
  newest 50 finished jobs are kept) or you run
  `python3 scripts/agy_companion.py cleanup`. Job state lives under
  `$CLAUDE_PLUGIN_DATA/state`, or the system temporary directory. The
  session-start hook exports that directory to the session as
  `AGY_COMPANION_STATE_DIR`, so commands and hooks share it.
- **Jobs belong to the Claude session that started them.** `/agy:status`,
  and `/agy:cancel`, `/agy:result`, `/agy:apply` and `--resume-last` without
  a job id, see only this session's jobs. Pass a job id to reach another
  session's job; `/agy:status --all` lists them. Run from a plain terminal,
  where no session id is set, the commands see every job.
- **`/agy:apply` checks for drift.** It refuses when a file the task changed
  has changed in your checkout since the job started, in content or mode.
  `--force` skips that check; git still refuses a patch that does not apply.

## Open questions

- **Container sign-in.** `containers/signin.sh` stores agy's sign-in in
  `~/.gemini/agy-container-home`, so a container user signs in twice, and
  again when the pool's session expires. `AGY_ADC_AUTH=true` (preview) with
  Application Default Credentials would avoid the second sign-in; whether it
  bills the Gemini Enterprise seat or pay-as-you-go is not verified.
- **Host mode's future.** Once container mode has run live for a while, the
  host-side policy, sandbox and `/agy:resume` loop are due for removal.
- **The Antigravity Python SDK** offers real permission callbacks, but it
  bills through Gemini Enterprise Agent Platform (formerly Vertex AI) rather
  than a Gemini Enterprise seat.
- **Live tests in CI.** The live smoke test runs by hand only: a live agy
  needs a saved seat sign-in, which is a long-lived secret.

## Licence

Apache License 2.0. Parts are adapted from OpenAI's codex-plugin-cc; see
`NOTICE` for what was changed.
