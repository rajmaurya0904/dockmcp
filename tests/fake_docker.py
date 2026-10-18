"""A tiny stateful stand-in for the ``docker`` executable.

State lives in the JSON file named by ``FAKE_DOCKER_STATE``. Only the sub-commands
dockmcp uses are implemented, with the same flags and output shapes as the real client.
"""

import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_DOCKER_STATE"])


def load() -> dict:
    return json.loads(STATE.read_text())


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state))


def positional(args: list[str]) -> str:
    return args[-1]


def main(argv: list[str]) -> int:
    state = load()
    containers = state["containers"]
    verb, rest = argv[0], argv[1:]

    if os.environ.get("FAKE_DOCKER_DOWN"):
        sys.stderr.write("Cannot connect to the Docker daemon. Is the docker daemon running?\n")
        return 1

    if verb == "version":
        print(json.dumps({"Client": {"Version": "27.0.0"}, "Server": {"Version": "27.0.0"}}))
        return 0
    if verb == "ps":
        for c in containers:
            if "--all" in rest or c["State"] == "running":
                print(json.dumps({k: c[k] for k in ("ID", "Names", "Image", "State", "Status")}
                                 | {"Ports": c.get("Ports", "")}))
        return 0
    if verb in {"start", "stop", "restart", "logs", "inspect"}:
        name = positional(rest)
        match = next((c for c in containers if c["Names"] == name), None)
        if match is None:
            sys.stderr.write(f"Error response from daemon: No such container: {name}\n")
            return 1
        if verb == "logs":
            tail = int(rest[rest.index("--tail") + 1])
            lines = match["Logs"][-tail:]
            sys.stdout.write("".join(line + "\n" for line in lines))
            return 0
        if verb == "inspect":
            print(json.dumps([{
                "Id": match["ID"], "Name": "/" + name, "Config": {"Image": match["Image"]},
                "State": {"Status": match["State"], "Running": match["State"] == "running"},
            }]))
            return 0
        if verb == "start":
            match["State"], match["Status"] = "running", "Up 1 second"
        else:
            match["State"], match["Status"] = "exited", "Exited (0) just now"
            if verb == "restart":
                match["State"], match["Status"] = "running", "Up 1 second"
        save(state)
        print(name)
        return 0
    sys.stderr.write(f"fake docker: unsupported command {verb}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
