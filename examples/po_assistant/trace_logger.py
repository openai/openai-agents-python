from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

REDACT_KEYS = {
    "authorization",
    "api_key",
    "openai_api_key",
    "airtable_pat",
    "zapier_mcp_key",
}


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > 8:
            return value[:4] + "…" + value[-2:]
        return "***"
    return "***"


def redact_payload(data: Any) -> Any:
    """Recursively redact likely secrets from a JSON-serializable payload."""
    try:
        if isinstance(data, dict):
            out: Dict[str, Any] = {}
            for k, v in data.items():
                key_lower = str(k).lower()
                if (
                    key_lower in REDACT_KEYS
                    or key_lower.endswith("_key")
                    or key_lower.endswith("_secret")
                ):
                    out[k] = _redact_value(v)
                else:
                    out[k] = redact_payload(v)
            return out
        if isinstance(data, list):
            return [redact_payload(v) for v in data]
        return data
    except Exception:
        # Best-effort redaction; never block logging.
        return "<unserializable>"


class TraceLogger:
    def __init__(self, task_id: str) -> None:
        # Store traces under repo_root/logs/po_assistant_traces/{task_id}.jsonl.
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        self._dir = os.path.join(root, "logs", "po_assistant_traces")
        os.makedirs(self._dir, exist_ok=True)
        # Ensure filename is safe.
        safe = "".join(ch for ch in task_id if ch.isalnum() or ch in ("-", "_")) or "unnamed"
        self._path = os.path.join(self._dir, f"{safe}.jsonl")

    def log(self, event_type: str, payload: Any | None = None) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "payload": redact_payload(payload) if payload is not None else None,
        }
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # Never raise from logging.
            pass
