from __future__ import annotations

from base64 import b64decode
import json
import os
from typing import Optional

from fastapi import APIRouter

from .api_models import CandidateModel, LineCandidateModel, SyncRequest, SyncResponse
from .airtable_client import config_from_env, AirtableClient
from .commit_models import PlanRequest, PlanResult
from .commit_planner import build_plan
from .agent_summary import summarize_plan
from .mcp_zapier import zapier_mcp_from_env
from agents import Agent, Runner
from .extract_stub import extract_from_pdf_bytes
from .reconcile import reconcile


routes = APIRouter()


@routes.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@routes.get("/schema/overview")
async def schema_overview() -> dict[str, list[str]]:
    # Static for now; later, load from data/airtable_*_schema.json using schema_loader.
    return {
        "tables": [
            "Users",
            "Clients",
            "Projects",
            "Tasks",
            "Invoices",
            "Purchase Orders",
            "Product Table",
            "Product Options",
            "Inventory",
        ]
    }


@routes.post("/po/sync", response_model=SyncResponse)
async def po_sync(req: SyncRequest) -> SyncResponse:
    pdf_bytes: Optional[bytes] = None
    if req.po_bytes_base64:
        pdf_bytes = b64decode(req.po_bytes_base64)
    # Use stub extractor for now.
    extract = extract_from_pdf_bytes(pdf_bytes or b"")
    rec = reconcile(extract)
    return SyncResponse(
        company_candidates=[
            CandidateModel.model_validate(c.__dict__) for c in rec.company_candidates
        ],
        lines=[
            LineCandidateModel(
                raw_description=lr.line.raw_description,
                quantity=lr.line.quantity,
                item_candidates=[CandidateModel.model_validate(c.__dict__) for c in lr.item_candidates],
                available_qty=lr.available_qty,
            )
            for lr in rec.lines
        ],
    )


@routes.post("/po/plan", response_model=PlanResult)
async def po_plan(req: PlanRequest) -> PlanResult:
    client = AirtableClient(config_from_env())
    plan = build_plan(client, req)
    return plan


@routes.post("/po/plan/summary")
async def po_plan_summary(req: PlanRequest) -> dict[str, str]:
    client = AirtableClient(config_from_env())
    plan = build_plan(client, req)
    # Include Zapier MCP server so the agent can use Airtable tools exposed via MCP if needed later.
    try:
        mcp_server = zapier_mcp_from_env()
        summary = await summarize_plan(plan)
    except Exception:
        # Fallback to summary without MCP if env not set.
        summary = await summarize_plan(plan)
    return {"summary": summary}


@routes.get("/mcp/tools")
async def mcp_list_tools() -> dict[str, list[str]]:
    async with zapier_mcp_from_env() as server:
        tools = await server.list_tools()
        return {"tools": [t.name for t in tools]}


@routes.post("/airtable/schema")
async def airtable_schema_via_mcp(base_id: Optional[str] = None) -> dict:
    base_id = base_id or os.getenv("AIRTABLE_BASE_ID", "appIQpYvYVDlVtAPS")
    async with zapier_mcp_from_env() as server:
        res = await server.call_tool(
            "airtable_get_base_schema",
            {
                "instructions": f"Return the raw schema JSON for base id {base_id}.",
                "baseId": base_id,
            },
        )
        # The result content is typically a list of TextContent entries with JSON string inside.
        content = res.content
        text = None
        if isinstance(content, list) and content:
            first = content[0]
            # TextContent has attribute 'text'; duck-type it
            text = getattr(first, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except Exception:
                return {"raw": text}
        return {"raw": str(content)}


