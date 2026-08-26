from __future__ import annotations

import argparse
import os
import re
import uuid
from typing import Any, TextIO

from .client import path_quote
from .input_utils import read_json_payload

DEFAULT_ANALYTICS_BASE_URL = "http://127.0.0.1:8080/analytics"
ANALYTICS_LOGICAL_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")

# Keep this registry aligned with AnalyticsFunctionOperations.SDK_V1. It is
# intentionally public so contract tests can detect transport drift.
ANALYTICS_SDK_V1_OPERATIONS = frozenset(
    {
        "analytics.capabilities",
        "analytics.bundles.list",
        "analytics.bundles.validate",
        "analytics.bundles.describe",
        "analytics.artifacts.describe",
        "analytics.model-dependencies.resolve",
        "analytics.model-dependencies.list",
        "analytics.semantic-models.describe",
        "analytics.semantic-queries.execute",
        "analytics.query-model.run",
        "analytics.compose.run",
        "analytics.reports.preview",
        "analytics.dashboards.preview",
        "analytics.dashboards.render",
    }
)


def register_analytics_parser(
    subparsers: Any,
    parser_class: type[argparse.ArgumentParser],
) -> None:
    """Register the independent Analytics Runtime API v1 command domain."""
    analytics = subparsers.add_parser(
        "analytics",
        help="Call the independent Analytics Runtime API v1.",
        description=(
            "Analytics Bundle, Report, Dashboard, and model dependency operations. "
            "This domain uses FOGGY_ANALYTICS_RUNTIME_API_URL and separate "
            "Analytics credentials."
        ),
    )
    add_transport_arguments(analytics)
    analytics.set_defaults(
        api_domain="analytics",
        capability_registry_key="operations",
        namespace=None,
    )
    commands = analytics.add_subparsers(
        dest="analytics_command",
        required=True,
        parser_class=parser_class,
    )

    capabilities = commands.add_parser("capabilities")
    capabilities.set_defaults(
        method="GET",
        path="/api/v1/capabilities",
        body_builder=no_body,
    )

    bundles = commands.add_parser("bundles")
    bundle_commands = bundles.add_subparsers(
        dest="analytics_bundles_command",
        required=True,
        parser_class=parser_class,
    )
    bundle_list = bundle_commands.add_parser("list")
    bundle_list.set_defaults(
        method="GET",
        path="/api/v1/bundles",
        body_builder=no_body,
        required_capabilities=["analytics.bundles.list"],
    )
    for action in ("validate", "describe"):
        bundle = bundle_commands.add_parser(action)
        bundle.add_argument("analytics_bundle", metavar="bundle")
        bundle.add_argument(
            "--revision",
            dest="analytics_revision",
            help="Optional exact sha256:<hex> Bundle revision assertion.",
        )
        add_correlation_arguments(bundle)
        bundle.set_defaults(
            method="POST",
            body_builder=bundle_inspection_body,
            path_builder=bundle_inspection_path,
            analytics_bundle_action=action,
            required_capabilities=[f"analytics.bundles.{action}"],
        )

    artifacts = commands.add_parser("artifacts")
    artifact_commands = artifacts.add_subparsers(
        dest="analytics_artifacts_command",
        required=True,
        parser_class=parser_class,
    )
    artifact_describe = artifact_commands.add_parser("describe")
    artifact_describe.add_argument("analytics_artifact", metavar="artifact")
    artifact_describe.add_argument(
        "--kind",
        dest="analytics_artifact_kind",
        choices=("report", "dashboard"),
        required=True,
    )
    artifact_describe.add_argument(
        "--bundle", dest="analytics_bundle", required=True
    )
    artifact_describe.add_argument(
        "--revision", dest="analytics_revision", required=True
    )
    add_correlation_arguments(artifact_describe)
    artifact_describe.set_defaults(
        method="POST",
        body_builder=artifact_describe_body,
        path_builder=artifact_describe_path,
        required_capabilities=["analytics.artifacts.describe"],
    )

    model_dependencies = commands.add_parser("model-dependencies")
    model_dependency_commands = model_dependencies.add_subparsers(
        dest="analytics_model_dependencies_command",
        required=True,
        parser_class=parser_class,
    )
    model_resolve = model_dependency_commands.add_parser("resolve")
    model_resolve.add_argument("analytics_model", metavar="model")
    model_resolve.add_argument(
        "--kind",
        dest="analytics_model_kind",
        choices=("tm", "qm"),
        required=True,
    )
    model_resolve.add_argument(
        "--namespace", dest="analytics_model_namespace", required=True
    )
    add_correlation_arguments(model_resolve)
    model_resolve.set_defaults(
        method="POST",
        path="/api/v1/model-dependencies/resolve",
        body_builder=model_dependency_resolve_body,
        required_capabilities=["analytics.model-dependencies.resolve"],
    )

    reports = commands.add_parser("reports")
    report_commands = reports.add_subparsers(
        dest="analytics_reports_command",
        required=True,
        parser_class=parser_class,
    )
    report_preview = report_commands.add_parser("preview")
    report_preview.add_argument("analytics_artifact", metavar="report")
    add_render_arguments(report_preview)
    report_preview.set_defaults(
        method="POST",
        body_builder=render_body,
        path_builder=report_preview_path,
        required_capabilities=["analytics.reports.preview"],
    )

    dashboards = commands.add_parser("dashboards")
    dashboard_commands = dashboards.add_subparsers(
        dest="analytics_dashboards_command",
        required=True,
        parser_class=parser_class,
    )
    for action in ("preview", "render"):
        dashboard = dashboard_commands.add_parser(action)
        dashboard.add_argument("analytics_artifact", metavar="dashboard")
        add_render_arguments(dashboard)
        dashboard.set_defaults(
            method="POST",
            body_builder=render_body,
            path_builder=dashboard_path,
            analytics_dashboard_action=action,
            required_capabilities=[f"analytics.dashboards.{action}"],
        )


def add_transport_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        default=argparse.SUPPRESS,
        help=(
            "Analytics Runtime API base URL. Overrides "
            "FOGGY_ANALYTICS_RUNTIME_API_URL."
        ),
    )
    parser.add_argument(
        "--output",
        choices=["json", "pretty"],
        default=argparse.SUPPRESS,
        help="Output format.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=argparse.SUPPRESS,
        help="HTTP timeout seconds.",
    )
    parser.add_argument(
        "--auth-code",
        default=argparse.SUPPRESS,
        help=(
            "Analytics API auth code. Overrides "
            "FOGGY_ANALYTICS_RUNTIME_API_AUTH_CODE and is sent as "
            "X-Foggy-Runtime-Code."
        ),
    )
    parser.add_argument(
        "--authorization",
        default=argparse.SUPPRESS,
        help=(
            "Opaque Analytics data-plane Authorization value. Overrides "
            "FOGGY_ANALYTICS_AUTHORIZATION."
        ),
    )


def add_correlation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--request-id")
    parser.add_argument("--trace-id")


def add_render_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bundle", dest="analytics_bundle", required=True)
    parser.add_argument("--revision", dest="analytics_revision", required=True)
    parser.add_argument(
        "--parameters",
        help="Path to a JSON object containing definition parameters, or '-' for stdin.",
    )
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--locale", default="en")
    parser.add_argument("--authority-provider", required=True)
    parser.add_argument("--authority-reference", required=True)
    add_correlation_arguments(parser)


def resolve_base_url(args: argparse.Namespace) -> str:
    if args.base_url:
        return args.base_url
    value = os.environ.get("FOGGY_ANALYTICS_RUNTIME_API_URL")
    return value if value else DEFAULT_ANALYTICS_BASE_URL


def resolve_auth_code(args: argparse.Namespace) -> str | None:
    if args.auth_code is not None:
        return args.auth_code
    value = os.environ.get("FOGGY_ANALYTICS_RUNTIME_API_AUTH_CODE")
    return value if value else None


def resolve_authorization(args: argparse.Namespace) -> str | None:
    if args.authorization is not None:
        return args.authorization if args.authorization else None
    value = os.environ.get("FOGGY_ANALYTICS_AUTHORIZATION")
    return value if value else None


def bundle_inspection_path(args: argparse.Namespace) -> str:
    bundle_ref = require_logical_ref("bundleRef", args.analytics_bundle)
    return (
        f"/api/v1/bundles/{path_quote(bundle_ref)}/"
        f"{path_quote(args.analytics_bundle_action)}"
    )


def artifact_describe_path(args: argparse.Namespace) -> str:
    bundle_ref = require_logical_ref("bundleRef", args.analytics_bundle)
    artifact_ref = require_logical_ref("artifactRef", args.analytics_artifact)
    return (
        f"/api/v1/bundles/{path_quote(bundle_ref)}/artifacts/"
        f"{path_quote(args.analytics_artifact_kind)}/"
        f"{path_quote(artifact_ref)}/describe"
    )


def report_preview_path(args: argparse.Namespace) -> str:
    bundle_ref = require_logical_ref("bundleRef", args.analytics_bundle)
    artifact_ref = require_logical_ref("artifactRef", args.analytics_artifact)
    return (
        f"/api/v1/bundles/{path_quote(bundle_ref)}/reports/"
        f"{path_quote(artifact_ref)}/preview"
    )


def dashboard_path(args: argparse.Namespace) -> str:
    bundle_ref = require_logical_ref("bundleRef", args.analytics_bundle)
    artifact_ref = require_logical_ref("artifactRef", args.analytics_artifact)
    return (
        f"/api/v1/bundles/{path_quote(bundle_ref)}/dashboards/"
        f"{path_quote(artifact_ref)}/"
        f"{path_quote(args.analytics_dashboard_action)}"
    )


def require_logical_ref(field: str, value: str) -> str:
    if not ANALYTICS_LOGICAL_REF.fullmatch(value):
        raise ValueError(
            f"{field} must be one 1-128 character ASCII URL-safe segment"
        )
    return value


def bundle_inspection_body(
    args: argparse.Namespace,
    _stdin: TextIO,
) -> dict[str, Any]:
    request_id, trace_id = correlation(args)
    body: dict[str, Any] = {"requestId": request_id, "traceId": trace_id}
    if args.analytics_revision:
        body["expectedBundleRevision"] = args.analytics_revision
    return body


def artifact_describe_body(
    args: argparse.Namespace,
    _stdin: TextIO,
) -> dict[str, Any]:
    request_id, trace_id = correlation(args)
    return {
        "expectedBundleRevision": args.analytics_revision,
        "requestId": request_id,
        "traceId": trace_id,
    }


def model_dependency_resolve_body(
    args: argparse.Namespace,
    _stdin: TextIO,
) -> dict[str, Any]:
    request_id, trace_id = correlation(args)
    return {
        "namespace": args.analytics_model_namespace,
        "modelKind": args.analytics_model_kind,
        "modelName": args.analytics_model,
        "requestId": request_id,
        "traceId": trace_id,
    }


def render_body(args: argparse.Namespace, stdin: TextIO) -> dict[str, Any]:
    request_id, trace_id = correlation(args)
    return {
        "expectedBundleRevision": args.analytics_revision,
        "parameters": (
            read_json_payload(args.parameters, stdin) if args.parameters else {}
        ),
        "timezone": args.timezone,
        "locale": args.locale,
        "authority": {
            "provider": args.authority_provider,
            "reference": args.authority_reference,
        },
        "requestId": request_id,
        "traceId": trace_id,
    }


def correlation(args: argparse.Namespace) -> tuple[str, str]:
    request_id = args.request_id or f"foggy-cli-{uuid.uuid4()}"
    return request_id, args.trace_id or request_id


def no_body(_args: argparse.Namespace, _stdin: TextIO) -> None:
    return None
