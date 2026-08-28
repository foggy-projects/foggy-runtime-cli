from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from foggy_runtime_cli.main import EXIT_API_ERROR, EXIT_OK, main


class FakeClient:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    responses: list[dict[str, Any]] = []

    def __init__(self, *_args: Any) -> None:
        pass

    def request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        type(self).calls.append((method, path, body))
        return type(self).responses.pop(0)


class ProfileCliTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.calls = []
        FakeClient.responses = []

    def run_cli(
        self, argv: list[str], store: Path, stdin: str = ""
    ) -> tuple[int, dict[str, Any], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(os.environ, {"FOGGY_RUNTIME_PROFILE_STORE": str(store)}):
            code = main(
                argv,
                stdout=stdout,
                stderr=stderr,
                stdin=io.StringIO(stdin),
                client_factory=FakeClient,
            )
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    @staticmethod
    def connection() -> dict[str, Any]:
        return {
            "schemaVersion": "foggy-runtime-profile-input/v1",
            "name": "business-db",
            "type": "mysql",
            "jdbcUrl": "jdbc:mysql://db.internal:3306/business?useSSL=true",
            "username": "analyst",
            "passwordEnv": "FOGGY_BUSINESS_DB_PASSWORD",
            "namespace": "business",
        }

    def test_create_and_show_never_print_connection_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Path(temp_dir) / "profiles"
            secret_input = json.dumps(self.connection())
            code, created, error = self.run_cli(
                ["profiles", "create", "--input", "-"], store, secret_input
            )
            profile_id = created["data"]["profileId"]
            revision = created["data"]["revision"]
            code_show, shown, _ = self.run_cli(["profiles", "show", profile_id], store)
            stored_text = (store / f"{profile_id}.json").read_text(encoding="utf-8")

        public_text = json.dumps([created, shown])
        self.assertEqual(EXIT_OK, code)
        self.assertEqual(EXIT_OK, code_show)
        self.assertEqual("", error)
        self.assertRegex(profile_id, r"^fop_[a-f0-9]{32}$")
        self.assertRegex(revision, r"^sha256:[a-f0-9]{64}$")
        self.assertNotIn("jdbc:", public_text)
        self.assertNotIn("db.internal", public_text)
        self.assertNotIn("FOGGY_BUSINESS_DB_PASSWORD", public_text)
        self.assertNotIn("analyst", public_text)
        self.assertIn("jdbc:mysql://db.internal", stored_text)
        self.assertIn("FOGGY_BUSINESS_DB_PASSWORD", stored_text)

    def test_create_rejects_embedded_password(self) -> None:
        connection = self.connection()
        connection["jdbcUrl"] = "jdbc:mysql://user:secret@db.internal/business"
        with tempfile.TemporaryDirectory() as temp_dir:
            code, payload, _ = self.run_cli(
                ["profiles", "create", "--input", "-"],
                Path(temp_dir) / "profiles",
                json.dumps(connection),
            )

        self.assertEqual(EXIT_API_ERROR, code)
        self.assertEqual("PROFILE_CREATE_FAILED", payload["error"]["code"])
        self.assertNotIn("secret", json.dumps(payload))

    def test_apply_dry_run_requires_revision_and_makes_no_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Path(temp_dir) / "profiles"
            _, created, _ = self.run_cli(
                ["profiles", "create", "--input", "-"], store, json.dumps(self.connection())
            )
            code, payload, _ = self.run_cli(
                [
                    "profiles",
                    "apply",
                    created["data"]["profileId"],
                    "--approve-revision",
                    created["data"]["revision"],
                ],
                store,
            )

        self.assertEqual(EXIT_OK, code)
        self.assertTrue(payload["data"]["dryRun"])
        self.assertEqual([], FakeClient.calls)

    def test_apply_resolves_privately_and_returns_only_sanitized_status(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "data": {
                    "capabilities": {
                        "datasources.add": "supported",
                        "datasources.test": "supported",
                        "datasources.bind": "supported",
                    }
                },
            },
            {"success": True, "data": {"jdbcUrl": "echoed-by-runtime"}},
            {"success": True, "data": {}},
            {"success": True, "data": {}},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Path(temp_dir) / "profiles"
            _, created, _ = self.run_cli(
                ["profiles", "create", "--input", "-"], store, json.dumps(self.connection())
            )
            code, payload, _ = self.run_cli(
                [
                    "profiles",
                    "apply",
                    created["data"]["profileId"],
                    "--approve-revision",
                    created["data"]["revision"],
                    "--approve-configure",
                    "--approve-bind",
                ],
                store,
            )

        add_body = FakeClient.calls[1][2]
        self.assertEqual(EXIT_OK, code)
        self.assertEqual(self.connection()["jdbcUrl"], add_body["jdbcUrl"])
        self.assertEqual("env:FOGGY_BUSINESS_DB_PASSWORD", add_body["passwordRef"])
        self.assertEqual(
            "PUT", FakeClient.calls[3][0]
        )
        self.assertEqual(
            "/api/v1/namespaces/business/datasource", FakeClient.calls[3][1]
        )
        public_text = json.dumps(payload)
        self.assertNotIn("jdbc:", public_text)
        self.assertNotIn("echoed-by-runtime", public_text)
        self.assertNotIn("FOGGY_BUSINESS_DB_PASSWORD", public_text)
        self.assertTrue(payload["data"]["configured"])
        self.assertTrue(payload["data"]["tested"])
        self.assertTrue(payload["data"]["bound"])

    def test_apply_rejects_revision_change_before_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Path(temp_dir) / "profiles"
            _, created, _ = self.run_cli(
                ["profiles", "create", "--input", "-"], store, json.dumps(self.connection())
            )
            code, payload, _ = self.run_cli(
                [
                    "profiles",
                    "apply",
                    created["data"]["profileId"],
                    "--approve-revision",
                    "sha256:" + "0" * 64,
                    "--approve-configure",
                ],
                store,
            )

        self.assertEqual(EXIT_API_ERROR, code)
        self.assertEqual("PROFILE_APPLY_FAILED", payload["error"]["code"])
        self.assertEqual([], FakeClient.calls)

    def test_bind_only_does_not_repeat_datasource_configuration(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "data": {
                    "capabilities": {
                        "datasources.test": "supported",
                        "datasources.bind": "supported",
                    }
                },
            },
            {"success": True, "data": {}},
            {"success": True, "data": {}},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Path(temp_dir) / "profiles"
            _, created, _ = self.run_cli(
                ["profiles", "create", "--input", "-"], store, json.dumps(self.connection())
            )
            code, payload, _ = self.run_cli(
                [
                    "profiles",
                    "apply",
                    created["data"]["profileId"],
                    "--approve-revision",
                    created["data"]["revision"],
                    "--approve-bind",
                ],
                store,
            )

        self.assertEqual(EXIT_OK, code)
        self.assertFalse(any(method == "POST" and path == "/api/v1/datasources" for method, path, _ in FakeClient.calls))
        self.assertEqual("POST", FakeClient.calls[1][0])
        self.assertEqual("PUT", FakeClient.calls[2][0])
        self.assertFalse(payload["data"]["configured"])
        self.assertTrue(payload["data"]["tested"])
        self.assertTrue(payload["data"]["bound"])

    def test_remove_requires_exact_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Path(temp_dir) / "profiles"
            _, created, _ = self.run_cli(
                ["profiles", "create", "--input", "-"], store, json.dumps(self.connection())
            )
            profile_id = created["data"]["profileId"]
            bad_code, _, _ = self.run_cli(
                ["profiles", "remove", profile_id, "--approve-revision", "sha256:" + "0" * 64],
                store,
            )
            ok_code, removed, _ = self.run_cli(
                ["profiles", "remove", profile_id, "--approve-revision", created["data"]["revision"]],
                store,
            )

        self.assertEqual(EXIT_API_ERROR, bad_code)
        self.assertEqual(EXIT_OK, ok_code)
        self.assertTrue(removed["data"]["removed"])


if __name__ == "__main__":
    unittest.main()
