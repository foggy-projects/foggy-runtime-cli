from __future__ import annotations

import json
from typing import Any, TextIO


def read_json_payload(payload_ref: str, stdin: TextIO) -> dict[str, Any]:
    """Read one JSON object from a UTF-8 file or stdin."""
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
