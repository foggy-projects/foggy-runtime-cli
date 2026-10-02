"""Open a semantic query through the launcher's existing MCP viewer tool."""
from __future__ import annotations

import argparse
import json
from typing import Any

from .client import RuntimeTransportError
from .input_utils import read_json_payload

TOOL_NAME = "dataset.open_in_viewer"
MCP_PATHS = ("/mcp/analyst/rpc", "/mcp/analyst", "/mcp/business/rpc", "/mcp/business")


def register_query_command(commands: Any) -> None:
    command = commands.add_parser(
        "open-in-viewer",
        help="Create a server-issued data-viewer link using the existing MCP connection authorization.",
    )
    command.add_argument("model")
    command.add_argument("--payload", required=True, help="Query DSL JSON file, or '-' for stdin.")
    command.add_argument("--title")
    command.add_argument("--mcp-path", choices=MCP_PATHS, default=MCP_PATHS[0],
                         help="MCP RPC path on --base-url; discovery verifies the tool before calling it.")
    command.set_defaults(runtime_handler=open_in_viewer)


def failure(code: str, phase: str, message: str) -> dict[str, Any]:
    return {"success": False, "engine": "mcp", "error": {"code": code, "phase": phase, "message": message}}


def rpc_result(response: dict[str, Any], phase: str) -> dict[str, Any]:
    if response.get("error") is not None:
        error = response["error"]
        message = error.get("message", "MCP request failed") if isinstance(error, dict) else "MCP request failed"
        raise ValueError(f"{phase}: {message}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"{phase}: missing MCP result object")
    return result


def open_in_viewer(args: argparse.Namespace, client: Any, _base_url: str) -> tuple[dict[str, Any], int]:
    try:
        payload = read_json_payload(args.payload, args._stdin)
        if not isinstance(payload.get("columns"), list) or not payload["columns"]:
            raise ValueError("Viewer payload requires a nonempty columns array")
        if not isinstance(payload.get("slice"), list) or not payload["slice"]:
            raise ValueError("Viewer payload requires a nonempty detail slice; aggregate filters remain in having")
    except (OSError, ValueError) as exc:
        return failure("INPUT_ERROR", "viewer.input", str(exc)), 1
    try:
        listing = client.request("POST", args.mcp_path, {
            "jsonrpc": "2.0", "id": "foggy-cli-viewer-list", "method": "tools/list", "params": {},
        })
        tools = rpc_result(listing, "tools/list").get("tools")
        if not isinstance(tools, list):
            raise ValueError("tools/list: missing tools array")
        if not any(isinstance(tool, dict) and tool.get("name") == TOOL_NAME for tool in tools):
            return failure("UNSUPPORTED_VIEWER_TOOL", "viewer.discovery",
                           "This MCP endpoint does not advertise dataset.open_in_viewer; enable the viewer or select the configured MCP path."), 3
        arguments = {"model": args.model, "payload": payload}
        if args.title:
            arguments["title"] = args.title
        response = client.request("POST", args.mcp_path, {
            "jsonrpc": "2.0", "id": "foggy-cli-viewer-open", "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": arguments},
        })
        result = rpc_result(response, "tools/call")
        if result.get("isError") or result.get("status") == "failed":
            return failure("VIEWER_TOOL_ERROR", "viewer.open", "MCP viewer tool reported a failure"), 2
        data = result.get("structuredContent")
        if not isinstance(data, dict):
            data = None
            content = result.get("content", [])
            if not isinstance(content, list):
                raise ValueError("Viewer tool content is not an array")
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    try:
                        decoded = json.loads(item.get("text", ""))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(decoded, dict) and "viewerUrl" in decoded:
                        data = decoded
                        break
        if not isinstance(data, dict) or not isinstance(data.get("viewerUrl"), str) or not data["viewerUrl"]:
            raise ValueError("Viewer tool did not return a viewerUrl; no link was synthesized")
        return {"success": True, "engine": "mcp", "data": data}, 0
    except RuntimeTransportError as exc:
        return failure("TRANSPORT_ERROR", "viewer.mcp", str(exc)), 4
    except ValueError as exc:
        return failure("MCP_ERROR", "viewer.mcp", str(exc)), 2
