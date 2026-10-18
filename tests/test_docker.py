import sys
import textwrap

import pytest

from dockmcp.docker import (
    CommandResult,
    Docker,
    DockerCLI,
    DockerError,
    DockerUnavailable,
    InvalidArgument,
    validate_image,
    validate_ref,
)
from tests.fakes import FakeBackend


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def docker(backend: FakeBackend) -> Docker:
    return Docker(backend)


def test_list_running_only_by_default(docker, backend):
    rows = docker.list_containers()
    assert [r["Names"] for r in rows] == ["web"]
    assert "--all" not in backend.calls[0]


def test_list_all_adds_flag_and_filters(docker, backend):
    rows = docker.list_containers(all=True, name="db", status="exited", label="tier=data")
    assert len(rows) == 2  # the fake does not filter; the real CLI would
    call = backend.calls[0]
    assert "--all" in call
    for flt in ("name=db", "status=exited", "label=tier=data"):
        assert call[call.index(flt) - 1] == "--filter"


def test_list_images(docker):
    assert docker.list_images()[0]["Repository"] == "nginx"


def test_inspect_returns_first_document(docker):
    assert docker.inspect("web")["Name"] == "/web"


def test_inspect_missing_container_raises_with_docker_message(docker):
    with pytest.raises(DockerError, match="No such container"):
        docker.inspect("ghost")


def test_logs_merges_stdout_and_stderr(docker):
    text = docker.logs("web", tail=5, since="10m", timestamps=True)
    assert "line one" in text and "something on stderr" in text


def test_logs_command_shape(docker, backend):
    docker.logs("web", tail=5, since="10m", timestamps=True)
    assert backend.calls[0] == ["logs", "--tail", "5", "--since", "10m", "--timestamps", "web"]


def test_stop_passes_grace_period(docker, backend):
    docker.stop("web", timeout=3)
    assert backend.calls[0] == ["stop", "--time", "3", "web"]


def test_restart_start_remove_command_shapes(docker, backend):
    docker.start("web")
    docker.restart("web", timeout=1)
    docker.remove("web", force=True, volumes=True)
    assert backend.calls == [
        ["start", "web"],
        ["restart", "--time", "1", "web"],
        ["rm", "--force", "--volumes", "web"],
    ]


def test_stats_single_container(docker, backend):
    assert docker.stats("web")[0]["Name"] == "web"
    assert backend.calls[0][-1] == "web"


def test_version_parses_json(docker):
    assert docker.version()["Server"]["Version"] == "27.3.1"


@pytest.mark.parametrize("bad", ["", "-rf", "--all", "a b", "a;b", "../x", 5, None])
def test_validate_ref_rejects_option_like_or_odd_values(bad):
    with pytest.raises(InvalidArgument):
        validate_ref(bad)


@pytest.mark.parametrize("good", ["web", "my-app_1.2", "a1b2c3d4e5f6"])
def test_validate_ref_accepts_names_and_ids(good):
    assert validate_ref(good) == good


def test_validate_image():
    assert validate_image("ghcr.io/org/app:1.0") == "ghcr.io/org/app:1.0"
    with pytest.raises(InvalidArgument):
        validate_image("--privileged")


def test_validation_happens_before_any_docker_call(docker, backend):
    with pytest.raises(InvalidArgument):
        docker.stop("--help")
    assert backend.calls == []


@pytest.mark.parametrize(
    "stderr",
    [
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?",
        "error during connect: Get http://%2F%2F.%2Fpipe%2Fdocker_engine/v1.24/containers/json",
    ],
)
def test_daemon_down_becomes_unavailable(stderr):
    docker = Docker(FakeBackend(fail={"ps": CommandResult(1, "", stderr)}))
    with pytest.raises(DockerUnavailable, match="Start Docker Desktop"):
        docker.list_containers()


def test_other_failures_stay_plain_docker_errors():
    docker = Docker(FakeBackend(fail={"start": CommandResult(1, "", "Error: boom")}))
    with pytest.raises(DockerError) as info:
        docker.start("web")
    assert not isinstance(info.value, DockerUnavailable)
    assert info.value.returncode == 1


def test_non_json_ps_output_is_reported():
    docker = Docker(FakeBackend(fail={"ps": CommandResult(0, "this is not json\n", "")}))
    with pytest.raises(DockerError, match="non-JSON"):
        docker.list_containers()


def test_missing_binary_is_reported_as_unavailable():
    cli = DockerCLI("definitely-not-a-real-docker-binary")
    with pytest.raises(DockerUnavailable, match="not found on PATH"):
        cli.run(["ps"])


def test_real_subprocess_round_trip(tmp_path):
    """Drive DockerCLI against a real child process standing in for ``docker``."""
    script = tmp_path / "fake_docker.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys
            if sys.argv[1:2] == ["ps"]:
                print(json.dumps({"Names": "demo", "State": "running"}))
            else:
                sys.stderr.write("Error: nope\\n")
                sys.exit(3)
            """
        )
    )
    docker = Docker(DockerCLI([sys.executable, str(script)]))
    assert docker.list_containers()[0]["Names"] == "demo"
    with pytest.raises(DockerError) as info:
        docker.start("demo")
    assert info.value.returncode == 3
    assert "nope" in str(info.value)


def test_subprocess_timeout(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(5)\n")
    cli = DockerCLI([sys.executable, str(script)], timeout=0.3)
    with pytest.raises(DockerError, match="timed out"):
        cli.run(["ps"])


def test_logs_normalise_windows_line_endings():
    backend = FakeBackend()
    backend.logs_output = ("a\r\nb\r\n", "")
    assert Docker(backend).logs("web") == "a\nb\n"
