"""Minimal MCP (JSON-RPC 2.0) server core: no I/O, just message in, message out."""

from __future__ import annotations

import json
from typing import Any

from . import __version__
from .tools import ToolError, ToolSet

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "Controls the local Docker engine. Use list_containers first to find names, "
    "container_logs and inspect_container to diagnose, then start_container, "
    "stop_container or restart_container to act."
)


def error_response(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def result_response(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


class McpServer:
    """Handles one JSON-RPC message at a time on behalf of a :class:`ToolSet`."""

    def __init__(self, tools: ToolSet) -> None:
        self.tools = tools
        self.initialized = False

    # -- entry points -----------------------------------------------------------

    def handle_line(self, line: str) -> str | None:
        """Parse one line of input; return the serialised reply, or None for no reply."""
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            return json.dumps(error_response(None, PARSE_ERROR, f"Parse error: {exc.msg}"))
        reply = self.handle_message(message)
        return None if reply is None else json.dumps(reply, ensure_ascii=False)

    def handle_message(self, message: Any) -> Any:
        if isinstance(message, list):
            if not message:
                return error_response(None, INVALID_REQUEST, "Empty batch")
            replies = [r for r in (self._handle_one(m) for m in message) if r is not None]
            return replies or None
        return self._handle_one(message)

    # -- dispatch -----------------------------------------------------------------

    def _handle_one(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return error_response(None, INVALID_REQUEST, "Not a JSON-RPC 2.0 message")
        method = message.get("method")
        has_id = "id" in message
        req_id = message.get("id")
        if not isinstance(method, str):
            # A response from the client (we never send requests) or garbage.
            if has_id and ("result" in message or "error" in message):
                return None
            return error_response(req_id if has_id else None, INVALID_REQUEST, "Missing method")
        if has_id and (isinstance(req_id, bool) or not isinstance(req_id, (str, int))):
            return error_response(None, INVALID_REQUEST, "Request id must be a string or integer")
        params = message.get("params")
        if params is not None and not isinstance(params, dict):
            if not has_id:
                return None
            return error_response(req_id, INVALID_PARAMS, "params must be an object")

        if not has_id:
            self._notification(method)
            return None

        handler = {
            "initialize": self._initialize,
            "ping": lambda _p: {},
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
        }.get(method)
        if handler is None:
            return error_response(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
        try:
            return result_response(req_id, handler(params or {}))
        except _ParamsError as exc:
            return error_response(req_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # last resort: never let a bug kill the stdio loop
            return error_response(req_id, INTERNAL_ERROR, f"Internal error: {exc}")

    def _notification(self, method: str) -> None:
        if method == "notifications/initialized":
            self.initialized = True

    # -- methods ------------------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if not isinstance(requested, str):
            raise _ParamsError("initialize requires a protocolVersion string")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "dockmcp", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"tools": self.tools.describe()}

    def _tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise _ParamsError("tools/call requires a tool name")
        if name not in self.tools.tools:
            raise _ParamsError(f"Unknown tool: {name}")
        try:
            text = self.tools.call(name, params.get("arguments"))
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        return {"content": [{"type": "text", "text": text}], "isError": False}


class _ParamsError(Exception):
    pass
