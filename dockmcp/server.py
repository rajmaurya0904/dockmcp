"""Command line entry point and the stdio transport for dockmcp."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import IO

from . import __version__
from .docker import Backend, Docker, DockerCLI
from .protocol import McpServer
from .tools import ToolError, ToolSet


def serve(server: McpServer, stdin: IO[str], stdout: IO[str]) -> int:
    """Read newline-delimited JSON-RPC from ``stdin`` until EOF, answering on ``stdout``."""
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        reply = server.handle_line(line)
        if reply is not None:
            stdout.write(reply + "\n")
            stdout.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dockmcp",
        description="MCP server (stdio) that lets an AI assistant list, start, stop and read "
        "logs of Docker containers.",
    )
    parser.add_argument("--version", action="version", version=f"dockmcp {__version__}")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="expose only inspection tools; start/stop/restart are not offered",
    )
    parser.add_argument(
        "--allow-remove",
        action="store_true",
        help="also offer remove_container (off by default)",
    )
    parser.add_argument(
        "--docker-bin",
        default="docker",
        metavar="PATH",
        help="docker client executable to run (default: docker)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="per-command timeout for docker calls (default: 30)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify Docker is reachable, print the tools that would be offered, and exit",
    )
    return parser


def _configure_stdio() -> None:
    # MCP messages are UTF-8 with bare newlines on every platform.
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[attr-defined]


def run_check(toolset: ToolSet, out: IO[str]) -> int:
    print(f"dockmcp {__version__}", file=out)
    print("tools: " + ", ".join(toolset.tools), file=out)
    try:
        print(toolset.call("docker_status", {}), file=out)
    except ToolError as exc:
        print(f"docker: NOT AVAILABLE - {exc}", file=out)
        return 1
    return 0


def main(argv: Sequence[str] | None = None, backend: Backend | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.read_only and args.allow_remove:
        print("dockmcp: --read-only and --allow-remove cannot be combined", file=sys.stderr)
        return 2
    if backend is None:
        try:
            backend = DockerCLI(args.docker_bin, timeout=args.timeout)
        except ValueError as exc:
            print(f"dockmcp: {exc}", file=sys.stderr)
            return 2
    toolset = ToolSet(Docker(backend), read_only=args.read_only, allow_remove=args.allow_remove)
    if args.check:
        return run_check(toolset, sys.stdout)
    _configure_stdio()
    print(f"dockmcp {__version__} ready on stdio ({len(toolset.tools)} tools)", file=sys.stderr)
    try:
        return serve(McpServer(toolset), sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
