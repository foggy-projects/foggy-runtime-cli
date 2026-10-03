from __future__ import annotations

import base64
import io
import json
import struct
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from foggy_runtime_cli.client import RuntimeTransportError
from foggy_runtime_cli.main import main


PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGMwav72HwAFQQKriSbhRwAAAABJRU5ErkJggg=="
PNG = base64.b64decode(PNG_BASE64)


class FakeClient:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    responses: list[Any] = []
    namespace = authorization = auth_code = base_url = None

    def __init__(self, _base_url, namespace, _timeout, auth_code=None, authorization=None):
        type(self).base_url = _base_url
        type(self).namespace = namespace
        type(self).authorization = authorization
        type(self).auth_code = auth_code

    def request(self, method, path, body):
        type(self).calls.append((method, path, body))
        response = type(self).responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ImageCliTest(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        FakeClient.responses = []
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "销售额.png"
        self.payload = {"columns": ["month", "totalAmount"], "groupBy": ["month"],
                        "orderBy": ["month"], "slice": [{"field": "year", "op": "=", "value": 2026}]}

    def run_cli(self, responses, payload=None, extra=None):
        FakeClient.responses = responses
        out, err = io.StringIO(), io.StringIO()
        argv = ["--namespace", "sales", "--authorization", "opaque-header", "--auth-code", "runtime-code",
                "query", "export-image", "SalesQueryModel", "--payload", "-", "--out", str(self.destination)]
        code = main(argv + (extra or []), stdout=out, stderr=err,
                    stdin=io.StringIO(json.dumps(self.payload if payload is None else payload)), client_factory=FakeClient)
        return code, json.loads(out.getvalue()), err.getvalue()

    @staticmethod
    def listing():
        return {"result": {"tools": [{"name": "dataset.export_image"}]}}

    @staticmethod
    def image_result(image=None, **metadata):
        data = {"success": True, "image": image or {"mimeType": "image/png", "data": PNG_BASE64}, **metadata}
        return {"result": {"structuredContent": data}}

    def test_table_saves_exact_png_and_only_safe_metadata(self):
        response = self.image_result(rowsReturned=100, rowsRendered=50, truncated=True,
                                     token="opaque-header", connection="jdbc:private", params={"password": "secret"})
        code, output, err = self.run_cli([self.listing(), response])
        self.assertEqual(0, code)
        self.assertFalse(err)
        self.assertEqual(PNG, self.destination.read_bytes())
        self.assertEqual({"file": str(self.destination), "mimeType": "image/png", "bytes": len(PNG),
                          "width": 1, "height": 1, "kind": "table", "rowsReturned": 100,
                          "rowsRendered": 50, "truncated": True}, output["data"])
        self.assertNotIn(PNG_BASE64, json.dumps(output))
        self.assertNotIn("opaque-header", json.dumps(output))
        self.assertNotIn("jdbc:private", json.dumps(output))
        arguments = FakeClient.calls[1][2]["params"]["arguments"]
        self.assertEqual(self.payload, arguments["payload"])
        self.assertEqual("SalesQueryModel", arguments["model"])
        self.assertEqual("inline", arguments["delivery"])
        self.assertEqual(1200, arguments["width"])
        self.assertNotIn("maxRows", arguments)
        self.assertNotIn("height", arguments)
        self.assertNotIn("authorization", arguments)
        self.assertEqual("sales", FakeClient.namespace)
        self.assertEqual("opaque-header", FakeClient.authorization)

    def test_chart_options_and_explicit_mcp_path_preserve_query(self):
        code, _, _ = self.run_cli([self.listing(), self.image_result()], extra=[
            "--kind", "line", "--x", "month", "--y", "totalAmount", "--title", "月度销售额",
            "--x-label", "月份", "--y-label", "销售额", "--unit", "元", "--width", "1600",
            "--height", "900", "--max-rows", "100", "--mcp-path", "/mcp/business",
        ])
        self.assertEqual(0, code)
        self.assertTrue(all(call[1] == "/mcp/business" for call in FakeClient.calls))
        arguments = FakeClient.calls[1][2]["params"]["arguments"]
        self.assertEqual({"model": "SalesQueryModel", "payload": self.payload, "delivery": "inline", "kind": "line",
                          "xField": "month", "yField": "totalAmount", "title": "月度销售额", "xLabel": "月份",
                          "yLabel": "销售额", "unit": "元", "width": 1600, "height": 900, "maxRows": 100}, arguments)

    def test_utf8_bom_columns_file_preserves_captions_and_units(self):
        columns_file = Path(self.temporary.name) / "columns.json"
        columns = [{"field": "month", "caption": "月份"}, {"field": "totalAmount", "caption": "销售额", "unit": "元"}]
        columns_file.write_text(json.dumps(columns, ensure_ascii=False), encoding="utf-8-sig")
        code, _, _ = self.run_cli([self.listing(), self.image_result()], extra=["--columns", str(columns_file)])
        self.assertEqual(0, code)
        self.assertEqual(columns, FakeClient.calls[1][2]["params"]["arguments"]["columns"])

    def test_mcp_image_block_and_text_json_fallback_both_save(self):
        for result in ({"content": [{"type": "image", "mimeType": "image/png", "data": PNG_BASE64}]},
                       {"structuredContent": {"success": True, "image": {"mimeType": "image/png", "width": 1, "height": 1}},
                        "content": [{"type": "image", "mimeType": "image/png", "data": PNG_BASE64}]},
                       {"content": [{"type": "text", "text": json.dumps({"success": True, "image": {
                           "mimeType": "image/png", "data": PNG_BASE64}})}]}):
            with self.subTest(result=result):
                code, _, _ = self.run_cli([self.listing(), {"result": result}])
                self.assertEqual(0, code)
                self.assertEqual(PNG, self.destination.read_bytes())

    def test_invalid_input_stops_before_discovery(self):
        for extra in (["--kind", "bar"], ["--kind", "line", "--x", "month"], ["--width", "639"],
                      ["--height", "2401"], ["--max-rows", "51"], ["--kind", "bar", "--x", "month", "--y", "totalAmount", "--max-rows", "41"],
                      ["--max-rows", "zero"], ["--title", ""]):
            with self.subTest(extra=extra):
                FakeClient.calls = []
                code, output, _ = self.run_cli([], extra=extra)
                self.assertEqual(1, code)
                self.assertEqual("INPUT_ERROR", output["error"]["code"])
                self.assertFalse(FakeClient.calls)
        code, _, _ = self.run_cli([], payload={})
        self.assertEqual(1, code)

    def test_table_and_chart_minimum_heights(self):
        for kind, minimum in (("table", 360), ("bar", 520), ("line", 520)):
            extra = ["--kind", kind, "--height", str(minimum)]
            if kind != "table":
                extra += ["--x", "month", "--y", "totalAmount"]
            with self.subTest(kind=kind, valid=True):
                FakeClient.calls = []
                code, _, _ = self.run_cli([self.listing(), self.image_result()], extra=extra)
                self.assertEqual(0, code)
                self.assertEqual(minimum, FakeClient.calls[1][2]["params"]["arguments"]["height"])
            with self.subTest(kind=kind, valid=False):
                FakeClient.calls = []
                extra[extra.index("--height") + 1] = str(minimum - 1)
                code, output, _ = self.run_cli([], extra=extra)
                self.assertEqual(1, code)
                self.assertEqual("INPUT_ERROR", output["error"]["code"])
                self.assertFalse(FakeClient.calls)

    def test_invalid_columns_stops_before_discovery(self):
        columns_file = Path(self.temporary.name) / "columns.json"
        for columns in ([], [{"field": "month", "token": "secret"}], [{"field": ""}],
                        [{"field": "month"}, {"field": "month"}], [{"field": "month", "unit": 1}]):
            with self.subTest(columns=columns):
                FakeClient.calls = []
                columns_file.write_text(json.dumps(columns), encoding="utf-8")
                code, _, _ = self.run_cli([], extra=["--columns", str(columns_file)])
                self.assertEqual(1, code)
                self.assertFalse(FakeClient.calls)

    def test_disabled_tool_and_api_failure_preserve_existing_destination(self):
        self.destination.write_bytes(b"existing file")
        for responses, expected in (([{"result": {"tools": []}}], 3),
                                    ([self.listing(), {"error": {"message": "Model denied"}}], 2),
                                    ([self.listing(), {"result": {"isError": True, "structuredContent": {
                                        "success": False, "error": {"code": "PERMISSION_DENIED", "message": "Access denied"},
                                        "image": {"mimeType": "image/png", "data": PNG_BASE64}}}}], 2)):
            with self.subTest(responses=responses):
                FakeClient.calls = []
                code, _, _ = self.run_cli(responses)
                self.assertEqual(expected, code)
                self.assertEqual(b"existing file", self.destination.read_bytes())

    def test_malformed_success_flag_cannot_replace_existing_file(self):
        self.destination.write_bytes(b"existing file")
        for flag in ("false", "true", 0, None):
            with self.subTest(flag=flag):
                response = self.image_result()
                response["result"]["structuredContent"]["success"] = flag
                code, output, _ = self.run_cli([self.listing(), response])
                self.assertEqual(2, code)
                self.assertEqual("MCP_ERROR", output["error"]["code"])
                self.assertEqual(b"existing file", self.destination.read_bytes())

    def test_invalid_image_and_links_never_replace_or_download(self):
        self.destination.write_bytes(b"existing file")
        for image in ({"mimeType": "image/png", "data": "not base64"},
                      {"mimeType": "image/jpeg", "data": PNG_BASE64},
                      {"mimeType": "image/png", "data": base64.b64encode(b"not PNG").decode()},
                      {"mimeType": "image/png", "data": base64.b64encode(PNG[:-12]).decode()},
                      {"mimeType": "image/png", "url": "https://other-origin.invalid/private"}):
            with self.subTest(image=image):
                FakeClient.calls = []
                code, output, _ = self.run_cli([self.listing(), self.image_result(image)])
                self.assertEqual(1, code)
                self.assertEqual("IMAGE_FORMAT_ERROR", output["error"]["code"])
                self.assertEqual(b"existing file", self.destination.read_bytes())
                self.assertEqual(2, len(FakeClient.calls))

    def test_size_limit_and_failed_atomic_replace_preserve_file_and_clean_temp(self):
        self.destination.write_bytes(b"existing file")
        with patch("foggy_runtime_cli.image_cli.MAX_IMAGE_BYTES", 32):
            code, _, _ = self.run_cli([self.listing(), self.image_result()])
        self.assertEqual(1, code)
        with patch("foggy_runtime_cli.image_cli.os.replace", side_effect=PermissionError("Cannot replace destination")):
            code, output, _ = self.run_cli([self.listing(), self.image_result()])
        self.assertEqual(1, code)
        self.assertEqual("IMAGE_WRITE_ERROR", output["error"]["code"])
        self.assertEqual(b"existing file", self.destination.read_bytes())
        self.assertEqual([], list(self.destination.parent.glob(".foggy-image-*")))

    def test_corrupt_or_misordered_png_chunks_preserve_existing_file(self):
        self.destination.write_bytes(b"existing file")
        damaged_data = bytearray(PNG)
        damaged_data[41] ^= 1
        damaged_crc = bytearray(PNG)
        damaged_crc[-16] ^= 1
        header, image_data, end = PNG[8:33], PNG[33:-12], PNG[-12:]
        text = b"note\x00test"
        chunk = b"tEXt" + text
        ancillary = struct.pack(">I", len(text)) + chunk + struct.pack(">I", zlib.crc32(chunk))
        cases = {
            "damaged IDAT": bytes(damaged_data),
            "bad CRC": bytes(damaged_crc),
            "truncated chunk before IEND": PNG[:41] + end,
            "missing IDAT": PNG[:33] + end,
            "duplicate IHDR": PNG[:33] + header + image_data + end,
            "nonconsecutive IDAT": PNG[:33] + image_data + ancillary + image_data + end,
        }
        for description, png in cases.items():
            with self.subTest(description=description):
                image = {"mimeType": "image/png", "data": base64.b64encode(png).decode("ascii")}
                code, output, _ = self.run_cli([self.listing(), self.image_result(image)])
                self.assertEqual(1, code)
                self.assertEqual("IMAGE_FORMAT_ERROR", output["error"]["code"])
                self.assertEqual(b"existing file", self.destination.read_bytes())
                self.assertEqual([], list(self.destination.parent.glob(".foggy-image-*")))

    def test_contiguous_idat_chunks_and_trailing_text_are_valid_png(self):
        def chunk(name, data):
            return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))

        compressed = PNG[41:-16]
        png = (PNG[:33] + chunk(b"IDAT", compressed[:3]) + chunk(b"IDAT", compressed[3:])
               + chunk(b"tEXt", b"note\x00valid metadata") + PNG[-12:])
        image = {"mimeType": "image/png", "data": base64.b64encode(png).decode("ascii")}
        code, _, _ = self.run_cli([self.listing(), self.image_result(image)])
        self.assertEqual(0, code)
        self.assertEqual(png, self.destination.read_bytes())

    def test_transport_errors_and_server_errors_redact_both_credentials(self):
        for responses, expected in (([RuntimeTransportError("failed opaque-header runtime-code")], 4),
                                    ([self.listing(), {"result": {"isError": True, "structuredContent": {
                                        "success": False, "error": {"code": "QUERY_FAILED", "message": "opaque-header runtime-code"}}}}], 2)):
            with self.subTest(responses=responses):
                code, output, _ = self.run_cli(responses)
                self.assertEqual(expected, code)
                self.assertNotIn("opaque-header", json.dumps(output))
                self.assertNotIn("runtime-code", json.dumps(output))
                self.assertFalse(self.destination.exists())

    def test_transport_error_does_not_echo_runtime_url_credentials(self):
        out, err = io.StringIO(), io.StringIO()
        FakeClient.responses = [RuntimeTransportError("Invalid JSON response from http://user:private-password@127.0.0.1:18066/mcp/analyst/rpc")]
        code = main(["--base-url", "http://user:private-password@127.0.0.1:18066", "query", "export-image", "Sales",
                     "--payload", "-", "--out", str(self.destination)], stdout=out, stderr=err,
                    stdin=io.StringIO(json.dumps(self.payload)), client_factory=FakeClient)
        self.assertEqual(4, code)
        self.assertEqual("TRANSPORT_ERROR", json.loads(out.getvalue())["error"]["code"])
        self.assertNotIn("private-password", out.getvalue())
        self.assertNotIn("127.0.0.1:18066", out.getvalue())
        self.assertFalse(self.destination.exists())


class ImageMcpHandler(BaseHTTPRequestHandler):
    captured: list[dict[str, Any]] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).captured.append({"path": self.path, "namespace": self.headers.get("X-NS"),
                                   "authorization": self.headers.get("Authorization"), "body": body})
        result = ImageCliTest.listing() if body["method"] == "tools/list" else ImageCliTest.image_result()
        encoded = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return


class ImageCliHttpTest(unittest.TestCase):
    def test_context_path_transports_namespace_and_opaque_authorization_for_both_mcp_calls(self):
        ImageMcpHandler.captured = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), ImageMcpHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                destination = Path(temporary) / "bar.png"
                code = main(["--base-url", f"http://127.0.0.1:{server.server_port}/context", "--namespace", "ns-two",
                             "--authorization", "Custom opaque value", "query", "export-image", "Sales",
                             "--payload", "-", "--kind", "bar", "--x", "month", "--y", "amount",
                             "--out", str(destination), "--mcp-path", "/mcp/business/rpc"],
                            stdin=io.StringIO('{"columns":["month","amount"]}'), stdout=io.StringIO(), stderr=io.StringIO())
                self.assertEqual(0, code)
                self.assertEqual(PNG, destination.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(2, len(ImageMcpHandler.captured))
        for request in ImageMcpHandler.captured:
            self.assertEqual("/context/mcp/business/rpc", request["path"])
            self.assertEqual("ns-two", request["namespace"])
            self.assertEqual("Custom opaque value", request["authorization"])
            self.assertNotIn("Custom opaque value", json.dumps(request["body"]))
