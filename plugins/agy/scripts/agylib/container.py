"""Run agy inside a Linux container, with the container as the isolation boundary.

The container sees the job's working copy, one subdirectory of the job's
state directory (for agy's log, the exported patch and the job's own home)
and the plugin's own files (read-only). Each is mounted at its host path, so
paths in agy's output, in its log and in the plan artifact mean the same
thing on both sides and nothing has to be translated. Nothing else from the
host is mounted, so a prompt-injected "read ~/.aws/credentials" finds no
such file. The job record, the baseline snapshot and the result stay outside
the mount: the host trusts them, so nothing inside the container may write
them.

The job's home is seeded at the start of every round from the sign-in home
(the token and settings that containers/signin.sh wrote) and is never
shared: one job cannot leave a setting, a hook or a cached package behind
for the next, whatever repository it was reviewing.

Inside the container agy runs with --dangerously-skip-permissions: it may
run any command and edit any file it can reach, and what it can reach is the
working copy. The patch is computed inside the container as well, so the
host never runs git over files agy has written.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
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
# Git configuration for every command in the container, agy's included. The
# working copy was created by the host user; through a virtual machine's
# shared filesystem its owner can differ from the container's user, and git
# then refuses the repository as "dubious ownership". Nothing outside the
# working copy is mounted, so trusting every directory costs nothing. An
# identity lets agy commit inside the copy if it wants to (only the patch
# leaves the container), and signing is off because there is no key.
_GIT_CONFIG = (
    ("safe.directory", "*"),
    ("user.name", "agy plugin"),
    ("user.email", "agy-plugin@localhost"),
    ("commit.gpgsign", "false"),
)
GIT_ENV = (
    f"--env=GIT_CONFIG_COUNT={len(_GIT_CONFIG)}",
    *[
        flag
        for i, (key, value) in enumerate(_GIT_CONFIG)
        for flag in (f"--env=GIT_CONFIG_KEY_{i}={key}", f"--env=GIT_CONFIG_VALUE_{i}={value}")
    ],
)


def docker_bin() -> Path | None:
    found = shutil.which("docker")
    return Path(found) if found else None


def image_name() -> str:
    return os.environ.get(IMAGE_ENV) or DEFAULT_IMAGE


SIGNIN_FILES = ("antigravity-oauth-token", "settings.json")
DEFAULT_SETTINGS = '{"enableTerminalSandbox": false}\n'


def signin_home() -> Path:
    """Where containers/signin.sh leaves the container sign-in and settings.

    One per user, beside the host's own ~/.gemini rather than inside a state
    directory, so every state directory (per plugin install, or per test)
    shares the one sign-in. It is never mounted into a job; each job gets a
    copy.
    """
    explicit = os.environ.get(HOME_ENV)
    if explicit:
        return Path(explicit).resolve()
    return Path.home() / ".gemini" / "agy-container-home"


def container_token_present() -> bool:
    token = signin_home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    return token.is_file() and token.stat().st_size > 0


def job_home(job_dir: Path) -> Path:
    """The job's own HOME inside its mounted subdirectory."""
    return job_mount(job_dir) / "home"


def seed_home(job_dir: Path) -> Path:
    """Give the job its own home with the sign-in and settings, and return it.

    The home belongs to one job: a new job starts empty, so nothing a job on
    another repository did can reach it. Within a job the home persists
    between rounds, because agy keeps the conversation it resumes with
    (conversations/<id>.db) and its plan artifacts there; only the sign-in
    and settings are written afresh each round, so a new sign-in or a
    setting agy changed does not carry over. The refresh token in the copy
    keeps working: refreshing does not rotate it, so copies refreshed
    independently stay valid until the pool session ends.
    """
    source = signin_home() / ".gemini" / "antigravity-cli"
    # Every directory below the job directory is agent-writable, so a round
    # could have replaced one with a link to somewhere on the host. Descend
    # with directory descriptors that refuse links, and write through them.
    fd = os.open(str(job_dir), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in (JOB_MOUNT, "home", ".gemini", "antigravity-cli"):
            fd = _descend(fd, name)
        for name in SIGNIN_FILES:
            if (source / name).is_file():
                _write_private(fd, name, (source / name).read_bytes())
        if not (source / "settings.json").is_file():
            _write_private(fd, "settings.json", DEFAULT_SETTINGS.encode())
    finally:
        os.close(fd)
    return job_home(job_dir)


def _descend(parent_fd: int, name: str) -> int:
    """Open (creating if needed) the directory `name` under parent_fd, refusing a link.

    On success the parent descriptor is closed and the child returned; on
    failure the parent stays open for the caller to close.
    """
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeError(f"the job's home component {name!r} is not a plain directory: {exc.strerror}") from exc
    os.close(parent_fd)
    return child


def _write_private(dir_fd: int, name: str, data: bytes) -> None:
    """Create or replace a file under dir_fd with mode 0600 from the start, never through a link."""
    try:
        if stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode):
            os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(name, 0o600, dir_fd=dir_fd, follow_symlinks=False)


def export_home(job_dir: Path) -> Path:
    """An empty home for the export container, created afresh each time.

    The export runs python and git with the working copy writable, so it
    must not pick up start-up files a round planted in the job's home
    (a sitecustomize module, a user git configuration).
    """
    home = job_mount(job_dir) / "export-home"
    if home.is_symlink() or home.is_file():
        home.unlink()
    elif home.is_dir():
        remove_tree(home)
    home.mkdir(parents=True)
    return home


def remove_tree(path: Path) -> None:
    """rmtree that first makes directories a round left read-only writable again."""

    def retry(func, target, _exc):
        parent = os.path.dirname(target)
        for candidate in (target, parent):
            try:
                os.chmod(candidate, 0o700)
            except OSError:
                pass
        func(target)

    shutil.rmtree(path, onerror=retry)


def overlaps_protected(directory: Path) -> str | None:
    """Why a --read-dir may not be mounted: it would expose the sign-in or other jobs' state."""
    wanted = Path(os.path.realpath(directory))
    for label, protected in (("the container sign-in", signin_home()), ("the plugin's state directory", state_root())):
        real = Path(os.path.realpath(protected))
        if wanted == real or wanted in real.parents or real in wanted.parents:
            return f"{directory} overlaps {label} ({protected})"
    return None


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


def image_user() -> str | None:
    """The image's USER as docker reports it ("501:20", "agent" or ""), or None when the inspection failed."""
    proc = _docker("image", "inspect", "--format", "{{.Config.User}}", image_name(), timeout=15)
    if not proc or proc.returncode != 0:
        return None
    return proc.stdout.strip()


def image_user_matches(user: str | None) -> bool:
    """Whether the image was built for this user.

    Jobs run as the host user so bind-mounted files have one owner, and the
    image's own files (agy's home skeleton, uv) must belong to that same
    user. A numeric USER is compared; a name or no USER cannot be checked
    and passes; a failed inspection does not.
    """
    if user is None:
        return False
    uid, _, gid = user.partition(":")
    if not (uid.isdigit() and gid.isdigit()):
        return True
    return user == f"{os.getuid()}:{os.getgid()}"


def job_mount(job_dir: Path) -> Path:
    return job_dir / JOB_MOUNT


def state_mountable() -> bool:
    """Whether the container can write into the plugin's state directory.

    A Docker virtual machine on macOS shares only some host directories
    (Colima shares the home directory, not /var/folders), so a state root in
    the system temporary directory cannot hold a job. The probe touches a
    file in the state root from inside the container. The name is fresh
    each time: the virtual machine's shared filesystem caches a file the host
    has since deleted, and touching that name again creates nothing the host
    can see.
    """
    probes = []
    for directory in (state_root(),):
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
    user = image_user() if present else None
    return {
        "docker": str(binary) if binary else None,
        "docker_reachable": reachable,
        "image": image_name(),
        "image_present": present,
        "image_user": user,
        "image_user_matches": present and image_user_matches(user),
        "state_mountable": present and state_mountable(),
        "signin_home": str(signin_home()),
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
    if not status["image_user_matches"]:
        if status["image_user"] is None:
            return f"docker could not inspect the image {status['image']}"
        return (
            f"the image {status['image']} was built for user {status['image_user']}, not "
            f"{os.getuid()}:{os.getgid()}; rebuild it with --build-arg UID=$(id -u) --build-arg GID=$(id -g)"
        )
    if not status["state_mountable"]:
        return (
            f"the container cannot write to the state directory {state_root()}; "
            "set AGY_COMPANION_STATE_DIR to a directory under your home"
        )
    if not status["container_signed_in"]:
        return f"agy is not signed in for containers; run containers/signin.sh (it writes to {signin_home()})"
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
        self.home = job_home(job_dir)

    def prefix(self, name: str) -> list[str]:
        """docker run options: identity, environment and the volumes."""
        binary = docker_bin()
        if not binary:
            raise RuntimeError("docker is not installed or not on PATH")
        if not self.home.is_dir():
            raise RuntimeError(f"the job's home {self.home} has not been seeded")
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


def export_command(baseline: str, *, name: str, mounts: Mounts, home: Path) -> list[str]:
    """A docker command that computes the job's patch inside the container,
    into the job's mounted subdirectory.

    It runs with an empty home, python in isolated mode and no user or
    system git configuration, so nothing a round left behind runs here.
    """
    companion = mounts.plugin_root / "scripts" / "agy_companion.py"
    prefix = [f"--env=HOME={home}" if o.startswith("--env=HOME=") else o for o in mounts.prefix(name)]
    return [
        *prefix,
        "--env=GIT_CONFIG_GLOBAL=/dev/null",
        "--env=GIT_CONFIG_NOSYSTEM=1",
        "--entrypoint",
        "python3",
        image_name(),
        "-I",
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
