from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RuntimeTransportError(Exception):
    """Raised when the Runtime API cannot be reached or returns invalid JSON."""


class _OriginSafeRedirectHandler(HTTPRedirectHandler):
    """Do not forward data-plane credentials to a different origin."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            # Authorization is scoped to the exact data-plane request selected
            # before the redirect; never infer scope from a server Location.
            _remove_header_case_insensitive(redirected, "Authorization")
        if redirected is not None and _origin(req.full_url) != _origin(newurl):
            for header in (
                "X-Foggy-Runtime-Code",
                "Cookie",
                "Proxy-Authorization",
            ):
                _remove_header_case_insensitive(redirected, header)
        return redirected


@dataclass(frozen=True)
class RuntimeApiClient:
    base_url: str
    namespace: str | None = None
    timeout: float = 30.0
    auth_code: str | None = None
    authorization: str | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = urljoin(self._normalized_base_url(), path.lstrip("/"))
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.namespace:
            headers["X-NS"] = self.namespace
        if self.auth_code:
            headers["X-Foggy-Runtime-Code"] = self.auth_code
        if self.authorization and is_data_plane_path(method, path):
            headers["Authorization"] = self.authorization

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with _open_request(request, self.timeout) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError as json_exc:
                safe_payload = _redact_text(
                    payload,
                    self.auth_code,
                    self.authorization,
                )
                raise RuntimeTransportError(f"HTTP {exc.code}: {safe_payload}") from json_exc
            if isinstance(decoded, dict):
                return _redact_value(
                    decoded,
                    self.auth_code,
                    self.authorization,
                )
            raise RuntimeTransportError(f"HTTP {exc.code}: response is not a JSON object")
        except URLError as exc:
            raise RuntimeTransportError(str(exc.reason)) from exc
        except OSError as exc:
            raise RuntimeTransportError(str(exc)) from exc

        if not payload:
            return {}
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeTransportError(f"Invalid JSON response from {url}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeTransportError(f"Response from {url} is not a JSON object")
        return _redact_value(decoded, self.auth_code, self.authorization)

    def _normalized_base_url(self) -> str:
        return self.base_url.rstrip("/") + "/"


def path_quote(value: str) -> str:
    return quote(value, safe="")


def _open_request(request: Request, timeout: float) -> Any:
    return build_opener(_OriginSafeRedirectHandler()).open(request, timeout=timeout)


def is_data_plane_path(method: str, path: str) -> bool:
    normalized = "/" + path.lstrip("/")
    upper_method = method.upper()
    if normalized == "/api/v1/models":
        return upper_method == "GET"
    if normalized.startswith("/api/v1/models/") and normalized.endswith("/describe"):
        return True
    return normalized.startswith((
        "/api/v1/query/",
        "/api/v1/compose/",
        "/api/v1/members/",
        "/api/v1/bundles/",
        "/jdbc-model/dimension/",
    )) and (
        not normalized.startswith("/api/v1/bundles/")
        or "/reports/" in normalized
        or "/dashboards/" in normalized
    )


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _remove_header_case_insensitive(request: Request, name: str) -> None:
    expected = name.lower()
    for collection in (request.headers, request.unredirected_hdrs):
        for actual in tuple(collection):
            if actual.lower() == expected:
                request.remove_header(actual)


def _redact_text(value: str, *credentials: str | None) -> str:
    redacted = value
    for credential in credentials:
        if credential:
            redacted = redacted.replace(credential, "[REDACTED]")
    return redacted


def _redact_value(value: Any, *credentials: str | None) -> Any:
    if isinstance(value, str):
        return _redact_text(value, *credentials)
    if isinstance(value, dict):
        return {
            key: _redact_value(item, *credentials)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, *credentials) for item in value]
    return value
