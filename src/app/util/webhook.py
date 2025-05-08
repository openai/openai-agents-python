"""utils/webhook.py
A single, reusable helper for posting JSON payloads to Bubble‑workflow URLs.

Usage in your FastAPI code:

    from agents.utils.webhook import send_webhook
    
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
# Only allow POSTs to URLs that start with one of these roots (prevents exfiltration)
ALLOWED_ROOTS = os.getenv("BUBBLE_DOMAIN_ROOTS", "https://rgtnow.com").split(",")

# Optional default timeout (seconds) for outbound webhook calls.
HTTP_TIMEOUT = float(os.getenv("WEBHOOK_TIMEOUT", "10"))

# -----------------------------------------------------------------------------
# Public helper
# -----------------------------------------------------------------------------
async def send_webhook(target_url: str, payload: Mapping[str, Any]) -> None:
    """POST *payload* as JSON to *target_url*.

    Raises:
        ValueError: if *target_url* is outside the allowed Bubble domain roots.
        httpx.HTTPStatusError: if Bubble responds with an error status code.
    """
    if not any(target_url.startswith(root.strip()) for root in ALLOWED_ROOTS):
        raise ValueError(
            f"Refusing to POST to {target_url!r} — must begin with one of {ALLOWED_ROOTS!r}"
        )

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        print("=== Webhook Dispatch →", target_url, "===\n",
              json.dumps(payload, indent=2, default=str))
        resp = await client.post(target_url, json=payload)
        resp.raise_for_status()
        return None
