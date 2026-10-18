"""A scripted stand-in for the docker CLI, used by the protocol and tool tests."""

from __future__ import annotations

import json
from collections.abc import Sequence

from dockmcp.docker import CommandResult

PS_ROWS = [
    {
        "ID": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "Names": "web",
        "Image": "nginx:1.27",
        "State": "running",
        "Status": "Up 2 hours",
        "Ports": "0.0.0.0:8080->80/tcp",
        "CreatedAt": "2026-10-18 08:00:00 +0000 UTC",
    },
    {
        "ID": "ffeeddccbbaaffeeddccbbaaffeeddccbbaaffeeddccbbaaffeeddccbbaaffee",
        "Names": "db",
        "Image": "postgres:16",
        "State": "exited",
        "Status": "Exited (0) 3 minutes ago",
        "Ports": "",
        "CreatedAt": "2026-10-17 21:30:00 +0000 UTC",
    },
]

INSPECT_WEB = {
    "Id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
    "Name": "/web",
    "Created": "2026-10-18T08:00:00Z",
    "RestartCount": 1,
    "State": {
        "Status": "running",
        "Running": True,
        "ExitCode": 0,
        "StartedAt": "2026-10-18T08:00:01Z",
    },
    "Config": {
        "Image": "nginx:1.27",
        "Env": ["API_KEY=hunter2", "MODE=prod", "BARE"],
        "Cmd": ["nginx", "-g", "daemon off;"],
    },
    "NetworkSettings": {
        "Ports": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}], "443/tcp": None}
    },
    "Mounts": [
        {"Type": "bind", "Source": "/srv/www", "Destination": "/usr/share/nginx/html", "RW": False}
    ],
    "HostConfig": {"RestartPolicy": {"Name": "always"}},
}


def jl(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r) for r in rows) + "\n"


class FakeBackend:
    """Records every call and answers like a small, healthy Docker host."""

    def __init__(self, fail: dict[str, CommandResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or {}
        self.logs_output = ("2026-10-18 line one\n", "warn: something on stderr\n")

    def run(self, args: Sequence[str], timeout: float | None = None) -> CommandResult:
        args = list(args)
        self.calls.append(args)
        verb = args[0]
        if verb in self.fail:
            return self.fail[verb]
        if verb == "version":
            version = {"Client": {"Version": "27.3.1"}, "Server": {"Version": "27.3.1"}}
            return CommandResult(0, json.dumps(version), "")
        if verb == "ps":
            rows = PS_ROWS
            if "--all" not in args:
                rows = [r for r in rows if r["State"] == "running"]
            return CommandResult(0, jl(rows), "")
        if verb == "images":
            image = {
                "Repository": "nginx",
                "Tag": "1.27",
                "ID": "sha256:abc",
                "Size": "188MB",
                "CreatedSince": "2 weeks ago",
            }
            return CommandResult(0, jl([image]), "")
        if verb == "inspect":
            if args[-1] == "web":
                return CommandResult(0, json.dumps([INSPECT_WEB]), "")
            return CommandResult(1, "[]", f"Error: No such container: {args[-1]}\n")
        if verb == "logs":
            return CommandResult(0, self.logs_output[0], self.logs_output[1])
        if verb == "stats":
            row = {
                "Name": "web",
                "CPUPerc": "0.10%",
                "MemUsage": "12MiB / 7.6GiB",
                "MemPerc": "0.15%",
                "NetIO": "1kB / 2kB",
                "BlockIO": "0B / 0B",
                "PIDs": "5",
            }
            return CommandResult(0, jl([row]), "")
        if verb in {"start", "stop", "restart", "rm"}:
            if args[-1] not in {"web", "db"}:
                message = f"Error response from daemon: No such container: {args[-1]}"
                return CommandResult(1, "", message)
            return CommandResult(0, args[-1] + "\n", "")
        return CommandResult(1, "", f"unknown command {verb}")
