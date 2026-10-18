import io
import json

import pytest

from dockmcp import __version__
from dockmcp.docker import CommandResult, Docker
from dockmcp.protocol import McpServer
from dockmcp.server import build_parser, main, run_check, serve
from dockmcp.tools import ToolSet
from tests.fakes import FakeBackend


def run_serve(text: str, backend=None) -> list[dict]:
    server = McpServer(ToolSet(Docker(backend or FakeBackend())))
    out = io.StringIO()
    assert serve(server, io.StringIO(text), out) == 0
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_serve_answers_requests_and_skips_notifications_and_blank_lines():
    replies = run_serve(
        '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        "\n"
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    assert [r["id"] for r in replies] == [1, 2]


def test_serve_survives_garbage_between_requests():
    replies = run_serve('garbage\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
    assert replies[0]["error"]["code"] == -32700
    assert replies[1]["id"] == 7


def test_serve_returns_zero_on_immediate_eof():
    assert run_serve("") == []


def test_serve_handles_unicode_logs():
    backend = FakeBackend()
    backend.logs_output = ("café ✓ started\n", "")
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "container_logs", "arguments": {"container": "web"}},
    }
    reply = run_serve(json.dumps(request) + "\n", backend)[0]
    assert "café ✓" in reply["result"]["content"][0]["text"]


def test_parser_defaults():
    args = build_parser().parse_args([])
    assert args.docker_bin == "docker"
    assert args.timeout == 30.0
    assert not args.read_only and not args.allow_remove and not args.check


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_run_check_ok():
    out = io.StringIO()
    code = run_check(ToolSet(Docker(FakeBackend())), out)
    assert code == 0
    assert "27.3.1" in out.getvalue() and "list_containers" in out.getvalue()


def test_run_check_reports_daemon_down():
    refused = CommandResult(1, "", "Cannot connect to the Docker daemon")
    down = FakeBackend(fail={"version": refused})
    out = io.StringIO()
    assert run_check(ToolSet(Docker(down)), out) == 1
    assert "NOT AVAILABLE" in out.getvalue()


def test_main_check_uses_injected_backend(capsys):
    assert main(["--check"], backend=FakeBackend()) == 0
    assert "reachable" in capsys.readouterr().out


def test_main_rejects_conflicting_flags(capsys):
    assert main(["--read-only", "--allow-remove"], backend=FakeBackend()) == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_main_check_read_only_lists_fewer_tools(capsys):
    main(["--check", "--read-only"], backend=FakeBackend())
    tools_line = capsys.readouterr().out.splitlines()[1]
    assert "stop_container" not in tools_line and "list_containers" in tools_line
