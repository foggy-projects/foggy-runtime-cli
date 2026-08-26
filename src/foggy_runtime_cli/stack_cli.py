from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__

ANALYSIS_SKILL = "foggy-ai-analysis"
SEMANTIC_QUERY_SKILL = "foggy-semantic-query"
DEFAULT_STACK_MANIFEST_URL = (
    "https://raw.githubusercontent.com/foggy-projects/foggy-ai-analysis/main/stack/stable.json"
)
STACK_MANIFEST_ENV = "FOGGY_STACK_MANIFEST_URL"
ANALYSIS_SKILL_RELEASE_VERSION = "0.1.17"
ANALYSIS_SKILL_RELEASE_ZIP = (
    f"foggy-ai-analysis-skill-{ANALYSIS_SKILL_RELEASE_VERSION}.zip"
)
ANALYSIS_SKILL_RELEASE_ZIP_URL = (
    "https://github.com/foggy-projects/foggy-ai-analysis/releases/download/"
    f"v{ANALYSIS_SKILL_RELEASE_VERSION}/{ANALYSIS_SKILL_RELEASE_ZIP}"
)


def register_stack_parser(subparsers: Any) -> None:
    stack = subparsers.add_parser(
        "stack",
        help="Inspect the recommended Foggy CLI/Skill/launcher release stack.",
    )
    stack_commands = stack.add_subparsers(dest="stack_command", required=True)
    stack_show = stack_commands.add_parser(
        "show",
        description="Resolve and print the recommended stable Foggy release stack.",
    )
    stack_show.add_argument(
        "--stack-manifest",
        help=(
            "Path or URL to a foggy-stack/v1 manifest. "
            f"Defaults to {DEFAULT_STACK_MANIFEST_URL}."
        ),
    )
    stack_show.add_argument(
        "--offline-stack",
        action="store_true",
        help="Use the CLI built-in fallback stack manifest without network access.",
    )
    stack_show.set_defaults(local_handler=stack_show_handler)



def default_stack_manifest() -> dict[str, Any]:
    skill_release_base = (
        "https://github.com/foggy-projects/foggy-ai-analysis/releases/download/"
        f"v{ANALYSIS_SKILL_RELEASE_VERSION}"
    )
    launcher_version = "0.1.18"
    launcher_tag = f"foggy-runtime-launcher-v{launcher_version}"
    launcher_base = "https://github.com/foggy-projects/foggy-data-mcp-bridge/releases/download/" + launcher_tag
    cli_tag = f"v{__version__}"
    return {
        "schemaVersion": "foggy-stack/v1",
        "channel": "stable",
        "source": "foggy-runtime-cli builtin fallback",
        "components": {
            "cli": {
                "name": "foggy-runtime-cli",
                "recommendedVersion": __version__,
                "tag": cli_tag,
                "releaseUrl": "https://github.com/foggy-projects/foggy-runtime-cli/releases/tag/" + cli_tag,
                "install": {
                    "windows": "https://github.com/foggy-projects/foggy-runtime-cli/releases/download/"
                    + cli_tag
                    + "/install-foggy-runtime-cli.ps1",
                    "posix": "https://github.com/foggy-projects/foggy-runtime-cli/releases/download/"
                    + cli_tag
                    + "/install-foggy-runtime-cli.sh",
                },
            },
            "launcher": {
                "name": "foggy-runtime-launcher",
                "recommendedTag": launcher_tag,
                "version": launcher_version,
                "runtimeApiContract": "foggy-runtime-api/v1",
                "breaking": False,
                "features": {
                    "analyticsConsole": {
                        "embedded": True,
                        "enabledByDefault": False,
                        "springProfile": "analytics-console",
                        "bashOptIn": "ANALYTICS_CONSOLE_ENABLED=true",
                        "powershellOptIn": "-AnalyticsConsole",
                        "webPath": "/analytics-console/",
                        "apiPath": "/analytics-console/api/v1",
                        "analyticsRuntimeApiPath": "/analytics/api/v1",
                        "fapEnabledByDefault": False,
                    },
                    "analyticsRuntimeApi": {
                        "embedded": True,
                        "enabledByDefault": False,
                    },
                },
                "releaseUrl": "https://github.com/foggy-projects/foggy-data-mcp-bridge/releases/tag/" + launcher_tag,
                "assets": {
                    "jar": {
                        "file": f"foggy-runtime-launcher-{launcher_version}.jar",
                        "url": launcher_base + f"/foggy-runtime-launcher-{launcher_version}.jar",
                        "sha256": "2953d2fab39208b3fffefed033b34ec6f59da851920b533047a1d0909a1de049",
                    },
                    "startPowerShell": {
                        "file": "start-foggy-runtime.ps1",
                        "url": launcher_base + "/start-foggy-runtime.ps1",
                        "sha256": "b54f2f144cfdc38afcc3e9167d0700248f476c93adb3e9d95431490430f7b549",
                    },
                    "startShell": {
                        "file": "start-foggy-runtime.sh",
                        "url": launcher_base + "/start-foggy-runtime.sh",
                        "sha256": "e801cc340bf0842e29045e5e24798921864114846b0e1aaff30640845c8f37d0",
                    },
                    "readme": {
                        "file": "README-foggy-runtime-launcher.md",
                        "url": launcher_base + "/README-foggy-runtime-launcher.md",
                        "sha256": "0202546c5221cbb9343cf7367ceb20bff785fd85c2ba77cd0dac5395f6362674",
                    },
                    "checksums": {
                        "file": "SHA256SUMS",
                        "url": launcher_base + "/SHA256SUMS",
                        "sha256": "26f65a7112c115ccd19c6f80336b4335ab7773d54841376aa81504d1c3fcd21f",
                    },
                    "manifest": {
                        "file": "runtime-launcher-manifest.json",
                        "url": launcher_base + "/runtime-launcher-manifest.json",
                        "sha256": "0065de9c516471b936927a0b13ed1b9380c484437352b2e589c5171932427326",
                    },
                },
            },
            "skills": {
                ANALYSIS_SKILL: {
                    "name": ANALYSIS_SKILL,
                    "recommendedVersion": ANALYSIS_SKILL_RELEASE_VERSION,
                    "tag": f"v{ANALYSIS_SKILL_RELEASE_VERSION}",
                    "language": "en",
                    "minCliVersion": "0.1.21",
                    "releaseUrl": f"https://github.com/foggy-projects/foggy-ai-analysis/releases/tag/v{ANALYSIS_SKILL_RELEASE_VERSION}",
                    "zip": {
                        "file": ANALYSIS_SKILL_RELEASE_ZIP,
                        "url": ANALYSIS_SKILL_RELEASE_ZIP_URL,
                        "sha256": "2c5a10090befc38dd2cf8c3de5121d19c28682ca9c0fe72daf778d315c56c7ff",
                    },
                    "manifest": {
                        "file": f"foggy-ai-analysis-skill-{ANALYSIS_SKILL_RELEASE_VERSION}-manifest.json",
                        "url": skill_release_base
                        + f"/foggy-ai-analysis-skill-{ANALYSIS_SKILL_RELEASE_VERSION}-manifest.json",
                        "sha256": "094b1401ff384f011ef80446dd639d8afb2585aa01d7a7ba49d39cb2968ae8bf",
                    },
                    "checksums": {
                        "file": f"foggy-ai-analysis-skill-{ANALYSIS_SKILL_RELEASE_VERSION}-SHA256SUMS",
                        "url": skill_release_base
                        + f"/foggy-ai-analysis-skill-{ANALYSIS_SKILL_RELEASE_VERSION}-SHA256SUMS",
                        "sha256": "18804088cd7da7af6bbf70d658cb0d6a5a31e765470ed673dfd274f1789db240",
                    },
                },
                SEMANTIC_QUERY_SKILL: {
                    "name": SEMANTIC_QUERY_SKILL,
                    "recommendedVersion": ANALYSIS_SKILL_RELEASE_VERSION,
                    "tag": f"v{ANALYSIS_SKILL_RELEASE_VERSION}",
                    "language": "en",
                    "minCliVersion": "0.1.21",
                    "releaseUrl": f"https://github.com/foggy-projects/foggy-ai-analysis/releases/tag/v{ANALYSIS_SKILL_RELEASE_VERSION}",
                    "zip": {
                        "file": f"foggy-semantic-query-skill-{ANALYSIS_SKILL_RELEASE_VERSION}.zip",
                        "url": skill_release_base
                        + f"/foggy-semantic-query-skill-{ANALYSIS_SKILL_RELEASE_VERSION}.zip",
                        "sha256": "2a9b38640816617c0ab336704471a662e1a218b7b8cf482b54b8fcccbba51ad1",
                    },
                    "manifest": {
                        "file": f"foggy-semantic-query-skill-{ANALYSIS_SKILL_RELEASE_VERSION}-manifest.json",
                        "url": skill_release_base
                        + f"/foggy-semantic-query-skill-{ANALYSIS_SKILL_RELEASE_VERSION}-manifest.json",
                        "sha256": "56cdd47764fb052100132dbff55e5e67fa1cc79b9bd7548208c6247a476ae72d",
                    },
                    "checksums": {
                        "file": f"foggy-semantic-query-skill-{ANALYSIS_SKILL_RELEASE_VERSION}-SHA256SUMS",
                        "url": skill_release_base
                        + f"/foggy-semantic-query-skill-{ANALYSIS_SKILL_RELEASE_VERSION}-SHA256SUMS",
                        "sha256": "34ff674f69daa51cc5afea9e58980d2b6fb92ff55a47872c1d08d1a0a7e549f4",
                    },
                },
            },
        },
    }


def load_stack_manifest(
    args: argparse.Namespace,
    *,
    allow_default_fallback: bool = True,
) -> tuple[dict[str, Any], str, list[str]]:
    if getattr(args, "offline_stack", False):
        return default_stack_manifest(), "builtin:fallback", []

    explicit_ref = getattr(args, "stack_manifest", None)
    env_ref = os.environ.get(STACK_MANIFEST_ENV)
    manifest_ref = explicit_ref or env_ref or DEFAULT_STACK_MANIFEST_URL
    explicit = bool(explicit_ref or env_ref)
    try:
        raw = read_text_resource(manifest_ref)
        payload = json.loads(raw.lstrip("\ufeff"))
        validate_stack_manifest(payload)
        return payload, manifest_ref, []
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        if explicit or not allow_default_fallback:
            raise ValueError(
                f"Cannot load stack manifest from {manifest_ref}: {exc}. "
                "Pass --offline-stack to use the CLI built-in fallback intentionally."
            ) from exc
        warning = f"Cannot load stack manifest from {manifest_ref}; using CLI built-in fallback: {exc}"
        return default_stack_manifest(), "builtin:fallback", [warning]


def read_text_resource(resource: str) -> str:
    parsed = urllib.parse.urlparse(resource)
    if parsed.scheme in {"http", "https", "file"}:
        with urllib.request.urlopen(resource, timeout=10) as response:
            return response.read().decode("utf-8-sig")
    return Path(resource).expanduser().read_text(encoding="utf-8-sig")


def validate_stack_manifest(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ValueError("stack manifest must be a JSON object")
    if payload.get("schemaVersion") != "foggy-stack/v1":
        raise ValueError("stack manifest schemaVersion must be foggy-stack/v1")
    components = payload.get("components")
    if not isinstance(components, dict):
        raise ValueError("stack manifest is missing components")
    skills = components.get("skills")
    if not isinstance(skills, dict):
        raise ValueError("stack manifest is missing components.skills")


def stack_skill_spec(stack_manifest: dict[str, Any], skill_name: str) -> dict[str, Any]:
    skills = stack_manifest.get("components", {}).get("skills", {})
    spec = skills.get(skill_name)
    if not isinstance(spec, dict):
        raise ValueError(f"stack manifest does not define Skill: {skill_name}")
    zip_spec = spec.get("zip")
    if not isinstance(zip_spec, dict) or not zip_spec.get("url"):
        raise ValueError(f"stack manifest Skill {skill_name} is missing zip.url")
    return spec


def parse_numeric_version(version: str) -> tuple[int, ...]:
    value = version.strip()
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:[-_][A-Za-z0-9][A-Za-z0-9._-]*)?", value)
    if match is None:
        raise ValueError(f"Unsupported version format: {version}")
    return tuple(int(part) for part in match.group(1).split("."))


def compare_numeric_versions(left: str, right: str) -> int:
    left_parts = list(parse_numeric_version(left))
    right_parts = list(parse_numeric_version(right))
    width = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (width - len(left_parts)))
    right_parts.extend([0] * (width - len(right_parts)))
    if left_parts < right_parts:
        return -1
    if left_parts > right_parts:
        return 1
    return 0


def ensure_skill_cli_compatible(stack_manifest: dict[str, Any], skill_name: str, spec: dict[str, Any]) -> None:
    min_cli_version = str(spec.get("minCliVersion", "")).strip()
    if not min_cli_version:
        return
    if compare_numeric_versions(__version__, min_cli_version) >= 0:
        return

    cli_spec = stack_manifest.get("components", {}).get("cli", {})
    recommended = cli_spec.get("recommendedVersion") or cli_spec.get("tag") or "the recommended CLI"
    install = cli_spec.get("install", {}) if isinstance(cli_spec, dict) else {}
    install_hint = install.get("windows") or install.get("posix") or cli_spec.get("releaseUrl") or DEFAULT_STACK_MANIFEST_URL
    raise ValueError(
        f"Skill {skill_name} requires foggy-runtime-cli >= {min_cli_version}; "
        f"current CLI is {__version__}. Upgrade to {recommended}: {install_hint}"
    )


def safe_release_zip_name(zip_spec: dict[str, Any], zip_url: str, skill_name: str) -> str:
    raw_name = str(zip_spec.get("file") or Path(urllib.parse.urlparse(zip_url).path).name or f"{skill_name}.zip")
    normalized = raw_name.replace("\\", "/")
    if not normalized or normalized in {".", ".."} or "/" in normalized:
        raise ValueError(f"Invalid Skill zip file name in stack manifest for {skill_name}: {raw_name}")
    return normalized


def stack_show_handler(args: argparse.Namespace) -> dict[str, Any]:
    try:
        stack_manifest, source, warnings = load_stack_manifest(args)
        return {
            "success": True,
            "engine": "local",
            "data": {
                "stackManifestSource": source,
                "stackManifestWarnings": warnings,
                "currentCliVersion": __version__,
                "stack": stack_manifest,
            },
        }
    except ValueError as exc:
        return {
            "success": False,
            "engine": "local",
            "data": {"currentCliVersion": __version__},
            "error": {
                "code": "STACK_MANIFEST_FAILED",
                "phase": "stack.show",
                "message": str(exc),
                "safeToAutoRepair": False,
            },
        }
