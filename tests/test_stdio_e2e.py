"""Run the real ``dockmcp`` process and speak MCP to it over stdio.

The docker executable is replaced by ``tests/fake_docker.py`` (wrapped in a launcher so
it can be passed to ``--docker-bin``). Everything else, including subprocess spawning,
framing and the CLI, is the real code path.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def make_launcher(tmp_path: Path) -> Path:
    script = HERE / "fake_docker.py"
    if os.name == "nt":
        launcher = tmp_path / "fakedocker.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        launcher = tmp_path / "fakedocker"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


@pytest.fixture
def env(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "containers": [
                    {"ID": "1" * 64, "Names": "api", "Image": "app:1", "State": "running",
                     "Status": "Up 5 minutes", "Ports": "0.0.0.0:3000->3000/tcp",
                     "Logs": [f"request {i}" for i in range(50)]},
                    {"ID": "2" * 64, "Names": "worker", "Image": "app:1", "State": "exited",
                     "Status": "Exited (1) 1 minute ago", "Logs": ["boot", "fatal: queue gone"]},
                ]
            }
        )
    )
    environment = dict(os.environ, FAKE_DOCKER_STATE=str(state), PYTHONIOENCODING="utf-8")
    environment.pop("FAKE_DOCKER_DOWN", None)
    return environment, make_launcher(tmp_path)


def talk(messages, launcher, environment, extra=()):
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, "-m", "dockmcp.server", "--docker-bin", str(launcher), *extra],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        timeout=60,
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return {r["id"]: r for r in replies}, proc


def tool_call(id_, name, **arguments):
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def text_of(reply):
    return reply["result"]["content"][0]["text"]


def test_full_session_list_stop_start_logs(env):
    environment, launcher = env
    replies, proc = talk(
        [
            INIT,
            INITIALIZED,
            tool_call(2, "list_containers"),
            tool_call(3, "list_containers", all=True),
            tool_call(4, "start_container", container="worker"),
            tool_call(5, "stop_container", container="api", timeout_seconds=1),
            tool_call(6, "list_containers", all=True),
            tool_call(7, "container_logs", container="api", tail=3),
            tool_call(8, "inspect_container", container="worker"),
        ],
        launcher,
        environment,
    )
    assert proc.returncode == 0
    assert replies[1]["result"]["serverInfo"]["name"] == "dockmcp"
    assert "api" in text_of(replies[2]) and "worker" not in text_of(replies[2])
    assert "worker" in text_of(replies[3])
    assert text_of(replies[4]) == "Started worker."
    assert text_of(replies[5]).startswith("Stopped api")
    final = text_of(replies[6])
    api_line = next(line for line in final.splitlines() if line.startswith("api"))
    worker_line = next(line for line in final.splitlines() if line.startswith("worker"))
    assert "exited" in api_line and "running" in worker_line
    assert text_of(replies[7]).splitlines() == ["request 47", "request 48", "request 49"]
    assert json.loads(text_of(replies[8]).split("\n(")[0])["status"] == "running"


def test_stderr_has_banner_but_stdout_is_pure_json(env):
    environment, launcher = env
    _, proc = talk([INIT], launcher, environment)
    assert "ready on stdio" in proc.stderr
    for line in proc.stdout.splitlines():
        json.loads(line)


def test_missing_container_is_reported_as_tool_error(env):
    environment, launcher = env
    call = tool_call(2, "container_logs", container="ghost")
    replies, _ = talk([INIT, call], launcher, environment)
    assert replies[2]["result"]["isError"] is True
    assert "No such container" in text_of(replies[2])


def test_daemon_down_message_is_actionable(env):
    environment, launcher = env
    environment["FAKE_DOCKER_DOWN"] = "1"
    replies, _ = talk([INIT, tool_call(2, "list_containers")], launcher, environment)
    assert replies[2]["result"]["isError"] is True
    assert "Start Docker Desktop" in text_of(replies[2])


def test_read_only_flag_removes_tools_from_the_list(env):
    environment, launcher = env
    list_tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    replies, _ = talk([INIT, list_tools], launcher, environment, extra=["--read-only"])
    names = {t["name"] for t in replies[2]["result"]["tools"]}
    assert "list_containers" in names and "stop_container" not in names


def test_blank_and_garbage_lines_do_not_stop_the_loop(env):
    environment, launcher = env
    proc = subprocess.run(
        [sys.executable, "-m", "dockmcp.server", "--docker-bin", str(launcher)],
        input='\n\nnot json\n{"jsonrpc":"2.0","id":3,"method":"ping"}\n',
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert replies[0]["error"]["code"] == -32700
    assert replies[1] == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_check_flag_reports_status(env):
    environment, launcher = env
    proc = subprocess.run(
        [sys.executable, "-m", "dockmcp.server", "--docker-bin", str(launcher), "--check"],
        capture_output=True, text=True, env=environment, timeout=60,
    )
    assert proc.returncode == 0
    assert "27.0.0" in proc.stdout and "list_containers" in proc.stdout


def test_check_flag_fails_cleanly_without_docker():
    proc = subprocess.run(
        [sys.executable, "-m", "dockmcp.server", "--docker-bin", "no-such-docker-bin", "--check"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 1
    assert "NOT AVAILABLE" in proc.stdout


def test_conflicting_flags_exit_with_usage_code():
    proc = subprocess.run(
        [sys.executable, "-m", "dockmcp.server", "--read-only", "--allow-remove"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "cannot be combined" in proc.stderr
