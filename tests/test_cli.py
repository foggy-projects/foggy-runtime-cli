from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from foggy_runtime_cli import __version__
from foggy_runtime_cli.main import (
    EXIT_API_ERROR,
    EXIT_CLI_ERROR,
    EXIT_OK,
    EXIT_TRANSPORT_ERROR,
    EXIT_UNSUPPORTED,
    build_parser,
    console_main,
    demo_available_query_fields,
    main,
)


class ReconfigurableStringIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.reconfigure_calls: list[dict[str, Any]] = []

    def reconfigure(self, **kwargs: Any) -> None:
        self.reconfigure_calls.append(kwargs)


class FakeClient:
    calls: list[tuple[str, str, dict[str, Any] | None]]
    response: dict[str, Any] = {"success": True, "engine": "java", "data": {}}
    responses: list[dict[str, Any] | Exception] | None = None
    raise_error: Exception | None = None
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

    def request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if type(self).raise_error is not None:
            raise type(self).raise_error
        type(self).calls.append((method, path, body))
        if type(self).responses is not None:
            response = type(self).responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return type(self).response


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.calls = []
        FakeClient.response = {"success": True, "engine": "java", "data": {}}
        FakeClient.responses = None
        FakeClient.raise_error = None
        FakeClient.init_args = None
        FakeClient.auth_code = None
        FakeClient.authorization = None

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

    @staticmethod
    def capability_response(capabilities: dict[str, str]) -> dict[str, Any]:
        return {
            "success": True,
            "engine": "java",
            "runtimeApiVersion": "foggy-runtime-api/v1",
            "data": {"capabilities": capabilities},
        }

    @classmethod
    def supported_capabilities_response(cls, *capabilities: str) -> dict[str, Any]:
        return cls.capability_response({capability: "supported" for capability in capabilities})

    @staticmethod
    def write_minimal_sales_drop_assets(skill_dir: Path) -> Path:
        demo_dir = skill_dir / "assets" / "sales-drop-demo"
        (demo_dir / "models").mkdir(parents=True)
        (demo_dir / "queries").mkdir()
        (demo_dir / "schema.sql").write_text("create table sales_drop_daily(id integer);", encoding="utf-8")
        (demo_dir / "data.sql").write_text("insert into sales_drop_daily values (1);", encoding="utf-8")
        (demo_dir / "queries" / "basic.json").write_text(json.dumps({"limit": 1}), encoding="utf-8")
        return demo_dir

    @staticmethod
    def write_minimal_skill_zip(zip_path: Path, skill_name: str) -> str:
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr(f"{skill_name}/SKILL.md", f"---\nname: {skill_name}\n---\n")
            archive.writestr(f"{skill_name}/references/README.md", "content\n")
        digest = hashlib.sha256()
        with open(zip_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def write_stack_manifest(manifest_path: Path, skill_specs: dict[str, dict[str, str]]) -> None:
        manifest_path.write_text(
            json.dumps(
                {
                    "schemaVersion": "foggy-stack/v1",
                    "channel": "stable-test",
                    "components": {
                        "cli": {
                            "name": "foggy-runtime-cli",
                            "recommendedVersion": "9.9.9",
                            "install": {"windows": "https://example.invalid/install.ps1"},
                        },
                        "skills": {
                            skill: {
                                "name": skill,
                                "recommendedVersion": spec["version"],
                                "tag": f"v{spec['version']}",
                                "releaseUrl": f"https://example.invalid/{skill}/v{spec['version']}",
                                "minCliVersion": spec.get("minCliVersion", "0.1.0"),
                                "zip": {
                                    "file": spec.get("zipFile", Path(spec["zipPath"]).name),
                                    "url": Path(spec["zipPath"]).as_uri(),
                                    "sha256": spec["sha256"],
                                },
                            }
                            for skill, spec in skill_specs.items()
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_version_flag_returns_cli_version(self) -> None:
        code, output, error = self.run_cli(["--version"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(f"foggy-runtime {__version__}\n", output)
        self.assertEqual("", error)
        self.assertEqual([], FakeClient.calls)

    def test_default_streams_are_reconfigured_to_utf8(self) -> None:
        stdout = ReconfigurableStringIO()
        stderr = ReconfigurableStringIO()

        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(["capabilities"], stdin=io.StringIO(""), client_factory=FakeClient)

        self.assertEqual(EXIT_OK, code)
        self.assertEqual([{"encoding": "utf-8"}], stdout.reconfigure_calls)
        self.assertEqual([{"encoding": "utf-8"}], stderr.reconfigure_calls)
        self.assertIn('"success": true', stdout.getvalue())

    def test_capabilities_route(self) -> None:
        code, output, error = self.run_cli(["--base-url", "http://runtime", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertEqual(("http://runtime", None, 30.0), FakeClient.init_args)
        self.assertIn('"success": true', output)
        self.assertEqual("", error)

    def test_single_dash_help_alias(self) -> None:
        for argv in (["-help"], ["skills", "install", "-help"], ["demo", "sales-drop", "-help"]):
            with self.subTest(argv=argv), patch("sys.stdout", new_callable=io.StringIO) as stdout:
                with self.assertRaises(SystemExit) as exit_context:
                    build_parser().parse_args(argv)

            self.assertEqual(0, exit_context.exception.code)
            self.assertIn("usage: foggy-runtime", stdout.getvalue())

    def test_help_mentions_analysis_skill_and_demo_assets(self) -> None:
        cases = [
            (["-help"], ["skills install foggy-ai-analysis", "stack show", "demo sales-drop"]),
            (["skills", "install", "-help"], ["sales-drop demo assets", "foggy-analysis-suite"]),
            (
                ["demo", "sales-drop", "-help"],
                [
                    "foggy-runtime skills install foggy-ai-analysis --zip foggy-ai-analysis-skill-0.1.16.zip --replace",
                    "~/.agents/skills/foggy-ai-analysis",
                ],
            ),
            (["demo", "sales-drop", "replay", "-help"], ["~/.agents/skills/foggy-ai-analysis"]),
        ]
        for argv, expected_parts in cases:
            with self.subTest(argv=argv), patch("sys.stdout", new_callable=io.StringIO) as stdout:
                with self.assertRaises(SystemExit) as exit_context:
                    build_parser().parse_args(argv)

            help_text = stdout.getvalue()
            self.assertEqual(0, exit_context.exception.code)
            for expected in expected_parts:
                self.assertIn(expected, help_text)

    def test_skills_install_uses_agents_skills_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            source = root / "source" / "foggy-ai-analysis"
            (source / "references").mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: foggy-ai-analysis\n---\n", encoding="utf-8")
            (source / "references" / "public-onboarding.md").write_text("content\n", encoding="utf-8")

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    ["skills", "install", "foggy-ai-analysis", "--source-dir", str(source)]
                )

            body = json.loads(output)
            target_dir = home / ".agents" / "skills" / "foggy-ai-analysis"
            self.assertEqual(EXIT_OK, code)
            self.assertEqual("", error)
            self.assertTrue((target_dir / "SKILL.md").is_file())
            self.assertTrue((target_dir / "references" / "public-onboarding.md").is_file())
            self.assertFalse((home / ".codex" / "skills" / "foggy-ai-analysis").exists())
            self.assertFalse((home / ".claude" / "skills" / "foggy-ai-analysis").exists())
            self.assertEqual(str(home / ".agents" / "skills"), body["data"]["targetRoot"])
            self.assertEqual("agents-skills-only", body["data"]["installPolicy"])
            self.assertEqual(str(target_dir / "assets" / "sales-drop-demo"), body["data"]["salesDropDemoDir"])
            self.assertIn("--zip foggy-ai-analysis-skill-0.1.16.zip", body["data"]["demoInstallCommand"])
            self.assertIn("foggy-ai-analysis/releases/download/v0.1.16", body["data"]["publicZipUrl"])
            self.assertIn("--workspace-root <workspace-root>", body["data"]["workspaceInstallCommand"])

    def test_stack_show_offline_returns_builtin_fallback(self) -> None:
        code, output, error = self.run_cli(["stack", "show", "--offline-stack"])

        body = json.loads(output)
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual("builtin:fallback", body["data"]["stackManifestSource"])
        self.assertEqual("0.1.16", body["data"]["stack"]["components"]["skills"]["foggy-ai-analysis"]["recommendedVersion"])
        self.assertEqual(
            "foggy-runtime-launcher-v0.1.17",
            body["data"]["stack"]["components"]["launcher"]["recommendedTag"],
        )
        self.assertIn("startPowerShell", body["data"]["stack"]["components"]["launcher"]["assets"])
        self.assertIn("sha256", body["data"]["stack"]["components"]["launcher"]["assets"]["startShell"])

    def test_stack_show_default_manifest_failure_falls_back_with_warning(self) -> None:
        with patch("foggy_runtime_cli.stack_cli.read_text_resource", side_effect=OSError("offline")):
            code, output, error = self.run_cli(["stack", "show"])

        body = json.loads(output)
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual("builtin:fallback", body["data"]["stackManifestSource"])
        self.assertIn("using CLI built-in fallback", body["data"]["stackManifestWarnings"][0])

    def test_skills_install_default_manifest_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            with (
                patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home),
                patch("foggy_runtime_cli.stack_cli.read_text_resource", side_effect=OSError("offline")),
            ):
                code, output, error = self.run_cli(["skills", "install", "foggy-ai-analysis", "--replace"])

            body = json.loads(output)
            self.assertEqual(EXIT_API_ERROR, code)
            self.assertEqual("", error)
            self.assertFalse(body["success"])
            self.assertIn("Cannot load stack manifest", body["error"]["message"])
            self.assertFalse((home / ".agents" / "skills" / "foggy-ai-analysis").exists())

    def test_skills_install_downloads_release_zip_from_stack_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            zip_path = root / "foggy-ai-analysis-skill-9.9.9.zip"
            sha256 = self.write_minimal_skill_zip(zip_path, "foggy-ai-analysis")
            manifest_path = root / "stable.json"
            self.write_stack_manifest(
                manifest_path,
                {
                    "foggy-ai-analysis": {
                        "version": "9.9.9",
                        "zipPath": str(zip_path),
                        "sha256": sha256,
                    }
                },
            )

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    [
                        "skills",
                        "install",
                        "foggy-ai-analysis",
                        "--stack-manifest",
                        str(manifest_path),
                    ]
                )

            body = json.loads(output)
            target_dir = home / ".agents" / "skills" / "foggy-ai-analysis"
            self.assertEqual(EXIT_OK, code)
            self.assertEqual("", error)
            self.assertTrue((target_dir / "SKILL.md").is_file())
            self.assertEqual("release-zip", body["data"]["sourceKind"])
            self.assertEqual("9.9.9", body["data"]["releaseVersion"])
            self.assertEqual(sha256, body["data"]["releaseZipSha256"])
            self.assertTrue(body["data"]["releaseZipSha256Verified"])
            self.assertEqual(str(manifest_path), body["data"]["stackManifestSource"])
            self.assertIn("foggy-ai-analysis-skill-9.9.9.zip", body["data"]["publicZipUrl"])
            self.assertIn("--zip foggy-ai-analysis-skill-9.9.9.zip", body["data"]["demoInstallCommand"])

    def test_skills_install_explicit_zip_uses_zip_version_for_hints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            zip_path = root / "foggy-ai-analysis-skill-9.9.9.zip"
            self.write_minimal_skill_zip(zip_path, "foggy-ai-analysis")

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    [
                        "skills",
                        "install",
                        "foggy-ai-analysis",
                        "--zip",
                        str(zip_path),
                        "--replace",
                    ]
                )

            body = json.loads(output)
            target_dir = home / ".agents" / "skills" / "foggy-ai-analysis"
            self.assertEqual(EXIT_OK, code)
            self.assertEqual("", error)
            self.assertTrue((target_dir / "SKILL.md").is_file())
            self.assertEqual("explicit-zip", body["data"]["sourceKind"])
            self.assertEqual("9.9.9", body["data"]["releaseVersion"])
            self.assertIn("foggy-ai-analysis/releases/download/v9.9.9", body["data"]["publicZipUrl"])
            self.assertIn("--zip foggy-ai-analysis-skill-9.9.9.zip", body["data"]["demoInstallCommand"])
            self.assertIn("--zip foggy-ai-analysis-skill-9.9.9.zip", body["data"]["zipInstallCommand"])

    def test_skills_install_explicit_zip_with_unknown_version_omits_versioned_hints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            zip_path = root / "custom-analysis.zip"
            self.write_minimal_skill_zip(zip_path, "foggy-ai-analysis")

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    [
                        "skills",
                        "install",
                        "foggy-ai-analysis",
                        "--zip",
                        str(zip_path),
                        "--replace",
                    ]
                )

            body = json.loads(output)
            self.assertEqual(EXIT_OK, code)
            self.assertEqual("", error)
            self.assertEqual("explicit-zip", body["data"]["sourceKind"])
            self.assertNotIn("releaseVersion", body["data"])
            self.assertNotIn("publicZipUrl", body["data"])
            self.assertNotIn("demoInstallCommand", body["data"])
            self.assertNotIn("zipInstallCommand", body["data"])

    def test_skills_install_rejects_min_cli_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            zip_path = root / "foggy-ai-analysis-skill-9.9.9.zip"
            sha256 = self.write_minimal_skill_zip(zip_path, "foggy-ai-analysis")
            manifest_path = root / "stable.json"
            self.write_stack_manifest(
                manifest_path,
                {
                    "foggy-ai-analysis": {
                        "version": "9.9.9",
                        "zipPath": str(zip_path),
                        "sha256": sha256,
                        "minCliVersion": "99.0.0",
                    }
                },
            )

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    ["skills", "install", "foggy-ai-analysis", "--stack-manifest", str(manifest_path)]
                )

            body = json.loads(output)
            self.assertEqual(EXIT_API_ERROR, code)
            self.assertEqual("", error)
            self.assertFalse(body["success"])
            self.assertIn("requires foggy-runtime-cli >= 99.0.0", body["error"]["message"])
            self.assertFalse((home / ".agents" / "skills" / "foggy-ai-analysis").exists())

    def test_skills_install_rejects_release_zip_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            zip_path = root / "foggy-ai-analysis-skill-9.9.9.zip"
            sha256 = self.write_minimal_skill_zip(zip_path, "foggy-ai-analysis")
            manifest_path = root / "stable.json"
            self.write_stack_manifest(
                manifest_path,
                {
                    "foggy-ai-analysis": {
                        "version": "9.9.9",
                        "zipPath": str(zip_path),
                        "zipFile": "../foggy-ai-analysis-skill-9.9.9.zip",
                        "sha256": sha256,
                    }
                },
            )

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    ["skills", "install", "foggy-ai-analysis", "--stack-manifest", str(manifest_path)]
                )

            body = json.loads(output)
            self.assertEqual(EXIT_API_ERROR, code)
            self.assertEqual("", error)
            self.assertFalse(body["success"])
            self.assertIn("Invalid Skill zip file name", body["error"]["message"])
            self.assertFalse((home / ".agents" / "skills" / "foggy-ai-analysis").exists())

    def test_skills_install_rejects_release_zip_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            zip_path = root / "foggy-ai-analysis-skill-9.9.9.zip"
            self.write_minimal_skill_zip(zip_path, "foggy-ai-analysis")
            manifest_path = root / "stable.json"
            self.write_stack_manifest(
                manifest_path,
                {
                    "foggy-ai-analysis": {
                        "version": "9.9.9",
                        "zipPath": str(zip_path),
                        "sha256": "0" * 64,
                    }
                },
            )

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    [
                        "skills",
                        "install",
                        "foggy-ai-analysis",
                        "--stack-manifest",
                        str(manifest_path),
                    ]
                )

            body = json.loads(output)
            target_dir = home / ".agents" / "skills" / "foggy-ai-analysis"
            self.assertEqual(EXIT_API_ERROR, code)
            self.assertEqual("", error)
            self.assertFalse(body["success"])
            self.assertEqual("SKILL_INSTALL_FAILED", body["error"]["code"])
            self.assertIn("checksum mismatch", body["error"]["message"])
            self.assertFalse(target_dir.exists())

    def test_skills_install_suite_downloads_release_zips_from_stack_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            analysis_zip = root / "foggy-ai-analysis-skill-9.9.9.zip"
            semantic_zip = root / "foggy-semantic-query-skill-9.9.9.zip"
            analysis_sha = self.write_minimal_skill_zip(analysis_zip, "foggy-ai-analysis")
            semantic_sha = self.write_minimal_skill_zip(semantic_zip, "foggy-semantic-query")
            manifest_path = root / "stable.json"
            self.write_stack_manifest(
                manifest_path,
                {
                    "foggy-ai-analysis": {
                        "version": "9.9.9",
                        "zipPath": str(analysis_zip),
                        "sha256": analysis_sha,
                    },
                    "foggy-semantic-query": {
                        "version": "9.9.9",
                        "zipPath": str(semantic_zip),
                        "sha256": semantic_sha,
                    },
                },
            )

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    [
                        "skills",
                        "install",
                        "foggy-analysis-suite",
                        "--stack-manifest",
                        str(manifest_path),
                    ]
                )

            body = json.loads(output)
            target_root = home / ".agents" / "skills"
            self.assertEqual(EXIT_OK, code)
            self.assertEqual("", error)
            self.assertTrue((target_root / "foggy-ai-analysis" / "SKILL.md").is_file())
            self.assertTrue((target_root / "foggy-semantic-query" / "SKILL.md").is_file())
            self.assertEqual("release-stack", body["data"]["sourceKind"])
            self.assertEqual(["foggy-ai-analysis", "foggy-semantic-query"], [item["skill"] for item in body["data"]["installedSkills"]])
            self.assertTrue(all(item["sourceKind"] == "release-zip" for item in body["data"]["installedSkills"]))

    def test_skills_install_suite_does_not_partially_replace_on_second_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            target_root = home / ".agents" / "skills"
            analysis_target = target_root / "foggy-ai-analysis"
            semantic_target = target_root / "foggy-semantic-query"
            analysis_target.mkdir(parents=True)
            semantic_target.mkdir(parents=True)
            (analysis_target / "SKILL.md").write_text("existing analysis\n", encoding="utf-8")
            (semantic_target / "SKILL.md").write_text("existing semantic\n", encoding="utf-8")

            analysis_zip = root / "foggy-ai-analysis-skill-9.9.9.zip"
            semantic_zip = root / "foggy-semantic-query-skill-9.9.9.zip"
            analysis_sha = self.write_minimal_skill_zip(analysis_zip, "foggy-ai-analysis")
            self.write_minimal_skill_zip(semantic_zip, "foggy-semantic-query")
            manifest_path = root / "stable.json"
            self.write_stack_manifest(
                manifest_path,
                {
                    "foggy-ai-analysis": {
                        "version": "9.9.9",
                        "zipPath": str(analysis_zip),
                        "sha256": analysis_sha,
                    },
                    "foggy-semantic-query": {
                        "version": "9.9.9",
                        "zipPath": str(semantic_zip),
                        "sha256": "0" * 64,
                    },
                },
            )

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    [
                        "skills",
                        "install",
                        "foggy-analysis-suite",
                        "--stack-manifest",
                        str(manifest_path),
                        "--replace",
                    ]
                )

            body = json.loads(output)
            self.assertEqual(EXIT_API_ERROR, code)
            self.assertEqual("", error)
            self.assertFalse(body["success"])
            self.assertIn("checksum mismatch", body["error"]["message"])
            self.assertEqual("existing analysis\n", (analysis_target / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual("existing semantic\n", (semantic_target / "SKILL.md").read_text(encoding="utf-8"))

    def test_skills_install_suite_cleans_partial_target_on_copy_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            workspace = root / "workspace"
            ai_source = workspace / "foggy-ai-analysis" / "locales" / "en"
            semantic_source = workspace / ".codex" / "skills" / "foggy-semantic-query"
            ai_source.mkdir(parents=True)
            semantic_source.mkdir(parents=True)
            (ai_source / "SKILL.md").write_text("---\nname: foggy-ai-analysis\n---\n", encoding="utf-8")
            (semantic_source / "SKILL.md").write_text("---\nname: foggy-semantic-query\n---\n", encoding="utf-8")

            real_copytree = shutil.copytree

            def flaky_copytree(src: Path, dst: Path, **kwargs: Any) -> str:
                target = Path(dst)
                if target.name == "foggy-semantic-query":
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "partial.txt").write_text("partial\n", encoding="utf-8")
                    raise OSError("copy failed")
                return real_copytree(src, dst, **kwargs)

            with (
                patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home),
                patch("foggy_runtime_cli.skills_cli.shutil.copytree", side_effect=flaky_copytree),
            ):
                code, output, error = self.run_cli(
                    ["skills", "install", "foggy-analysis-suite", "--workspace-root", str(workspace)]
                )

            body = json.loads(output)
            target_root = home / ".agents" / "skills"
            self.assertEqual(EXIT_API_ERROR, code)
            self.assertEqual("", error)
            self.assertFalse(body["success"])
            self.assertIn("copy failed", body["error"]["message"])
            self.assertFalse((target_root / "foggy-ai-analysis").exists())
            self.assertFalse((target_root / "foggy-semantic-query").exists())

    def test_skills_install_existing_requires_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            source = root / "source" / "foggy-ai-analysis"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: foggy-ai-analysis\n---\n", encoding="utf-8")
            target = home / ".agents" / "skills" / "foggy-ai-analysis"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("existing\n", encoding="utf-8")

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    ["skills", "install", "foggy-ai-analysis", "--source-dir", str(source)]
                )

            body = json.loads(output)
            self.assertEqual(EXIT_API_ERROR, code)
            self.assertEqual("", error)
            self.assertFalse(body["success"])
            self.assertEqual("existing\n", (target / "SKILL.md").read_text(encoding="utf-8"))

    def test_skills_install_semantic_query_from_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            workspace = root / "workspace"
            source = workspace / ".codex" / "skills" / "foggy-semantic-query"
            (source / "references").mkdir(parents=True)
            (source / "SKILL.md").write_text("---\nname: foggy-semantic-query\n---\n", encoding="utf-8")
            (source / "references" / "query-model-dsl.md").write_text("content\n", encoding="utf-8")

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    ["skills", "install", "foggy-semantic-query", "--workspace-root", str(workspace)]
                )

            body = json.loads(output)
            target_dir = home / ".agents" / "skills" / "foggy-semantic-query"
            self.assertEqual(EXIT_OK, code)
            self.assertEqual("", error)
            self.assertTrue((target_dir / "SKILL.md").is_file())
            self.assertTrue((target_dir / "references" / "query-model-dsl.md").is_file())
            self.assertFalse((home / ".codex" / "skills" / "foggy-semantic-query").exists())
            self.assertFalse((home / ".claude" / "skills" / "foggy-semantic-query").exists())
            self.assertEqual("agents-skills-only", body["data"]["installPolicy"])

    def test_skills_install_suite_from_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            workspace = root / "workspace"
            ai_source = workspace / "foggy-ai-analysis" / "locales" / "en"
            semantic_source = workspace / ".codex" / "skills" / "foggy-semantic-query"
            ai_source.mkdir(parents=True)
            semantic_source.mkdir(parents=True)
            (ai_source / "SKILL.md").write_text("---\nname: foggy-ai-analysis\n---\n", encoding="utf-8")
            (semantic_source / "SKILL.md").write_text("---\nname: foggy-semantic-query\n---\n", encoding="utf-8")

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    ["skills", "install", "foggy-analysis-suite", "--workspace-root", str(workspace)]
                )

            body = json.loads(output)
            target_root = home / ".agents" / "skills"
            self.assertEqual(EXIT_OK, code)
            self.assertEqual("", error)
            self.assertTrue((target_root / "foggy-ai-analysis" / "SKILL.md").is_file())
            self.assertTrue((target_root / "foggy-semantic-query" / "SKILL.md").is_file())
            self.assertFalse((home / ".codex" / "skills" / "foggy-ai-analysis").exists())
            self.assertFalse((home / ".claude" / "skills" / "foggy-semantic-query").exists())
            self.assertEqual("foggy-analysis-suite", body["data"]["skill"])
            self.assertEqual(["foggy-ai-analysis", "foggy-semantic-query"], [item["skill"] for item in body["data"]["installedSkills"]])
            self.assertEqual(
                str(target_root / "foggy-ai-analysis" / "assets" / "sales-drop-demo"),
                body["data"]["installedSkills"][0]["salesDropDemoDir"],
            )
            self.assertEqual("foggy-ai-analysis", body["data"]["installedSkills"][1]["companionSkill"])

    def test_wait_ready_succeeds_after_transport_error(self) -> None:
        from foggy_runtime_cli.client import RuntimeTransportError

        FakeClient.responses = [
            RuntimeTransportError("connection refused"),
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {
                    "schemaVersion": "2026-06-06",
                    "securityMode": "none-dev-test-only",
                    "capabilities": {"query.execute": "supported"},
                },
            },
        ]

        code, output, error = self.run_cli(
            ["--base-url", "http://runtime", "wait-ready", "--timeout-seconds", "1", "--interval-seconds", "0"]
        )

        body = json.loads(output)
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [("GET", "/api/v1/capabilities", None), ("GET", "/api/v1/capabilities", None)],
            FakeClient.calls,
        )
        self.assertTrue(body["data"]["ready"])
        self.assertEqual(2, body["data"]["attemptCount"])
        self.assertEqual("transport-error", body["data"]["attempts"][0]["result"])
        self.assertEqual("passed", body["data"]["attempts"][1]["result"])

    def test_wait_ready_timeout_returns_transport_exit(self) -> None:
        from foggy_runtime_cli.client import RuntimeTransportError

        FakeClient.responses = [RuntimeTransportError("connection refused")]

        code, output, error = self.run_cli(
            ["--base-url", "http://runtime", "wait-ready", "--timeout-seconds", "0", "--interval-seconds", "0"]
        )

        body = json.loads(output)
        self.assertEqual(EXIT_TRANSPORT_ERROR, code)
        self.assertEqual("", error)
        self.assertEqual("RUNTIME_NOT_READY", body["error"]["code"])
        self.assertFalse(body["data"]["ready"])
        self.assertEqual(1, body["data"]["attemptCount"])
        self.assertEqual("transport-error", body["data"]["attempts"][0]["result"])

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

    def test_auth_code_option_is_passed_to_client(self) -> None:
        code, _output, _error = self.run_cli(["--auth-code", "runtime-secret", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("runtime-secret", FakeClient.auth_code)

    def test_auth_code_env_is_passed_to_client(self) -> None:
        with patch.dict(os.environ, {"FOGGY_RUNTIME_API_AUTH_CODE": "env-secret"}, clear=True):
            code, _output, _error = self.run_cli(["capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("env-secret", FakeClient.auth_code)

    def test_auth_code_option_overrides_env(self) -> None:
        with patch.dict(os.environ, {"FOGGY_RUNTIME_API_AUTH_CODE": "env-secret"}, clear=True):
            code, _output, _error = self.run_cli(["--auth-code", "runtime-secret", "capabilities"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("runtime-secret", FakeClient.auth_code)

    def test_authorization_option_is_opaque_and_coexists_with_auth_code(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("models.list"),
            {"success": True, "engine": "java", "data": {"models": []}},
        ]
        code, output, error = self.run_cli([
            "--auth-code",
            "runtime-secret",
            "--authorization",
            "Custom opaque value",
            "models",
            "list",
        ])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("runtime-secret", FakeClient.auth_code)
        self.assertEqual("Custom opaque value", FakeClient.authorization)
        self.assertNotIn("Custom opaque value", output)
        self.assertNotIn("Custom opaque value", error)

    def test_authorization_env_and_option_precedence(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("models.list"),
            {"success": True, "engine": "java", "data": {"models": []}},
        ]
        with patch.dict(
            os.environ,
            {"FOGGY_RUNTIME_AUTHORIZATION": "env-opaque"},
            clear=True,
        ):
            code, _output, _error = self.run_cli(["models", "list"])
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("env-opaque", FakeClient.authorization)

        FakeClient.responses = [
            self.supported_capabilities_response("models.list"),
            {"success": True, "engine": "java", "data": {"models": []}},
        ]
        with patch.dict(
            os.environ,
            {"FOGGY_RUNTIME_AUTHORIZATION": "env-opaque"},
            clear=True,
        ):
            code, _output, _error = self.run_cli([
                "--authorization",
                "option-opaque",
                "models",
                "list",
            ])
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("option-opaque", FakeClient.authorization)

    def test_member_list_route(self) -> None:
        code, _output, _error = self.run_cli([
            "--authorization",
            "opaque",
            "members",
            "list",
            "Sales Model",
            "customer$id",
        ])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [("POST", "/jdbc-model/dimension/v2/Sales%20Model/customer%24id", None)],
            FakeClient.calls,
        )

    def test_engine_option_is_not_supported(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.run_cli(["--engine", "python", "capabilities"])

        self.assertEqual(2, raised.exception.code)
        self.assertEqual([], FakeClient.calls)

    def test_namespace_and_model_describe_body(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("models.describe"),
            {"success": True, "engine": "java", "data": {}},
        ]

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
                ("GET", "/api/v1/capabilities", None),
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
        FakeClient.responses = [
            self.supported_capabilities_response("models.refresh"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, _error = self.run_cli(
            ["--namespace", "dev", "models", "refresh", "--model", "A", "--model", "B"]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                ("POST", "/api/v1/models/refresh", {"namespace": "dev", "models": ["A", "B"]}),
            ],
            FakeClient.calls,
        )

    def test_validate_models_dir_body(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("models.validate"),
            {"success": True, "engine": "java", "data": {}},
        ]

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
                ("GET", "/api/v1/capabilities", None),
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
        FakeClient.responses = [
            self.supported_capabilities_response("models.validate"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, _error = self.run_cli(
            ["models", "validate", "--models-dir", "./models", "--no-clear-existing"]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(False, FakeClient.calls[1][2]["clearExisting"])

    def test_query_payload_from_stdin(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("query.validate"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, _error = self.run_cli(
            ["query", "validate", "FactSales", "--payload", "-"],
            stdin=json.dumps({"columns": ["amount"], "limit": 1}),
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/query/FactSales/validate",
                    {"columns": ["amount"], "limit": 1},
                )
            ],
            FakeClient.calls,
        )

    def test_query_payload_normalizes_group_by_string_array(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("query.validate"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, _error = self.run_cli(
            ["query", "validate", "FactSales", "--payload", "-"],
            stdin=json.dumps(
                {
                    "columns": ["customerName", "sum(amount) as totalAmount"],
                    "groupBy": ["customerName", {"field": "customerSegment"}],
                    "limit": 10,
                }
            ),
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/query/FactSales/validate",
                    {
                        "columns": ["customerName", "sum(amount) as totalAmount"],
                        "groupBy": [
                            {"field": "customerName"},
                            {"field": "customerSegment"},
                        ],
                        "limit": 10,
                    },
                )
            ],
            FakeClient.calls,
        )

    def test_query_payload_normalizes_wrapped_group_by_string_array(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("query.execute"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, _error = self.run_cli(
            ["query", "execute", "FactSales", "--payload", "-"],
            stdin=json.dumps(
                {
                    "payload": {
                        "columns": ["customerName", "sum(amount) as totalAmount"],
                        "groupBy": ["customerName"],
                    }
                }
            ),
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/query/FactSales/execute",
                    {
                        "payload": {
                            "columns": ["customerName", "sum(amount) as totalAmount"],
                            "groupBy": [{"field": "customerName"}],
                        }
                    },
                )
            ],
            FakeClient.calls,
        )

    def test_query_execute_payload_from_file(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("query.execute"),
            {"success": True, "engine": "java", "data": {}},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            payload_path = Path(temp_dir) / "payload.json"
            payload_path.write_text(json.dumps({"columns": ["amount"], "limit": 10}), encoding="utf-8")

            code, _output, _error = self.run_cli(["query", "execute", "Fact Sales", "--payload", str(payload_path)])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/query/Fact%20Sales/execute",
                    {"columns": ["amount"], "limit": 10},
                )
            ],
            FakeClient.calls,
        )

    def test_query_explain_definition_body(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("query.explain"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, error = self.run_cli(
            [
                "query", "explain", "Fact Sales",
                "--field", "amount",
                "--field", "customer$name",
                "--include-physical-names",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/query/Fact%20Sales/explain",
                    {
                        "depth": "STANDARD",
                        "fields": ["amount", "customer$name"],
                        "includePhysicalNames": True,
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_query_explain_recompiled_body(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("query.explain"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, error = self.run_cli(
            [
                "query", "explain", "FactSales",
                "--payload", "-",
                "--depth", "detailed",
                "--include-sql",
                "--include-physical-names",
            ],
            stdin=json.dumps({
                "columns": ["customer", "sum(amount) as total"],
                "groupBy": ["customer"],
            }),
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            {
                "depth": "DETAILED",
                "payload": {
                    "columns": ["customer", "sum(amount) as total"],
                    "groupBy": [{"field": "customer"}],
                },
                "includeSql": True,
                "includePhysicalNames": True,
            },
            FakeClient.calls[1][2],
        )

    def test_query_explain_unsupported_capability_stops_before_route(self) -> None:
        FakeClient.responses = [
            self.capability_response({"query.explain": "unsupported"}),
        ]

        code, output, error = self.run_cli(
            ["query", "explain", "FactSales", "--field", "amount"]
        )

        self.assertEqual(EXIT_UNSUPPORTED, code)
        self.assertEqual("", error)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertIn('"phase": "query.explain"', output)

    def test_table_inspect_body(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("tables.inspect"),
            {"success": True, "engine": "java", "data": {}},
        ]

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
                ("GET", "/api/v1/capabilities", None),
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

    def test_table_inspect_omits_foreign_keys_when_unspecified(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("tables.inspect"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, _error = self.run_cli(["tables", "inspect", "--table", "sale_order"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/tables/inspect",
                    {
                        "table": "sale_order",
                        "includeIndexes": False,
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_table_inspect_can_disable_foreign_keys(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("tables.inspect"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, _error = self.run_cli(
            ["tables", "inspect", "--table", "sale_order", "--no-foreign-keys"]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(False, FakeClient.calls[1][2]["includeForeignKeys"])

    def test_query_execute_payload_file_accepts_utf8_bom(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("query.execute"),
            {"success": True, "engine": "java", "data": {}},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            payload_path = Path(temp_dir) / "payload.json"
            payload_path.write_text(json.dumps({"columns": ["amount"], "limit": 10}), encoding="utf-8-sig")

            code, _output, _error = self.run_cli(["query", "execute", "Fact Sales", "--payload", str(payload_path)])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/query/Fact%20Sales/execute",
                    {"columns": ["amount"], "limit": 10},
                )
            ],
            FakeClient.calls,
        )

    def test_query_payload_from_stdin_accepts_utf8_bom(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("query.validate"),
            {"success": True, "engine": "java", "data": {}},
        ]

        code, _output, _error = self.run_cli(
            ["query", "validate", "FactSales", "--payload", "-"],
            stdin="\ufeff" + json.dumps({"columns": ["amount"], "limit": 1}),
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/query/FactSales/validate",
                    {"columns": ["amount"], "limit": 1},
                )
            ],
            FakeClient.calls,
        )

    def test_tables_list_checks_capability_and_body(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"tables.list": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"tables": []}},
        ]

        code, _output, error = self.run_cli(
            [
                "tables",
                "list",
                "--data-source",
                "sales sqlite",
                "--schema",
                "public",
                "--pattern",
                "sales_%",
                "--no-views",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/tables/list",
                    {
                        "dataSource": "sales sqlite",
                        "schema": "public",
                        "pattern": "sales_%",
                        "includeViews": False,
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_sql_query_checks_capability_and_body(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"sql.query": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"rows": []}},
        ]

        code, _output, error = self.run_cli(
            [
                "sql",
                "query",
                "--data-source",
                "sales-sqlite",
                "--sql",
                "select * from sales_drop_daily",
                "--max-rows",
                "20",
                "--timeout-seconds",
                "5",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/sql/query",
                    {
                        "dataSource": "sales-sqlite",
                        "sql": "select * from sales_drop_daily",
                        "maxRows": 20,
                        "timeoutSeconds": 5,
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_sql_query_reads_stdin_file(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"sql.query": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"rows": []}},
        ]

        code, _output, error = self.run_cli(["sql", "query", "--file", "-"], stdin="select 1")

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                ("POST", "/api/v1/sql/query", {"sql": "select 1"}),
            ],
            FakeClient.calls,
        )

    def test_sql_query_unsupported_capability_stops_before_route(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"sql.query": "unsupported"}},
            }
        ]

        code, output, error = self.run_cli(["sql", "query", "--sql", "select 1"])

        self.assertEqual(EXIT_UNSUPPORTED, code)
        self.assertEqual("", error)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertIn('"code": "UNSUPPORTED_OPERATION"', output)
        self.assertIn('"phase": "sql.query"', output)

    def test_query_execute_unsupported_capability_stops_before_route(self) -> None:
        FakeClient.responses = [
            self.capability_response({"query.execute": "unsupported"}),
        ]

        code, output, error = self.run_cli(
            ["query", "execute", "FactSales", "--payload", "-"],
            stdin=json.dumps({"columns": ["amount"]}),
        )

        self.assertEqual(EXIT_UNSUPPORTED, code)
        self.assertEqual("", error)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertIn('"code": "UNSUPPORTED_OPERATION"', output)
        self.assertIn('"phase": "query.execute"', output)

    def test_models_refresh_unsupported_capability_stops_before_route(self) -> None:
        FakeClient.responses = [
            self.capability_response({"models.refresh": "unsupported"}),
        ]

        code, output, error = self.run_cli(["models", "refresh", "--model", "FactSales"])

        self.assertEqual(EXIT_UNSUPPORTED, code)
        self.assertEqual("", error)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertIn('"code": "UNSUPPORTED_OPERATION"', output)
        self.assertIn('"phase": "models.refresh"', output)

    def test_tables_inspect_unsupported_capability_stops_before_route(self) -> None:
        FakeClient.responses = [
            self.capability_response({"tables.inspect": "unsupported"}),
        ]

        code, output, error = self.run_cli(["tables", "inspect", "--table", "sale_order"])

        self.assertEqual(EXIT_UNSUPPORTED, code)
        self.assertEqual("", error)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertIn('"code": "UNSUPPORTED_OPERATION"', output)
        self.assertIn('"phase": "tables.inspect"', output)

    def test_bundles_list_checks_capability(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"bundles.list": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"bundles": []}},
        ]

        code, _output, error = self.run_cli(["bundles", "list"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                ("GET", "/api/v1/bundles", None),
            ],
            FakeClient.calls,
        )

    def test_bundles_add_body(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"bundles.add": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"bundle": {"name": "sales-drop-dev"}}},
        ]

        code, _output, error = self.run_cli(
            [
                "--namespace",
                "dev",
                "bundles",
                "add",
                "--name",
                "sales-drop-dev",
                "--path",
                "./models",
                "--watch",
                "--replace",
                "--validate",
                "--refresh",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/bundles",
                    {
                        "name": "sales-drop-dev",
                        "path": "./models",
                        "watch": True,
                        "replace": True,
                        "validate": True,
                        "refresh": True,
                        "enabled": True,
                        "namespace": "dev",
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_bundles_update_path_and_body(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"bundles.update": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"bundle": {"name": "sales drop"}}},
        ]

        code, _output, error = self.run_cli(
            [
                "--namespace",
                "dev",
                "bundles",
                "update",
                "sales drop",
                "--path",
                "./models-v2",
                "--watch",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "PUT",
                    "/api/v1/bundles/sales%20drop",
                    {
                        "name": "sales drop",
                        "path": "./models-v2",
                        "watch": True,
                        "replace": True,
                        "validate": False,
                        "refresh": False,
                        "enabled": True,
                        "namespace": "dev",
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_bundles_update_omits_watch_when_unspecified(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("bundles.update"),
            {"success": True, "engine": "java", "data": {"bundle": {"name": "sales-drop-dev"}}},
        ]

        code, _output, error = self.run_cli(
            ["bundles", "update", "sales-drop-dev", "--path", "./models-v2"]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertNotIn("watch", FakeClient.calls[1][2])

    def test_bundles_update_can_disable_watch(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("bundles.update"),
            {"success": True, "engine": "java", "data": {"bundle": {"name": "sales-drop-dev"}}},
        ]

        code, _output, error = self.run_cli(
            ["bundles", "update", "sales-drop-dev", "--path", "./models-v2", "--no-watch"]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(False, FakeClient.calls[1][2]["watch"])

    def test_bundles_remove_path_and_capability(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"bundles.remove": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"removed": True}},
        ]

        code, _output, error = self.run_cli(["bundles", "remove", "sales-drop-dev"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                ("DELETE", "/api/v1/bundles/sales-drop-dev", None),
            ],
            FakeClient.calls,
        )

    def test_bundles_command_unsupported_capability_stops_before_route(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "python",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"bundles.add": "unsupported"}},
            }
        ]

        code, output, error = self.run_cli(
            ["bundles", "add", "--name", "dev-bundle", "--path", "./models"]
        )

        self.assertEqual(EXIT_UNSUPPORTED, code)
        self.assertEqual("", error)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertIn('"code": "UNSUPPORTED_OPERATION"', output)
        self.assertIn('"phase": "bundles.add"', output)

    def test_datasources_list_checks_capability(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"datasources.list": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"datasources": []}},
        ]

        code, _output, error = self.run_cli(["datasources", "list"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                ("GET", "/api/v1/datasources", None),
            ],
            FakeClient.calls,
        )

    def test_datasources_add_body(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"datasources.add": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"datasource": {"name": "sales-sqlite"}}},
        ]

        code, _output, error = self.run_cli(
            [
                "datasources",
                "add",
                "--name",
                "sales-sqlite",
                "--type",
                "sqlite",
                "--jdbc-url",
                "jdbc:sqlite:./sales.db",
                "--replace",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/datasources",
                    {
                        "name": "sales-sqlite",
                        "type": "sqlite",
                        "jdbcUrl": "jdbc:sqlite:./sales.db",
                        "replace": True,
                        "enabled": True,
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_datasources_add_mysql_password_env_body(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"datasources.add": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"datasource": {"name": "sales-mysql"}}},
        ]

        code, _output, error = self.run_cli(
            [
                "datasources",
                "add",
                "--name",
                "sales-mysql",
                "--type",
                "mysql",
                "--jdbc-url",
                "jdbc:mysql://127.0.0.1:13308/foggy_demo?useSSL=false",
                "--username",
                "foggy",
                "--password-env",
                "FOGGY_MYSQL_PASSWORD",
                "--replace",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/datasources",
                    {
                        "name": "sales-mysql",
                        "type": "mysql",
                        "jdbcUrl": "jdbc:mysql://127.0.0.1:13308/foggy_demo?useSSL=false",
                        "username": "foggy",
                        "passwordRef": "env:FOGGY_MYSQL_PASSWORD",
                        "replace": True,
                        "enabled": True,
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_datasources_update_path_and_body(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"datasources.update": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"datasource": {"name": "sales sqlite"}}},
        ]

        code, _output, error = self.run_cli(
            [
                "datasources",
                "update",
                "sales sqlite",
                "--jdbc-url",
                "jdbc:sqlite:./sales-v2.db",
                "--disabled",
            ]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "PUT",
                    "/api/v1/datasources/sales%20sqlite",
                    {
                        "name": "sales sqlite",
                        "type": "sqlite",
                        "jdbcUrl": "jdbc:sqlite:./sales-v2.db",
                        "replace": True,
                        "enabled": False,
                    },
                ),
            ],
            FakeClient.calls,
        )

    def test_datasources_test_path(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"datasources.test": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"connected": True}},
        ]

        code, _output, error = self.run_cli(["datasources", "test", "sales sqlite"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                ("POST", "/api/v1/datasources/sales%20sqlite/test", None),
            ],
            FakeClient.calls,
        )

    def test_datasources_bind_path_and_body(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"datasources.bind": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"namespace": "dev"}},
        ]

        code, _output, error = self.run_cli(
            ["datasources", "bind", "--namespace", "dev ns", "--data-source", "sales-sqlite"]
        )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "PUT",
                    "/api/v1/namespaces/dev%20ns/datasource",
                    {"namespace": "dev ns", "dataSource": "sales-sqlite"},
                ),
            ],
            FakeClient.calls,
        )

    def test_datasources_binding_path(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("datasources.bind"),
            {"success": True, "engine": "java", "data": {"namespace": "dev ns", "dataSource": "sales-sqlite"}},
        ]

        code, _output, error = self.run_cli(["datasources", "binding", "--namespace", "dev ns"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                ("GET", "/api/v1/namespaces/dev%20ns/datasource", None),
            ],
            FakeClient.calls,
        )

    def test_datasources_diagnostics_path(self) -> None:
        FakeClient.responses = [
            self.supported_capabilities_response("datasources.diagnostics"),
            {
                "success": True,
                "engine": "java",
                "data": {
                    "registryPath": "D:/runtime/.foggy-runtime/runtime-datasources.json",
                    "namespaceBindings": {"dev ns": "sales-sqlite"},
                    "datasources": [],
                    "warnings": [],
                },
            },
        ]

        code, _output, error = self.run_cli(["datasources", "diagnostics"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                ("GET", "/api/v1/datasources/diagnostics", None),
            ],
            FakeClient.calls,
        )

    def test_datasources_command_unsupported_capability_stops_before_route(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"datasources.add": "unsupported"}},
            }
        ]

        code, output, error = self.run_cli(
            ["datasources", "add", "--name", "sales-sqlite", "--jdbc-url", "jdbc:sqlite:./sales.db"]
        )

        self.assertEqual(EXIT_UNSUPPORTED, code)
        self.assertEqual("", error)
        self.assertEqual([("GET", "/api/v1/capabilities", None)], FakeClient.calls)
        self.assertIn('"code": "UNSUPPORTED_OPERATION"', output)
        self.assertIn('"phase": "datasources.add"', output)

    def test_resources_pull_writes_files_and_checks_capability(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"resources.export": "supported"}},
            },
            {
                "success": True,
                "engine": "java",
                "data": {
                    "bundle": "sales-drop-dev",
                    "resources": [
                        {"path": "model/Sales.tm", "content": "table_model Sales {}\n"},
                        {"path": "query/SalesModel.qm", "content": "query_model SalesModel {}\n"},
                    ],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            code, output, error = self.run_cli(
                [
                    "--namespace",
                    "dev",
                    "resources",
                    "pull",
                    "--bundle",
                    "sales-drop-dev",
                    "--out",
                    temp_dir,
                ]
            )
            out_dir = Path(temp_dir)
            tm_text = (out_dir / "model" / "Sales.tm").read_text(encoding="utf-8")
            qm_text = (out_dir / "query" / "SalesModel.qm").read_text(encoding="utf-8")

        payload = json.loads(output)
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual("table_model Sales {}\n", tm_text)
        self.assertEqual("query_model SalesModel {}\n", qm_text)
        self.assertEqual(
            [
                ("GET", "/api/v1/capabilities", None),
                (
                    "POST",
                    "/api/v1/resources/export",
                    {"bundle": "sales-drop-dev", "includeContent": True, "namespace": "dev"},
                ),
            ],
            FakeClient.calls,
        )
        self.assertEqual(2, len(payload["data"]["writtenFiles"]))

    def test_resources_save_collects_semantic_files(self) -> None:
        FakeClient.responses = [
            {
                "success": True,
                "engine": "java",
                "runtimeApiVersion": "foggy-runtime-api/v1",
                "data": {"capabilities": {"resources.save": "supported"}},
            },
            {"success": True, "engine": "java", "data": {"savedCount": 3}},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "model").mkdir()
            (root / "query").mkdir()
            (root / "model" / "Sales.tm").write_text("table_model Sales {}\n", encoding="utf-8")
            (root / "query" / "SalesModel.qm").write_text("query_model SalesModel {}\n", encoding="utf-8")
            (root / "model-list.yml").write_text("models: []\n", encoding="utf-8")
            (root / "README.md").write_text("ignored\n", encoding="utf-8")

            code, _output, error = self.run_cli(
                [
                    "--namespace",
                    "dev",
                    "resources",
                    "save",
                    "--bundle",
                    "sales-drop-dev",
                    "--dir",
                    temp_dir,
                    "--validate",
                    "--refresh",
                ]
            )

        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual("POST", FakeClient.calls[1][0])
        self.assertEqual("/api/v1/resources/save", FakeClient.calls[1][1])
        body = FakeClient.calls[1][2]
        self.assertEqual("sales-drop-dev", body["bundle"])
        self.assertEqual("dev", body["namespace"])
        self.assertTrue(body["validate"])
        self.assertTrue(body["refresh"])
        self.assertEqual(
            ["model/Sales.tm", "model-list.yml", "query/SalesModel.qm"],
            [item["path"] for item in body["files"]],
        )

    def test_resources_save_rejects_empty_dir_before_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            code, _output, error = self.run_cli(
                ["resources", "save", "--bundle", "sales-drop-dev", "--dir", temp_dir]
            )

        self.assertEqual(1, code)
        self.assertIn("no semantic resources found", error)
        self.assertEqual([], FakeClient.calls)

    def test_demo_sales_drop_plan_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            home = repo_root / "home"
            skill_dir = repo_root / ".codex" / "skills" / "foggy-ai-analysis"
            demo_dir = self.write_minimal_sales_drop_assets(skill_dir)

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    ["demo", "sales-drop", "plan", "--repo-root", str(repo_root), "--port", "18066"]
                )

        payload = json.loads(output)
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual([], FakeClient.calls)
        self.assertTrue(payload["success"])
        self.assertEqual("local", payload["engine"])
        self.assertEqual("sales-drop", payload["data"]["demo"])
        self.assertEqual("http://127.0.0.1:18066", payload["data"]["baseUrl"])
        self.assertEqual("default", payload["data"]["namespace"])
        self.assertEqual(str(skill_dir), payload["data"]["skillDir"])
        self.assertEqual(str(demo_dir), payload["data"]["demoDir"])
        self.assertIn("commands", payload["data"])

    def test_demo_sales_drop_plan_prefers_installed_analysis_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            repo_root = root / "workspace"
            installed_skill = home / ".agents" / "skills" / "foggy-ai-analysis"
            workspace_skill = repo_root / ".codex" / "skills" / "foggy-ai-analysis"
            installed_demo_dir = self.write_minimal_sales_drop_assets(installed_skill)
            self.write_minimal_sales_drop_assets(workspace_skill)

            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    ["demo", "sales-drop", "plan", "--repo-root", str(repo_root)]
                )

        payload = json.loads(output)
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertTrue(payload["success"])
        self.assertEqual(str(installed_skill), payload["data"]["skillDir"])
        self.assertEqual(str(installed_demo_dir), payload["data"]["demoDir"])

    def test_demo_sales_drop_plan_accepts_unpacked_skill_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo_root = root / "workspace"
            skill_dir = root / "release-skill" / "foggy-ai-analysis"
            demo_dir = self.write_minimal_sales_drop_assets(skill_dir)

            code, output, error = self.run_cli(
                [
                    "demo",
                    "sales-drop",
                    "plan",
                    "--repo-root",
                    str(repo_root),
                    "--skill-dir",
                    str(skill_dir),
                    "--port",
                    "18067",
                ]
            )

        payload = json.loads(output)
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertEqual([], FakeClient.calls)
        self.assertTrue(payload["success"])
        self.assertEqual(str(skill_dir), payload["data"]["skillDir"])
        self.assertEqual(str(demo_dir), payload["data"]["demoDir"])
        self.assertEqual(str(demo_dir / "models"), payload["data"]["modelsDir"])
        self.assertEqual(str(demo_dir / "queries" / "basic.json"), payload["data"]["queryPayload"])
        self.assertIn(str(demo_dir / "schema.sql"), payload["data"]["commands"][1]["argv"][2])

    def test_demo_sales_drop_plan_reports_missing_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(["demo", "sales-drop", "plan", "--repo-root", temp_dir])

        payload = json.loads(output)
        self.assertEqual(EXIT_API_ERROR, code)
        self.assertEqual("", error)
        self.assertEqual([], FakeClient.calls)
        self.assertFalse(payload["success"])
        self.assertEqual("DEMO_ASSET_MISSING", payload["error"]["code"])
        self.assertEqual(
            "foggy-runtime skills install foggy-ai-analysis --zip foggy-ai-analysis-skill-0.1.16.zip --replace",
            payload["data"]["installCommand"],
        )
        self.assertIn("foggy-ai-analysis/releases/download/v0.1.16", payload["data"]["publicZipUrl"])
        self.assertIn("--zip foggy-ai-analysis-skill-0.1.16.zip", payload["error"]["message"])

    def test_demo_available_query_fields_supports_date_grains(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            query_model = Path(temp_dir) / "SalesDropDailyQueryModel.qm"
            query_model.write_text(
                "salesDrop.observationDate\nsalesDrop.observationDate$week\nsalesDrop.customerName\n",
                encoding="utf-8",
            )

            fields = demo_available_query_fields(query_model)

        self.assertEqual(["customerName", "observationDate", "observationDate$week"], fields)

    def test_demo_sales_drop_replay_runs_against_runtime_api(self) -> None:
        capabilities = {
            "success": True,
            "engine": "java",
            "runtimeApiVersion": "foggy-runtime-api/v1",
            "data": {
                "schemaVersion": "2026-06-06",
                "securityMode": "none-dev-test-only",
                "capabilities": {
                    "runtime.capabilities": "supported",
                    "datasources.add": "supported",
                    "datasources.test": "supported",
                    "datasources.bind": "supported",
                    "tables.list": "supported",
                    "tables.inspect": "supported",
                    "sql.query": "supported",
                    "models.validate": "supported",
                    "bundles.list": "supported",
                    "bundles.add": "supported",
                    "models.refresh": "supported",
                    "models.describe": "supported",
                    "query.validate": "supported",
                    "query.execute": "supported",
                },
            },
        }
        query_result = {
            "success": True,
            "engine": "java",
            "data": {
                "items": [{"severity": "CRITICAL", "region": "North China", "observationDate$week": 24}],
                "schema": {
                    "columns": [{"name": "severity"}, {"name": "region"}, {"name": "observationDate$week"}]
                },
            },
        }
        FakeClient.responses = [
            capabilities,
            capabilities,
            {"success": True, "engine": "java", "data": {}},
            {"success": True, "engine": "java", "data": {}},
            {"success": True, "engine": "java", "data": {}},
            {"success": True, "engine": "java", "data": {"tables": ["sales_drop_daily"]}},
            {"success": True, "engine": "java", "data": {"table": "sales_drop_daily"}},
            {"success": True, "engine": "java", "data": {"items": []}},
            {"success": True, "engine": "java", "data": {"valid": True, "invalidFiles": 0}},
            {"success": True, "engine": "java", "data": {"bundles": []}},
            {"success": True, "engine": "java", "data": {"bundle": {"name": "sales-drop-models"}}},
            {
                "success": True,
                "engine": "java",
                "data": {"failedCount": 0, "refreshedModels": ["SalesDropDailyQueryModel"]},
            },
            {"success": True, "engine": "java", "data": {"model": "SalesDropDailyQueryModel"}},
            {"success": True, "engine": "java", "data": {"valid": True}},
            query_result,
            {"success": True, "engine": "java", "data": {"valid": True}},
            query_result,
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "foggy-ai-analysis"
            demo_dir = skill_dir / "assets" / "sales-drop-demo"
            models_dir = demo_dir / "models"
            (models_dir / "query").mkdir(parents=True)
            (demo_dir / "queries" / "question-bank").mkdir(parents=True)
            (demo_dir / "schema.sql").write_text(
                "drop table if exists sales_drop_daily; create table sales_drop_daily(id integer);",
                encoding="utf-8",
            )
            (demo_dir / "data.sql").write_text("insert into sales_drop_daily values (1);", encoding="utf-8")
            (models_dir / "query" / "SalesDropDailyQueryModel.qm").write_text(
                "salesDrop.severity\nsalesDrop.region\nsalesDrop.observationDate$week\n",
                encoding="utf-8",
            )
            (demo_dir / "queries" / "basic.json").write_text(json.dumps({"limit": 1}), encoding="utf-8")
            (demo_dir / "queries" / "question-bank" / "SD-001.json").write_text(
                json.dumps({"limit": 1}),
                encoding="utf-8",
            )
            (demo_dir / "question-bank.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": "foggy-demo-question-bank/v1",
                        "model": "SalesDropDailyQueryModel",
                        "cases": [
                            {
                                "id": "SD-001",
                                "status": "executable",
                                "question": "critical rows",
                                "requiredFields": ["severity", "region"],
                                "payloadFile": "queries/question-bank/SD-001.json",
                                "assertions": {
                                    "rowCountMin": 1,
                                    "requiredColumns": ["severity", "region", "observationDate$week"],
                                    "expectedValues": [{"field": "severity", "value": "CRITICAL"}],
                                },
                            },
                            {
                                "id": "SD-002",
                                "status": "needs-clarification",
                                "question": "forecast",
                                "requiredFields": [],
                                "skipReason": "forecast source missing",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            evidence_dir = root / "evidence"

            code, output, error = self.run_cli(
                [
                    "--base-url",
                    "http://runtime:18066",
                    "demo",
                    "sales-drop",
                    "replay",
                    "--skill-dir",
                    str(skill_dir),
                    "--evidence-dir",
                    str(evidence_dir),
                    "--ready-timeout-seconds",
                    "0",
                    "--ready-interval-seconds",
                    "0",
                ]
            )

            summary = json.loads((evidence_dir / "summary.json").read_text(encoding="utf-8"))
            question_replay = json.loads((evidence_dir / "question-bank-replay.json").read_text(encoding="utf-8"))

        payload = json.loads(output)
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertTrue(payload["success"])
        self.assertEqual(("http://runtime:18066", "salesdrop", 30.0), FakeClient.init_args)
        self.assertEqual("passed", summary["status"])
        self.assertEqual("salesdrop", summary["namespace"])
        self.assertEqual(2, summary["questionBankSummary"]["total"])
        self.assertEqual(1, summary["questionBankSummary"]["passed"])
        self.assertEqual(1, summary["questionBankSummary"]["needsClarification"])
        self.assertEqual(2, question_replay["totalCases"])
        self.assertEqual("GET", FakeClient.calls[0][0])
        self.assertEqual("/api/v1/capabilities", FakeClient.calls[0][1])
        self.assertEqual("POST", FakeClient.calls[2][0])
        self.assertEqual("/api/v1/datasources", FakeClient.calls[2][1])
        self.assertEqual("sales-drop-sqlite", FakeClient.calls[2][2]["name"])

    def test_demo_sales_drop_replay_can_use_default_datasource(self) -> None:
        capabilities = {
            "success": True,
            "engine": "java",
            "runtimeApiVersion": "foggy-runtime-api/v1",
            "data": {
                "schemaVersion": "2026-06-06",
                "securityMode": "none-dev-test-only",
                "capabilities": {
                    "runtime.capabilities": "supported",
                    "datasources.test": "supported",
                    "tables.list": "supported",
                    "tables.inspect": "supported",
                    "sql.query": "supported",
                    "models.validate": "supported",
                    "bundles.list": "supported",
                    "bundles.add": "supported",
                    "models.refresh": "supported",
                    "models.describe": "supported",
                    "query.validate": "supported",
                    "query.execute": "supported",
                },
            },
        }
        FakeClient.responses = [
            capabilities,
            capabilities,
            {"success": True, "engine": "java", "data": {}},
            {"success": True, "engine": "java", "data": {"tables": ["sales_drop_daily"]}},
            {"success": True, "engine": "java", "data": {"table": "sales_drop_daily"}},
            {"success": True, "engine": "java", "data": {"items": []}},
            {"success": True, "engine": "java", "data": {"valid": True, "invalidFiles": 0}},
            {"success": True, "engine": "java", "data": {"bundles": []}},
            {"success": True, "engine": "java", "data": {"bundle": {"name": "sales-drop-models"}}},
            {
                "success": True,
                "engine": "java",
                "data": {"failedCount": 0, "refreshedModels": ["SalesDropDailyQueryModel"]},
            },
            {"success": True, "engine": "java", "data": {"model": "SalesDropDailyQueryModel"}},
            {"success": True, "engine": "java", "data": {"valid": True}},
            {"success": True, "engine": "java", "data": {"items": []}},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_dir = root / "foggy-ai-analysis"
            demo_dir = skill_dir / "assets" / "sales-drop-demo"
            models_dir = demo_dir / "models"
            models_dir.mkdir(parents=True)
            (demo_dir / "queries").mkdir(parents=True)
            (demo_dir / "schema.sql").write_text(
                "drop table if exists sales_drop_daily; create table sales_drop_daily(id integer);",
                encoding="utf-8",
            )
            (demo_dir / "data.sql").write_text("insert into sales_drop_daily values (1);", encoding="utf-8")
            (demo_dir / "queries" / "basic.json").write_text(json.dumps({"limit": 1}), encoding="utf-8")
            evidence_dir = root / "evidence"
            sqlite_path = root / "runtime-default.sqlite"

            code, output, error = self.run_cli(
                [
                    "--base-url",
                    "http://runtime:18066",
                    "demo",
                    "sales-drop",
                    "replay",
                    "--skill-dir",
                    str(skill_dir),
                    "--evidence-dir",
                    str(evidence_dir),
                    "--sqlite-path",
                    str(sqlite_path),
                    "--use-default-datasource",
                    "--skip-question-bank",
                    "--ready-timeout-seconds",
                    "0",
                    "--ready-interval-seconds",
                    "0",
                ]
            )

            summary = json.loads((evidence_dir / "summary.json").read_text(encoding="utf-8"))

        payload = json.loads(output)
        self.assertEqual(EXIT_OK, code)
        self.assertEqual("", error)
        self.assertTrue(payload["success"])
        self.assertEqual("default", summary["dataSource"])
        self.assertEqual("default", summary["dataSourceMode"])
        self.assertFalse(any(method == "POST" and path == "/api/v1/datasources" for method, path, _ in FakeClient.calls))
        self.assertFalse(any(method == "PUT" and path.endswith("/datasource") for method, path, _ in FakeClient.calls))
        self.assertEqual(("POST", "/api/v1/datasources/default/test", None), FakeClient.calls[2])
        self.assertEqual("default", FakeClient.calls[3][2]["dataSource"])

    def test_demo_sales_drop_replay_default_datasource_requires_sqlite_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skill_dir = Path(temp_dir) / "foggy-ai-analysis"
            code, output, error = self.run_cli(
                [
                    "--base-url",
                    "http://runtime:18066",
                    "demo",
                    "sales-drop",
                    "replay",
                    "--skill-dir",
                    str(skill_dir),
                    "--use-default-datasource",
                ]
            )

        payload = json.loads(output)
        self.assertEqual(EXIT_CLI_ERROR, code)
        self.assertEqual("", error)
        self.assertFalse(payload["success"])
        self.assertEqual("DEMO_SQLITE_PATH_REQUIRED", payload["error"]["code"])
        self.assertEqual([], FakeClient.calls)

    def test_demo_sales_drop_replay_missing_assets_mentions_install_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            with patch("foggy_runtime_cli.skills_cli.Path.home", return_value=home):
                code, output, error = self.run_cli(
                    [
                        "--base-url",
                        "http://runtime:18066",
                        "demo",
                        "sales-drop",
                        "replay",
                    ]
                )

        payload = json.loads(output)
        self.assertEqual(EXIT_API_ERROR, code)
        self.assertEqual("", error)
        self.assertFalse(payload["success"])
        self.assertEqual("DEMO_ASSET_MISSING", payload["error"]["code"])
        self.assertEqual(
            "foggy-runtime skills install foggy-ai-analysis --zip foggy-ai-analysis-skill-0.1.16.zip --replace",
            payload["data"]["installCommand"],
        )
        self.assertIn("foggy-ai-analysis/releases/download/v0.1.16", payload["data"]["publicZipUrl"])
        self.assertIn("--zip foggy-ai-analysis-skill-0.1.16.zip", payload["error"]["message"])
        self.assertEqual([], FakeClient.calls)

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
        FakeClient.responses = [
            self.supported_capabilities_response("models.list"),
            {"success": True, "engine": "java", "data": {"models": ["FactSales", "DimCustomer"]}},
        ]

        code, output, _error = self.run_cli(["--output", "pretty", "models", "list"])

        self.assertEqual(EXIT_OK, code)
        self.assertEqual(
            [("GET", "/api/v1/capabilities", None), ("GET", "/api/v1/models", None)],
            FakeClient.calls,
        )
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
