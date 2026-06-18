from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from foggy_runtime_cli.main import (
    EXIT_API_ERROR,
    EXIT_OK,
    EXIT_TRANSPORT_ERROR,
    EXIT_UNSUPPORTED,
    console_main,
    main,
)


class FakeClient:
    calls: list[tuple[str, str, dict[str, Any] | None]]
    response: dict[str, Any] = {"success": True, "engine": "java", "data": {}}
    responses: list[dict[str, Any]] | None = None
    raise_error: Exception | None = None
    init_args: tuple[str, str | None, float] | None = None

    def __init__(self, base_url: str, namespace: str | None, timeout: float) -> None:
        type(self).init_args = (base_url, namespace, timeout)

    def request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if type(self).raise_error is not None:
            raise type(self).raise_error
        type(self).calls.append((method, path, body))
        if type(self).responses is not None:
            return type(self).responses.pop(0)
        return type(self).response


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.calls = []
        FakeClient.response = {"success": True, "engine": "java", "data": {}}
        FakeClient.responses = None
        FakeClient.raise_error = None
        FakeClient.init_args = None

    def run_cli(self, argv: list[str], stdin: str = "") -> tuple[int, str, str]:
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

    def test_capabilities_route(self) -> None:
        code, output, error = self.run_cli(["--base-url", "http://runtime", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertEqual(("http://runtime", None, 30.0), FakeClient.init_args)
        self.assertIn('"success": true', output)
        self.assertEqual("", error)

    def test_default_base_url_is_generic_local_runtime(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            code, _output, _error = self.run_cli(["capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(("http://127.0.0.1:8080", None, 30.0), FakeClient.init_args)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)

    def test_base_url_overrides_generic_env_base_url(self) -> None:
        with patch.dict(os.environ, {"FOGGY_RUNTIME_API_URL": "http://generic-runtime"}, clear=True):
            code, _output, _error = self.run_cli(["--base-url", "http://runtime", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(("http://runtime", None, 30.0), FakeClient.init_args)

    def test_generic_env_base_url(self) -> None:
        with patch.dict(os.environ, {"FOGGY_RUNTIME_API_URL": "http://generic-runtime"}, clear=True):
            code, _output, _error = self.run_cli(["capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(("http://generic-runtime", None, 30.0), FakeClient.init_args)

    def test_engine_option_is_not_supported(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.run_cli(["--engine", "python", "capabilities"])

        self.assertEqual(2, raised.exception.code)
        self.assertEqual([], FakeClient.calls)

    def test_namespace_and_model_describe_body(self) -> None:
        code, _output, _error = self.run_cli(
            [
                "--namespace",
                "dev",
                "models",
                "describe",
                "Sales Model",
                "--format",
                "frontend-meta",
                "--field",
                "amount",
                "--level",
                "1",
                "--include-examples",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("dev", FakeClient.init_args[1])
        self.assertEqual(
            [
                (
                    "POST",
                    "/api/v1/models/Sales%20Model/describe",
                    {
                        "namespace": "dev",
                        "format": "frontend-meta",
                        "fields": ["amount"],
                        "levels": [1],
                        "includeExamples": True,
                    },
                )
            ],
            FakeClient.calls,
        )

    def test_refresh_models_body(self) -> None:
        code, _output, _error = self.run_cli(
            ["--namespace", "dev", "models", "refresh", "--model", "A", "--model", "B"]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [("POST", "/api/v1/models/refresh", {"namespace": "dev", "models": ["A", "B"]})],
            FakeClient.calls,
        )

    def test_validate_models_dir_body(self) -> None:
        code, _output, _error = self.run_cli(
            [
                "--namespace",
                "dev",
                "models",
                "validate",
                "--models-dir",
                "./models",
                "--watch",
                "--include-stack-trace",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                (
                    "POST",
                    "/api/v1/models/validate",
                    {
                        "path": "./models",
                        "watch": True,
                        "clearExisting": True,
                        "includeStackTrace": True,
                        "namespace": "dev",
                    },
                )
            ],
            FakeClient.calls,
        )

    def test_validate_models_dir_can_disable_clear_existing(self) -> None:
        code, _output, _error = self.run_cli(
            ["models", "validate", "--models-dir", "./models", "--no-clear-existing"]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(False, FakeClient.calls[0][2]["clearExisting"])

    def test_query_payload_from_stdin(self) -> None:
        code, _output, _error = self.run_cli(
            ["query", "validate", "FactSales", "--payload", "-"],
            stdin=json.dumps({"columns": ["amount"], "limit": 1}),
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                (
                    "POST",
                    "/api/v1/query/FactSales/validate",
                    {"columns": ["amount"], "limit": 1},
                )
            ],
            FakeClient.calls,
        )

    def test_query_execute_payload_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload_path = Path(temp_dir) / "payload.json"
            payload_path.write_text(json.dumps({"columns": ["amount"], "limit": 10}), encoding="utf-8")

            code, _output, _error = self.run_cli(["query", "execute", "Fact Sales", "--payload", str(payload_path)])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                (
                    "POST",
                    "/api/v1/query/Fact%20Sales/execute",
                    {"columns": ["amount"], "limit": 10},
                )
            ],
            FakeClient.calls,
        )

    def test_table_inspect_body(self) -> None:
        code, _output, _error = self.run_cli(
            [
                "tables",
                "inspect",
                "--table",
                "sale_order",
                "--schema",
                "public",
                "--data-source",
                "main",
                "--include-indexes",
                "--include-foreign-keys",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                (
                    "POST",
                    "/api/v1/tables/inspect",
                    {
                        "table": "sale_order",
                        "schema": "public",
                        "dataSource": "main",
                        "includeIndexes": True,
                        "includeForeignKeys": True,
                    },
                )
            ],
            FakeClient.calls,
        )

    def test_compose_validate_reads_script_file_and_checks_capability(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"compose.validate": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"valid": True}},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "compose.fsscript"
            params_path = Path(temp_dir) / "params.json"
            script_path.write_text("return { plans: [] };", encoding="utf-8")
            params_path.write_text(json.dumps({"region": "east"}), encoding="utf-8")

            code, _output, error = self.run_cli(
                [
                    "--namespace",
                    "dev",
                    "compose",
                    "validate",
                    "--script",
                    str(script_path),
                    "--params",
                    str(params_path),
                ]
            )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/compose/validate",
                    {
                        "script": "return { plans: [] };",
                        "namespace": "dev",
                        "params": {"region": "east"},
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_compose_preview_unsupported_capability_stops_before_route(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "python",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"compose.preview": "unsupported"}},
            }
        ]

        code, output, error = self.run_cli(
            ["compose", "preview", "--script-text", "return { plans: [] };"]
        )

        self.assertEqual(EXIT_UNSUPPORTED, code)
        self.assertEqual("", error)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertIn('"code": "UNSUPPORTED_OPERATION"', output)
        self.assertIn('"phase": "compose.preview"', output)

    def test_fsscript_run_routes_to_execute(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"fsscript.execute": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"value": 3}},
        ]

        code, _output, _error = self.run_cli(["fsscript", "run", "--script-text", "return 1 + 2;"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                ("POST", "/api/v1/fsscript/execute", {"script": "return 1 + 2;"}),
            ],
            FakeClient.calls,
        )

    def test_fsscript_run_with_cte_bridge_requires_bridge_capability(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {
                    "capabilities": {
                        "fsscript.execute": "supported",
                        "fsscript.cteBridge": "supported",
                    }
                },
            },
            {"success": True, "engine": "java", "data": {"value": {"mode": "preview"}}},
        ]

        code, _output, _error = self.run_cli(
            [
                "fsscript",
                "run",
                "--script-text",
                "return foggy.cte.preview({script: 'return { plans: [] };'});",
                "--enable-cte-bridge",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/fsscript/execute",
                    {
                        "script": "return foggy.cte.preview({script: 'return { plans: [] };'});",
                        "capabilities": {"cteBridge": True},
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_models_list_pretty_output(self) -> None:
        FakeClient.response = {"success": True, "engine": "java", "data": {"models": ["FactSales", "DimCustomer"]}}

        code, output, _error = self.run_cli(["--output", "pretty", "models", "list"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual([("GET", "/api/v1/models", None)], FakeClient.calls)
        self.assertEqual("FactSales\nDimCustomer\n", output)

    def test_capabilities_pretty_output(self) -> None:
        FakeClient.response = {
            "success": True,
            "engine": "java",
            "runtimeApiVersion": "foggy-runtime-api/v1",
            "data": {
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "schemaVersion": "2026-06-06",
                "enabled": True,
                "securityMode": "none-dev-test-only",
                "capabilities": {"models.refresh": "supported", "query.execute": "unsupported"},
            },
        }

        code, output, _error = self.run_cli(["--output", "pretty", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertIn("engine: java", output)
        self.assertIn("runtimeApiVersion: foggy-runtime-api/v1", output)
        self.assertIn("schemaVersion: 2026-06-06", output)
        self.assertIn("enabled: true", output)
        self.assertIn("securityMode: none-dev-test-only", output)
        self.assertIn("capabilities:", output)
        self.assertIn("  models.refresh: supported", output)
        self.assertIn("  query.execute: unsupported", output)

    def test_generic_success_pretty_output(self) -> None:
        FakeClient.response = {"success": True, "engine": "python", "data": {"ok": True}}

        code, output, _error = self.run_cli(["--output", "pretty", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("OK [python]\n", output)

    def test_api_error_exit_code(self) -> None:
        FakeClient.response = {
            "success": False,
            "error": {"code": "FIELD_NOT_FOUND", "phase": "query.validate", "message": "bad field"},
        }

        code, _output, _error = self.run_cli(["capabilities"])

        self.assertEqual(EXIT_API_ERROR, code)

    def test_api_error_pretty_output(self) -> None:
        FakeClient.response = {
            "success": False,
            "engine": "java",
            "error": {"code": "FIELD_NOT_FOUND", "phase": "query.validate", "message": "bad field"},
        }

        code, output, _error = self.run_cli(["--output", "pretty", "capabilities"])

        self.assertEqual(EXIT_API_ERROR, code)
        self.assertEqual("ERROR [java] FIELD_NOT_FOUND at query.validate: bad field\n", output)

    def test_malformed_envelope_is_api_error(self) -> None:
        FakeClient.response = {"engine": "java", "data": {}}

        code, _output, _error = self.run_cli(["capabilities"])

        self.assertEqual(EXIT_API_ERROR, code)

    def test_unsupported_exit_code(self) -> None:
        FakeClient.response = {
            "success": False,
            "error": {"code": "UNSUPPORTED_OPERATION", "phase": "compose.validate", "message": "unsupported"},
        }

        code, _output, _error = self.run_cli(["capabilities"])

        self.assertEqual(EXIT_UNSUPPORTED, code)

    def test_invalid_json_payload_is_cli_error(self) -> None:
        code, _output, error = self.run_cli(["query", "execute", "FactSales", "--payload", "-"], stdin="{bad")

        self.assertEqual(1, code)
        self.assertIn("input error", error)
        self.assertEqual([], FakeClient.calls)

    def test_json_payload_must_be_object(self) -> None:
        code, _output, error = self.run_cli(["query", "execute", "FactSales", "--payload", "-"], stdin="[]")

        self.assertEqual(1, code)
        self.assertIn("must contain a JSON object", error)
        self.assertEqual([], FakeClient.calls)

    def test_missing_payload_file_is_cli_error(self) -> None:
        code, _output, error = self.run_cli(["query", "execute", "FactSales", "--payload", "missing.json"])

        self.assertEqual(1, code)
        self.assertIn("input error", error)
        self.assertEqual([], FakeClient.calls)

    def test_transport_error_exit_code(self) -> None:
        from foggy_runtime_cli.client import RuntimeTransportError

        FakeClient.raise_error = RuntimeTransportError("connection refused")

        code, _output, error = self.run_cli(["capabilities"])

        self.assertEqual(EXIT_TRANSPORT_ERROR, code)
        self.assertIn("transport error", error)

    def test_console_main_raises_system_exit(self) -> None:
        with patch("foggy_runtime_cli.main.main", return_value=EXIT_OK):
            with self.assertRaises(SystemExit) as raised:
                console_main()

        self.assertEqual(EXIT_OK, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
