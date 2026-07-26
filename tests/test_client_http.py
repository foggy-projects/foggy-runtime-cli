from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch
from urllib.error import URLError

from foggy_runtime_cli.client import RuntimeApiClient, RuntimeTransportError


class CapturingHandler(BaseHTTPRequestHandler):
    response_status = 200
    response_body: dict[str, Any] = {"success": True, "engine": "java", "data": {"ok": True}}
    response_payload: bytes | None = None
    captured: dict[str, Any] = {}

    def do_GET(self) -> None:
        type(self).captured = {
            "method": "GET",
            "path": self.path,
            "x_ns": self.headers.get("X-NS"),
            "runtime_code": self.headers.get("X-Foggy-Runtime-Code"),
            "authorization": self.headers.get("Authorization"),
            "body": None,
        }
        self._write_response()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8") if length else ""
        type(self).captured = {
            "method": "POST",
            "path": self.path,
            "x_ns": self.headers.get("X-NS"),
            "runtime_code": self.headers.get("X-Foggy-Runtime-Code"),
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body": json.loads(raw_body) if raw_body else None,
        }
        self._write_response()

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _write_response(self) -> None:
        payload = type(self).response_payload
        if payload is None:
            payload = json.dumps(type(self).response_body).encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RedirectingHandler(BaseHTTPRequestHandler):
    target_url = ""

    def do_GET(self) -> None:
        self.send_response(302)
        self.send_header("Location", type(self).target_url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class RuntimeApiClientHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        CapturingHandler.response_status = 200
        CapturingHandler.response_body = {"success": True, "engine": "java", "data": {"ok": True}}
        CapturingHandler.response_payload = None
        CapturingHandler.captured = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CapturingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_get_request_sends_namespace_header(self) -> None:
        client = RuntimeApiClient(self.base_url, namespace="dev", timeout=5)

        response = client.request("GET", "/api/v1/capabilities")

        self.assertEqual({"success": True, "engine": "java", "data": {"ok": True}}, response)
        self.assertEqual(
            {
                "method": "GET",
                "path": "/api/v1/capabilities",
                "x_ns": "dev",
                "runtime_code": None,
                "authorization": None,
                "body": None,
            },
            CapturingHandler.captured,
        )

    def test_request_sends_runtime_auth_code_header(self) -> None:
        client = RuntimeApiClient(self.base_url, auth_code="runtime-secret", timeout=5)

        response = client.request("GET", "/api/v1/capabilities")

        self.assertTrue(response["success"])
        self.assertEqual("runtime-secret", CapturingHandler.captured["runtime_code"])

    def test_data_plane_authorization_is_exact_and_management_paths_do_not_receive_it(self) -> None:
        client = RuntimeApiClient(
            self.base_url,
            auth_code="runtime-secret",
            authorization="Custom opaque value",
            timeout=5,
        )

        client.request("GET", "/api/v1/models")
        self.assertEqual("runtime-secret", CapturingHandler.captured["runtime_code"])
        self.assertEqual("Custom opaque value", CapturingHandler.captured["authorization"])

        client.request("POST", "/api/v1/models/refresh", {"models": ["A"]})
        self.assertEqual("runtime-secret", CapturingHandler.captured["runtime_code"])
        self.assertIsNone(CapturingHandler.captured["authorization"])

        client.request("POST", "/api/v1/fsscript/execute", {"script": "1"})
        self.assertEqual("runtime-secret", CapturingHandler.captured["runtime_code"])
        self.assertIsNone(CapturingHandler.captured["authorization"])

    def test_query_compose_describe_and_member_paths_receive_authorization(self) -> None:
        client = RuntimeApiClient(
            self.base_url,
            authorization="opaque-no-bearer-prefix",
            timeout=5,
        )

        for method, path in (
            ("POST", "/api/v1/query/Sales/validate"),
            ("POST", "/api/v1/query/Sales/execute"),
            ("POST", "/api/v1/models/Sales/describe"),
            ("POST", "/api/v1/compose/execute"),
            ("POST", "/jdbc-model/dimension/v2/Sales/customer"),
        ):
            client.request(method, path, {})
            self.assertEqual(
                "opaque-no-bearer-prefix",
                CapturingHandler.captured["authorization"],
                path,
            )

    def test_cross_origin_redirect_does_not_forward_authorization(self) -> None:
        redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectingHandler)
        RedirectingHandler.target_url = (
            f"http://localhost:{self.server.server_port}/api/v1/models"
        )
        redirect_thread = threading.Thread(
            target=redirect_server.serve_forever,
            daemon=True,
        )
        redirect_thread.start()
        try:
            client = RuntimeApiClient(
                f"http://127.0.0.1:{redirect_server.server_port}",
                authorization="opaque-secret",
                timeout=5,
            )
            response = client.request("GET", "/api/v1/models")
        finally:
            redirect_server.shutdown()
            redirect_server.server_close()
            redirect_thread.join(timeout=5)

        self.assertTrue(response["success"])
        self.assertIsNone(CapturingHandler.captured["authorization"])

    def test_response_and_http_error_payloads_redact_authorization(self) -> None:
        CapturingHandler.response_body = {
            "success": True,
            "data": {"echo": "prefix opaque-secret suffix"},
        }
        client = RuntimeApiClient(
            self.base_url,
            authorization="opaque-secret",
            timeout=5,
        )

        response = client.request("GET", "/api/v1/models")
        self.assertEqual("prefix [REDACTED] suffix", response["data"]["echo"])

        CapturingHandler.response_status = 500
        CapturingHandler.response_payload = b"upstream echoed opaque-secret"
        with self.assertRaises(RuntimeTransportError) as raised:
            client.request("GET", "/api/v1/models")
        self.assertNotIn("opaque-secret", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_post_request_sends_json_body(self) -> None:
        client = RuntimeApiClient(self.base_url, namespace="dev", timeout=5)

        response = client.request("POST", "/api/v1/models/refresh", {"models": ["A"]})

        self.assertTrue(response["success"])
        self.assertEqual("POST", CapturingHandler.captured["method"])
        self.assertEqual("/api/v1/models/refresh", CapturingHandler.captured["path"])
        self.assertEqual("dev", CapturingHandler.captured["x_ns"])
        self.assertEqual("application/json", CapturingHandler.captured["content_type"])
        self.assertEqual({"models": ["A"]}, CapturingHandler.captured["body"])

    def test_http_error_returns_runtime_envelope(self) -> None:
        CapturingHandler.response_status = 400
        CapturingHandler.response_body = {
            "success": False,
            "engine": "java",
            "error": {"code": "FIELD_NOT_FOUND", "message": "bad field"},
        }
        client = RuntimeApiClient(self.base_url, timeout=5)

        response = client.request("POST", "/api/v1/query/Fact/validate", {"columns": ["bad"]})

        self.assertFalse(response["success"])
        self.assertEqual("FIELD_NOT_FOUND", response["error"]["code"])

    def test_empty_response_returns_empty_dict(self) -> None:
        CapturingHandler.response_payload = b""
        client = RuntimeApiClient(self.base_url, timeout=5)

        response = client.request("GET", "/api/v1/capabilities")

        self.assertEqual({}, response)

    def test_invalid_json_response_raises_transport_error(self) -> None:
        CapturingHandler.response_payload = b"not-json"
        client = RuntimeApiClient(self.base_url, timeout=5)

        with self.assertRaises(RuntimeTransportError) as raised:
            client.request("GET", "/api/v1/capabilities")

        self.assertIn("Invalid JSON response", str(raised.exception))

    def test_non_object_response_raises_transport_error(self) -> None:
        CapturingHandler.response_payload = b"[]"
        client = RuntimeApiClient(self.base_url, timeout=5)

        with self.assertRaises(RuntimeTransportError) as raised:
            client.request("GET", "/api/v1/capabilities")

        self.assertIn("is not a JSON object", str(raised.exception))

    def test_http_error_with_invalid_json_raises_transport_error(self) -> None:
        CapturingHandler.response_status = 500
        CapturingHandler.response_payload = b"<html>server error</html>"
        client = RuntimeApiClient(self.base_url, timeout=5)

        with self.assertRaises(RuntimeTransportError) as raised:
            client.request("GET", "/api/v1/capabilities")

        self.assertIn("HTTP 500", str(raised.exception))

    def test_http_error_with_json_array_raises_transport_error(self) -> None:
        CapturingHandler.response_status = 500
        CapturingHandler.response_payload = b"[]"
        client = RuntimeApiClient(self.base_url, timeout=5)

        with self.assertRaises(RuntimeTransportError) as raised:
            client.request("GET", "/api/v1/capabilities")

        self.assertIn("response is not a JSON object", str(raised.exception))

    def test_url_error_raises_transport_error(self) -> None:
        client = RuntimeApiClient(self.base_url, timeout=5)

        with patch("foggy_runtime_cli.client._open_request", side_effect=URLError("connection refused")):
            with self.assertRaises(RuntimeTransportError) as raised:
                client.request("GET", "/api/v1/capabilities")

        self.assertIn("connection refused", str(raised.exception))

    def test_os_error_raises_transport_error(self) -> None:
        client = RuntimeApiClient(self.base_url, timeout=5)

        with patch("foggy_runtime_cli.client._open_request", side_effect=OSError("broken pipe")):
            with self.assertRaises(RuntimeTransportError) as raised:
                client.request("GET", "/api/v1/capabilities")

        self.assertIn("broken pipe", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
