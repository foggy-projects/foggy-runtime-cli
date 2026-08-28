"""Opaque local onboarding profiles for Runtime datasource provisioning."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .client import RuntimeTransportError, path_quote


PROFILE_SCHEMA = "foggy-runtime-onboarding-profile/v1"
PUBLIC_SCHEMA = "foggy-runtime-onboarding-profile-summary/v1"
PROFILE_ID_PATTERN = re.compile(r"^fop_[a-f0-9]{32}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EMBEDDED_PASSWORD_PATTERN = re.compile(
    r"(?i)(?:password|passwd|pwd)\s*=|//[^/@:]+:[^/@]+@"
)


class ProfileError(ValueError):
    """A safe, user-facing profile validation or storage error."""


def register_profile_parser(
    subparsers: Any,
    parser_class: type[argparse.ArgumentParser],
) -> None:
    profiles = subparsers.add_parser(
        "profiles",
        help="Manage opaque local onboarding profiles without printing connection material.",
    )
    commands = profiles.add_subparsers(
        dest="profiles_command", required=True, parser_class=parser_class
    )

    create = commands.add_parser("create")
    create.add_argument("--input", required=True, help="Private connection JSON file, or '-' for stdin.")
    create.add_argument("--replace-profile", help="Replace this profile ID after revision approval.")
    create.add_argument("--approve-revision", help="Current revision required with --replace-profile.")
    create.set_defaults(local_handler=create_handler)

    listing = commands.add_parser("list")
    listing.set_defaults(local_handler=list_handler)

    show = commands.add_parser("show")
    show.add_argument("profile_id")
    show.set_defaults(local_handler=show_handler)

    apply_profile = commands.add_parser("apply")
    apply_profile.add_argument("profile_id")
    apply_profile.add_argument("--approve-revision", required=True)
    apply_profile.add_argument("--approve-configure", action="store_true")
    apply_profile.add_argument("--approve-bind", action="store_true")
    apply_profile.add_argument("--replace", action="store_true")
    apply_profile.set_defaults(runtime_handler=apply_handler)

    remove = commands.add_parser("remove")
    remove.add_argument("profile_id")
    remove.add_argument("--approve-revision", required=True)
    remove.set_defaults(local_handler=remove_handler)


def default_profile_store() -> Path:
    override = os.environ.get("FOGGY_RUNTIME_PROFILE_STORE")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Foggy" / "runtime-cli" / "profiles"
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "foggy-runtime" / "profiles"
    return Path.home() / ".config" / "foggy-runtime" / "profiles"


def create_handler(args: argparse.Namespace) -> dict[str, Any]:
    try:
        payload = _read_input(args.input, args._stdin)
        connection = _validate_connection(payload)
        store = _prepare_store()
        now = _utc_now()
        profile_id = args.replace_profile or f"fop_{secrets.token_hex(16)}"
        _validate_profile_id(profile_id)
        target = _profile_path(store, profile_id)
        created_at = now
        if args.replace_profile:
            current = _load_private(profile_id)
            if args.approve_revision != current["revision"]:
                raise ProfileError("Profile revision approval does not match the stored profile")
            created_at = current["createdAt"]
        elif target.exists():
            raise ProfileError("Generated profile ID already exists; retry creation")

        profile = {
            "schemaVersion": PROFILE_SCHEMA,
            "profileId": profile_id,
            "revision": _connection_revision(connection),
            "createdAt": created_at,
            "updatedAt": now,
            "connection": connection,
        }
        _atomic_write_private(target, profile)
        return _success(_public_summary(profile))
    except (OSError, json.JSONDecodeError, ProfileError) as exc:
        return _error("PROFILE_CREATE_FAILED", "profiles.create", str(exc))


def list_handler(_args: argparse.Namespace) -> dict[str, Any]:
    try:
        items = []
        for path in sorted(_prepare_store().glob("fop_*.json")):
            try:
                items.append(_public_summary(_load_path(path)))
            except (OSError, json.JSONDecodeError, ProfileError):
                continue
        return _success({"schemaVersion": PUBLIC_SCHEMA, "profiles": items})
    except OSError as exc:
        return _error("PROFILE_LIST_FAILED", "profiles.list", str(exc))


def show_handler(args: argparse.Namespace) -> dict[str, Any]:
    try:
        return _success(_public_summary(_load_private(args.profile_id)))
    except (OSError, json.JSONDecodeError, ProfileError) as exc:
        return _error("PROFILE_NOT_FOUND", "profiles.show", str(exc))


def remove_handler(args: argparse.Namespace) -> dict[str, Any]:
    try:
        profile = _load_private(args.profile_id)
        if args.approve_revision != profile["revision"]:
            raise ProfileError("Profile revision approval does not match the stored profile")
        _profile_path(_prepare_store(), args.profile_id).unlink()
        return _success(
            {"schemaVersion": PUBLIC_SCHEMA, "profileId": args.profile_id, "removed": True}
        )
    except (OSError, json.JSONDecodeError, ProfileError) as exc:
        return _error("PROFILE_REMOVE_FAILED", "profiles.remove", str(exc))


def apply_handler(
    args: argparse.Namespace,
    client: Any,
    _base_url: str,
) -> tuple[dict[str, Any], int]:
    try:
        profile = _load_private(args.profile_id)
        if args.approve_revision != profile["revision"]:
            raise ProfileError("Profile revision approval does not match the stored profile")
        summary = _public_summary(profile)
        if not args.approve_configure and not args.approve_bind:
            summary["dryRun"] = True
            summary["next"] = (
                "rerun with --approve-configure or --approve-bind for the reviewed revision"
            )
            return _success(summary), 0

        connection = profile["connection"]
        capabilities = client.request("GET", "/api/v1/capabilities", None)
        required = ["datasources.test"]
        if args.approve_configure:
            required.insert(0, "datasources.add")
        if args.approve_bind:
            required.append("datasources.bind")
        unsupported = _unsupported_capabilities(capabilities, required)
        if unsupported:
            return (
                _error(
                    "UNSUPPORTED_OPERATION",
                    "profiles.apply",
                    "Connected Runtime does not support: " + ", ".join(unsupported),
                ),
                3,
            )

        if args.approve_configure:
            body: dict[str, Any] = {
                "name": connection["name"],
                "type": connection["type"],
                "jdbcUrl": connection["jdbcUrl"],
                "replace": bool(args.replace),
                "enabled": True,
            }
            if connection.get("username"):
                body["username"] = connection["username"]
            if connection.get("passwordEnv"):
                body["passwordRef"] = f"env:{connection['passwordEnv']}"

            failed = _failed_step(
                client.request("POST", "/api/v1/datasources", body),
                "datasource configure",
            )
            if failed:
                return failed, 2
        failed = _failed_step(
            client.request(
                "POST", f"/api/v1/datasources/{path_quote(connection['name'])}/test", None
            ),
            "datasource test",
        )
        if failed:
            return failed, 2

        result = {
            **summary,
            "dryRun": False,
            "configured": bool(args.approve_configure),
            "tested": True,
            "bound": False,
        }
        if args.approve_bind:
            failed = _failed_step(
                client.request(
                    "PUT",
                    f"/api/v1/namespaces/{path_quote(connection['namespace'])}/datasource",
                    {"namespace": connection["namespace"], "dataSource": connection["name"]},
                ),
                "namespace bind",
            )
            if failed:
                return failed, 2
            result["bound"] = True
        return _success(result), 0
    except RuntimeTransportError as exc:
        return _error("TRANSPORT_ERROR", "profiles.apply", str(exc)), 4
    except (OSError, json.JSONDecodeError, ProfileError) as exc:
        return _error("PROFILE_APPLY_FAILED", "profiles.apply", str(exc)), 2


def _read_input(source: str, stdin: TextIO) -> dict[str, Any]:
    text = stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8-sig")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ProfileError("Profile input must be a JSON object")
    return payload


def _validate_connection(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"schemaVersion", "name", "type", "jdbcUrl", "username", "passwordEnv", "namespace"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ProfileError("Unsupported profile fields: " + ", ".join(unknown))
    for name in ("name", "type", "jdbcUrl", "namespace"):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ProfileError(f"{name} must be a non-empty string")
    jdbc_url = payload["jdbcUrl"].strip()
    if EMBEDDED_PASSWORD_PATTERN.search(jdbc_url):
        raise ProfileError("Do not embed passwords in jdbcUrl; use passwordEnv")
    password_env = payload.get("passwordEnv")
    if password_env is not None and (
        not isinstance(password_env, str) or not ENV_NAME_PATTERN.fullmatch(password_env)
    ):
        raise ProfileError("passwordEnv must be an environment variable name")
    username = payload.get("username")
    if username is not None and not isinstance(username, str):
        raise ProfileError("username must be a string")
    return {
        "name": payload["name"].strip(),
        "type": payload["type"].strip().lower(),
        "jdbcUrl": jdbc_url,
        "username": username.strip() if isinstance(username, str) and username.strip() else None,
        "passwordEnv": password_env,
        "namespace": payload["namespace"].strip(),
    }


def _prepare_store() -> Path:
    configured = default_profile_store().expanduser()
    if configured.exists() and configured.is_symlink():
        raise ProfileError("Profile store must not be a symbolic link")
    store = configured.resolve()
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_permissions(store, directory=True)
    return store


def _profile_path(store: Path, profile_id: str) -> Path:
    _validate_profile_id(profile_id)
    candidate = store / f"{profile_id}.json"
    if candidate.exists() and candidate.is_symlink():
        raise ProfileError("Profile file must not be a symbolic link")
    target = candidate.resolve()
    if target.parent != store:
        raise ProfileError("Invalid profile path")
    return target


def _load_private(profile_id: str) -> dict[str, Any]:
    return _load_path(_profile_path(_prepare_store(), profile_id))


def _load_path(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ProfileError("Profile file must not be a symbolic link")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schemaVersion") != PROFILE_SCHEMA:
        raise ProfileError("Unsupported or malformed stored profile")
    if payload.get("revision") != _connection_revision(payload.get("connection")):
        raise ProfileError("Stored profile revision does not match its connection material")
    return payload


def _atomic_write_private(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".profile-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_permissions(temporary, directory=False)
        os.replace(temporary, path)
        _restrict_permissions(path, directory=False)
    finally:
        if temporary.exists():
            temporary.unlink()


def _restrict_permissions(path: Path, directory: bool) -> None:
    mode = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if directory else 0)
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise ProfileError(f"Cannot restrict profile store permissions: {exc}") from exc


def _connection_revision(connection: Any) -> str:
    if not isinstance(connection, dict):
        raise ProfileError("Stored connection must be an object")
    encoded = json.dumps(connection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _public_summary(profile: dict[str, Any]) -> dict[str, Any]:
    connection = profile["connection"]
    return {
        "schemaVersion": PUBLIC_SCHEMA,
        "profileId": profile["profileId"],
        "revision": profile["revision"],
        "createdAt": profile["createdAt"],
        "updatedAt": profile["updatedAt"],
        "datasource": {
            "name": connection["name"],
            "type": connection["type"],
            "usernamePresent": bool(connection.get("username")),
            "passwordReferencePresent": bool(connection.get("passwordEnv")),
        },
        "namespace": connection["namespace"],
        "approval": {"configureRequired": True, "bindRequired": True},
    }


def _unsupported_capabilities(response: dict[str, Any], required: list[str]) -> list[str]:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    capabilities = data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}
    return [name for name in required if capabilities.get(name) != "supported"]


def _failed_step(response: dict[str, Any], label: str) -> dict[str, Any] | None:
    if response.get("success") is True:
        return None
    error = response.get("error") if isinstance(response.get("error"), dict) else {}
    return _error(
        str(error.get("code") or "RUNTIME_OPERATION_FAILED"),
        str(error.get("phase") or "profiles.apply"),
        f"Runtime {label} failed; inspect Runtime diagnostics",
    )


def _validate_profile_id(profile_id: Any) -> None:
    if not isinstance(profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise ProfileError("Invalid opaque profile ID")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "engine": "local", "data": data}


def _error(code: str, phase: str, message: str) -> dict[str, Any]:
    return {
        "success": False,
        "engine": "local",
        "data": None,
        "error": {
            "code": code,
            "phase": phase,
            "message": message,
            "safeToAutoRepair": False,
        },
    }
