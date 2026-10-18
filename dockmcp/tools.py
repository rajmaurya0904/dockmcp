"""The MCP tools: schemas, argument checking and text rendering.

Each tool maps one natural-language intent ("what is running?", "why did db die?",
"restart web") onto a small number of docker CLI calls and renders the answer as plain
text a model can read without further parsing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .docker import Docker, DockerError, InvalidArgument

MAX_LOG_CHARS = 20_000
DEFAULT_TAIL = 100
MAX_TAIL = 5_000
STATUS_VALUES = ["created", "running", "paused", "restarting", "removing", "exited", "dead"]


class ToolError(Exception):
    """A problem to show to the model as a tool result with ``isError`` set."""


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, dict[str, Any]]
    handler: Callable[[Docker, dict[str, Any]], str]
    required: list[str] = field(default_factory=list)
    read_only: bool = True
    destructive: bool = False

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": self.properties,
            "required": self.required,
            "additionalProperties": False,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {
                "title": self.name.replace("_", " "),
                "readOnlyHint": self.read_only,
                "destructiveHint": self.destructive,
                "idempotentHint": self.read_only,
                "openWorldHint": False,
            },
        }


# -- argument checking ---------------------------------------------------------------


def check_arguments(tool: Tool, arguments: Any) -> dict[str, Any]:
    """Validate ``arguments`` against the tool's (small subset of) JSON schema."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError("arguments must be an object")
    unknown = sorted(set(arguments) - set(tool.properties))
    if unknown:
        raise ToolError(f"Unknown argument(s): {', '.join(unknown)}")
    missing = [name for name in tool.required if name not in arguments]
    if missing:
        raise ToolError(f"Missing required argument(s): {', '.join(missing)}")
    for name, value in arguments.items():
        spec = tool.properties[name]
        kind = spec.get("type")
        if kind == "string":
            if not isinstance(value, str):
                raise ToolError(f"'{name}' must be a string")
            if "enum" in spec and value not in spec["enum"]:
                raise ToolError(f"'{name}' must be one of: {', '.join(spec['enum'])}")
        elif kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError(f"'{name}' must be an integer")
            low, high = spec.get("minimum"), spec.get("maximum")
            if (low is not None and value < low) or (high is not None and value > high):
                raise ToolError(f"'{name}' must be between {low} and {high}")
        elif kind == "boolean" and not isinstance(value, bool):
            raise ToolError(f"'{name}' must be true or false")
    return arguments


# -- rendering helpers ---------------------------------------------------------------


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    for row in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
    return "\n".join(lines)


def truncate_tail(text: str, limit: int = MAX_LOG_CHARS) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    cut = text[-limit:]
    newline = cut.find("\n")
    if 0 <= newline < 200:
        cut = cut[newline + 1 :]
    return f"[... {dropped} earlier characters omitted; ask for a smaller tail ...]\n{cut}"


def _short_id(value: str) -> str:
    return value.removeprefix("sha256:")[:12]


# -- handlers ------------------------------------------------------------------------


def docker_status(docker: Docker, args: dict[str, Any]) -> str:
    data = docker.version()
    client = data.get("Client", {}) or {}
    server = data.get("Server", {}) or {}
    lines = [f"Docker client {client.get('Version', 'unknown')} is installed."]
    if server:
        lines.append(f"Daemon is reachable: engine {server.get('Version', 'unknown')}.")
    else:
        lines.append("The daemon did not report a server version.")
    return "\n".join(lines)


def list_containers(docker: Docker, args: dict[str, Any]) -> str:
    rows = docker.list_containers(
        all=args.get("all", False),
        name=args.get("name"),
        status=args.get("status"),
        label=args.get("label"),
    )
    if not rows:
        scope = "containers" if args.get("all") else "running containers"
        return f"No {scope} match."
    body = [
        [
            r.get("Names", ""),
            r.get("State", ""),
            r.get("Image", ""),
            r.get("Status", ""),
            r.get("Ports", "") or "-",
            _short_id(r.get("ID", "")),
        ]
        for r in rows
    ]
    header = ["NAME", "STATE", "IMAGE", "STATUS", "PORTS", "ID"]
    return f"{len(rows)} container(s)\n" + table(header, body)


def inspect_container(docker: Docker, args: dict[str, Any]) -> str:
    data = docker.inspect(args["container"])
    state = data.get("State", {}) or {}
    config = data.get("Config", {}) or {}
    ports = (data.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
    published = {}
    for container_port, bindings in ports.items():
        published[container_port] = (
            [f"{b.get('HostIp', '')}:{b.get('HostPort', '')}" for b in bindings] if bindings else []
        )
    env_names = sorted(entry.split("=", 1)[0] for entry in config.get("Env") or [])
    mounts = [
        {
            "type": m.get("Type"),
            "source": m.get("Source") or m.get("Name"),
            "destination": m.get("Destination"),
            "read_write": m.get("RW"),
        }
        for m in data.get("Mounts") or []
    ]
    summary = {
        "name": str(data.get("Name", "")).lstrip("/"),
        "id": _short_id(str(data.get("Id", ""))),
        "image": config.get("Image"),
        "created": data.get("Created"),
        "status": state.get("Status"),
        "running": state.get("Running"),
        "exit_code": state.get("ExitCode"),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "oom_killed": state.get("OOMKilled"),
        "restart_count": data.get("RestartCount"),
        "restart_policy": ((data.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"),
        "command": config.get("Cmd"),
        "ports": published,
        "mounts": mounts,
        "env_variable_names": env_names,
    }
    summary = {k: v for k, v in summary.items() if v not in (None, "")}
    return json.dumps(summary, indent=2) + "\n(environment values are deliberately not shown)"


def start_container(docker: Docker, args: dict[str, Any]) -> str:
    docker.start(args["container"])
    return f"Started {args['container']}."


def stop_container(docker: Docker, args: dict[str, Any]) -> str:
    grace = args.get("timeout_seconds", 10)
    docker.stop(args["container"], timeout=grace)
    return f"Stopped {args['container']} (grace period {grace}s)."


def restart_container(docker: Docker, args: dict[str, Any]) -> str:
    grace = args.get("timeout_seconds", 10)
    docker.restart(args["container"], timeout=grace)
    return f"Restarted {args['container']} (grace period {grace}s)."


def remove_container(docker: Docker, args: dict[str, Any]) -> str:
    docker.remove(
        args["container"], force=args.get("force", False), volumes=args.get("volumes", False)
    )
    extra = " and its anonymous volumes" if args.get("volumes") else ""
    return f"Removed {args['container']}{extra}."


def container_logs(docker: Docker, args: dict[str, Any]) -> str:
    tail = args.get("tail", DEFAULT_TAIL)
    text = docker.logs(
        args["container"],
        tail=tail,
        since=args.get("since"),
        timestamps=args.get("timestamps", False),
    )
    if not text.strip():
        return f"{args['container']} has produced no log output in that window."
    return truncate_tail(text.rstrip("\n"))


def container_stats(docker: Docker, args: dict[str, Any]) -> str:
    rows = docker.stats(args.get("container"))
    if not rows:
        return "No running containers to report on."
    body = [
        [
            r.get("Name", ""),
            r.get("CPUPerc", ""),
            r.get("MemUsage", ""),
            r.get("MemPerc", ""),
            r.get("NetIO", ""),
            r.get("BlockIO", ""),
            r.get("PIDs", ""),
        ]
        for r in rows
    ]
    return table(["NAME", "CPU", "MEM USAGE", "MEM %", "NET I/O", "BLOCK I/O", "PIDS"], body)


def list_images(docker: Docker, args: dict[str, Any]) -> str:
    rows = docker.list_images()
    if not rows:
        return "No images are present."
    body = [
        [
            r.get("Repository", ""),
            r.get("Tag", ""),
            _short_id(r.get("ID", "")),
            r.get("Size", ""),
            r.get("CreatedSince", ""),
        ]
        for r in rows
    ]
    return f"{len(rows)} image(s)\n" + table(["REPOSITORY", "TAG", "ID", "SIZE", "CREATED"], body)


_CONTAINER = {
    "type": "string",
    "description": "Container name or id, as shown by list_containers.",
}
_GRACE = {
    "type": "integer",
    "description": "Seconds to wait for a clean shutdown before the container is killed.",
    "minimum": 0,
    "maximum": 600,
}

TOOLS: list[Tool] = [
    Tool(
        "docker_status",
        "Check that Docker is installed and its daemon is reachable; reports versions.",
        {},
        docker_status,
    ),
    Tool(
        "list_containers",
        "List containers. Running ones by default; pass all=true to include stopped ones. "
        "Optionally filter by name fragment, state or label.",
        {
            "all": {"type": "boolean", "description": "Include stopped containers."},
            "name": {"type": "string", "description": "Only containers whose name matches."},
            "status": {
                "type": "string",
                "enum": STATUS_VALUES,
                "description": "Only containers in this state.",
            },
            "label": {"type": "string", "description": "Label filter, 'key' or 'key=value'."},
        },
        list_containers,
    ),
    Tool(
        "inspect_container",
        "Summarise one container: state, exit code, restart count, ports, mounts, command "
        "and environment variable names (never their values).",
        {"container": _CONTAINER},
        inspect_container,
        required=["container"],
    ),
    Tool(
        "container_logs",
        "Read the most recent log lines of a container (stdout and stderr combined).",
        {
            "container": _CONTAINER,
            "tail": {
                "type": "integer",
                "description": f"How many trailing lines to return (default {DEFAULT_TAIL}).",
                "minimum": 1,
                "maximum": MAX_TAIL,
            },
            "since": {
                "type": "string",
                "description": "Only logs newer than this, e.g. '15m', '2h' or an RFC 3339 time.",
            },
            "timestamps": {"type": "boolean", "description": "Prefix each line with its time."},
        },
        container_logs,
        required=["container"],
    ),
    Tool(
        "container_stats",
        "One-shot CPU, memory, network and block I/O figures for running containers.",
        {"container": {**_CONTAINER, "description": "Limit to one container (optional)."}},
        container_stats,
    ),
    Tool("list_images", "List local images with size and age.", {}, list_images),
    Tool(
        "start_container",
        "Start an existing, stopped container.",
        {"container": _CONTAINER},
        start_container,
        required=["container"],
        read_only=False,
    ),
    Tool(
        "stop_container",
        "Stop a running container, giving it a grace period to shut down cleanly.",
        {"container": _CONTAINER, "timeout_seconds": _GRACE},
        stop_container,
        required=["container"],
        read_only=False,
    ),
    Tool(
        "restart_container",
        "Restart a container (stop then start).",
        {"container": _CONTAINER, "timeout_seconds": _GRACE},
        restart_container,
        required=["container"],
        read_only=False,
    ),
    Tool(
        "remove_container",
        "Delete a container. Only available when the server was started with --allow-remove.",
        {
            "container": _CONTAINER,
            "force": {"type": "boolean", "description": "Kill it first if it is running."},
            "volumes": {"type": "boolean", "description": "Also delete its anonymous volumes."},
        },
        remove_container,
        required=["container"],
        read_only=False,
        destructive=True,
    ),
]


class ToolSet:
    """The tools a particular server instance exposes, given its safety switches."""

    def __init__(self, docker: Docker, read_only: bool = False, allow_remove: bool = False):
        if read_only and allow_remove:
            raise ValueError("--read-only and --allow-remove cannot be combined")
        self.docker = docker
        self.tools: dict[str, Tool] = {}
        for tool in TOOLS:
            if read_only and not tool.read_only:
                continue
            if tool.destructive and not allow_remove:
                continue
            self.tools[tool.name] = tool

    def describe(self) -> list[dict[str, Any]]:
        return [tool.describe() for tool in self.tools.values()]

    def call(self, name: str, arguments: Any) -> str:
        """Run a tool and return its text. Raises ``KeyError`` for an unknown tool."""
        tool = self.tools[name]
        args = check_arguments(tool, arguments)
        try:
            return tool.handler(self.docker, args)
        except InvalidArgument as exc:
            raise ToolError(str(exc)) from exc
        except DockerError as exc:
            raise ToolError(str(exc)) from exc
