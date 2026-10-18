# dockmcp

**An MCP server that lets Claude Code (or any MCP client) list, start, stop and read the logs of your Docker containers, so you can ask instead of typing `docker ps`.**

You are mid-debug, the worker container died, and you are alt-tabbing to a terminal to run `docker ps -a`, then `docker logs --tail 50 worker`, then `docker start worker`. dockmcp puts those commands behind a stdio MCP server, so "why did the worker exit, and restart it" becomes one request to your assistant. It shells out to the `docker` client you already have, so it works with Docker Desktop on Windows and macOS as well as plain Linux engines.

This machine has no Docker installed, so here is what the server prints, first against the stand-in `docker` executable the test-suite uses (`tests/fake_docker.py`, two made-up containers), then against the real, missing, Docker. The requests are plain JSON-RPC lines on stdin.

```console
$ dockmcp --docker-bin ./fakedocker.cmd
> {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_containers","arguments":{"all":true}}}
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "2 container(s)\nNAME    STATE    IMAGE  STATUS                   PORTS                   ID\napi     running  app:1  Up 5 minutes             0.0.0.0:3000->3000/tcp  111111111111\nworker  exited   app:1  Exited (1) 1 minute ago  -                       222222222222"}], "isError": false}}
> {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"container_logs","arguments":{"container":"worker","tail":5}}}
{"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "boot\nfatal: queue gone"}], "isError": false}}
> {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"start_container","arguments":{"container":"wroker"}}}
{"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text": "Error response from daemon: No such container: wroker\nDid you mean: worker?"}], "isError": true}}
```

With no Docker at all the server still starts, and every tool fails with a message the model can act on:

```console
$ dockmcp --check
dockmcp 0.1.0
tools: docker_status, list_containers, inspect_container, container_logs, container_stats, list_images, start_container, stop_container, restart_container
docker: NOT AVAILABLE - The 'docker' executable was not found on PATH. Install Docker (Docker Desktop on Windows/macOS) or pass --docker-bin to point at it.
$ echo $?
1
```

## Install

dockmcp is not on PyPI yet. It needs Python 3.11+ and has no runtime dependencies.

```bash
pip install -e .
```

## Usage

```bash
# Register it with Claude Code (any MCP client that can launch a stdio command works)
claude mcp add docker -- dockmcp

# Inspection only: no start/stop/restart tools are offered at all
claude mcp add docker -- dockmcp --read-only

# Check that Docker is reachable and see which tools would be offered
dockmcp --check

# Also offer remove_container (off by default)
dockmcp --allow-remove
```

For clients configured with JSON:

```json
{ "mcpServers": { "docker": { "command": "dockmcp", "args": ["--read-only"] } } }
```

After that, ask in plain language: "what containers are down?", "show the last 200 lines from api since 10 minutes ago", "restart the worker".

## How it works

- **Transport.** Newline-delimited JSON-RPC 2.0 on stdin/stdout, implemented in `dockmcp/protocol.py` without the MCP SDK. It handles `initialize` (negotiating 2025-06-18, 2025-03-26 or 2024-11-05), `ping`, `tools/list` and `tools/call`, ignores notifications, answers batches, and returns proper JSON-RPC error codes for bad input. A bug inside a tool becomes an error reply, not a dead server.
- **Tools.** `docker_status`, `list_containers` (running or all, filter by name, state or label), `inspect_container`, `container_logs` (tail, since, timestamps), `container_stats`, `list_images`, `start_container`, `stop_container`, `restart_container` and, only with `--allow-remove`, `remove_container`. Each carries a JSON schema and read-only/destructive annotations.
- **Docker access.** Every tool is a few calls to the `docker` CLI using `--format '{{json .}}'`, run without a shell. Container arguments must look like a name or id and may not start with `-`, so a model cannot smuggle in flags.
- **Output for models.** Tables and short messages instead of raw JSON. Logs are capped at 20,000 characters keeping the newest lines. `inspect_container` reports environment variable names but never their values. A wrong container name gets a "did you mean" from the existing names.
- **Failure modes.** Missing binary, stopped daemon, command timeout and a nonzero docker exit all come back as `isError` tool results with the underlying message.

## Limitations

- There is no Docker on the machine this was written on. The docker layer is tested through a fake backend and through a stateful stand-in executable, over a real stdio session; it has not been run against a live Docker daemon. The command lines it builds are plain `docker ps/inspect/logs/stats/images/start/stop/restart/rm` invocations, but treat the first run on your machine accordingly.
- Container management only: no `docker run`, image pulls, builds, exec, networks, volumes or Compose.
- `container_stats` is a one-shot sample, not a stream.
- Talks to whichever daemon the `docker` client points at (honouring `DOCKER_HOST` and contexts). It has no authentication of its own, because MCP over stdio inherits the permissions of the user who launched it.
- Stdio transport only; no HTTP/SSE transport, resources or prompts.

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```

The suite has unit tests for command construction and validation, tests of the JSON-RPC layer, and end-to-end tests that spawn the real `dockmcp` process and talk to it over stdio against `tests/fake_docker.py`.

## Licence

MIT. See [LICENSE](LICENSE).
