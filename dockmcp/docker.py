"""Thin wrapper around the ``docker`` command line client.

Everything dockmcp knows about Docker goes through a :class:`Backend`, which is just
"run this argument list and give me exit code, stdout and stderr". The real backend
shells out to the ``docker`` binary; tests plug in a fake one.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_TIMEOUT = 30.0

_DAEMON_DOWN_MARKERS = (
    "cannot connect to the docker daemon",
    "error during connect",
    "is the docker daemon running",
    "docker daemon is not running",
    "failed to connect to the docker api",
    "the system cannot find the file specified",
)

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class DockerError(Exception):
    """A docker command ran and failed (or could not complete)."""

    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class DockerUnavailable(DockerError):
    """Docker is not installed, or its daemon is not reachable."""


class InvalidArgument(ValueError):
    """The caller passed something that should never reach the docker CLI."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Backend(Protocol):
    def run(self, args: Sequence[str], timeout: float | None = None) -> CommandResult: ...


class DockerCLI:
    """Runs ``docker <args>`` as a subprocess.

    ``command`` is the program to run, either a bare name/path (``"docker"``) or a
    full prefix such as ``["podman"]``. It defaults to whatever ``docker`` resolves to
    on ``PATH``.
    """

    def __init__(self, command: str | Sequence[str] = "docker", timeout: float = DEFAULT_TIMEOUT):
        if isinstance(command, str):
            command = shlex.split(command, posix=False) if " " in command.strip() else [command]
        self.command = list(command)
        if not self.command:
            raise ValueError("docker command must not be empty")
        self.timeout = timeout

    def _resolve(self) -> list[str]:
        program = self.command[0]
        found = shutil.which(program)
        if found is None:
            raise DockerUnavailable(
                f"The '{program}' executable was not found on PATH. Install Docker "
                "(Docker Desktop on Windows/macOS) or pass --docker-bin to point at it."
            )
        return [found, *self.command[1:]]

    def run(self, args: Sequence[str], timeout: float | None = None) -> CommandResult:
        argv = [*self._resolve(), *args]
        limit = self.timeout if timeout is None else timeout
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                timeout=limit,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerError(f"docker {' '.join(args[:2])} timed out after {limit:g}s") from exc
        except OSError as exc:
            raise DockerUnavailable(f"Could not start docker: {exc}") from exc
        return CommandResult(
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )


def validate_ref(value: Any, what: str = "container") -> str:
    """Accept container/image-ish names and ids; reject anything option-shaped."""
    if not isinstance(value, str) or not value:
        raise InvalidArgument(f"{what} must be a non-empty string")
    if not _REF_RE.match(value) or len(value) > 255:
        raise InvalidArgument(
            f"{what} {value!r} is not a valid container name or id "
            "(letters, digits, '_', '.', '-'; must not start with '-')"
        )
    return value


def validate_image(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise InvalidArgument("image must be a non-empty string")
    if value.startswith("-") or any(c.isspace() for c in value):
        raise InvalidArgument(f"image {value!r} is not a valid image reference")
    return value


def _is_daemon_down(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _DAEMON_DOWN_MARKERS)


def _json_lines(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DockerError(f"Unexpected non-JSON output from docker: {line[:120]!r}") from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows


class Docker:
    """High level operations used by the MCP tools."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    # -- plumbing ---------------------------------------------------------------

    def _run(self, args: Sequence[str], timeout: float | None = None) -> CommandResult:
        result = self.backend.run(args, timeout)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or "docker exited with an error"
            if _is_daemon_down(message):
                raise DockerUnavailable(
                    "The Docker daemon is not reachable. Start Docker Desktop (or the "
                    f"docker service) and try again. Details: {message.splitlines()[0]}",
                    result.returncode,
                )
            raise DockerError(message, result.returncode)
        return result

    # -- queries ----------------------------------------------------------------

    def version(self) -> dict[str, Any]:
        out = self._run(["version", "--format", "{{json .}}"]).stdout.strip()
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise DockerError(f"Could not parse docker version output: {out[:120]!r}") from exc
        return data if isinstance(data, dict) else {}

    def list_containers(
        self,
        all: bool = False,
        name: str | None = None,
        status: str | None = None,
        label: str | None = None,
    ) -> list[dict[str, Any]]:
        args = ["ps", "--no-trunc", "--format", "{{json .}}"]
        if all:
            args.append("--all")
        if name:
            args += ["--filter", f"name={name}"]
        if status:
            args += ["--filter", f"status={status}"]
        if label:
            args += ["--filter", f"label={label}"]
        return _json_lines(self._run(args).stdout)

    def list_images(self) -> list[dict[str, Any]]:
        return _json_lines(self._run(["images", "--format", "{{json .}}"]).stdout)

    def inspect(self, container: str) -> dict[str, Any]:
        validate_ref(container)
        out = self._run(["inspect", "--type", "container", container]).stdout
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise DockerError("Could not parse docker inspect output") from exc
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise DockerError(f"No such container: {container}")
        return data[0]

    def logs(
        self,
        container: str,
        tail: int = 100,
        since: str | None = None,
        timestamps: bool = False,
    ) -> str:
        validate_ref(container)
        args = ["logs", "--tail", str(tail)]
        if since:
            args += ["--since", since]
        if timestamps:
            args.append("--timestamps")
        args.append(container)
        result = self._run(args)
        # The docker CLI replays the container's stderr on its own stderr.
        return result.stdout + result.stderr

    def stats(self, container: str | None = None) -> list[dict[str, Any]]:
        args = ["stats", "--no-stream", "--format", "{{json .}}"]
        if container:
            args.append(validate_ref(container))
        return _json_lines(self._run(args, timeout=60).stdout)

    # -- actions ----------------------------------------------------------------

    def start(self, container: str) -> str:
        validate_ref(container)
        self._run(["start", container])
        return container

    def stop(self, container: str, timeout: int = 10) -> str:
        validate_ref(container)
        self._run(["stop", "--time", str(timeout), container], timeout=timeout + 30)
        return container

    def restart(self, container: str, timeout: int = 10) -> str:
        validate_ref(container)
        self._run(["restart", "--time", str(timeout), container], timeout=timeout + 30)
        return container

    def remove(self, container: str, force: bool = False, volumes: bool = False) -> str:
        validate_ref(container)
        args = ["rm"]
        if force:
            args.append("--force")
        if volumes:
            args.append("--volumes")
        args.append(container)
        self._run(args)
        return container
