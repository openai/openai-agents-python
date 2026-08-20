"""Shared verification core for the delivery-confirmed demo.

This module is imported by both the MCP server (verify_claim tool) and the
agent's finalize tool so enforcement is deterministic and framework-independent.
In production, swap ``check_delivery`` for a real carrier / delivery system API.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

MOCK_DELIVERIES = {
    "ORD-1001": {
        "status": "delivered",
        "delivered_at": "2026-08-20T12:00:00Z",
        "order_id": "ORD-1001",
    },
    "ORD-1002": {"status": "in_transit", "delivered_at": "", "order_id": "ORD-1002"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_delivery(reference_id: str) -> dict:
    """Look up a delivery reference (mock). Returns found/status/delivered_at."""
    record = MOCK_DELIVERIES.get(str(reference_id or "").strip().upper())
    if not record:
        return {"found": False, "status": "not_found"}
    return {
        "found": True,
        "status": record["status"],
        "delivered_at": record["delivered_at"],
        "order_id": record["order_id"],
    }


def plausible_gps(lat, lng) -> bool:
    try:
        return abs(float(lat)) <= 90 and abs(float(lng)) <= 180
    except (TypeError, ValueError):
        return False


def valid_image_hash(value) -> bool:
    return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(value or "").strip().lower()))


def hash_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def run_delivery_verification(evidence: dict) -> dict:
    """Run the six-step verification chain for a delivery claim."""
    content = evidence.get("content") or {}
    time_dim = evidence.get("time") or {}
    location = evidence.get("location") or {}
    process = evidence.get("process") or {}
    ref = content.get("referenceId")
    captured_at = time_dim.get("capturedAt")
    gps = location.get("gps") or {}
    hashes = content.get("imageHashes") or []
    source = process.get("source") or "unknown"

    checks = []
    checks.append(
        {
            "name": "capture",
            "passed": bool(ref and captured_at),
            "detail": f"order {ref} captured at {captured_at}"
            if ref and captured_at
            else "referenceId and capturedAt are required",
        }
    )
    checks.append(
        {
            "name": "integrity",
            "passed": len(hashes) > 0 and all(valid_image_hash(h) for h in hashes),
            "detail": f"{len(hashes)} image hash(es) present",
        }
    )
    checks.append(
        {
            "name": "authenticity",
            "passed": source in ("in_app_capture", "unknown"),
            "detail": f"capture source: {source}",
        }
    )

    delivery = check_delivery(ref or "")
    consistency_ok = False
    detail = "delivery reference not found"
    if delivery["found"]:
        if delivery["status"] == "delivered":
            try:
                captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
                delivered = datetime.fromisoformat(delivery["delivered_at"].replace("Z", "+00:00"))
                consistency_ok = abs((captured - delivered).total_seconds()) < 3600
                detail = (
                    f"order {ref} delivered, capture within 1h"
                    if consistency_ok
                    else "capture time far from delivery time"
                )
            except ValueError:
                detail = "could not parse timestamps"
        else:
            detail = f"order {ref} is {delivery['status']}, not delivered"
    checks.append({"name": "consistency", "passed": consistency_ok, "detail": detail})

    gps_ok = plausible_gps(gps.get("lat"), gps.get("lng")) if gps else False
    checks.append(
        {
            "name": "judgment",
            "passed": gps_ok,
            "detail": f"gps {gps} plausible"
            if gps_ok
            else "gps coordinates implausible or missing",
        }
    )
    checks.append({"name": "anchor", "passed": True, "detail": "receipt issued by engine"})

    missing = []
    if not captured_at:
        missing.append("time")
    if not gps:
        missing.append("location")
    if not ref:
        missing.append("content")

    failed = [c for c in checks if not c.get("passed")]
    if missing:
        verdict = "resubmit"
    elif failed:
        verdict = "fail"
    else:
        verdict = "pass"

    record = {
        "verificationId": f"v_{len(_RECEIPTS) + 1}",
        "claimType": "delivery_confirmed",
        "policy": {"policyId": "delivery_confirmed", "version": 1, "level": "L3"},
        "status": {"pass": "passed", "fail": "failed", "resubmit": "resubmission"}[verdict],
        "verdict": verdict,
        "checks": checks,
        "missing": missing,
        "evidence": evidence,
    }
    if verdict in ("pass", "fail"):
        receipt = {
            "schemaVersion": 1,
            "receiptId": f"r_{len(_RECEIPTS) + 1}",
            "verificationId": record["verificationId"],
            "claimHash": hash_json({"claimType": "delivery_confirmed"}),
            "evidenceHash": hash_json(evidence),
            "checksHash": hash_json(checks),
            "verdict": verdict,
            "signer": "ai2human-verify",
            "issuedAt": _now(),
        }
        record["receipt"] = receipt
        _RECEIPTS[receipt["receiptId"]] = record
    return record


_RECEIPTS: dict[str, dict] = {}
