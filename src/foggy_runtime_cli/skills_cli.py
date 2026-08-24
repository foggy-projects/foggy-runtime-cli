from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import stack_cli

ANALYSIS_SKILL = stack_cli.ANALYSIS_SKILL
SEMANTIC_QUERY_SKILL = stack_cli.SEMANTIC_QUERY_SKILL
LEGACY_ANALYSIS_DEMO_SKILL = "foggy-ai-analysis-demo"
SALES_DROP_DEMO_RELATIVE_DIR = Path("assets") / "sales-drop-demo"
DEFAULT_STACK_MANIFEST_URL = stack_cli.DEFAULT_STACK_MANIFEST_URL
ANALYSIS_SKILL_RELEASE_VERSION = stack_cli.ANALYSIS_SKILL_RELEASE_VERSION
ANALYSIS_SKILL_RELEASE_ZIP = stack_cli.ANALYSIS_SKILL_RELEASE_ZIP
ANALYSIS_SKILL_RELEASE_ZIP_URL = stack_cli.ANALYSIS_SKILL_RELEASE_ZIP_URL
ANALYSIS_SKILL_ZIP_INSTALL_COMMAND = (
    f"foggy-runtime skills install {ANALYSIS_SKILL} "
    f"--zip {ANALYSIS_SKILL_RELEASE_ZIP} --replace"
)
ANALYSIS_SKILL_WORKSPACE_INSTALL_COMMAND = (
    f"foggy-runtime skills install {ANALYSIS_SKILL} "
    "--workspace-root <workspace-root> --replace"
)
INSTALLABLE_SKILLS = (ANALYSIS_SKILL, SEMANTIC_QUERY_SKILL)
SKILL_SUITES = {
    "foggy-analysis-suite": INSTALLABLE_SKILLS,
}
SKILL_HELP = {
    ANALYSIS_SKILL: (
        "onboarding, runtime setup, semantic modeling, "
        "and bundled sales-drop demo assets"
    ),
    SEMANTIC_QUERY_SKILL: (
        "queryModel DSL, Compose/CTE, and governed semantic query workflows"
    ),
    "foggy-analysis-suite": (
        "installs both foggy-ai-analysis and foggy-semantic-query"
    ),
}


def register_skills_parser(subparsers: Any) -> None:
    skills = subparsers.add_parser(
        "skills",
        help=(
            "Install Foggy Skills such as foggy-ai-analysis "
            "and foggy-semantic-query."
        ),
        description="Install supported Foggy Skills into ~/.agents/skills.",
    )
    skill_commands = skills.add_subparsers(dest="skills_command", required=True)
    skill_install = skill_commands.add_parser(
        "install",
        description="Install Foggy Skills into ~/.agents/skills.",
        epilog=(
            "Skills:\n"
            "  foggy-ai-analysis     Onboarding, runtime setup, semantic "
            "modeling, and sales-drop demo assets.\n"
            "  foggy-semantic-query  queryModel DSL, Compose/CTE, and governed "
            "semantic query workflows.\n"
            "  foggy-analysis-suite  Installs both Skills.\n\n"
            "Examples:\n"
            f"  {ANALYSIS_SKILL_ZIP_INSTALL_COMMAND}\n"
            f"  {ANALYSIS_SKILL_WORKSPACE_INSTALL_COMMAND}\n"
            "  foggy-runtime skills install foggy-analysis-suite --replace"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    skill_install.add_argument(
        "skill",
        choices=[*INSTALLABLE_SKILLS, *SKILL_SUITES.keys()],
        help="Skill or suite to install.",
    )
    skill_source = skill_install.add_mutually_exclusive_group()
    skill_source.add_argument(
        "--source-dir", help="Path to an unpacked Skill source directory."
    )
    skill_source.add_argument(
        "--zip", dest="zip_path", help="Path to a released Skill zip."
    )
    skill_install.add_argument(
        "--workspace-root",
        help=(
            "Workspace root used to auto-discover local Skill sources when "
            "--source-dir/--zip is omitted."
        ),
    )
    skill_install.add_argument(
        "--stack-manifest",
        help=(
            "Path or URL to a foggy-stack/v1 manifest for default release "
            f"downloads. Defaults to {DEFAULT_STACK_MANIFEST_URL}."
        ),
    )
    skill_install.add_argument(
        "--offline-stack",
        action="store_true",
        help=(
            "Use the CLI built-in fallback stack manifest for default "
            "release downloads."
        ),
    )
    skill_install.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing installation in ~/.agents/skills.",
    )
    skill_install.set_defaults(local_handler=skills_install_handler)


def skills_install_handler(args: argparse.Namespace) -> dict[str, Any]:
    requested_skill = str(args.skill)
    target_root = default_skill_install_root()
    try:
        if requested_skill in SKILL_SUITES:
            if args.source_dir or args.zip_path:
                raise ValueError(f"{requested_skill} installs multiple Skills; use --workspace-root, not --source-dir/--zip.")
            if not args.workspace_root:
                stack_manifest, stack_source, stack_warnings = stack_cli.load_stack_manifest(
                    args,
                    allow_default_fallback=False,
                )
                with tempfile.TemporaryDirectory(prefix="foggy-skill-suite-release-") as temp_dir:
                    prepared = [
                        prepare_skill_from_release(
                            skill_name,
                            stack_manifest,
                            stack_source,
                            stack_warnings,
                            Path(temp_dir) / skill_name,
                        )
                        for skill_name in SKILL_SUITES[requested_skill]
                    ]
                    installed = install_prepared_skills_atomically(prepared, target_root, args.replace)
                return {
                    "success": True,
                    "engine": "local",
                    "data": {
                        "skill": requested_skill,
                        "targetRoot": str(target_root.expanduser().resolve()),
                        "installPolicy": "agents-skills-only",
                        "sourceKind": "release-stack",
                        "stackManifestSource": stack_source,
                        "stackManifestWarnings": stack_warnings,
                        "installedSkills": installed,
                    },
                }
            workspace_root = resolve_skill_workspace_root(args.workspace_root)
            prepared = [
                {
                    "skill": skill_name,
                    "sourceDir": resolve_local_skill_source_dir(skill_name, workspace_root),
                    "metadata": {},
                }
                for skill_name in SKILL_SUITES[requested_skill]
            ]
            installed = install_prepared_skills_atomically(prepared, target_root, args.replace)
            return {
                "success": True,
                "engine": "local",
                "data": {
                    "skill": requested_skill,
                    "workspaceRoot": str(workspace_root),
                    "targetRoot": str(target_root.expanduser().resolve()),
                    "installPolicy": "agents-skills-only",
                    "installedSkills": installed,
                },
            }

        if args.source_dir:
            source_dir = Path(args.source_dir).expanduser().resolve()
            return install_skill_from_source_dir(requested_skill, source_dir, target_root, args.replace)
        if args.zip_path:
            zip_path = Path(args.zip_path).expanduser().resolve()
            with tempfile.TemporaryDirectory(prefix="foggy-skill-install-") as temp_dir:
                source_dir = extract_skill_zip(zip_path, requested_skill, Path(temp_dir))
                response = install_skill_from_source_dir(requested_skill, source_dir, target_root, args.replace)
                apply_skill_install_metadata(response["data"], explicit_skill_zip_metadata(requested_skill, zip_path))
                return response
        if not args.workspace_root:
            stack_manifest, stack_source, stack_warnings = stack_cli.load_stack_manifest(
                args, allow_default_fallback=False
            )
            return install_skill_from_release(
                requested_skill,
                target_root,
                args.replace,
                stack_manifest,
                stack_source,
                stack_warnings,
            )
        workspace_root = resolve_skill_workspace_root(args.workspace_root)
        source_dir = resolve_local_skill_source_dir(requested_skill, workspace_root)
        return install_skill_from_source_dir(requested_skill, source_dir, target_root, args.replace)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {
            "success": False,
            "engine": "local",
            "data": {
                "skill": requested_skill,
                "targetRoot": str(target_root),
            },
            "error": {
                "code": "SKILL_INSTALL_FAILED",
                "phase": "skills.install",
                "message": str(exc),
                "safeToAutoRepair": False,
            },
        }


def install_skill_from_release(
    skill_name: str,
    target_root: Path,
    replace: bool,
    stack_manifest: dict[str, Any],
    stack_source: str,
    stack_warnings: list[str],
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="foggy-skill-release-") as temp_dir:
        prepared = prepare_skill_from_release(skill_name, stack_manifest, stack_source, stack_warnings, Path(temp_dir))
        response = install_skill_from_source_dir(skill_name, prepared["sourceDir"], target_root, replace)
        response["data"].update(prepared["metadata"])
        return response


def prepare_skill_from_release(
    skill_name: str,
    stack_manifest: dict[str, Any],
    stack_source: str,
    stack_warnings: list[str],
    temp_root: Path,
) -> dict[str, Any]:
    spec = stack_cli.stack_skill_spec(stack_manifest, skill_name)
    stack_cli.ensure_skill_cli_compatible(stack_manifest, skill_name, spec)
    zip_spec = spec["zip"]
    zip_url = str(zip_spec["url"])
    zip_name = stack_cli.safe_release_zip_name(zip_spec, zip_url, skill_name)
    expected_sha256 = str(zip_spec.get("sha256", "")).strip().lower()
    temp_root.mkdir(parents=True, exist_ok=True)
    zip_path = temp_root / zip_name
    download_binary_resource(zip_url, zip_path)
    actual_sha256 = sha256_file(zip_path)
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise ValueError(
            f"Downloaded Skill zip checksum mismatch for {skill_name}: "
            f"expected {expected_sha256}, actual {actual_sha256}"
        )
    source_dir = extract_skill_zip(zip_path, skill_name, temp_root / "unpacked")
    metadata: dict[str, Any] = {
        "sourceKind": "release-zip",
        "releaseVersion": spec.get("recommendedVersion") or spec.get("version"),
        "releaseTag": spec.get("tag"),
        "releaseUrl": spec.get("releaseUrl"),
        "releaseZipUrl": zip_url,
        "releaseZipSha256": actual_sha256,
        "releaseZipSha256Verified": bool(expected_sha256),
        "stackManifestSource": stack_source,
        "stackManifestWarnings": stack_warnings,
    }
    if skill_name == ANALYSIS_SKILL:
        metadata["publicZipUrl"] = zip_url
        metadata["demoInstallCommand"] = analysis_skill_zip_install_command(
            str(spec.get("recommendedVersion") or ANALYSIS_SKILL_RELEASE_VERSION)
        )
        metadata["zipInstallCommand"] = metadata["demoInstallCommand"]
    return {
        "skill": skill_name,
        "sourceDir": source_dir,
        "metadata": metadata,
    }


def download_binary_resource(resource: str, target: Path) -> None:
    parsed = urllib.parse.urlparse(resource)
    target.parent.mkdir(parents=True, exist_ok=True)
    if parsed.scheme in {"http", "https", "file"}:
        with urllib.request.urlopen(resource, timeout=30) as response:
            with open(target, "wb") as handle:
                shutil.copyfileobj(response, handle)
        return
    shutil.copyfile(Path(resource).expanduser(), target)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def analysis_skill_zip_install_command(version: str) -> str:
    return f"foggy-runtime skills install {ANALYSIS_SKILL} --zip foggy-ai-analysis-skill-{version}.zip --replace"


def infer_skill_zip_version(skill_name: str, zip_path: Path) -> str | None:
    pattern = rf"{re.escape(skill_name)}-skill-(\d+(?:\.\d+)*(?:[-_][A-Za-z0-9][A-Za-z0-9._-]*)?)\.zip"
    match = re.fullmatch(pattern, zip_path.name)
    if match is None:
        return None
    return match.group(1)


def skill_release_zip_url(skill_name: str, version: str) -> str:
    return (
        "https://github.com/foggy-projects/foggy-ai-analysis/releases/download/"
        f"v{version}/{skill_name}-skill-{version}.zip"
    )


def explicit_skill_zip_metadata(skill_name: str, zip_path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "sourceKind": "explicit-zip",
        "explicitZipPath": str(zip_path),
    }
    version = infer_skill_zip_version(skill_name, zip_path)
    if version is None:
        if skill_name == ANALYSIS_SKILL:
            metadata["_removeKeys"] = [
                "publicZipUrl",
                "demoInstallCommand",
                "zipInstallCommand",
            ]
        return metadata
    metadata["releaseVersion"] = version
    metadata["releaseTag"] = f"v{version}"
    metadata["releaseZipUrl"] = skill_release_zip_url(skill_name, version)
    metadata["releaseZipSha256"] = sha256_file(zip_path)
    metadata["releaseZipSha256Verified"] = False
    if skill_name == ANALYSIS_SKILL:
        metadata["publicZipUrl"] = metadata["releaseZipUrl"]
        metadata["demoInstallCommand"] = analysis_skill_zip_install_command(version)
        metadata["zipInstallCommand"] = metadata["demoInstallCommand"]
    return metadata


def apply_skill_install_metadata(data: dict[str, Any], metadata: dict[str, Any]) -> None:
    remove_keys = metadata.pop("_removeKeys", [])
    for key in remove_keys:
        data.pop(str(key), None)
    data.update(metadata)


def default_skill_install_root() -> Path:
    return Path.home() / ".agents" / "skills"


def default_sales_drop_skill_candidates(repo_root: Path | None = None) -> list[Path]:
    candidates = [default_skill_install_root() / ANALYSIS_SKILL]
    roots: list[Path] = []
    if repo_root is not None:
        roots.append(repo_root)
    roots.append(Path.cwd())

    for root in roots:
        candidates.extend(
            [
                root / ".codex" / "skills" / ANALYSIS_SKILL,
                root / "foggy-ai-analysis" / "locales" / "en",
                root / "locales" / "en",
                root / ".codex" / "skills" / LEGACY_ANALYSIS_DEMO_SKILL,
            ]
        )

    resolved_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        resolved_candidates.append(resolved)
    return resolved_candidates


def resolve_sales_drop_skill_dir(explicit_skill_dir: str | None, repo_root: Path | None = None) -> tuple[Path, list[Path]]:
    if explicit_skill_dir:
        skill_dir = Path(explicit_skill_dir).expanduser().resolve()
        return skill_dir, [skill_dir]

    candidates = default_sales_drop_skill_candidates(repo_root)
    existing_skill_dir: Path | None = None
    for candidate in candidates:
        if (candidate / SALES_DROP_DEMO_RELATIVE_DIR).is_dir():
            return candidate, candidates
        if existing_skill_dir is None and (candidate / "SKILL.md").is_file():
            existing_skill_dir = candidate
    if existing_skill_dir is not None:
        return existing_skill_dir, candidates
    return candidates[0], candidates


def sales_drop_demo_dir(skill_dir: Path) -> Path:
    return skill_dir / SALES_DROP_DEMO_RELATIVE_DIR


def resolve_skill_workspace_root(workspace_root: str | None) -> Path:
    candidates: list[Path] = []
    if workspace_root:
        candidates.append(Path(workspace_root).expanduser())
    env_root = os.environ.get("FOGGY_DATA_MCP_ROOT")
    if env_root:
        candidates.append(Path(env_root).expanduser())
    cwd = Path.cwd().resolve()
    candidates.extend([cwd, *cwd.parents])

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if has_any_local_skill_source(resolved):
            return resolved
    raise ValueError("Cannot locate local Foggy Skill sources. Pass --workspace-root or --source-dir.")


def has_any_local_skill_source(workspace_root: Path) -> bool:
    return any(resolve_local_skill_source_dir(skill_name, workspace_root, required=False) is not None for skill_name in INSTALLABLE_SKILLS)


def resolve_local_skill_source_dir(skill_name: str, workspace_root: Path, required: bool = True) -> Path | None:
    if skill_name == "foggy-ai-analysis":
        candidates = [
            workspace_root / "foggy-ai-analysis" / "locales" / "en",
            workspace_root / "locales" / "en",
            workspace_root / ".codex" / "skills" / "foggy-ai-analysis",
        ]
    elif skill_name == "foggy-semantic-query":
        candidates = [
            workspace_root / ".codex" / "skills" / "foggy-semantic-query",
            workspace_root / "foggy-semantic-query",
        ]
    else:
        raise ValueError(f"Unsupported Skill: {skill_name}")

    for candidate in candidates:
        if (candidate / "SKILL.md").is_file():
            return candidate.resolve()
    if required:
        raise ValueError(f"Cannot locate local source for {skill_name} under {workspace_root}")
    return None


def install_skill_from_source_dir(
    skill_name: str,
    source_dir: Path,
    target_root: Path,
    replace: bool,
) -> dict[str, Any]:
    source_dir = validate_skill_source_for_install(skill_name, source_dir)
    target_root, target_dir = resolve_skill_install_target(skill_name, target_root)
    target_root.mkdir(parents=True, exist_ok=True)
    target_existed = target_dir.exists()
    if target_existed and not replace:
        raise ValueError(f"Skill already installed: {target_dir}. Pass --replace to overwrite it.")
    if target_existed:
        shutil.rmtree(target_dir)

    shutil.copytree(source_dir, target_dir, ignore=skill_copy_ignore())
    data = build_skill_install_data(skill_name, source_dir, target_root, target_dir, target_existed)
    return {
        "success": True,
        "engine": "local",
        "data": data,
    }


def install_prepared_skills_atomically(
    prepared_skills: list[dict[str, Any]],
    target_root: Path,
    replace: bool,
) -> list[dict[str, Any]]:
    target_root = target_root.expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    install_plan: list[dict[str, Any]] = []
    seen_targets: set[Path] = set()

    for prepared in prepared_skills:
        skill_name = str(prepared["skill"])
        source_dir = validate_skill_source_for_install(skill_name, Path(prepared["sourceDir"]))
        _, target_dir = resolve_skill_install_target(skill_name, target_root)
        if target_dir in seen_targets:
            raise ValueError(f"Duplicate Skill install target: {target_dir}")
        seen_targets.add(target_dir)
        target_existed = target_dir.exists()
        if target_existed and not replace:
            raise ValueError(f"Skill already installed: {target_dir}. Pass --replace to overwrite it.")
        install_plan.append(
            {
                "skill": skill_name,
                "sourceDir": source_dir,
                "targetDir": target_dir,
                "targetExisted": target_existed,
                "metadata": dict(prepared.get("metadata") or {}),
            }
        )

    backup_root = Path(tempfile.mkdtemp(prefix="foggy-skill-install-backup-"))
    moved_backups: list[tuple[Path, Path]] = []
    try:
        for item in install_plan:
            target_dir = item["targetDir"]
            if target_dir.exists():
                backup_dir = backup_root / target_dir.name
                shutil.move(str(target_dir), str(backup_dir))
                moved_backups.append((target_dir, backup_dir))

        for item in install_plan:
            shutil.copytree(item["sourceDir"], item["targetDir"], ignore=skill_copy_ignore())

        installed: list[dict[str, Any]] = []
        for item in install_plan:
            data = build_skill_install_data(
                item["skill"],
                item["sourceDir"],
                target_root,
                item["targetDir"],
                item["targetExisted"],
            )
            data.update(item["metadata"])
            installed.append(data)
        return installed
    except Exception:
        for item in install_plan:
            target_dir = item["targetDir"]
            if target_dir.exists():
                shutil.rmtree(target_dir)
        for target_dir, backup_dir in reversed(moved_backups):
            if backup_dir.exists():
                shutil.move(str(backup_dir), str(target_dir))
        raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def validate_skill_source_for_install(skill_name: str, source_dir: Path) -> Path:
    validate_skill_name(skill_name)
    source_dir = source_dir.expanduser().resolve()
    if not source_dir.is_dir():
        raise ValueError(f"Skill source directory does not exist: {source_dir}")
    skill_file = source_dir / "SKILL.md"
    if not skill_file.is_file():
        raise ValueError(f"Skill source is missing SKILL.md: {source_dir}")
    validate_skill_source_name(skill_file, skill_name)
    return source_dir


def resolve_skill_install_target(skill_name: str, target_root: Path) -> tuple[Path, Path]:
    validate_skill_name(skill_name)
    target_root = target_root.expanduser().resolve()
    target_dir = (target_root / skill_name).resolve()
    if target_dir.parent != target_root:
        raise ValueError(f"Skill install target escapes target root: {target_dir}")
    return target_root, target_dir


def skill_copy_ignore() -> Callable[[str, list[str]], set[str]]:
    return shutil.ignore_patterns(
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".release-venv",
        "dist",
        ".codex-tmp",
    )


def build_skill_install_data(
    skill_name: str,
    source_dir: Path,
    target_root: Path,
    target_dir: Path,
    target_existed: bool,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "skill": skill_name,
        "description": SKILL_HELP.get(skill_name),
        "sourceDir": str(source_dir),
        "targetRoot": str(target_root),
        "targetDir": str(target_dir),
        "installPolicy": "agents-skills-only",
        "excludedTargetRoots": [
            str(Path.home() / ".codex" / "skills"),
            str(Path.home() / ".claude" / "skills"),
        ],
        "replaced": target_existed,
    }
    if skill_name == ANALYSIS_SKILL:
        data["salesDropDemoDir"] = str(sales_drop_demo_dir(target_dir))
        data["demoInstallCommand"] = ANALYSIS_SKILL_ZIP_INSTALL_COMMAND
        data["publicZipUrl"] = ANALYSIS_SKILL_RELEASE_ZIP_URL
        data["zipInstallCommand"] = ANALYSIS_SKILL_ZIP_INSTALL_COMMAND
        data["workspaceInstallCommand"] = ANALYSIS_SKILL_WORKSPACE_INSTALL_COMMAND
        data["demoReplayHint"] = (
            "Use foggy-runtime demo sales-drop replay --skill-dir "
            f"{target_dir} --sqlite-path <runtime-default-sqlite-path> --use-default-datasource"
        )
    elif skill_name == SEMANTIC_QUERY_SKILL:
        data["companionSkill"] = ANALYSIS_SKILL

    return data


def validate_skill_name(skill_name: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}[a-z0-9]", skill_name):
        raise ValueError(f"Invalid Skill name: {skill_name}")


def validate_skill_source_name(skill_file: Path, expected_name: str) -> None:
    content = skill_file.read_text(encoding="utf-8-sig")
    expected = re.escape(expected_name)
    if re.search(rf"(?m)^name:\s*{expected}\s*$", content) is None:
        raise ValueError(f"SKILL.md does not declare name: {expected_name}")


def extract_skill_zip(zip_path: Path, skill_name: str, temp_root: Path) -> Path:
    if not zip_path.is_file():
        raise ValueError(f"Skill zip does not exist: {zip_path}")
    temp_root = temp_root.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
                raise ValueError(f"Unsafe Skill zip entry: {info.filename}")
            parts = [part for part in name.split("/") if part and part != "."]
            if any(part == ".." for part in parts):
                raise ValueError(f"Unsafe Skill zip entry: {info.filename}")
            target = (temp_root / name).resolve()
            if not target.is_relative_to(temp_root):
                raise ValueError(f"Unsafe Skill zip entry: {info.filename}")
        archive.extractall(temp_root)

    expected_dir = temp_root / skill_name
    if expected_dir.is_dir():
        return expected_dir.resolve()
    candidates = [path for path in temp_root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()]
    if len(candidates) == 1:
        return candidates[0].resolve()
    raise ValueError(f"Skill zip does not contain a single unpacked {skill_name} Skill directory")
