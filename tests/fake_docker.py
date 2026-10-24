#!/usr/bin/env python3
"""A stand-in for the docker client, driven by FAKE_DOCKER_MODE.

With FAKE_DOCKER_MODE=absent (the default) the daemon is unreachable, so the
companion falls back to host mode. With FAKE_DOCKER_MODE=present, `docker run`
runs the container's command on the host in the same process: the image's
entrypoint is the fake agy named by AGY_BIN, `--entrypoint` picks another
program, and the mounts are ignored because the plugin mounts every path at
its host path. Every invocation is appended to FAKE_DOCKER_CALLS.
"""

from __future__ import annotations

import json
import os
import shutil
import sys

VALUE_OPTIONS = {"--entrypoint", "--name", "--user", "--env", "--volume", "--workdir", "--format"}


def record(argv: list[str]) -> None:
    calls = os.environ.get("FAKE_DOCKER_CALLS")
    if calls:
        with open(calls, "a") as handle:
            handle.write(json.dumps(argv) + "\n")


def parse_run(argv: list[str]) -> tuple[dict[str, list[str]], str, list[str]]:
    """Split `docker run` arguments into options (each with its values), the image and the command."""
    options: dict[str, list[str]] = {}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if not arg.startswith("-"):
            return options, arg, argv[index + 1 :]
        if "=" in arg:
            key, value = arg.split("=", 1)
            options.setdefault(key, []).append(value)
        elif arg in VALUE_OPTIONS:
            options.setdefault(arg, []).append(argv[index + 1])
            index += 1
        else:
            options.setdefault(arg, [])
        index += 1
    raise SystemExit("fake docker: no image given")


def run(argv: list[str]) -> None:
    options, _image, command = parse_run(argv)
    entrypoint = options.get("--entrypoint", ["agy"])[0]
    if entrypoint == "agy":
        program = os.environ["AGY_BIN"]
    elif entrypoint == "python3":
        program = sys.executable
    else:
        program = shutil.which(entrypoint) or entrypoint
    env = dict(os.environ, FAKE_AGY_IN_CONTAINER="1")
    for assignment in options.get("--env", []):
        key, value = assignment.split("=", 1)
        env[key] = value
    if options.get("--workdir"):
        os.chdir(options["--workdir"][0])
    os.execve(program, [program, *command], env)


def main() -> int:
    argv = sys.argv[1:]
    record(argv)
    mode = os.environ.get("FAKE_DOCKER_MODE", "absent")
    if mode == "absent":
        sys.stderr.write("Cannot connect to the Docker daemon\n")
        return 1
    if argv[:1] == ["info"]:
        print("29.0.0")
        return 0
    if argv[:2] == ["image", "inspect"]:
        print("sha256:fake")
        return 0
    if argv[:1] == ["rm"]:
        return 0
    if argv[:1] == ["run"]:
        run(argv[1:])
    sys.stderr.write(f"fake docker: unsupported command {argv}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
