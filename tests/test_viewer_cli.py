from __future__ import annotations

import io
import json
import unittest
from typing import Any

from foggy_runtime_cli.main import main
from foggy_runtime_cli.client import RuntimeTransportError


class FakeClient:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    responses: list[Any] = []
    namespace = authorization = None

    def __init__(self, _base_url, namespace, _timeout, _auth_code=None, authorization=None):
        type(self).namespace, type(self).authorization = namespace, authorization

    def request(self, method, path, body):
        type(self).calls.append((method, path, body))
        response = type(self).responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ViewerCliTest(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        FakeClient.responses = []
        self.payload = {"columns": ["station", "waybillCount"],
                        "slice": [{"field": "businessDate", "op": "=", "value": "2026-09-26"}],
                        "having": [{"field": "waybillCount", "op": "[]", "value": [1, 2]}],
                        "groupBy": ["station"]}

    def run_cli(self, responses, payload=None, extra=None, output="json"):
        FakeClient.responses = responses
        out, err = io.StringIO(), io.StringIO()
        argv = ["--namespace", "demo", "--authorization", "opaque-header", "--output", output,
                "query", "open-in-viewer", "Waybill", "--payload", "-"] + (extra or [])
        code = main(argv, stdout=out, stderr=err, stdin=io.StringIO(json.dumps(self.payload if payload is None else payload)),
                    client_factory=FakeClient)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def listing():
        return {"jsonrpc": "2.0", "result": {"tools": [{"name": "dataset.open_in_viewer"}]}}

    def test_mcp_link_preserves_dsl_authorization_namespace_and_server_ttls(self):
        data = {"viewerUrl": "https://demo.invalid/open#server-issued", "viewerLinkExpiresAt": None,
                "expiresAt": "2026-10-03T12:00:00Z"}
        code, out, err = self.run_cli([self.listing(), {"result": {"content": [{"type": "text", "text": json.dumps(data)}]}}],
                                    extra=["--title", "Current freight"])
        self.assertEqual(0, code)
        self.assertFalse(err)
        self.assertEqual(data, json.loads(out)["data"])
        self.assertEqual("demo", FakeClient.namespace)
        self.assertEqual("opaque-header", FakeClient.authorization)
        self.assertEqual("/mcp/analyst/rpc", FakeClient.calls[0][1])
        arguments = FakeClient.calls[1][2]["params"]["arguments"]
        self.assertEqual(self.payload, arguments["payload"])
        self.assertEqual("Waybill", arguments["model"])
        self.assertEqual("Current freight", arguments["title"])
        self.assertNotIn("authorization", arguments)

    def test_structured_content_and_pretty_link(self):
        url = "https://demo.invalid/view"
        code, out, _ = self.run_cli([self.listing(), {"result": {"structuredContent": {"viewerUrl": url}}}], output="pretty")
        self.assertEqual(0, code)
        self.assertEqual(url + "\n", out)

    def test_disabled_viewer_stops_after_discovery(self):
        code, out, _ = self.run_cli([{"result": {"tools": []}}])
        self.assertEqual(3, code)
        self.assertEqual("UNSUPPORTED_VIEWER_TOOL", json.loads(out)["error"]["code"])
        self.assertEqual(1, len(FakeClient.calls))

    def test_input_requires_detail_slice_before_any_request(self):
        code, _, _ = self.run_cli([], payload={"columns": ["count"], "having": self.payload["having"]})
        self.assertEqual(1, code)
        self.assertFalse(FakeClient.calls)

    def test_rpc_failure_does_not_become_a_link(self):
        code, out, _ = self.run_cli([self.listing(), {"error": {"code": -32603, "message": "Forbidden model"}}])
        self.assertEqual(2, code)
        self.assertNotIn("viewerUrl", out)

    def test_tool_failure_is_error_even_if_content_contains_url(self):
        code, out, _ = self.run_cli([self.listing(), {"result": {"isError": True, "structuredContent": {"viewerUrl": "invalid"}}}])
        self.assertEqual(2, code)
        self.assertEqual("VIEWER_TOOL_ERROR", json.loads(out)["error"]["code"])

    def test_missing_url_is_never_synthesized(self):
        code, out, _ = self.run_cli([self.listing(), {"result": {"content": [{"type": "text", "text": "not a link"}]}}])
        self.assertEqual(2, code)
        self.assertIn("no link was synthesized", out)

    def test_malformed_tool_content_returns_a_json_error(self):
        for content in (None, "not an array", {"type": "text"}):
            with self.subTest(content=content):
                code, out, _ = self.run_cli([self.listing(), {"result": {"content": content}}])
                self.assertEqual(2, code)
                self.assertEqual("MCP_ERROR", json.loads(out)["error"]["code"])
                self.assertNotIn("viewerUrl", out)

    def test_transport_failure_and_explicit_path(self):
        code, _, _ = self.run_cli([RuntimeTransportError("HTTP 503")], extra=["--mcp-path", "/mcp/analyst"])
        self.assertEqual(4, code)
        self.assertEqual("/mcp/analyst", FakeClient.calls[0][1])
