# Recorded agy output

Real `--output-format stream-json` output from agy 1.4.7 on gemini-4-argon-low
or -medium, recorded on 24 October 2026. Local paths are replaced with
`/tmp/agy-probe` and `/Users/user`. `tests/test_lifecycle.py` replays some through the fake agy; the others document the event shapes the fake agy copies.

| File | Run |
| --- | --- |
| `pong-plan.ndjson` | Plan mode, prompt on stdin, one-word reply. |
| `read-file-plan.ndjson` | Plan mode, reads a file with `read_file`. |
| `command-and-write-accept-edits.ndjson` | Accept-edits, `run_command` (allow-listed) and `write_file` both succeed. |
| `plan-mode-writes-brain-plan.ndjson` | Plan mode asked to change files; writes `implementation_plan.md` under the brain directory instead. |
| `plan-mode-refuses-workspace-write.ndjson` | Plan mode told the plan was approved; still only writes the brain plan. |
| `headless-permission-auto-deny.ndjson` | A command outside `permissions.allow` is auto-denied; `result.denied_actions` is set. Its stderr is in the `.stderr.txt` file. |
| `hook-denies-run-command.ndjson` | A workspace PreToolUse hook denies `run_command`; `write_file` was not in the deny list and succeeded. |
| `hook-input-payloads.ndjson` | The JSON the PreToolUse hook received on stdin for those two calls. |
