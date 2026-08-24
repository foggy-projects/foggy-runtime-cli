from __future__ import annotations

import io
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from foggy_runtime_cli.client import RuntimeApiClient
from foggy_runtime_cli.analytics_cli import ANALYTICS_SDK_V1_OPERATIONS
from foggy_runtime_cli.main import EXIT_CLI_ERROR, EXIT_OK, EXIT_UNSUPPORTED, main


REVISION = "sha256:" + "a" * 64


class FakeClient:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    responses: list[dict[str, Any]] = []
    init_args: tuple[str, str | None, float] | None = None
    auth_code: str | None = None
    authorization: str | None = None

    def __init__(
        self,
        base_url: str,
        namespace: str | None,
        timeout: float,
        auth_code: str | None = None,
        authorization: str | None = None,
    ) -> None:
        type(self).init_args = (base_url, namespace, timeout)
        type(self).auth_code = auth_code
        type(self).authorization = authorization

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        type(self).calls.append((method, path, body))
        if type(self).responses:
            return type(self).responses.pop(0)
        return {"success": True, "engine": "java", "data": {}}


def analytics_capabilities(**operations: str) -> dict[str, Any]:
    return {
        "success": True,
        "engine": "java",
        "analyticsRuntimeApiVersion": "foggy-analytics-runtime-api/v1",
        "data": {"operations": operations},
    }


class AnalyticsCliContractTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.calls = []
        FakeClient.responses = []
        FakeClient.init_args = None
        FakeClient.auth_code = None
        FakeClient.authorization = None

    def run_cli(
        self,
        argv: list[str],
        stdin: str = "",
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            argv,
            stdout=stdout,
            stderr=stderr,
            stdin=io.StringIO(stdin),
            client_factory=FakeClient,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_named_runtime_domain_preserves_existing_runtime_lane(self) -> None:
        code, _output, error = self.run_cli(
            ["runtime", "--base-url", "http://runtime", "capabilities"]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(("http://runtime", None, 30.0), FakeClient.init_args)
        self.assertEqual(
            [("GET", "/api/v1/capabilities", None)],
            FakeClient.calls,
        )

    def test_analytics_domain_uses_only_analytics_url_and_credentials(self) -> None:
        env = {
            "FOGGY_RUNTIME_API_URL": "http://runtime-only",
            "FOGGY_RUNTIME_API_AUTH_CODE": "runtime-code",
            "FOGGY_RUNTIME_AUTHORIZATION": "runtime-authority",
            "FOGGY_ANALYTICS_RUNTIME_API_URL": "http://analytics-only/base",
            "FOGGY_ANALYTICS_RUNTIME_API_AUTH_CODE": "analytics-code",
            "FOGGY_ANALYTICS_AUTHORIZATION": "analytics-authority",
        }
        with patch.dict(os.environ, env, clear=False):
            code, _output, error = self.run_cli(["analytics", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            ("http://analytics-only/base", None, 30.0),
            FakeClient.init_args,
        )
        self.assertEqual("analytics-code", FakeClient.auth_code)
        self.assertEqual("analytics-authority", FakeClient.authorization)

    def test_analytics_credentials_do_not_fall_back_to_runtime_secrets(self) -> None:
        env = {
            "FOGGY_RUNTIME_API_AUTH_CODE": "runtime-code",
            "FOGGY_RUNTIME_AUTHORIZATION": "runtime-authority",
            "FOGGY_ANALYTICS_RUNTIME_API_AUTH_CODE": "",
            "FOGGY_ANALYTICS_AUTHORIZATION": "",
        }
        with patch.dict(os.environ, env, clear=False):
            code, _output, _error = self.run_cli(["analytics", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertIsNone(FakeClient.auth_code)
        self.assertIsNone(FakeClient.authorization)

    def test_cli_operation_registry_matches_frozen_fixture_and_java_sdk_v1(self) -> None:
        workspace = Path(__file__).resolve().parents[2]
        fixture = json.loads((
            workspace
            / "docs/v4.1/contracts/analytics-function-v1/sdk-v1-operations.json"
        ).read_text(encoding="utf-8"))
        expected = set(fixture["operations"])

        self.assertEqual(expected, set(ANALYTICS_SDK_V1_OPERATIONS))

        java_source = (
            workspace
            / "foggy-data-mcp-bridge/foggy-analytics-function-contract/src/main/java"
            / "com/foggyframework/analytics/function/contract/AnalyticsFunctionOperations.java"
        )
        if not java_source.is_file():
            self.skipTest("Java Analytics contract checkout is not present")
        source = java_source.read_text(encoding="utf-8")
        constants = dict(re.findall(
            r'public static final String ([A-Z_]+)\s*=\s*"([^"]+)";',
            source,
        ))
        sdk_block = re.search(
            r"SDK_V1\s*=\s*Set\.of\((.*?)\);",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(sdk_block)
        java_sdk = {
            constants[name]
            for name in re.findall(r"\b[A-Z][A-Z_]+\b", sdk_block.group(1))
        }
        self.assertEqual(expected, java_sdk)

    def test_bundle_validate_uses_analytics_preflight_and_exact_route(self) -> None:
        FakeClient.responses = [
            analytics_capabilities(**{"analytics.bundles.validate": "supported"}),
            {"success": True, "engine": "java", "data": {"bundleRef": "sales"}},
        ]

        code, _output, error = self.run_cli(
            [
                "analytics",
                "--base-url",
                "http://analytics",
                "bundles",
                "validate",
                "sales-2026",
                "--revision",
                REVISION,
                "--request-id",
                "request-validate",
                "--trace-id",
                "trace-validate",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/bundles/sales-2026/validate",
                    {
                        "expectedBundleRevision": REVISION,
                        "requestId": "request-validate",
                        "traceId": "trace-validate",
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_design_time_operations_use_exact_routes_and_control_plane_payloads(self) -> None:
        cases = (
            (
                [
                    "analytics", "bundles", "describe", "sales",
                    "--revision", REVISION, "--request-id", "describe-bundle",
                ],
                "analytics.bundles.describe",
                "/api/v1/bundles/sales/describe",
                {
                    "expectedBundleRevision": REVISION,
                    "requestId": "describe-bundle",
                    "traceId": "describe-bundle",
                },
            ),
            (
                [
                    "analytics", "artifacts", "describe", "sales-summary",
                    "--kind", "report", "--bundle", "sales",
                    "--revision", REVISION, "--request-id", "describe-artifact",
                ],
                "analytics.artifacts.describe",
                "/api/v1/bundles/sales/artifacts/report/sales-summary/describe",
                {
                    "expectedBundleRevision": REVISION,
                    "requestId": "describe-artifact",
                    "traceId": "describe-artifact",
                },
            ),
            (
                [
                    "analytics", "model-dependencies", "resolve", "SalesQuery",
                    "--kind", "qm", "--namespace", "tms-ai",
                    "--request-id", "resolve-model", "--trace-id", "trace-model",
                ],
                "analytics.model-dependencies.resolve",
                "/api/v1/model-dependencies/resolve",
                {
                    "namespace": "tms-ai",
                    "modelKind": "qm",
                    "modelName": "SalesQuery",
                    "requestId": "resolve-model",
                    "traceId": "trace-model",
                },
            ),
        )

        for argv, operation, path, body in cases:
            with self.subTest(operation=operation):
                FakeClient.calls = []
                FakeClient.responses = [
                    analytics_capabilities(**{operation: "supported"}),
                    {"success": True, "engine": "java", "data": {}},
                ]
                code, _output, error = self.run_cli(argv)
                self.assertEqual(EXIT_OK, code)
                self.assertEqual("", error)
                self.assertEqual(
                    [
                        ("GET", "/api/v1/capabilities", None),
                        ("POST", path, body),
                    ],
                    FakeClient.calls,
                )

    def test_report_preview_builds_product_neutral_payload(self) -> None:
        FakeClient.responses = [
            analytics_capabilities(**{"analytics.reports.preview": "supported"}),
            {"success": True, "engine": "java", "data": {"state": "ready"}},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            parameters = Path(temp_dir) / "parameters.json"
            parameters.write_text('{"region":"east"}', encoding="utf-8")

            code, _output, error = self.run_cli(
                [
                    "analytics",
                    "reports",
                    "preview",
                    "sales-summary",
                    "--bundle",
                    "sales",
                    "--revision",
                    REVISION,
                    "--parameters",
                    str(parameters),
                    "--timezone",
                    "Asia/Shanghai",
                    "--locale",
                    "zh-CN",
                    "--authority-provider",
                    "tms",
                    "--authority-reference",
                    "subject:42",
                    "--request-id",
                    "request-preview",
                    "--trace-id",
                    "trace-preview",
                ]
            )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        method, path, body = FakeClient.calls[1]
        self.assertEqual("POST", method)
        self.assertEqual(
            "/api/v1/bundles/sales/reports/sales-summary/preview",
            path,
        )
        self.assertEqual(REVISION, body["expectedBundleRevision"])
        self.assertEqual({"region": "east"}, body["parameters"])
        self.assertEqual(
            {"provider": "tms", "reference": "subject:42"},
            body["authority"],
        )
        self.assertNotIn("owner", body)
        self.assertNotIn("filters", body)

    def test_analytics_rejects_refs_that_cannot_be_one_route_segment(self) -> None:
        for command in (
            ["analytics", "bundles", "validate", ".."],
            [
                "analytics",
                "reports",
                "preview",
                "sales/report",
                "--bundle",
                "sales",
                "--revision",
                REVISION,
                "--authority-provider",
                "tms",
                "--authority-reference",
                "subject:42",
            ],
        ):
            with self.subTest(command=command):
                code, output, error = self.run_cli(command)
                self.assertEqual(EXIT_CLI_ERROR, code)
                self.assertEqual("", output)
                self.assertIn("URL-safe segment", error)
                self.assertEqual([], FakeClient.calls)

    def test_dashboard_render_has_independent_capability_and_route(self) -> None:
        FakeClient.responses = [
            analytics_capabilities(**{"analytics.dashboards.render": "supported"}),
            {"success": True, "engine": "java", "data": {"state": "ready"}},
        ]

        code, _output, _error = self.run_cli(
            [
                "analytics",
                "dashboards",
                "render",
                "sales-board",
                "--bundle",
                "sales",
                "--revision",
                REVISION,
                "--authority-provider",
                "console",
                "--authority-reference",
                "session:7",
                "--request-id",
                "request-render",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            "/api/v1/bundles/sales/dashboards/sales-board/render",
            FakeClient.calls[1][1],
        )
        self.assertEqual(
            "request-render",
            FakeClient.calls[1][2]["traceId"],
        )

    def test_unavailable_analytics_capability_stops_before_operation(self) -> None:
        FakeClient.responses = [
            analytics_capabilities(**{"analytics.dashboards.render": "unavailable"})
        ]

        code, output, error = self.run_cli(
            [
                "analytics",
                "dashboards",
                "render",
                "sales-board",
                "--bundle",
                "sales",
                "--revision",
                REVISION,
                "--authority-provider",
                "console",
                "--authority-reference",
                "session:7",
            ]
        )

        self.assertEqual(EXIT_UNSUPPORTED, code)
        self.assertEqual("", error)
        self.assertIn('"code": "UNSUPPORTED_OPERATION"', output)
        self.assertIn('"analyticsRuntimeApiVersion"', output)
        self.assertEqual(
            [("GET", "/api/v1/capabilities", None)],
            FakeClient.calls,
        )


class AnalyticsHttpHandler(BaseHTTPRequestHandler):
    captured: list[dict[str, Any]] = []

    def do_GET(self) -> None:
        type(self).captured.append(
            {
                "method": "GET",
                "path": self.path,
                "runtime_code": self.headers.get("X-Foggy-Runtime-Code"),
                "authorization": self.headers.get("Authorization"),
            }
        )
        self._respond(
            analytics_capabilities(**{"analytics.reports.preview": "supported"})
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).captured.append(
            {
                "method": "POST",
                "path": self.path,
                "runtime_code": self.headers.get("X-Foggy-Runtime-Code"),
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        self._respond({"success": True, "engine": "java", "data": {"state": "ready"}})

    def _respond(self, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class AnalyticsCliFakeHttpTest(unittest.TestCase):
    def test_analytics_base_path_and_data_plane_authorization(self) -> None:
        AnalyticsHttpHandler.captured = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), AnalyticsHttpHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = main(
                [
                    "analytics",
                    "--base-url",
                    f"http://127.0.0.1:{server.server_port}/analytics",
                    "--auth-code",
                    "analytics-runtime-code",
                    "--authorization",
                    "opaque-authority-header",
                    "reports",
                    "preview",
                    "sales-summary",
                    "--bundle",
                    "sales",
                    "--revision",
                    REVISION,
                    "--authority-provider",
                    "tms",
                    "--authority-reference",
                    "subject:42",
                    "--request-id",
                    "request-http",
                ],
                stdout=stdout,
                stderr=stderr,
                stdin=io.StringIO(""),
                client_factory=RuntimeApiClient,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(
            [
                {
                    "method": "GET",
                    "path": "/analytics/api/v1/capabilities",
                    "runtime_code": "analytics-runtime-code",
                    "authorization": None,
                },
                {
                    "method": "POST",
                    "path": "/analytics/api/v1/bundles/sales/reports/sales-summary/preview",
                    "runtime_code": "analytics-runtime-code",
                    "authorization": "opaque-authority-header",
                    "body": {
                        "expectedBundleRevision": REVISION,
                        "parameters": {},
                        "timezone": "UTC",
                        "locale": "en",
                        "authority": {
                            "provider": "tms",
                            "reference": "subject:42",
                        },
                        "requestId": "request-http",
                        "traceId": "request-http",
                    },
                },
            ],
            AnalyticsHttpHandler.captured,
        )


if __name__ == "__main__":
    unittest.main()
