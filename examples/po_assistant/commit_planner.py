from __future__ import annotations

from typing import Dict, List, Optional

from .airtable_client import AirtableClient
from .commit_models import (
    PlanLineComputed,
    PlanRequest,
    PlanResult,
    PurchaseOrderCreate,
)


def _fetch_on_hand_for_options(
    client: AirtableClient, option_ids: List[str]
) -> Dict[str, Optional[int]]:
    # Minimal pass: fetch Product Options by IDs, get On Hand Rollup/Stock if present via fields.
    # Fallback: None when unavailable.
    # For now, we list all and filter locally to minimize extra endpoints.
    records = client.list_records("Product Options")
    on_hand: Dict[str, Optional[int]] = {}
    idx = {r.get("id"): r for r in records}
    for oid in option_ids:
        r = idx.get(oid)
        val: Optional[int] = None
        if r:
            fields = r.get("fields", {})
            # Common names from schema: "On Hand (from Product Table)", rollups, or "Stock".
            if isinstance(fields.get("On Hand (from Product Table)"), list):
                arr = fields["On Hand (from Product Table)"]
                if arr and isinstance(arr[0], (int, float)):
                    val = int(arr[0])
            elif isinstance(fields.get("Stock"), (int, float)):
                val = int(fields["Stock"])  # sometimes a formula number
        on_hand[oid] = val
    return on_hand


def build_plan(client: AirtableClient, req: PlanRequest) -> PlanResult:
    option_ids = [line.product_option_id for line in req.lines]
    on_hand = _fetch_on_hand_for_options(client, option_ids)

    computed: List[PlanLineComputed] = []
    for line in req.lines:
        available = on_hand.get(line.product_option_id)
        reserve = line.reserve_now
        if reserve is None:
            if available is None:
                reserve = 0
            else:
                reserve = max(0, min(available, line.requested_qty))
        backorder = max(0, line.requested_qty - reserve)
        computed.append(
            PlanLineComputed(
                product_option_id=line.product_option_id,
                requested_qty=line.requested_qty,
                available_qty=available,
                reserve_now=reserve,
                backorder_qty=backorder,
            )
        )

    # Build a minimal PO create payload (no write yet; for preview only).
    fields: Dict[str, object] = {}
    if req.po_number:
        fields["PO Number"] = req.po_number
    fields["Clients"] = [req.client_id]

    po = PurchaseOrderCreate(fields=fields)
    return PlanResult(
        idempotency_key=req.idempotency_key,
        purchase_order=po,
        computed_lines=computed,
        notes="Preview only. Nothing written until commit.",
    )
