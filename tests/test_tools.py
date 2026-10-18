import json

import pytest

from dockmcp.docker import CommandResult, Docker
from dockmcp.tools import (
    MAX_LOG_CHARS,
    TOOLS,
    ToolError,
    ToolSet,
    check_arguments,
    table,
    truncate_tail,
)
from tests.fakes import FakeBackend


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def tools(backend) -> ToolSet:
    return ToolSet(Docker(backend))


def tool(name):
    return next(t for t in TOOLS if t.name == name)


def test_list_all_shows_stopped_containers(tools):
    text = tools.call("list_containers", {"all": True})
    assert "2 container(s)" in text
    assert "exited" in text and "postgres:16" in text


def test_list_uses_short_ids_and_dash_for_no_ports(tools):
    text = tools.call("list_containers", {"all": True})
    web_line = next(line for line in text.splitlines() if line.startswith("web"))
    assert web_line.endswith("a1b2c3d4e5f6")
    db_line = next(line for line in text.splitlines() if line.startswith("db"))
    assert " - " in db_line


def test_list_empty(tools):
    empty = ToolSet(Docker(FakeBackend(fail={"ps": CommandResult(0, "", "")})))
    assert empty.call("list_containers", {}) == "No running containers match."
    assert empty.call("list_containers", {"all": True}) == "No containers match."


def test_inspect_hides_environment_values(tools):
    text = tools.call("inspect_container", {"container": "web"})
    assert "hunter2" not in text
    body = json.loads(text.split("\n(environment")[0])
    assert body["env_variable_names"] == ["API_KEY", "BARE", "MODE"]
    assert body["name"] == "web" and body["status"] == "running"
    assert body["ports"]["80/tcp"] == ["0.0.0.0:8080"]
    assert body["ports"]["443/tcp"] == []
    assert body["restart_policy"] == "always"
    assert body["mounts"][0]["destination"] == "/usr/share/nginx/html"


def test_logs_default_tail_and_text(tools, backend):
    text = tools.call("container_logs", {"container": "web"})
    assert "line one" in text and "stderr" in text
    assert backend.calls[0][:3] == ["logs", "--tail", "100"]


def test_logs_empty(backend):
    backend.logs_output = ("", "")
    text = ToolSet(Docker(backend)).call("container_logs", {"container": "web"})
    assert "no log output" in text


def test_logs_tail_bounds_enforced(tools):
    for bad in (0, 5001, "10", True):
        with pytest.raises(ToolError):
            tools.call("container_logs", {"container": "web", "tail": bad})


def test_truncate_tail_keeps_the_end_and_says_so():
    text = "\n".join(f"line {i}" for i in range(10_000))
    out = truncate_tail(text)
    assert out.startswith("[... ") and "omitted" in out.splitlines()[0]
    assert out.endswith("line 9999")
    assert len(out) <= MAX_LOG_CHARS + 120


def test_truncate_tail_leaves_short_text_alone():
    assert truncate_tail("short") == "short"


def test_stats_table(tools):
    text = tools.call("container_stats", {})
    assert "CPU" in text.splitlines()[0] and "0.10%" in text


def test_list_images(tools):
    text = tools.call("list_images", {})
    assert "1 image(s)" in text and "nginx" in text and "sha256" not in text


def test_stop_defaults_to_ten_seconds(tools, backend):
    assert "grace period 10s" in tools.call("stop_container", {"container": "web"})
    assert backend.calls[0] == ["stop", "--time", "10", "web"]


def test_restart_message(tools):
    assert tools.call("restart_container", {"container": "web", "timeout_seconds": 0}).startswith(
        "Restarted web"
    )


def test_docker_status_reports_versions(tools):
    text = tools.call("docker_status", {})
    assert "27.3.1" in text and "reachable" in text


def test_read_only_hides_mutating_tools(backend):
    names = set(ToolSet(Docker(backend), read_only=True).tools)
    assert "list_containers" in names and "container_logs" in names
    assert not names & {"start_container", "stop_container", "restart_container"}


def test_remove_is_opt_in(backend):
    assert "remove_container" not in ToolSet(Docker(backend)).tools
    assert "remove_container" in ToolSet(Docker(backend), allow_remove=True).tools


def test_read_only_and_allow_remove_conflict(backend):
    with pytest.raises(ValueError):
        ToolSet(Docker(backend), read_only=True, allow_remove=True)


def test_remove_container_flags(backend):
    tools = ToolSet(Docker(backend), allow_remove=True)
    text = tools.call("remove_container", {"container": "db", "force": True, "volumes": True})
    assert "anonymous volumes" in text
    assert backend.calls[-1] == ["rm", "--force", "--volumes", "db"]


def test_removed_tool_cannot_be_called_when_disabled(tools):
    with pytest.raises(KeyError):
        tools.call("remove_container", {"container": "db"})


def test_destructive_flag_in_annotations():
    descriptors = {t.name: t.describe() for t in TOOLS}
    assert descriptors["remove_container"]["annotations"]["destructiveHint"] is True
    assert descriptors["list_containers"]["annotations"]["readOnlyHint"] is True
    assert descriptors["stop_container"]["annotations"]["readOnlyHint"] is False


def test_check_arguments_rejects_unknown_and_missing():
    t = tool("start_container")
    with pytest.raises(ToolError, match="Missing required"):
        check_arguments(t, {})
    with pytest.raises(ToolError, match="Unknown argument"):
        check_arguments(t, {"container": "web", "rm": True})
    with pytest.raises(ToolError, match="must be an object"):
        check_arguments(t, ["web"])


def test_check_arguments_enum_and_types():
    t = tool("list_containers")
    assert check_arguments(t, None) == {}
    with pytest.raises(ToolError, match="one of"):
        check_arguments(t, {"status": "sleeping"})
    with pytest.raises(ToolError, match="true or false"):
        check_arguments(t, {"all": "yes"})
    with pytest.raises(ToolError, match="must be a string"):
        check_arguments(t, {"name": 3})


def test_every_tool_description_mentions_nothing_empty():
    for t in TOOLS:
        assert len(t.description) > 20
        for spec in t.properties.values():
            assert spec["type"] in {"string", "integer", "boolean"}


def test_table_aligns_columns():
    out = table(["A", "BBB"], [["xx", "1"], ["y", "22"]])
    assert out.splitlines() == ["A   BBB", "xx  1", "y   22"]


def test_unknown_container_suggests_close_names(tools):
    with pytest.raises(ToolError) as info:
        tools.call("start_container", {"container": "wbe"})
    assert "Did you mean: web" in str(info.value)


def test_unknown_container_lists_existing_when_nothing_is_close(tools):
    with pytest.raises(ToolError) as info:
        tools.call("inspect_container", {"container": "zzzzzz"})
    assert "Existing containers: web, db" in str(info.value)
