"""Save PNG query visualizations from the Runtime's shared MCP renderer."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .client import RuntimeTransportError
from .input_utils import read_json_payload
from .viewer_cli import MCP_PATHS, failure, rpc_result

TOOL_NAME = "dataset.export_image"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def register_query_command(commands: Any) -> None:
    command = commands.add_parser(
        "export-image",
        help="Query the model and save a PNG rendered inside the Java Runtime.",
    )
    command.add_argument("model")
    command.add_argument("--payload", required=True, help="Query DSL JSON file, or '-' for stdin.")
    command.add_argument("--kind", choices=("table", "bar", "line"), default="table")
    command.add_argument("--out", required=True, help="Destination PNG file; its parent directory must exist.")
    command.add_argument("--title")
    command.add_argument("--x", dest="x_field", help="Explicit result field for the chart categories.")
    command.add_argument("--y", dest="y_field", help="Explicit numeric result field for the chart values.")
    command.add_argument("--x-label")
    command.add_argument("--y-label")
    command.add_argument("--unit")
    command.add_argument("--columns", help="UTF-8 JSON array of {field, caption?, unit?} for table columns.")
    command.add_argument("--max-rows", help="Display at most 50 table rows, 40 bars or 100 line points; truncation is labelled.")
    command.add_argument("--width", default=1200, help="PNG width in pixels (640..2400; default: 1200).")
    command.add_argument("--height", help="Requested height in pixels (table: 360..2400, charts: 520..2400); tables size automatically.")
    command.add_argument("--mcp-path", choices=MCP_PATHS, default=MCP_PATHS[0],
                         help="MCP RPC path on --base-url; discovery verifies the tool before querying.")
    command.set_defaults(runtime_handler=export_image)


def _bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _read_columns(path: str) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        try:
            columns = json.load(handle)
        except ValueError as exc:
            raise ValueError("--columns must contain a JSON array") from exc
    if not isinstance(columns, list) or not 1 <= len(columns) <= 12:
        raise ValueError("--columns requires an array of 1..12 column objects")
    for column in columns:
        if not isinstance(column, dict) or set(column) - {"field", "caption", "unit"}:
            raise ValueError("Each image column must contain only field, caption and unit")
        if not isinstance(column.get("field"), str) or not column["field"].strip():
            raise ValueError("Each image column requires an explicit nonempty field")
        if any(not isinstance(value, str) for value in column.values()):
            raise ValueError("Image column field, caption and unit must be strings")
    if len({column["field"] for column in columns}) != len(columns):
        raise ValueError("Image column fields must be unique")
    return columns


def _arguments(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    payload = read_json_payload(args.payload, args._stdin)
    if not isinstance(payload.get("columns"), list) or not payload["columns"]:
        raise ValueError("Image query payload requires a nonempty columns array")
    if not args.model.strip():
        raise ValueError("Image export requires an explicit model")
    if args.kind != "table" and not (args.x_field and args.x_field.strip() and args.y_field and args.y_field.strip()):
        raise ValueError("Bar and line images require explicit --x and --y result fields")
    destination = Path(args.out).expanduser().absolute()
    if not destination.parent.is_dir() or destination.is_dir():
        raise ValueError("The PNG destination must be a file in an existing directory")
    arguments: dict[str, Any] = {
        "model": args.model, "payload": payload, "kind": args.kind,
        "delivery": "inline", "width": _bounded_integer(args.width, "--width", 640, 2400),
    }
    for name, attribute in (
        ("title", "title"), ("xField", "x_field"), ("yField", "y_field"),
        ("xLabel", "x_label"), ("yLabel", "y_label"), ("unit", "unit"),
    ):
        value = getattr(args, attribute)
        if value is not None:
            if not value.strip():
                raise ValueError(f"{name} must be a nonempty string when provided")
            arguments[name] = value
    if args.max_rows is not None:
        arguments["maxRows"] = _bounded_integer(args.max_rows, "--max-rows", 1, {"table": 50, "bar": 40, "line": 100}[args.kind])
    if args.height is not None:
        arguments["height"] = _bounded_integer(args.height, "--height", 360 if args.kind == "table" else 520, 2400)
    if args.columns is not None:
        arguments["columns"] = _read_columns(args.columns)
    return arguments, destination


def _tool_data(result: dict[str, Any]) -> tuple[dict[str, Any] | None, list[Any]]:
    content = result.get("content", [])
    if not isinstance(content, list):
        raise ValueError("Image tool content is not an array")
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        data = None
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    decoded = json.loads(item.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(decoded, dict) and ("success" in decoded or "image" in decoded):
                    data = decoded
                    break
    return data, content


def _png(data: dict[str, Any] | None, content: list[Any]) -> tuple[bytes, int, int]:
    image = data.get("image") if data else None
    if not isinstance(image, dict) or "data" not in image:
        image = next((item for item in content if isinstance(item, dict) and item.get("type") == "image"), None)
    if not isinstance(image, dict) or image.get("mimeType") != "image/png" or not isinstance(image.get("data"), str):
        raise ValueError("Image tool must return inline image/png content; URLs are not downloaded")
    encoded = image["data"]
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("Image tool PNG exceeds the 8 MiB limit")
    try:
        png = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Image tool returned invalid base64 PNG content") from exc
    if len(png) > MAX_IMAGE_BYTES:
        raise ValueError("Image tool PNG exceeds the 8 MiB limit")
    width, height = _validate_png_chunks(png)
    return png, width, height


def _validate_png_chunks(png: bytes) -> tuple[int, int]:
    if len(png) < 45 or png[:8] != PNG_SIGNATURE:
        raise ValueError("Image tool returned invalid or truncated PNG content")
    offset = 8
    width = height = bit_depth = color_type = 0
    palette_seen = data_seen = data_finished = False
    data_bytes = 0
    while offset < len(png):
        if len(png) - offset < 12:
            raise ValueError("Image tool returned a truncated PNG chunk")
        length = struct.unpack_from(">I", png, offset)[0]
        end = offset + 12 + length
        if length > 0x7FFFFFFF or end > len(png):
            raise ValueError("Image tool returned a truncated PNG chunk")
        chunk_type = png[offset + 4:offset + 8]
        if (any(not (65 <= value <= 90 or 97 <= value <= 122) for value in chunk_type)
                or chunk_type[2] & 32):
            raise ValueError("Image tool returned an invalid PNG chunk type")
        actual_crc = zlib.crc32(memoryview(png)[offset + 4:end - 4]) & 0xFFFFFFFF
        if actual_crc != struct.unpack_from(">I", png, end - 4)[0]:
            raise ValueError("Image tool returned PNG content with an invalid chunk CRC")
        if offset == 8:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("Image tool PNG must begin with one valid IHDR chunk")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack_from(
                ">IIBBBBB", png, offset + 8)
            if not (1 <= width <= 2400 and 1 <= height <= 4096 and width * height <= 8_000_000):
                raise ValueError("Image tool returned PNG dimensions outside the supported limits")
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if bit_depth not in depths.get(color_type, ()) or compression != 0 or filtering != 0 or interlace not in (0, 1):
                raise ValueError("Image tool returned an invalid PNG IHDR format")
        elif chunk_type == b"IHDR":
            raise ValueError("Image tool PNG contains a duplicate IHDR chunk")
        elif chunk_type == b"PLTE":
            if (palette_seen or data_seen or color_type in (0, 4) or not 3 <= length <= 768
                    or length % 3 or (color_type == 3 and length // 3 > 1 << bit_depth)):
                raise ValueError("Image tool PNG contains an invalid or misplaced PLTE chunk")
            palette_seen = True
        elif chunk_type == b"IDAT":
            if data_finished or (color_type == 3 and not palette_seen):
                raise ValueError("Image tool PNG contains invalid IDAT chunk ordering")
            data_seen = True
            data_bytes += length
        elif chunk_type == b"IEND":
            if length != 0 or not data_seen or data_bytes == 0 or end != len(png):
                raise ValueError("Image tool PNG requires image data and one final empty IEND chunk")
            return width, height
        elif not chunk_type[0] & 32:
            raise ValueError("Image tool PNG contains an unsupported critical chunk")
        if data_seen and chunk_type != b"IDAT":
            data_finished = True
        offset = end
    raise ValueError("Image tool returned PNG content without a final IEND chunk")


def _write_atomic(destination: Path, png: bytes) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".foggy-image-", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(png)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _safe_message(message: str, args: argparse.Namespace, client: Any) -> str:
    for source in (args, client):
        for name in ("auth_code", "authorization"):
            credential = getattr(source, name, None)
            if isinstance(credential, str) and credential:
                message = message.replace(credential, "[REDACTED]")
        configured_url = getattr(source, "base_url", None)
        if isinstance(configured_url, str) and configured_url:
            message = message.replace(configured_url.rstrip("/"), "[Runtime]")
            try:
                parsed_url = urlsplit(configured_url)
            except ValueError:
                continue
            if parsed_url.scheme and parsed_url.netloc:
                message = message.replace(f"{parsed_url.scheme}://{parsed_url.netloc}", "[Runtime]")
    return message


def export_image(args: argparse.Namespace, client: Any, _base_url: str) -> tuple[dict[str, Any], int]:
    try:
        arguments, destination = _arguments(args)
    except (OSError, ValueError) as exc:
        return failure("INPUT_ERROR", "image.input", _safe_message(str(exc), args, client)), 1
    try:
        listing = client.request("POST", args.mcp_path, {
            "jsonrpc": "2.0", "id": "foggy-cli-image-list", "method": "tools/list", "params": {},
        })
        tools = rpc_result(listing, "tools/list").get("tools")
        if not isinstance(tools, list):
            raise ValueError("tools/list: missing tools array")
        if not any(isinstance(tool, dict) and tool.get("name") == TOOL_NAME for tool in tools):
            return failure("UNSUPPORTED_IMAGE_TOOL", "image.discovery",
                           "This MCP endpoint does not advertise dataset.export_image; update the Java Runtime or select its configured MCP path."), 3
        response = client.request("POST", args.mcp_path, {
            "jsonrpc": "2.0", "id": "foggy-cli-image-export", "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": arguments},
        })
        result = rpc_result(response, "tools/call")
        data, content = _tool_data(result)
        if data is not None and "success" in data and not isinstance(data["success"], bool):
            raise ValueError("Image tool success must be a boolean")
        if result.get("isError") or result.get("status") == "failed" or (data and data.get("success") is False):
            error = data.get("error") if data else None
            error = error if isinstance(error, dict) else {}
            return failure(_safe_message(str(error.get("code", "IMAGE_TOOL_ERROR")), args, client), "image.export",
                           _safe_message(str(error.get("message", "MCP image tool reported a failure")), args, client)), 2
    except RuntimeTransportError as exc:
        return failure("TRANSPORT_ERROR", "image.mcp", _safe_message(str(exc), args, client)), 4
    except ValueError as exc:
        return failure("MCP_ERROR", "image.mcp", _safe_message(str(exc), args, client)), 2
    try:
        png, width, height = _png(data, content)
    except ValueError as exc:
        return failure("IMAGE_FORMAT_ERROR", "image.decode", str(exc)), 1
    try:
        _write_atomic(destination, png)
    except OSError as exc:
        return failure("IMAGE_WRITE_ERROR", "image.save", _safe_message(str(exc), args, client)), 1
    metadata: dict[str, Any] = {
        "file": str(destination), "mimeType": "image/png", "bytes": len(png),
        "width": width, "height": height, "kind": args.kind,
    }
    # Only expose useful numeric metadata, never the raw MCP body, base64 or runtime parameters.
    if data:
        for name in ("rowsRendered", "rowsReturned"):
            value = data.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metadata[name] = value
        if isinstance(data.get("truncated"), bool):
            metadata["truncated"] = data["truncated"]
    return {"success": True, "engine": "mcp", "data": metadata}, 0
