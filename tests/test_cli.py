from __future__ import annotations

import io
import json
import os
import unittest
from typing import Any
from unittest.mock import patch

from foggy_runtime_cli.main import (
    EXIT_API_ERROR,
    EXIT_OK,
    EXIT_TRANSPORT_ERROR,
    EXIT_UNSUPPORTED,
    main,
)


class FakeClient:
    calls: list[tuple[str, str, dict[str, Any] | None]]
    response: dict[str, Any] = {"success": True, "engine": "java", "data": {}}
    raise_error: Exception | None = None
    init_args: tuple[str, str | None, float] | None = None

    def __init__(self, base_url: str, namespace: str | None, timeout: float) -> None:
        type(self).init_args = (base_url, namespace, timeout)

    def request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if type(self).raise_error is not None:
            raise type(self).raise_error
        type(self).calls.append((method, path, body))
        return type(self).response


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.calls = []
        FakeClient.response = {"success": True, "engine": "java", "data": {}}
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

    def test_python_engine_uses_python_base_url_profile(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            code, _output, _error = self.run_cli(["--engine", "python", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(("http://127.0.0.1:8066", None, 30.0), FakeClient.init_args)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)

    def test_base_url_overrides_engine_profile(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            code, _output, _error = self.run_cli(
                ["--engine", "python", "--base-url", "http://runtime", "capabilities"]
            )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(("http://runtime", None, 30.0), FakeClient.init_args)

    def test_engine_specific_env_base_url(self) -> None:
        with patch.dict(os.environ, {"FOGGY_PYTHON_RUNTIME_API_URL": "http://python-runtime"}, clear=True):
            code, _output, _error = self.run_cli(["--engine", "python", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(("http://python-runtime", None, 30.0), FakeClient.init_args)

    def test_namespace_and_model_describe_body(self) -> None:
        code, _output, _error = self.run_cli(
            [
                "--namespace",
                "dev",
                "models",
                "describe",
                "Sales Model",
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
                        "fields": ["amount"],
                        "levels": [1],
                        "includeExamples": True,
                    },
                )
            ],
            FakeClient.calls,
        )

    def test_refresh_models_body(self) -> None:
        code, _output, _error = self.run_cli(["models", "refresh", "--model", "A", "--model", "B"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual([("POST", "/api/v1/models/refresh", {"models": ["A", "B"]})], FakeClient.calls)

    def test_validate_models_dir_body(self) -> None:
        code, _output, _error = self.run_cli(["models", "validate", "--models-dir", "./models"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                (
                    "POST",
                    "/api/v1/models/validate",
                    {
                        "path": "./models",
                        "watch": False,
                        "clearExisting": True,
                        "includeStackTrace": False,
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

    def test_table_inspect_body(self) -> None:
        code, _output, _error = self.run_cli(
            ["tables", "inspect", "--table", "sale_order", "--schema", "public", "--include-indexes"]
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
                        "includeIndexes": True,
                        "includeForeignKeys": False,
                    },
                )
            ],
            FakeClient.calls,
        )

    def test_api_error_exit_code(self) -> None:
        FakeClient.response = {
            "success": False,
            "error": {"code": "FIELD_NOT_FOUND", "phase": "query.validate", "message": "bad field"},
        }

        code, _output, _error = self.run_cli(["capabilities"])

        self.assertEqual(EXIT_API_ERROR, code)

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


if __name__ == "__main__":
    unittest.main()
