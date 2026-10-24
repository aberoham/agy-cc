# agy-cc

A Claude Code plugin that hands code review, adversarial review, plan writing
and rescue tasks to Google's Antigravity CLI (`agy`, Gemini 4 Argon) as
tracked, isolated background jobs.

- The plugin is in [`plugins/agy`](plugins/agy); its [README](plugins/agy/README.md) covers prerequisites, install, commands and gotchas.
- [`NOTES.md`](NOTES.md) records how agy 1.4.7 was observed to behave, and the open questions.
- [`containers/`](containers) holds an experimental image for running agy in a container.

Development checks, matching CI:

```
uvx ruff@0.16.6 check plugins tests
uvx ruff@0.16.6 format --check plugins tests
uvx --with pytest==9.1.1 ty@0.0.78 check plugins tests
uv run --no-project --python 3.9 --with 'pytest>=8.4,<10' pytest -q
```

Try it locally with `claude --plugin-dir plugins/agy`. Before a release, run
`scripts/live-smoke.sh --apply` on a machine where agy is signed in; it checks
every command against the real agy.

Licensed under the Apache License 2.0. See [NOTICE](NOTICE) for the parts adapted from OpenAI's codex-plugin-cc.
