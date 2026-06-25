from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from .client import RuntimeApiClient, RuntimeTransportError, path_quote

EXIT_OK = 0
EXIT_CLI_ERROR = 1
EXIT_API_ERROR = 2
EXIT_UNSUPPORTED = 3
EXIT_TRANSPORT_ERROR = 4

DEFAULT_BASE_URL = "http://127.0.0.1:8080"


def console_main() -> None:
    raise SystemExit(main())


def main(
    argv: list[str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
    client_factory: Callable[..., Any] = RuntimeApiClient,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    stdin = stdin or sys.stdin

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.namespace is None and getattr(args, "default_namespace", None):
        args.namespace = args.default_namespace

    local_handler = getattr(args, "local_handler", None)
    if local_handler is not None:
        response = local_handler(args)
        render_response(response, args.output, stdout)
        return exit_code_for_response(response)

    runtime_handler = getattr(args, "runtime_handler", None)
    if runtime_handler is not None:
        base_url = resolve_base_url(args)
        client = client_factory(base_url, args.namespace, args.timeout, resolve_auth_code(args))
        response, exit_code = runtime_handler(args, client, base_url)
        render_response(response, args.output, stdout)
        return exit_code

    body = build_body(args, stdin, stderr)
    if body is _BODY_ERROR:
        return EXIT_CLI_ERROR

    base_url = resolve_base_url(args)
    client = client_factory(base_url, args.namespace, args.timeout, resolve_auth_code(args))
    required_capabilities = required_capabilities_for(args)
    if required_capabilities:
        try:
            capability_response = client.request("GET", "/api/v1/capabilities", None)
        except RuntimeTransportError as exc:
            print(f"transport error: {exc}", file=stderr)
            return EXIT_TRANSPORT_ERROR
        unsupported_response = unsupported_capability_response(capability_response, required_capabilities)
        if unsupported_response is not None:
            render_response(unsupported_response, args.output, stdout)
            return exit_code_for_response(unsupported_response)

    try:
        response = client.request(args.method, args.path, body)
    except RuntimeTransportError as exc:
        print(f"transport error: {exc}", file=stderr)
        return EXIT_TRANSPORT_ERROR

    response_handler = getattr(args, "response_handler", None)
    if response_handler is not None and response.get("success") is True:
        try:
            response = response_handler(args, response)
        except (OSError, ValueError) as exc:
            print(f"input error: {exc}", file=stderr)
            return EXIT_CLI_ERROR

    render_response(response, args.output, stdout)
    return exit_code_for_response(response)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="foggy-runtime")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Foggy Runtime API base URL. Overrides FOGGY_RUNTIME_API_URL.",
    )
    parser.add_argument("--namespace", default=os.environ.get("FOGGY_NAMESPACE"), help="Namespace sent as X-NS.")
    parser.add_argument("--output", choices=["json", "pretty"], default="json", help="Output format.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds.")
    parser.add_argument(
        "--auth-code",
        default=None,
        help="Runtime API auth code. Overrides FOGGY_RUNTIME_API_AUTH_CODE and is sent as X-Foggy-Runtime-Code.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities = subparsers.add_parser("capabilities")
    capabilities.set_defaults(method="GET", path="/api/v1/capabilities", body_builder=no_body)

    wait_ready = subparsers.add_parser("wait-ready")
    wait_ready.add_argument("--timeout-seconds", type=float, default=90.0)
    wait_ready.add_argument("--interval-seconds", type=float, default=2.0)
    wait_ready.set_defaults(runtime_handler=wait_ready_handler)

    bundles = subparsers.add_parser("bundles")
    bundle_commands = bundles.add_subparsers(dest="bundles_command", required=True)

    bundle_list = bundle_commands.add_parser("list")
    bundle_list.set_defaults(
        method="GET",
        path="/api/v1/bundles",
        body_builder=no_body,
        required_capabilities=["bundles.list"],
    )

    bundle_add = bundle_commands.add_parser("add")
    bundle_add.add_argument("--name", required=True)
    bundle_add.add_argument("--path", dest="bundle_path", required=True)
    bundle_add.add_argument("--watch", action="store_true")
    bundle_add.add_argument("--replace", action="store_true")
    bundle_add.add_argument("--validate", action="store_true")
    bundle_add.add_argument("--refresh", action="store_true")
    bundle_add.add_argument("--disabled", dest="enabled", action="store_false", default=True)
    bundle_add.set_defaults(
        method="POST",
        path="/api/v1/bundles",
        body_builder=bundle_add_body,
        required_capabilities=["bundles.add"],
    )

    bundle_update = bundle_commands.add_parser("update")
    bundle_update.add_argument("bundle")
    bundle_update.add_argument("--path", dest="bundle_path", required=True)
    bundle_update.add_argument("--watch", action="store_true")
    bundle_update.add_argument("--validate", action="store_true")
    bundle_update.add_argument("--refresh", action="store_true")
    bundle_update.add_argument("--disabled", dest="enabled", action="store_false", default=True)
    bundle_update.set_defaults(
        method="PUT",
        body_builder=bundle_update_body,
        required_capabilities=["bundles.update"],
    )

    bundle_remove = bundle_commands.add_parser("remove")
    bundle_remove.add_argument("bundle")
    bundle_remove.add_argument("--refresh", action="store_true")
    bundle_remove.set_defaults(
        method="DELETE",
        body_builder=bundle_remove_body,
        required_capabilities=["bundles.remove"],
    )

    datasources = subparsers.add_parser("datasources")
    datasource_commands = datasources.add_subparsers(dest="datasources_command", required=True)

    datasource_list = datasource_commands.add_parser("list")
    datasource_list.set_defaults(
        method="GET",
        path="/api/v1/datasources",
        body_builder=no_body,
        required_capabilities=["datasources.list"],
    )

    datasource_add = datasource_commands.add_parser("add")
    datasource_add.add_argument("--name", required=True)
    datasource_add.add_argument("--type", default="sqlite", choices=["sqlite"])
    datasource_add.add_argument("--jdbc-url", required=True)
    datasource_add.add_argument("--username")
    datasource_add.add_argument("--password-ref")
    datasource_add.add_argument("--replace", action="store_true")
    datasource_add.add_argument("--disabled", dest="enabled", action="store_false", default=True)
    datasource_add.set_defaults(
        method="POST",
        path="/api/v1/datasources",
        body_builder=datasource_add_body,
        required_capabilities=["datasources.add"],
    )

    datasource_update = datasource_commands.add_parser("update")
    datasource_update.add_argument("datasource")
    datasource_update.add_argument("--type", default="sqlite", choices=["sqlite"])
    datasource_update.add_argument("--jdbc-url", required=True)
    datasource_update.add_argument("--username")
    datasource_update.add_argument("--password-ref")
    datasource_update.add_argument("--disabled", dest="enabled", action="store_false", default=True)
    datasource_update.set_defaults(
        method="PUT",
        body_builder=datasource_update_body,
        required_capabilities=["datasources.update"],
    )

    datasource_remove = datasource_commands.add_parser("remove")
    datasource_remove.add_argument("datasource")
    datasource_remove.set_defaults(
        method="DELETE",
        body_builder=no_body,
        required_capabilities=["datasources.remove"],
    )

    datasource_test = datasource_commands.add_parser("test")
    datasource_test.add_argument("datasource")
    datasource_test.set_defaults(
        method="POST",
        body_builder=no_body,
        required_capabilities=["datasources.test"],
    )

    datasource_bind = datasource_commands.add_parser("bind")
    datasource_bind.add_argument("--namespace", dest="bind_namespace", required=True)
    datasource_bind.add_argument("--data-source", required=True)
    datasource_bind.set_defaults(
        method="PUT",
        body_builder=datasource_bind_body,
        required_capabilities=["datasources.bind"],
    )

    resources = subparsers.add_parser("resources")
    resource_commands = resources.add_subparsers(dest="resources_command", required=True)

    resource_pull = resource_commands.add_parser("pull")
    resource_pull.add_argument("--bundle", required=True)
    resource_pull.add_argument("--out", required=True, help="Local directory to write exported resources into.")
    resource_pull.add_argument("--path", action="append", dest="resource_paths", default=None)
    resource_pull.set_defaults(
        method="POST",
        path="/api/v1/resources/export",
        body_builder=resource_export_body,
        response_handler=resource_pull_response,
        required_capabilities=["resources.export"],
    )

    resource_save = resource_commands.add_parser("save")
    resource_save.add_argument("--bundle", required=True)
    resource_save.add_argument("--dir", dest="resource_dir", required=True)
    resource_save.add_argument("--validate", action="store_true")
    resource_save.add_argument("--refresh", action="store_true")
    resource_save.set_defaults(
        method="POST",
        path="/api/v1/resources/save",
        body_builder=resource_save_body,
        required_capabilities=["resources.save"],
    )

    models = subparsers.add_parser("models")
    model_commands = models.add_subparsers(dest="models_command", required=True)

    model_list = model_commands.add_parser("list")
    model_list.set_defaults(method="GET", path="/api/v1/models", body_builder=no_body)

    describe = model_commands.add_parser("describe")
    describe.add_argument("model")
    describe.add_argument("--format", default=None)
    describe.add_argument("--field", action="append", dest="fields", default=None)
    describe.add_argument("--level", action="append", dest="levels", type=int, default=None)
    describe.add_argument("--include-examples", action="store_true")
    describe.set_defaults(method="POST", body_builder=describe_body)

    refresh = model_commands.add_parser("refresh")
    refresh.add_argument("--model", action="append", dest="models", default=None)
    refresh.set_defaults(method="POST", path="/api/v1/models/refresh", body_builder=refresh_body)

    validate = model_commands.add_parser("validate")
    validate.add_argument("--models-dir", required=True)
    validate.add_argument("--watch", action="store_true")
    validate.add_argument("--clear-existing", dest="clear_existing", action="store_true", default=True)
    validate.add_argument("--no-clear-existing", dest="clear_existing", action="store_false")
    validate.add_argument("--include-stack-trace", action="store_true")
    validate.set_defaults(method="POST", path="/api/v1/models/validate", body_builder=model_validate_body)

    query = subparsers.add_parser("query")
    query_commands = query.add_subparsers(dest="query_command", required=True)

    query_validate = query_commands.add_parser("validate")
    query_validate.add_argument("model")
    query_validate.add_argument("--payload", required=True)
    query_validate.set_defaults(method="POST", body_builder=query_payload_body, query_action="validate")

    query_execute = query_commands.add_parser("execute")
    query_execute.add_argument("model")
    query_execute.add_argument("--payload", required=True)
    query_execute.set_defaults(method="POST", body_builder=query_payload_body, query_action="execute")

    compose = subparsers.add_parser("compose")
    compose_commands = compose.add_subparsers(dest="compose_command", required=True)

    compose_validate = compose_commands.add_parser("validate")
    add_script_request_arguments(compose_validate)
    compose_validate.set_defaults(
        method="POST",
        path="/api/v1/compose/validate",
        body_builder=compose_script_body,
        required_capabilities=["compose.validate"],
    )

    compose_preview = compose_commands.add_parser("preview")
    add_script_request_arguments(compose_preview)
    compose_preview.set_defaults(
        method="POST",
        path="/api/v1/compose/preview",
        body_builder=compose_script_body,
        required_capabilities=["compose.preview"],
    )

    compose_execute = compose_commands.add_parser("execute")
    add_script_request_arguments(compose_execute)
    compose_execute.set_defaults(
        method="POST",
        path="/api/v1/compose/execute",
        body_builder=compose_script_body,
        required_capabilities=["compose.execute"],
    )

    fsscript = subparsers.add_parser("fsscript")
    fsscript_commands = fsscript.add_subparsers(dest="fsscript_command", required=True)

    fsscript_run = fsscript_commands.add_parser("run")
    add_script_request_arguments(fsscript_run)
    fsscript_run.add_argument(
        "--enable-cte-bridge",
        action="store_true",
        help="Request host-injected foggy.cte.* access when the runtime supports it.",
    )
    fsscript_run.set_defaults(
        method="POST",
        path="/api/v1/fsscript/execute",
        body_builder=fsscript_body,
        required_capabilities=["fsscript.execute"],
    )

    tables = subparsers.add_parser("tables")
    table_commands = tables.add_subparsers(dest="tables_command", required=True)

    table_list = table_commands.add_parser("list")
    table_list.add_argument("--schema")
    table_list.add_argument("--data-source")
    table_list.add_argument("--pattern")
    table_list.add_argument("--no-views", dest="include_views", action="store_false", default=True)
    table_list.set_defaults(
        method="POST",
        path="/api/v1/tables/list",
        body_builder=table_list_body,
        required_capabilities=["tables.list"],
    )

    inspect = table_commands.add_parser("inspect")
    inspect.add_argument("--table", required=True)
    inspect.add_argument("--schema")
    inspect.add_argument("--data-source")
    inspect.add_argument("--include-indexes", action="store_true")
    inspect.add_argument("--include-foreign-keys", action="store_true")
    inspect.set_defaults(method="POST", path="/api/v1/tables/inspect", body_builder=table_inspect_body)

    sql = subparsers.add_parser("sql")
    sql_commands = sql.add_subparsers(dest="sql_command", required=True)

    sql_query = sql_commands.add_parser("query")
    sql_source = sql_query.add_mutually_exclusive_group(required=True)
    sql_source.add_argument("--sql")
    sql_source.add_argument("--file", dest="sql_file", help="Path to a SQL file, or '-' for stdin.")
    sql_query.add_argument("--data-source")
    sql_query.add_argument("--max-rows", type=int)
    sql_query.add_argument("--timeout-seconds", type=int)
    sql_query.set_defaults(
        method="POST",
        path="/api/v1/sql/query",
        body_builder=sql_query_body,
        required_capabilities=["sql.query"],
    )

    demo = subparsers.add_parser("demo")
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)

    sales_drop = demo_commands.add_parser("sales-drop")
    sales_drop_commands = sales_drop.add_subparsers(dest="sales_drop_command", required=True)

    sales_drop_plan = sales_drop_commands.add_parser("plan")
    sales_drop_plan.add_argument(
        "--repo-root",
        default=os.getcwd(),
        help="Workspace root containing the demo skill.",
    )
    sales_drop_plan.add_argument(
        "--skill-dir",
        help="Path to an unpacked foggy-ai-analysis-demo skill directory. "
        "Defaults to <repo-root>/.codex/skills/foggy-ai-analysis-demo.",
    )
    sales_drop_plan.add_argument(
        "--port",
        type=int,
        default=18066,
        help="Local runtime port used in generated commands.",
    )
    sales_drop_plan.add_argument("--sqlite-path", help="SQLite database path used in generated commands.")
    sales_drop_plan.add_argument("--launcher-jar", help="Foggy MCP launcher jar path used in generated commands.")
    sales_drop_plan.add_argument("--models-dir", help="Sales-drop TM/QM model directory.")
    sales_drop_plan.add_argument("--query-payload", help="Sales-drop query payload path.")
    sales_drop_plan.set_defaults(local_handler=demo_sales_drop_plan)

    sales_drop_replay = sales_drop_commands.add_parser("replay")
    sales_drop_replay.add_argument(
        "--skill-dir",
        required=True,
        help="Path to an unpacked foggy-ai-analysis-demo skill directory.",
    )
    sales_drop_replay.add_argument(
        "--evidence-dir",
        help="Directory where replay evidence will be written. Defaults to .foggy-demo/sales-drop-replay-<stamp>.",
    )
    sales_drop_replay.add_argument("--sqlite-path", help="SQLite database path to seed and register.")
    sales_drop_replay.add_argument("--models-dir", help="Sales-drop TM/QM model directory.")
    sales_drop_replay.add_argument("--query-payload", help="Basic sales-drop query payload path.")
    sales_drop_replay.add_argument("--question-bank", help="Question-bank JSON path.")
    sales_drop_replay.add_argument("--data-source-name", default="sales-drop-sqlite")
    sales_drop_replay.add_argument(
        "--use-default-datasource",
        action="store_true",
        help=(
            "Seed --sqlite-path and use the runtime default datasource instead of registering a new datasource. "
            "Use this with current Java runtimes where model validation still reads the default datasource."
        ),
    )
    sales_drop_replay.add_argument("--bundle-name", default="sales-drop-models")
    sales_drop_replay.add_argument("--ready-timeout-seconds", type=float, default=120.0)
    sales_drop_replay.add_argument("--ready-interval-seconds", type=float, default=2.0)
    sales_drop_replay.add_argument(
        "--skip-question-bank",
        action="store_true",
        help="Run only the core sales-drop smoke sequence.",
    )
    sales_drop_replay.set_defaults(
        runtime_handler=demo_sales_drop_replay,
        default_namespace="salesdrop",
    )

    return parser


_BODY_ERROR = object()


def add_script_request_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--script", help="Path to script file, or '-' for stdin.")
    source.add_argument("--script-text", help="Inline script source.")
    parser.add_argument("--params", help="Path to JSON object params file, or '-' for stdin.")
    parser.add_argument("--options", help="Path to JSON object options file, or '-' for stdin.")


def resolve_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url
    generic = os.environ.get("FOGGY_RUNTIME_API_URL")
    if generic:
        return generic
    return DEFAULT_BASE_URL


def resolve_auth_code(args: argparse.Namespace) -> str | None:
    if args.auth_code is not None:
        return args.auth_code
    env_auth_code = os.environ.get("FOGGY_RUNTIME_API_AUTH_CODE")
    return env_auth_code if env_auth_code else None


def wait_ready_handler(
    args: argparse.Namespace,
    client: Any,
    base_url: str,
) -> tuple[dict[str, Any], int]:
    timeout_seconds = max(float(args.timeout_seconds), 0.0)
    interval_seconds = max(float(args.interval_seconds), 0.0)
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    attempts: list[dict[str, Any]] = []
    last_error: dict[str, Any] | None = None
    attempt_number = 0

    while True:
        attempt_number += 1
        try:
            response = client.request("GET", "/api/v1/capabilities", None)
        except RuntimeTransportError as exc:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            last_error = {"code": "TRANSPORT_ERROR", "message": str(exc)}
            attempts.append(
                {
                    "attempt": attempt_number,
                    "elapsedMs": elapsed_ms,
                    "result": "transport-error",
                    "message": str(exc),
                }
            )
        else:
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            if response.get("success") is True:
                data = response.get("data") if isinstance(response.get("data"), dict) else {}
                attempts.append(
                    {
                        "attempt": attempt_number,
                        "elapsedMs": elapsed_ms,
                        "result": "passed",
                        "engine": response.get("engine", "unknown"),
                    }
                )
                return (
                    {
                        "success": True,
                        "engine": response.get("engine", "unknown"),
                        "runtimeApiVersion": response.get("runtimeApiVersion"),
                        "data": {
                            "ready": True,
                            "baseUrl": base_url,
                            "namespace": args.namespace,
                            "attemptCount": attempt_number,
                            "elapsedMs": elapsed_ms,
                            "attempts": attempts,
                            "schemaVersion": data.get("schemaVersion"),
                            "securityMode": data.get("securityMode"),
                            "capabilities": data.get("capabilities", {}),
                        },
                        "diagnostics": response.get("diagnostics", {"attributes": {}}),
                    },
                    EXIT_OK,
                )

            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            last_error = {
                "code": error.get("code", "API_ERROR"),
                "message": error.get("message", "Runtime API readiness check failed."),
            }
            attempts.append(
                {
                    "attempt": attempt_number,
                    "elapsedMs": elapsed_ms,
                    "result": "api-error",
                    "code": last_error["code"],
                    "message": last_error["message"],
                }
            )

        if time.monotonic() >= deadline:
            break

        sleep_seconds = min(interval_seconds, max(deadline - time.monotonic(), 0.0))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return (
        {
            "success": False,
            "engine": "unknown",
            "runtimeApiVersion": None,
            "data": {
                "ready": False,
                "baseUrl": base_url,
                "namespace": args.namespace,
                "attemptCount": attempt_number,
                "elapsedMs": elapsed_ms,
                "attempts": attempts,
                "lastError": last_error,
            },
            "error": {
                "code": "RUNTIME_NOT_READY",
                "phase": "wait-ready",
                "message": f"Runtime did not become ready within {timeout_seconds:g} seconds.",
                "safeToAutoRepair": False,
            },
            "diagnostics": {"attributes": {"attempts": attempts}},
        },
        EXIT_TRANSPORT_ERROR,
    )


def build_body(args: argparse.Namespace, stdin: TextIO, stderr: TextIO) -> dict[str, Any] | None | object:
    if hasattr(args, "model") and not hasattr(args, "path"):
        if getattr(args, "query_action", None):
            args.path = f"/api/v1/query/{path_quote(args.model)}/{args.query_action}"
        elif getattr(args, "models_command", None) == "describe":
            args.path = f"/api/v1/models/{path_quote(args.model)}/describe"
    if getattr(args, "bundles_command", None) in {"update", "remove"}:
        args.path = f"/api/v1/bundles/{path_quote(args.bundle)}"
    if getattr(args, "datasources_command", None) in {"update", "remove"}:
        args.path = f"/api/v1/datasources/{path_quote(args.datasource)}"
    if getattr(args, "datasources_command", None) == "test":
        args.path = f"/api/v1/datasources/{path_quote(args.datasource)}/test"
    if getattr(args, "datasources_command", None) == "bind":
        args.path = f"/api/v1/namespaces/{path_quote(args.bind_namespace)}/datasource"
    try:
        return args.body_builder(args, stdin)
    except (OSError, ValueError) as exc:
        print(f"input error: {exc}", file=stderr)
        return _BODY_ERROR


def no_body(_args: argparse.Namespace, _stdin: TextIO) -> None:
    return None


def bundle_add_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": args.name,
        "path": args.bundle_path,
        "watch": args.watch,
        "replace": args.replace,
        "validate": args.validate,
        "refresh": args.refresh,
        "enabled": args.enabled,
    }
    if args.namespace:
        body["namespace"] = args.namespace
    return body


def bundle_update_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": args.bundle,
        "path": args.bundle_path,
        "watch": args.watch,
        "replace": True,
        "validate": args.validate,
        "refresh": args.refresh,
        "enabled": args.enabled,
    }
    if args.namespace:
        body["namespace"] = args.namespace
    return body


def bundle_remove_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any] | None:
    if not args.refresh and not args.namespace:
        return None
    body: dict[str, Any] = {"refresh": args.refresh}
    if args.namespace:
        body["namespace"] = args.namespace
    return body


def datasource_add_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": args.name,
        "type": args.type,
        "jdbcUrl": args.jdbc_url,
        "replace": args.replace,
        "enabled": args.enabled,
    }
    if args.username:
        body["username"] = args.username
    if args.password_ref:
        body["passwordRef"] = args.password_ref
    return body


def datasource_update_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": args.datasource,
        "type": args.type,
        "jdbcUrl": args.jdbc_url,
        "replace": True,
        "enabled": args.enabled,
    }
    if args.username:
        body["username"] = args.username
    if args.password_ref:
        body["passwordRef"] = args.password_ref
    return body


def datasource_bind_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    return {
        "namespace": args.bind_namespace,
        "dataSource": args.data_source,
    }


def resource_export_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {
        "bundle": args.bundle,
        "includeContent": True,
    }
    if args.namespace:
        body["namespace"] = args.namespace
    if args.resource_paths:
        body["paths"] = args.resource_paths
    return body


def resource_save_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    root = Path(args.resource_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"resource directory does not exist: {root}")
    files: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if not is_semantic_resource_path(relative):
            continue
        files.append({"path": relative, "content": path.read_text(encoding="utf-8")})
    if not files:
        raise ValueError(f"no semantic resources found under {root}")
    body: dict[str, Any] = {
        "bundle": args.bundle,
        "files": files,
        "validate": args.validate,
        "refresh": args.refresh,
    }
    if args.namespace:
        body["namespace"] = args.namespace
    return body


def resource_pull_response(args: argparse.Namespace, response: dict[str, Any]) -> dict[str, Any]:
    out_dir = Path(args.out).resolve()
    data = response.get("data")
    resources = data.get("resources") if isinstance(data, dict) else None
    if not isinstance(resources, list):
        raise ValueError("resources export response is missing data.resources")
    written_files: list[str] = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        relative = str(resource.get("path") or "")
        content = resource.get("content")
        if not relative or content is None:
            continue
        target = safe_resource_output_path(out_dir, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        written_files.append(str(target))
    if isinstance(data, dict):
        data["localOutputDir"] = str(out_dir)
        data["writtenFiles"] = written_files
    return response


def describe_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if args.namespace:
        body["namespace"] = args.namespace
    if args.format:
        body["format"] = args.format
    if args.fields:
        body["fields"] = args.fields
    if args.levels:
        body["levels"] = args.levels
    if args.include_examples:
        body["includeExamples"] = True
    return body


def refresh_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if args.namespace:
        body["namespace"] = args.namespace
    if args.models:
        body["models"] = args.models
    return body


def model_validate_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {
        "path": args.models_dir,
        "watch": args.watch,
        "clearExisting": args.clear_existing,
        "includeStackTrace": args.include_stack_trace,
    }
    if args.namespace:
        body["namespace"] = args.namespace
    return body


def query_payload_body(args: argparse.Namespace, stdin: TextIO) -> dict[str, Any]:
    return read_json_payload(args.payload, stdin)


def table_inspect_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {
        "table": args.table,
        "includeIndexes": args.include_indexes,
        "includeForeignKeys": args.include_foreign_keys,
    }
    if args.schema:
        body["schema"] = args.schema
    if args.data_source:
        body["dataSource"] = args.data_source
    return body


def table_list_body(args: argparse.Namespace, _stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {
        "includeViews": args.include_views,
    }
    if args.schema:
        body["schema"] = args.schema
    if args.data_source:
        body["dataSource"] = args.data_source
    if args.pattern:
        body["pattern"] = args.pattern
    return body


def sql_query_body(args: argparse.Namespace, stdin: TextIO) -> dict[str, Any]:
    if args.sql is not None:
        sql = args.sql
    elif args.sql_file == "-":
        sql = stdin.read()
    else:
        with open(args.sql_file, "r", encoding="utf-8") as handle:
            sql = handle.read()
    body: dict[str, Any] = {"sql": sql}
    if args.data_source:
        body["dataSource"] = args.data_source
    if args.max_rows is not None:
        body["maxRows"] = args.max_rows
    if args.timeout_seconds is not None:
        body["timeoutSeconds"] = args.timeout_seconds
    return body


def compose_script_body(args: argparse.Namespace, stdin: TextIO) -> dict[str, Any]:
    body = script_body(args, stdin)
    return body


def fsscript_body(args: argparse.Namespace, stdin: TextIO) -> dict[str, Any]:
    body = script_body(args, stdin)
    if args.enable_cte_bridge:
        body["capabilities"] = {"cteBridge": True}
    return body


def script_body(args: argparse.Namespace, stdin: TextIO) -> dict[str, Any]:
    body: dict[str, Any] = {
        "script": read_script_source(args, stdin),
    }
    if args.namespace:
        body["namespace"] = args.namespace
    if args.params:
        body["params"] = read_json_payload(args.params, stdin)
    if args.options:
        body["options"] = read_json_payload(args.options, stdin)
    return body


def read_script_source(args: argparse.Namespace, stdin: TextIO) -> str:
    if args.script_text is not None:
        return args.script_text
    if args.script == "-":
        return stdin.read()
    with open(args.script, "r", encoding="utf-8") as handle:
        return handle.read()


def read_json_payload(payload_ref: str, stdin: TextIO) -> dict[str, Any]:
    if payload_ref == "-":
        raw = stdin.read()
        source = "stdin"
    else:
        source = payload_ref
        with open(payload_ref, "r", encoding="utf-8-sig") as handle:
            raw = handle.read()
    raw = raw.lstrip("\ufeff")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{source} must contain a JSON object")
    return payload


def is_semantic_resource_path(path: str) -> bool:
    name = Path(path).name.lower()
    if name.endswith(".tm") or name.endswith(".qm"):
        return True
    is_model_list = "model-list" in name or "modellist" in name
    return is_model_list and name.endswith((".yml", ".yaml", ".json", ".txt"))


def safe_resource_output_path(root: Path, relative: str) -> Path:
    relative_path = Path(relative.replace("\\", "/"))
    if relative_path.is_absolute() or any(part == ".." for part in relative_path.parts):
        raise ValueError(f"resource path escapes output directory: {relative}")
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"resource path escapes output directory: {relative}")
    return target


def demo_sales_drop_plan(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    skill_dir = (
        Path(args.skill_dir).resolve()
        if args.skill_dir
        else repo_root / ".codex" / "skills" / "foggy-ai-analysis-demo"
    )
    demo_dir = skill_dir / "assets" / "sales-drop-demo"
    schema = demo_dir / "schema.sql"
    data = demo_dir / "data.sql"
    models_dir = Path(args.models_dir).resolve() if args.models_dir else demo_dir / "models"
    query_payload = (
        Path(args.query_payload).resolve()
        if args.query_payload
        else demo_dir / "queries" / "basic.json"
    )
    sqlite_path = (
        Path(args.sqlite_path).resolve()
        if args.sqlite_path
        else repo_root / ".codex-tmp" / "foggy-ai-analysis-demo" / "sales-drop" / "foggy_mcp_lite.db"
    )
    launcher_jar = (
        Path(args.launcher_jar).resolve()
        if args.launcher_jar
        else repo_root
        / "foggy-data-mcp-bridge-wt-dev-compose"
        / "foggy-mcp-launcher"
        / "target"
        / "foggy-mcp-launcher-9.1.0.beta.jar"
    )
    base_url = resolve_base_url(args) if args.base_url else f"http://127.0.0.1:{args.port}"
    namespace = args.namespace or "default"

    required_assets = {
        "skill": skill_dir,
        "schema": schema,
        "data": data,
        "modelsDir": models_dir,
        "queryPayload": query_payload,
    }
    missing_assets = [name for name, path in required_assets.items() if not path.exists()]
    if missing_assets:
        return {
            "success": False,
            "engine": "local",
            "data": {
                "repoRoot": str(repo_root),
                "skillDir": str(skill_dir),
                "demoDir": str(demo_dir),
                "missingAssets": missing_assets,
            },
            "error": {
                "code": "DEMO_ASSET_MISSING",
                "phase": "demo.sales-drop.plan",
                "message": "Required sales-drop demo assets are missing.",
                "safeToAutoRepair": False,
            },
        }

    start_command = [
        "java",
        "-Dfile.encoding=UTF-8",
        "-jar",
        str(launcher_jar),
        f"--server.port={args.port}",
        "--spring.profiles.active=lite",
        "--foggy.runtime-api.enabled=true",
        "--foggy.data-viewer.enabled=false",
        "--foggy.mcp.audit.enabled=false",
        f"--spring.datasource.url=jdbc:sqlite:{sqlite_path}",
        "--spring.ai.openai.api-key=sk-runtime-demo",
        "--spring.ai.openai.base-url=http://127.0.0.1:9",
        "--spring.ai.openai.chat.options.model=runtime-demo",
    ]

    cli_base = [
        "python",
        "-m",
        "foggy_runtime_cli.main",
        "--base-url",
        base_url,
        "--namespace",
        namespace,
    ]
    commands = [
        {
            "name": "create-demo-db-dir",
            "argv": [
                "powershell",
                "New-Item",
                "-ItemType",
                "Directory",
                "-Force",
                "-Path",
                str(sqlite_path.parent),
            ],
        },
        {"name": "seed-schema", "argv": ["sqlite3", str(sqlite_path), f".read {schema}"]},
        {"name": "seed-data", "argv": ["sqlite3", str(sqlite_path), f".read {data}"]},
        {"name": "start-runtime", "argv": start_command},
        {
            "name": "wait-ready",
            "argv": [*cli_base, "wait-ready", "--timeout-seconds", "90", "--interval-seconds", "2"],
        },
        {"name": "capabilities", "argv": [*cli_base, "capabilities"]},
        {
            "name": "table-inspect",
            "argv": [
                *cli_base,
                "tables",
                "inspect",
                "--table",
                "sales_drop_daily",
                "--include-indexes",
            ],
        },
        {
            "name": "models-validate",
            "argv": [*cli_base, "models", "validate", "--models-dir", str(models_dir)],
        },
        {
            "name": "models-refresh",
            "argv": [*cli_base, "models", "refresh", "--model", "SalesDropDailyQueryModel"],
        },
        {"name": "models-describe", "argv": [*cli_base, "models", "describe", "SalesDropDailyQueryModel"]},
        {
            "name": "query-validate",
            "argv": [
                *cli_base,
                "query",
                "validate",
                "SalesDropDailyQueryModel",
                "--payload",
                str(query_payload),
            ],
        },
        {
            "name": "query-execute",
            "argv": [
                *cli_base,
                "query",
                "execute",
                "SalesDropDailyQueryModel",
                "--payload",
                str(query_payload),
            ],
        },
    ]

    warnings = []
    if not launcher_jar.exists():
        warnings.append(
            "Launcher JAR was not found. Build foggy-mcp-launcher first or pass --launcher-jar."
        )

    return {
        "success": True,
        "engine": "local",
        "data": {
            "demo": "sales-drop",
            "repoRoot": str(repo_root),
            "skillDir": str(skill_dir),
            "demoDir": str(demo_dir),
            "baseUrl": base_url,
            "namespace": namespace,
            "port": args.port,
            "sqlitePath": str(sqlite_path),
            "launcherJar": str(launcher_jar),
            "modelsDir": str(models_dir),
            "queryPayload": str(query_payload),
            "runtimeSecurityMode": "none-dev-test-only",
            "warnings": warnings,
            "commands": commands,
        },
    }


class DemoReplayFailure(Exception):
    """Raised after replay evidence has been recorded for a failed step."""


def demo_sales_drop_replay(
    args: argparse.Namespace,
    client: Any,
    base_url: str,
) -> tuple[dict[str, Any], int]:
    namespace = args.namespace or "salesdrop"
    skill_dir = Path(args.skill_dir).resolve()
    demo_dir = skill_dir / "assets" / "sales-drop-demo"
    schema = demo_dir / "schema.sql"
    data = demo_dir / "data.sql"
    models_dir = Path(args.models_dir).resolve() if args.models_dir else demo_dir / "models"
    query_payload = (
        Path(args.query_payload).resolve()
        if args.query_payload
        else demo_dir / "queries" / "basic.json"
    )
    question_bank = (
        Path(args.question_bank).resolve()
        if args.question_bank
        else demo_dir / "question-bank.json"
    )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    evidence_dir = (
        Path(args.evidence_dir).resolve()
        if args.evidence_dir
        else Path.cwd().resolve() / ".foggy-demo" / f"sales-drop-replay-{stamp}"
    )
    sqlite_path = (
        Path(args.sqlite_path).resolve()
        if args.sqlite_path
        else evidence_dir / "sales_drop_demo.sqlite"
    )
    use_default_datasource = bool(args.use_default_datasource)
    if use_default_datasource and not args.sqlite_path:
        return (
            {
                "success": False,
                "engine": "local",
                "data": {
                    "runtimeUrl": base_url,
                    "namespace": namespace,
                    "skillDir": str(skill_dir),
                    "evidenceDir": str(evidence_dir),
                },
                "error": {
                    "code": "DEMO_SQLITE_PATH_REQUIRED",
                    "phase": "demo.sales-drop.replay",
                    "message": (
                        "--use-default-datasource requires --sqlite-path. Pass the same SQLite file path "
                        "used by the running Runtime API default datasource."
                    ),
                    "safeToAutoRepair": False,
                },
            },
            EXIT_CLI_ERROR,
        )
    if not use_default_datasource and args.data_source_name == "default":
        return (
            {
                "success": False,
                "engine": "local",
                "data": {
                    "runtimeUrl": base_url,
                    "namespace": namespace,
                    "skillDir": str(skill_dir),
                    "evidenceDir": str(evidence_dir),
                },
                "error": {
                    "code": "DEMO_DEFAULT_DATASOURCE_MODE_REQUIRED",
                    "phase": "demo.sales-drop.replay",
                    "message": (
                        "The runtime default datasource cannot be registered through datasources add. "
                        "Use --use-default-datasource with --sqlite-path instead."
                    ),
                    "safeToAutoRepair": False,
                },
            },
            EXIT_CLI_ERROR,
        )
    data_source_name = "default" if use_default_datasource else args.data_source_name
    data_source_mode = "default" if use_default_datasource else "runtime-managed"
    data_source_slug = demo_safe_name(data_source_name)

    required_assets = {
        "skill": skill_dir,
        "schema": schema,
        "data": data,
        "modelsDir": models_dir,
        "queryPayload": query_payload,
    }
    if not args.skip_question_bank:
        required_assets["questionBank"] = question_bank
    missing_assets = [name for name, path in required_assets.items() if not path.exists()]
    if missing_assets:
        return (
            {
                "success": False,
                "engine": "local",
                "data": {
                    "skillDir": str(skill_dir),
                    "demoDir": str(demo_dir),
                    "missingAssets": missing_assets,
                },
                "error": {
                    "code": "DEMO_ASSET_MISSING",
                    "phase": "demo.sales-drop.replay",
                    "message": "Required sales-drop demo assets are missing.",
                    "safeToAutoRepair": False,
                },
            },
            EXIT_API_ERROR,
        )

    evidence_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = evidence_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    command_status_path = evidence_dir / "command-status.json"
    question_replay_path = evidence_dir / "question-bank-replay.json"
    summary_path = evidence_dir / "summary.json"
    report_path = evidence_dir / "cli-sales-drop-replay-report.md"

    started_at = time.monotonic()
    results: list[dict[str, Any]] = []
    question_bank_summary: dict[str, Any] | None = None
    failure_message = ""

    try:
        seed_response = demo_seed_sales_drop_sqlite(sqlite_path, schema, data)
        seed_result = demo_record_step(results, logs_dir, "seed-sales-drop-sqlite", seed_response)
        if seed_result["result"] != "passed":
            raise DemoReplayFailure("seed-sales-drop-sqlite failed.")

        ready_args = argparse.Namespace(
            timeout_seconds=args.ready_timeout_seconds,
            interval_seconds=args.ready_interval_seconds,
            namespace=namespace,
        )
        ready_response, ready_code = wait_ready_handler(ready_args, client, base_url)
        ready_result = demo_record_step(results, logs_dir, "wait-ready", ready_response)
        if ready_code != EXIT_OK or ready_result["result"] != "passed":
            raise DemoReplayFailure("wait-ready failed.")

        demo_run_required_step(
            client,
            results,
            logs_dir,
            "capabilities",
            "GET",
            "/api/v1/capabilities",
            None,
            lambda response: demo_validate_capabilities_response(
                response,
                managed_datasource=not use_default_datasource,
            ),
        )
        if use_default_datasource:
            demo_run_required_step(
                client,
                results,
                logs_dir,
                "datasources-test-default",
                "POST",
                f"/api/v1/datasources/{path_quote(data_source_name)}/test",
                None,
            )
        else:
            demo_run_required_step(
                client,
                results,
                logs_dir,
                f"datasources-add-{data_source_slug}",
                "POST",
                "/api/v1/datasources",
                {
                    "name": data_source_name,
                    "type": "sqlite",
                    "jdbcUrl": f"jdbc:sqlite:{sqlite_path}",
                    "replace": True,
                    "enabled": True,
                },
            )
            demo_run_required_step(
                client,
                results,
                logs_dir,
                f"datasources-test-{data_source_slug}",
                "POST",
                f"/api/v1/datasources/{path_quote(data_source_name)}/test",
                None,
            )
            demo_run_required_step(
                client,
                results,
                logs_dir,
                f"datasources-bind-{demo_safe_name(namespace)}-{data_source_slug}",
                "PUT",
                f"/api/v1/namespaces/{path_quote(namespace)}/datasource",
                {"namespace": namespace, "dataSource": data_source_name},
            )
        demo_run_required_step(
            client,
            results,
            logs_dir,
            f"tables-list-{data_source_slug}",
            "POST",
            "/api/v1/tables/list",
            {"includeViews": True, "dataSource": data_source_name},
        )
        demo_run_required_step(
            client,
            results,
            logs_dir,
            "tables-inspect-sales_drop_daily",
            "POST",
            "/api/v1/tables/inspect",
            {
                "table": "sales_drop_daily",
                "includeIndexes": True,
                "includeForeignKeys": False,
                "dataSource": data_source_name,
            },
        )
        demo_run_required_step(
            client,
            results,
            logs_dir,
            "sql-query-sales-drop-top5",
            "POST",
            "/api/v1/sql/query",
            {
                "sql": (
                    "select sales_drop_id, observation_date, region, channel, severity, root_cause, "
                    "sales_drop_amount, sales_drop_rate from sales_drop_daily "
                    "order by sales_drop_amount desc"
                ),
                "dataSource": data_source_name,
                "maxRows": 5,
                "timeoutSeconds": 5,
            },
        )
        demo_run_required_step(
            client,
            results,
            logs_dir,
            "models-validate",
            "POST",
            "/api/v1/models/validate",
            {
                "path": str(models_dir),
                "watch": False,
                "clearExisting": True,
                "includeStackTrace": False,
                "namespace": namespace,
            },
            demo_validate_models_validate_response,
        )
        demo_run_required_step(client, results, logs_dir, "bundles-list-before-add", "GET", "/api/v1/bundles", None)
        demo_run_required_step(
            client,
            results,
            logs_dir,
            "bundles-add-sales-drop-models",
            "POST",
            "/api/v1/bundles",
            {
                "name": args.bundle_name,
                "path": str(models_dir),
                "watch": True,
                "replace": True,
                "validate": False,
                "refresh": False,
                "enabled": True,
                "namespace": namespace,
            },
        )
        demo_run_required_step(
            client,
            results,
            logs_dir,
            "models-refresh",
            "POST",
            "/api/v1/models/refresh",
            {"namespace": namespace},
            demo_validate_models_refresh_response,
        )
        demo_run_required_step(
            client,
            results,
            logs_dir,
            "models-describe-SalesDropDailyQueryModel",
            "POST",
            "/api/v1/models/SalesDropDailyQueryModel/describe",
            {"namespace": namespace},
        )
        basic_payload = demo_read_json_object(query_payload)
        demo_run_required_step(
            client,
            results,
            logs_dir,
            "query-validate-basic",
            "POST",
            "/api/v1/query/SalesDropDailyQueryModel/validate",
            basic_payload,
        )
        demo_run_required_step(
            client,
            results,
            logs_dir,
            "query-execute-basic",
            "POST",
            "/api/v1/query/SalesDropDailyQueryModel/execute",
            basic_payload,
        )

        if not args.skip_question_bank:
            question_bank_summary = demo_replay_sales_drop_question_bank(
                client,
                results,
                logs_dir,
                demo_dir,
                models_dir,
                question_bank,
                question_replay_path,
            )
            if int(question_bank_summary.get("failed", 0)) > 0:
                raise DemoReplayFailure(
                    f"question-bank replay has {question_bank_summary['failed']} failed case(s)."
                )

    except DemoReplayFailure as exc:
        failure_message = str(exc)
    except (OSError, RuntimeTransportError, ValueError, sqlite3.Error) as exc:
        failure_message = str(exc)

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    failed_results = [item for item in results if item.get("result") != "passed"]
    final_status = "passed" if not failure_message and not failed_results else "failed"
    summary = {
        "schemaVersion": "foggy-demo-evidence/v1",
        "generatedAt": demo_now_iso(),
        "mode": "cli-sales-drop-replay",
        "status": final_status,
        "failure": failure_message or None,
        "runtimeUrl": base_url,
        "runtimePort": demo_runtime_port(base_url),
        "namespace": namespace,
        "dataSource": data_source_name,
        "dataSourceMode": data_source_mode,
        "bundle": args.bundle_name,
        "elapsedMs": elapsed_ms,
        "evidenceDir": str(evidence_dir),
        "skillDir": str(skill_dir),
        "demoDir": str(demo_dir),
        "sqlitePath": str(sqlite_path),
        "modelsDir": str(models_dir),
        "queryPayload": str(query_payload),
        "questionBankPath": str(question_bank) if not args.skip_question_bank else None,
        "questionBankEvidence": str(question_replay_path) if question_bank_summary else None,
        "questionBankSummary": question_bank_summary,
        "commandStatus": str(command_status_path),
        "report": str(report_path),
        "results": results,
        "notes": [
            "This command uses an unpacked foggy-ai-analysis-demo Skill directory and a running Runtime API.",
            "It does not require a foggy-runtime-cli source checkout or workspace wrapper script.",
            "This replay is for trusted local dev/test use with Runtime API securityMode=none-dev-test-only.",
            "Production permission, auth, RBAC, audit, and governance remain deferred.",
        ],
    }
    demo_write_json(command_status_path, {"schemaVersion": "foggy-demo-command-status/v1", "results": results})
    demo_write_json(summary_path, summary)
    demo_write_replay_report(report_path, summary)

    response: dict[str, Any] = {
        "success": final_status == "passed",
        "engine": "local",
        "data": {
            "status": final_status,
            "runtimeUrl": base_url,
            "namespace": namespace,
            "evidenceDir": str(evidence_dir),
            "summary": str(summary_path),
            "report": str(report_path),
            "commandStatus": str(command_status_path),
            "questionBankSummary": question_bank_summary,
        },
    }
    if final_status != "passed":
        response["error"] = {
            "code": "DEMO_REPLAY_FAILED",
            "phase": "demo.sales-drop.replay",
            "message": failure_message or f"{len(failed_results)} replay step(s) failed.",
            "safeToAutoRepair": False,
        }
        return response, EXIT_API_ERROR
    return response, EXIT_OK


def demo_seed_sales_drop_sqlite(sqlite_path: Path, schema_path: Path, data_path: Path) -> dict[str, Any]:
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    schema = schema_path.read_text(encoding="utf-8")
    data = data_path.read_text(encoding="utf-8")
    connection = sqlite3.connect(str(sqlite_path))
    try:
        connection.executescript(schema)
        connection.executescript(data)
        connection.commit()
    finally:
        connection.close()
    return {
        "success": True,
        "engine": "local",
        "data": {
            "sqlitePath": str(sqlite_path),
            "schema": str(schema_path),
            "data": str(data_path),
        },
    }


def demo_run_required_step(
    client: Any,
    results: list[dict[str, Any]],
    logs_dir: Path,
    name: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    validator: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    response, result = demo_run_runtime_step(client, results, logs_dir, name, method, path, body, validator)
    if result["result"] != "passed":
        message = result.get("validationMessage") or demo_response_error_message(response) or f"{name} failed."
        raise DemoReplayFailure(message)
    return response


def demo_run_runtime_step(
    client: Any,
    results: list[dict[str, Any]],
    logs_dir: Path,
    name: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    validator: Callable[[dict[str, Any]], str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        response = client.request(method, path, body)
    except RuntimeTransportError as exc:
        response = {
            "success": False,
            "engine": "unknown",
            "error": {
                "code": "TRANSPORT_ERROR",
                "phase": name,
                "message": str(exc),
                "safeToAutoRepair": False,
            },
        }
    validation_message = validator(response) if validator else ""
    result = demo_record_step(
        results,
        logs_dir,
        name,
        response,
        validation_message=validation_message,
        runtime_request={"method": method, "path": path, "body": body},
    )
    return response, result


def demo_record_step(
    results: list[dict[str, Any]],
    logs_dir: Path,
    name: str,
    response: dict[str, Any],
    validation_message: str = "",
    runtime_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step = len(results) + 1
    output_path = logs_dir / f"{step:02d}-{demo_safe_name(name)}.json"
    demo_write_json(output_path, response)
    result = "passed" if response.get("success") is True and not validation_message else "failed"
    row: dict[str, Any] = {
        "step": step,
        "name": name,
        "result": result,
        "output": str(output_path),
        "validationMessage": validation_message,
    }
    if runtime_request is not None:
        row["runtimeRequest"] = runtime_request
    results.append(row)
    return row


def demo_replay_sales_drop_question_bank(
    client: Any,
    results: list[dict[str, Any]],
    logs_dir: Path,
    demo_dir: Path,
    models_dir: Path,
    question_bank_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    bank = demo_read_json_object(question_bank_path)
    cases = bank.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"{question_bank_path} must contain a cases array")

    query_model_file = demo_find_query_model_file(models_dir)
    available_fields = demo_available_query_fields(query_model_file)
    available_field_set = set(available_fields)
    case_results: list[dict[str, Any]] = []

    for case in cases:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or f"case-{len(case_results) + 1}")
        declared_status = str(case.get("status") or "executable")
        required_fields = [str(item) for item in case.get("requiredFields") or []]
        missing_fields = [field for field in required_fields if field not in available_field_set]
        case_result: dict[str, Any] = {
            "id": case_id,
            "question": case.get("question"),
            "declaredStatus": declared_status,
            "capabilitySet": case.get("capabilitySet"),
            "decision": case.get("decision"),
            "expectedBehavior": case.get("expectedBehavior"),
            "requiredFields": required_fields,
            "missingFields": missing_fields,
            "payloadFile": case.get("payloadFile"),
            "payload": None,
            "sourcePayload": None,
            "validate": None,
            "execute": None,
            "assertionMessage": "",
            "skipReason": case.get("skipReason"),
            "status": "pending",
            "tuningNotes": [],
        }

        if missing_fields:
            case_result["status"] = "fail"
            case_result["tuningNotes"].append("Missing required fields in SalesDropDailyQueryModel.")
            case_results.append(case_result)
            continue

        if declared_status != "executable":
            case_result["status"] = declared_status
            if case.get("skipReason"):
                case_result["tuningNotes"].append(str(case["skipReason"]))
            case_results.append(case_result)
            continue

        relative_payload = str(case.get("payloadFile") or "")
        if not relative_payload:
            case_result["status"] = "fail"
            case_result["tuningNotes"].append(f"Question {case_id} does not define payloadFile.")
            case_results.append(case_result)
            continue
        source_payload = (demo_dir / relative_payload).resolve()
        if not source_payload.exists():
            case_result["status"] = "fail"
            case_result["tuningNotes"].append(f"Question {case_id} payloadFile not found: {source_payload}")
            case_results.append(case_result)
            continue

        payload = demo_read_json_object(source_payload)
        case_result["payload"] = str(source_payload)
        case_result["sourcePayload"] = str(source_payload)
        validate_response, validate_result = demo_run_runtime_step(
            client,
            results,
            logs_dir,
            f"question-{case_id}-validate",
            "POST",
            "/api/v1/query/SalesDropDailyQueryModel/validate",
            payload,
        )
        case_result["validate"] = validate_result["output"]
        execute_response: dict[str, Any] | None = None
        execute_result: dict[str, Any] | None = None
        if validate_result["result"] == "passed":
            execute_response, execute_result = demo_run_runtime_step(
                client,
                results,
                logs_dir,
                f"question-{case_id}-execute",
                "POST",
                "/api/v1/query/SalesDropDailyQueryModel/execute",
                payload,
            )
            case_result["execute"] = execute_result["output"]

        if (
            validate_result["result"] == "passed"
            and execute_result is not None
            and execute_result["result"] == "passed"
            and execute_response is not None
        ):
            assertion_message = demo_check_question_assertions(execute_response, case.get("assertions"))
            case_result["assertionMessage"] = assertion_message
            if not assertion_message:
                case_result["status"] = "pass"
            else:
                case_result["status"] = "fail"
                case_result["tuningNotes"].append(assertion_message)
        else:
            case_result["status"] = "fail"
            case_result["tuningNotes"].append("Validate or execute failed; inspect linked evidence.")
            if validate_response.get("success") is not True:
                case_result["tuningNotes"].append(demo_response_error_message(validate_response))

        case_results.append(case_result)

    pass_count = sum(1 for item in case_results if item.get("status") == "pass")
    fail_count = sum(1 for item in case_results if item.get("status") == "fail")
    clarify_count = sum(1 for item in case_results if item.get("status") == "needs-clarification")
    unsupported_count = sum(1 for item in case_results if item.get("status") == "unsupported")
    executable_count = sum(1 for item in case_results if item.get("declaredStatus") == "executable")
    replay = {
        "schemaVersion": "foggy-demo-question-bank-replay/v1",
        "generatedAt": demo_now_iso(),
        "questionBank": str(question_bank_path),
        "questionBankSchemaVersion": bank.get("schemaVersion"),
        "model": bank.get("model"),
        "queryModelFile": str(query_model_file) if query_model_file else None,
        "availableFields": available_fields,
        "totalCases": len(case_results),
        "executableCases": executable_count,
        "passCases": pass_count,
        "failCases": fail_count,
        "needsClarificationCases": clarify_count,
        "unsupportedCases": unsupported_count,
        "cases": case_results,
    }
    demo_write_json(output_path, replay)
    return {
        "path": str(output_path),
        "total": len(case_results),
        "executable": executable_count,
        "passed": pass_count,
        "failed": fail_count,
        "needsClarification": clarify_count,
        "unsupported": unsupported_count,
    }


def demo_check_question_assertions(response: dict[str, Any], assertions: Any) -> str:
    if not assertions:
        return ""
    if response.get("success") is not True:
        return "Question execute envelope success is not true."
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    if assertions.get("rowCountMin") is not None and len(items) < int(assertions["rowCountMin"]):
        return f"Expected at least {assertions['rowCountMin']} row(s), got {len(items)}."

    schema = data.get("schema") if isinstance(data.get("schema"), dict) else {}
    columns = schema.get("columns") if isinstance(schema.get("columns"), list) else []
    schema_columns = [
        str(column.get("name"))
        for column in columns
        if isinstance(column, dict) and column.get("name") is not None
    ]
    for column in assertions.get("requiredColumns") or []:
        if str(column) not in schema_columns:
            return f"Expected result schema column '{column}' was not returned."

    for expected in assertions.get("expectedValues") or []:
        if not isinstance(expected, dict):
            continue
        field = str(expected.get("field") or "")
        value = str(expected.get("value") or "")
        mismatched = [
            item
            for item in items
            if isinstance(item, dict) and str(item.get(field)) != value
        ]
        if items and mismatched:
            return f"Expected all result rows to have {field}={value}."
    return ""


def demo_validate_capabilities_response(response: dict[str, Any], managed_datasource: bool = True) -> str:
    message = demo_validate_success_response(response)
    if message:
        return message
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    if data.get("securityMode") != "none-dev-test-only":
        return f"securityMode is {data.get('securityMode')}, expected none-dev-test-only."
    capabilities = data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}
    required = [
        "runtime.capabilities",
        "datasources.test",
        "tables.list",
        "tables.inspect",
        "sql.query",
        "models.validate",
        "bundles.list",
        "bundles.add",
        "models.refresh",
        "models.describe",
        "query.validate",
        "query.execute",
    ]
    if managed_datasource:
        required.extend(["datasources.add", "datasources.bind"])
    for capability in required:
        if capabilities.get(capability) != "supported":
            return f"capability {capability} is {capabilities.get(capability)}."
    return ""


def demo_validate_models_validate_response(response: dict[str, Any]) -> str:
    message = demo_validate_success_response(response)
    if message:
        return message
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    if data.get("valid") is not True:
        return "models.validate data.valid is not true."
    if int(data.get("invalidFiles") or 0) != 0:
        return f"models.validate invalidFiles is {data.get('invalidFiles')}."
    return ""


def demo_validate_models_refresh_response(response: dict[str, Any]) -> str:
    message = demo_validate_success_response(response)
    if message:
        return message
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    if int(data.get("failedCount") or 0) != 0:
        return f"models.refresh failedCount is {data.get('failedCount')}."
    refreshed = data.get("refreshedModels") if isinstance(data.get("refreshedModels"), list) else []
    if "SalesDropDailyQueryModel" not in refreshed:
        return "models.refresh did not refresh SalesDropDailyQueryModel."
    return ""


def demo_validate_success_response(response: dict[str, Any]) -> str:
    if response.get("success") is True:
        return ""
    return demo_response_error_message(response) or "Runtime envelope success is not true."


def demo_response_error_message(response: dict[str, Any]) -> str:
    error = response.get("error") if isinstance(response.get("error"), dict) else {}
    code = error.get("code")
    message = error.get("message")
    if code and message:
        return f"{code}: {message}"
    if message:
        return str(message)
    if code:
        return str(code)
    return ""


def demo_find_query_model_file(models_dir: Path) -> Path | None:
    direct = models_dir / "query" / "SalesDropDailyQueryModel.qm"
    if direct.exists():
        return direct
    matches = sorted(models_dir.rglob("SalesDropDailyQueryModel.qm")) if models_dir.exists() else []
    return matches[0] if matches else None


def demo_available_query_fields(query_model_file: Path | None) -> list[str]:
    if query_model_file is None:
        return []
    text = query_model_file.read_text(encoding="utf-8")
    return sorted(set(match.group(1) for match in re.finditer(r"salesDrop\.([A-Za-z][A-Za-z0-9_]*)", text)))


def demo_read_json_object(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8-sig").lstrip("\ufeff")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def demo_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def demo_write_replay_report(path: Path, summary: dict[str, Any]) -> None:
    question = summary.get("questionBankSummary") or {}
    lines = [
        "# Foggy sales-drop CLI replay",
        "",
        f"- Status: {summary.get('status')}",
        f"- Failure: {summary.get('failure') or 'none'}",
        f"- Runtime URL: {summary.get('runtimeUrl')}",
        f"- Runtime port: {summary.get('runtimePort')}",
        f"- Namespace: {summary.get('namespace')}",
        f"- Data source: {summary.get('dataSource')}",
        f"- Bundle: {summary.get('bundle')}",
        f"- SQLite: {summary.get('sqlitePath')}",
        f"- Skill dir: {summary.get('skillDir')}",
        f"- Evidence dir: {summary.get('evidenceDir')}",
        f"- Command status: {summary.get('commandStatus')}",
        f"- Question bank: total={question.get('total')}, executable={question.get('executable')}, "
        f"pass={question.get('passed')}, fail={question.get('failed')}, "
        f"needs-clarification={question.get('needsClarification')}",
        "",
        "## Commands",
        "",
    ]
    for item in summary.get("results") or []:
        line = f"- {item.get('name')}: {item.get('result')}"
        if item.get("validationMessage"):
            line += f" - {item.get('validationMessage')}"
        line += f" ({item.get('output')})"
        lines.append(line)
    lines.extend(
        [
            "",
            "## Scope",
            "",
            "This replay uses a running Runtime API plus an unpacked foggy-ai-analysis-demo Skill directory. "
            "Production permission, auth, RBAC, audit, and governance remain deferred.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def demo_safe_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return safe or "step"


def demo_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def demo_runtime_port(base_url: str) -> int | None:
    match = re.search(r":(\d+)(?:/|$)", base_url)
    return int(match.group(1)) if match else None


def required_capabilities_for(args: argparse.Namespace) -> list[str]:
    capabilities = list(getattr(args, "required_capabilities", []) or [])
    if getattr(args, "enable_cte_bridge", False):
        capabilities.append("fsscript.cteBridge")
    return capabilities


def unsupported_capability_response(
    capability_response: dict[str, Any],
    required_capabilities: list[str],
) -> dict[str, Any] | None:
    if capability_response.get("success") is not True:
        return capability_response
    data = capability_response.get("data")
    capabilities = data.get("capabilities") if isinstance(data, dict) else None
    if not isinstance(capabilities, dict):
        return unsupported_response(capability_response, required_capabilities[0], "missing")
    for capability in required_capabilities:
        state = capabilities.get(capability)
        if state != "supported":
            return unsupported_response(capability_response, capability, state)
    return None


def unsupported_response(
    capability_response: dict[str, Any],
    capability: str,
    state: Any,
) -> dict[str, Any]:
    engine = capability_response.get("engine", "unknown")
    runtime_api_version = capability_response.get("runtimeApiVersion")
    response: dict[str, Any] = {
        "success": False,
        "engine": engine,
        "data": None,
        "error": {
            "code": "UNSUPPORTED_OPERATION",
            "phase": capability,
            "message": f"Capability {capability} is not supported by the connected runtime (state={state}).",
            "safeToAutoRepair": False,
        },
    }
    if runtime_api_version is not None:
        response["runtimeApiVersion"] = runtime_api_version
    return response


def render_response(response: dict[str, Any], output: str, stdout: TextIO) -> None:
    if output == "json":
        print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True), file=stdout)
        return
    print(pretty_response(response), file=stdout)


def pretty_response(response: dict[str, Any]) -> str:
    success = response.get("success")
    engine = response.get("engine", "unknown")
    if success is False:
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        code = error.get("code", "UNKNOWN_ERROR")
        phase = error.get("phase", "unknown")
        message = error.get("message", "")
        return f"ERROR [{engine}] {code} at {phase}: {message}".rstrip()

    data = response.get("data")
    if isinstance(data, dict):
        if "models" in data and isinstance(data["models"], list):
            return "\n".join(str(item) for item in data["models"])
        if "capabilities" in data and isinstance(data["capabilities"], dict):
            return pretty_capabilities(response, data)
    return f"OK [{engine}]"


def pretty_capabilities(response: dict[str, Any], data: dict[str, Any]) -> str:
    engine = response.get("engine") or data.get("engine") or "unknown"
    runtime_api_version = response.get("runtimeApiVersion") or data.get("runtimeApiVersion") or "unknown"
    lines = [
        f"engine: {engine}",
        f"runtimeApiVersion: {runtime_api_version}",
    ]
    schema_version = data.get("schemaVersion")
    if schema_version is not None:
        lines.append(f"schemaVersion: {schema_version}")
    enabled = data.get("enabled")
    if enabled is not None:
        lines.append(f"enabled: {str(bool(enabled)).lower()}")
    security_mode = data.get("securityMode")
    if security_mode is not None:
        lines.append(f"securityMode: {security_mode}")
    lines.append("capabilities:")
    capabilities = data["capabilities"]
    lines.extend(f"  {key}: {value}" for key, value in sorted(capabilities.items()))
    return "\n".join(lines)


def exit_code_for_response(response: dict[str, Any]) -> int:
    if response.get("success") is True:
        return EXIT_OK
    error = response.get("error")
    code = str(error.get("code", "")) if isinstance(error, dict) else ""
    message = str(error.get("message", "")) if isinstance(error, dict) else ""
    if "UNSUPPORTED" in code.upper() or "unsupported" in message.lower():
        return EXIT_UNSUPPORTED
    return EXIT_API_ERROR


if __name__ == "__main__":
    console_main()
