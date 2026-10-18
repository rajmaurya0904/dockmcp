import json

import pytest

from dockmcp.docker import Docker
from dockmcp.protocol import SUPPORTED_VERSIONS, McpServer
from dockmcp.tools import ToolSet
from tests.fakes import FakeBackend


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def server(backend) -> McpServer:
    return McpServer(ToolSet(Docker(backend)))


def rpc(server: McpServer, method: str, params=None, id=1):
    message = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle_message(message)


def call(server, name, arguments=None):
    return rpc(server, "tools/call", {"name": name, "arguments": arguments or {}})["result"]


def test_initialize_echoes_supported_version(server):
    reply = rpc(server, "initialize", {"protocolVersion": "2024-11-05"})
    assert reply["id"] == 1
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["serverInfo"]["name"] == "dockmcp"
    assert "tools" in reply["result"]["capabilities"]


def test_initialize_falls_back_to_latest_for_unknown_version(server):
    reply = rpc(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert reply["result"]["protocolVersion"] == SUPPORTED_VERSIONS[0]


def test_initialize_without_version_is_invalid_params(server):
    reply = rpc(server, "initialize", {})
    assert reply["error"]["code"] == -32602


def test_initialized_notification_gets_no_reply(server):
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.initialized is True


def test_unknown_notification_is_ignored(server):
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/whatever"}) is None


def test_ping(server):
    assert rpc(server, "ping")["result"] == {}


def test_unknown_method(server):
    reply = rpc(server, "resources/list")
    assert reply["error"]["code"] == -32601
    assert "resources/list" in reply["error"]["message"]


def test_string_ids_are_preserved(server):
    assert rpc(server, "ping", id="abc")["id"] == "abc"


def test_bad_id_type_rejected(server):
    reply = server.handle_message({"jsonrpc": "2.0", "id": [1], "method": "ping"})
    assert reply["error"]["code"] == -32600


def test_wrong_jsonrpc_version_rejected(server):
    reply = server.handle_message({"jsonrpc": "1.0", "id": 1, "method": "ping"})
    assert reply["error"]["code"] == -32600


def test_non_object_message_rejected(server):
    assert server.handle_message(42)["error"]["code"] == -32600


def test_params_must_be_object(server):
    reply = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]})
    assert reply["error"]["code"] == -32602


def test_client_responses_are_ignored(server):
    assert server.handle_message({"jsonrpc": "2.0", "id": 9, "result": {}}) is None


def test_handle_line_parse_error(server):
    reply = json.loads(server.handle_line("{not json"))
    assert reply["error"]["code"] == -32700
    assert reply["id"] is None


def test_handle_line_round_trip(server):
    out = server.handle_line('{"jsonrpc":"2.0","id":5,"method":"ping"}')
    assert json.loads(out) == {"jsonrpc": "2.0", "id": 5, "result": {}}


def test_batch_returns_only_replies_to_requests(server):
    replies = server.handle_message(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "nope"},
        ]
    )
    assert [r["id"] for r in replies] == [1, 2]
    assert "error" in replies[1]


def test_empty_batch_is_invalid(server):
    assert server.handle_message([])["error"]["code"] == -32600


def test_tools_list_has_valid_schemas(server):
    tools = rpc(server, "tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"list_containers", "container_logs", "start_container", "stop_container"} <= names
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]
        assert set(tool["inputSchema"]["required"]) <= set(tool["inputSchema"]["properties"])
        assert isinstance(tool["annotations"]["readOnlyHint"], bool)
    json.dumps(tools)  # must be serialisable


def test_unknown_tool_is_a_protocol_error(server):
    reply = rpc(server, "tools/call", {"name": "launch_missiles", "arguments": {}})
    assert reply["error"]["code"] == -32602


def test_tools_call_requires_name(server):
    assert rpc(server, "tools/call", {})["error"]["code"] == -32602


def test_call_list_containers_renders_a_table(server):
    result = call(server, "list_containers")
    text = result["content"][0]["text"]
    assert result["isError"] is False
    assert "web" in text and "nginx:1.27" in text and "0.0.0.0:8080->80/tcp" in text
    assert "db" not in text.split("\n", 1)[1].replace("nginx", "")  # stopped, so not listed


def test_call_start_runs_the_right_docker_command(server, backend):
    result = call(server, "start_container", {"container": "db"})
    assert result["content"][0]["text"] == "Started db."
    assert backend.calls == [["start", "db"]]


def test_argument_errors_are_tool_errors_not_protocol_errors(server, backend):
    result = call(server, "stop_container", {"container": "web", "timeout_seconds": "soon"})
    assert result["isError"] is True
    assert "timeout_seconds" in result["content"][0]["text"]
    assert backend.calls == []


def test_option_injection_is_rejected(server, backend):
    result = call(server, "start_container", {"container": "--all"})
    assert result["isError"] is True
    assert backend.calls == []


def test_docker_failure_surfaces_as_is_error(server):
    result = call(server, "inspect_container", {"container": "ghost"})
    assert result["isError"] is True
    assert "No such container" in result["content"][0]["text"]


def test_unexpected_exception_does_not_kill_the_server(server, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(server.tools, "call", boom)
    reply = rpc(server, "tools/call", {"name": "list_images", "arguments": {}})
    assert reply["error"]["code"] == -32603
    assert rpc(server, "ping")["result"] == {}
