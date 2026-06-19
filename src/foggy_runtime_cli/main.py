from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
    client_factory: Callable[[str, str | None, float], Any] = RuntimeApiClient,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    stdin = stdin or sys.stdin

    parser = build_parser()
    args = parser.parse_args(argv)

    local_handler = getattr(args, "local_handler", None)
    if local_handler is not None:
        response = local_handler(args)
        render_response(response, args.output, stdout)
        return exit_code_for_response(response)

    runtime_handler = getattr(args, "runtime_handler", None)
    if runtime_handler is not None:
        base_url = resolve_base_url(args)
        client = client_factory(base_url, args.namespace, args.timeout)
        response, exit_code = runtime_handler(args, client, base_url)
        render_response(response, args.output, stdout)
        return exit_code

    body = build_body(args, stdin, stderr)
    if body is _BODY_ERROR:
        return EXIT_CLI_ERROR

    base_url = resolve_base_url(args)
    client = client_factory(base_url, args.namespace, args.timeout)
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
    skill_dir = repo_root / ".codex" / "skills" / "foggy-ai-analysis-demo"
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
