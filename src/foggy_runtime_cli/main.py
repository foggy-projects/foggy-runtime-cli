from __future__ import annotations

import argparse
import json
import os
import sys
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

    inspect = table_commands.add_parser("inspect")
    inspect.add_argument("--table", required=True)
    inspect.add_argument("--schema")
    inspect.add_argument("--data-source")
    inspect.add_argument("--include-indexes", action="store_true")
    inspect.add_argument("--include-foreign-keys", action="store_true")
    inspect.set_defaults(method="POST", path="/api/v1/tables/inspect", body_builder=table_inspect_body)

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


def build_body(args: argparse.Namespace, stdin: TextIO, stderr: TextIO) -> dict[str, Any] | None | object:
    if hasattr(args, "model") and not hasattr(args, "path"):
        if getattr(args, "query_action", None):
            args.path = f"/api/v1/query/{path_quote(args.model)}/{args.query_action}"
        elif getattr(args, "models_command", None) == "describe":
            args.path = f"/api/v1/models/{path_quote(args.model)}/describe"
    try:
        return args.body_builder(args, stdin)
    except (OSError, ValueError) as exc:
        print(f"input error: {exc}", file=stderr)
        return _BODY_ERROR


def no_body(_args: argparse.Namespace, _stdin: TextIO) -> None:
    return None


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
        with open(payload_ref, "r", encoding="utf-8") as handle:
            raw = handle.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{source} must contain a JSON object")
    return payload


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
