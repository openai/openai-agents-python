"""utils/webhook.py
A single, reusable helper for posting JSON payloads to Bubble‑workflow URLs.

Usage in your FastAPI code:

    from utils.webhook import send_webhook
    
    url = TASK_URL_MAP[task_type]      # looked up from env‑vars
    await send_webhook(url, flattened_payload)

You keep *all* Bubble‑specific routing logic (task_type → URL) in your
FastAPI service, while this helper focuses solely on safe, idempotent
HTTP posting and basic allow‑list protection.
"""
from __future__ import annotations

import os
import json
import httpx
from typing import Any, Mapping

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
# Only allow POSTs to URLs that start with this root (prevents exfiltration
# if someone accidentally passes a malicious URL in the incoming payload).
ALLOWED_ROOT = os.getenv("BUBBLE_DOMAIN_ROOT", "https://rgtnow.com")

# Optional default timeout (seconds) for outbound webhook calls.
HTTP_TIMEOUT = float(os.getenv("WEBHOOK_TIMEOUT", "10"))

# -----------------------------------------------------------------------------
# Public helper
# -----------------------------------------------------------------------------
async def send_webhook(target_url: str, payload: Mapping[str, Any]) -> None:
    """POST *payload* as JSON to *target_url*.

    Raises:
        ValueError: if *target_url* is outside the allowed Bubble domain root.
        httpx.HTTPStatusError: if Bubble responds with an error status code.
    """
    if not target_url.startswith(ALLOWED_ROOT):
        raise ValueError(
            f"Refusing to POST to {target_url!r} — must begin with {ALLOWED_ROOT!r}"
        )

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        print("=== Webhook Dispatch →", target_url, "===\n",
              json.dumps(payload, indent=2, default=str))
        resp = await client.post(target_url, json=payload)
        resp.raise_for_status()  # bubble up 4xx/5xx to caller for logging
        # We ignore / return the response body so the caller may log it if needed.
        return None
