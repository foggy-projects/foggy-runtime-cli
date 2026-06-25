from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


class RuntimeTransportError(Exception):
    """Raised when the Runtime API cannot be reached or returns invalid JSON."""


@dataclass(frozen=True)
class RuntimeApiClient:
    base_url: str
    namespace: str | None = None
    timeout: float = 30.0
    auth_code: str | None = None

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

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError as json_exc:
                raise RuntimeTransportError(f"HTTP {exc.code}: {payload}") from json_exc
            if isinstance(decoded, dict):
                return decoded
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
        return decoded

    def _normalized_base_url(self) -> str:
        return self.base_url.rstrip("/") + "/"


def path_quote(value: str) -> str:
    return quote(value, safe="")
