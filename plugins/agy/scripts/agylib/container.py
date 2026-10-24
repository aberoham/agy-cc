"""Run agy inside a Linux container, with the container as the isolation boundary.

The container sees the job's working copy, one subdirectory of the job's
state directory (for agy's log and the exported patch), the plugin's own
files (read-only) and a separate agy home that holds the container sign-in.
Each is mounted at its host path, so paths in agy's output, in its log and
in the plan artifact mean the same thing on both sides and nothing has to be
translated. Nothing else from the host is mounted, so a prompt-injected
"read ~/.aws/credentials" finds no such file. The job record, the baseline
snapshot and the result stay outside the mount: the host trusts them, so
nothing inside the container may write them.

Inside the container agy runs with --dangerously-skip-permissions: it may
run any command and edit any file it can reach, and what it can reach is the
working copy. The patch is computed inside the container as well, so the
host never runs git over files agy has written.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path

from . import agybin
from .state import state_root

IMAGE_ENV = "AGY_IMAGE"
DEFAULT_IMAGE = "agy-sandbox:experiment"
HOME_ENV = "AGY_CONTAINER_HOME"
WORKSPACE_PROBE = ".agy-mount-probe-"  # plus a random suffix, see state_mountable
JOB_MOUNT = "container"  # the one job subdirectory the container may write
DOCKER_TIMEOUT = 60
# The working copy was created by the host user; through a virtual machine's
# shared filesystem its owner can differ from the container's user, and git
# then refuses the repository as "dubious ownership". Nothing outside the
# working copy is mounted, so trusting every directory costs nothing.
GIT_ENV = ("--env=GIT_CONFIG_COUNT=1", "--env=GIT_CONFIG_KEY_0=safe.directory", "--env=GIT_CONFIG_VALUE_0=*")


def docker_bin() -> Path | None:
    found = shutil.which("docker")
    return Path(found) if found else None


def image_name() -> str:
    return os.environ.get(IMAGE_ENV) or DEFAULT_IMAGE


def agy_home() -> Path:
    """The container's HOME, where agy keeps the container sign-in, its settings and its brain.

    One per user, beside the host's own ~/.gemini rather than inside a state
    directory, so every state directory (per plugin install, or per test)
    shares the one sign-in. Under the home directory, which a Docker virtual
    machine on macOS shares by default.
    """
    explicit = os.environ.get(HOME_ENV)
    if explicit:
        return Path(explicit).resolve()
    return Path.home() / ".gemini" / "agy-container-home"


def container_token_present() -> bool:
    token = agy_home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    return token.is_file() and token.stat().st_size > 0


def _docker(*args: str, timeout: float = DOCKER_TIMEOUT) -> subprocess.CompletedProcess | None:
    binary = docker_bin()
    if not binary:
        return None
    try:
        return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def daemon_reachable() -> bool:
    proc = _docker("info", "--format", "{{.ServerVersion}}", timeout=15)
    return bool(proc and proc.returncode == 0)


def image_present() -> bool:
    proc = _docker("image", "inspect", "--format", "{{.Id}}", image_name(), timeout=15)
    return bool(proc and proc.returncode == 0)


def job_mount(job_dir: Path) -> Path:
    return job_dir / JOB_MOUNT


def state_mountable() -> bool:
    """Whether the container can write into the plugin's state directory and the container home.

    A Docker virtual machine on macOS shares only some host directories
    (Colima shares the home directory, not /var/folders), so a state root in
    the system temporary directory cannot hold a job. The probe touches a
    file in each directory from inside the container. The names are fresh
    each time: the virtual machine's shared filesystem caches a file the host
    has since deleted, and touching that name again creates nothing the host
    can see.
    """
    probes = []
    for directory in (state_root(), agy_home()):
        directory.mkdir(parents=True, exist_ok=True)
        probes.append(directory / (WORKSPACE_PROBE + secrets.token_hex(4)))
    proc = _docker(
        "run",
        "--rm",
        f"--user={os.getuid()}:{os.getgid()}",
        "--entrypoint",
        "touch",
        *[f"--volume={p.parent}:{p.parent}" for p in probes],
        image_name(),
        *[str(p) for p in probes],
        timeout=DOCKER_TIMEOUT,
    )
    written = all(p.is_file() for p in probes)
    for probe in probes:
        probe.unlink(missing_ok=True)
    return bool(proc and proc.returncode == 0 and written)


def report() -> dict[str, object]:
    binary = docker_bin()
    reachable = bool(binary) and daemon_reachable()
    present = reachable and image_present()
    return {
        "docker": str(binary) if binary else None,
        "docker_reachable": reachable,
        "image": image_name(),
        "image_present": present,
        "state_mountable": present and state_mountable(),
        "agy_home": str(agy_home()),
        "container_signed_in": container_token_present(),
    }


def unavailable_reason(status: dict[str, object] | None = None) -> str | None:
    """Why a container job cannot start, or None when one can."""
    status = status if status is not None else report()
    if not status["docker"]:
        return "docker is not installed or not on PATH"
    if not status["docker_reachable"]:
        return "the Docker daemon is not reachable (is Colima or Docker Desktop running?)"
    if not status["image_present"]:
        return f"the image {status['image']} is not built; see containers/ in the plugin's source repository"
    if not status["state_mountable"]:
        return (
            f"the container cannot write to the state directory {state_root()}; "
            "set AGY_COMPANION_STATE_DIR to a directory under your home"
        )
    if not status["container_signed_in"]:
        return f"agy is not signed in for containers; run containers/signin.sh (it writes to {agy_home()})"
    return None


def container_name(job_id: str, generation: int) -> str:
    return f"agy-{job_id}-{generation}"


class Mounts:
    """What a job's containers may see, shared by the agy turn and the export
    so that a program agy relies on (a git filter in the home, say) is there
    for both."""

    def __init__(self, worktree: Path, job_dir: Path, plugin_root: Path, read_dirs: list[Path], writable: bool):
        self.worktree = worktree
        self.job_mount = job_mount(job_dir)
        self.plugin_root = plugin_root
        self.read_dirs = read_dirs
        self.writable = writable
        self.home = agy_home()

    def prefix(self, name: str) -> list[str]:
        """docker run options: identity, environment and the volumes."""
        binary = docker_bin()
        if not binary:
            raise RuntimeError("docker is not installed or not on PATH")
        (self.home / ".gemini").mkdir(parents=True, exist_ok=True)
        self.job_mount.mkdir(parents=True, exist_ok=True)
        command = [
            str(binary),
            "run",
            "--rm",
            "--init",
            f"--name={name}",
            f"--user={os.getuid()}:{os.getgid()}",
            f"--env=HOME={self.home}",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            *GIT_ENV,
            f"--volume={self.worktree}:{self.worktree}" + ("" if self.writable else ":ro"),
            f"--volume={self.job_mount}:{self.job_mount}",
            f"--volume={self.home}:{self.home}",
            f"--volume={self.plugin_root}:{self.plugin_root}:ro",
        ]
        for directory in self.read_dirs:
            command.append(f"--volume={directory}:{directory}:ro")
        command.append(f"--workdir={self.worktree}")
        return command


def wrap(agy_args: list[str], *, name: str, mounts: Mounts) -> list[str]:
    """The docker command that runs agy's arguments inside the container.

    agy_args comes from agybin.build_command without the binary, which the
    image's entrypoint supplies. --init gives the container a real init, so
    the SIGTERM that docker forwards on stop reaches agy and its children.
    A read-only round (a review, a plan, or a task round with --read-only)
    mounts the working copy read-only, which is what enforces it: inside the
    container agy has no permission checks.
    """
    command = mounts.prefix(name)
    command.insert(2, "--interactive")
    return command + [image_name(), *agy_args[1:], *agybin.SKIP_PERMISSIONS_FLAGS]


def export_command(baseline: str, *, name: str, mounts: Mounts) -> list[str]:
    """A docker command that computes the job's patch inside the container,
    into the job's mounted subdirectory."""
    companion = mounts.plugin_root / "scripts" / "agy_companion.py"
    return [
        *mounts.prefix(name),
        "--entrypoint",
        "python3",
        image_name(),
        str(companion),
        "_export",
        str(mounts.worktree),
        baseline,
        str(mounts.job_mount),
    ]


def remove(name: str, timeout: float = 30) -> None:
    """Stop and remove the container, if it still exists. Stopping the docker
    client does not stop the container, so this runs after every turn."""
    _docker("rm", "--force", name, timeout=timeout)
