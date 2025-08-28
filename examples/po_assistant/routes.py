from __future__ import annotations

import json
import os
from base64 import b64decode
from collections.abc import AsyncIterator
from typing import Any, Optional, cast

from fastapi import APIRouter, HTTPException
from starlette.responses import HTMLResponse, StreamingResponse

from .agent_summary import stream_summary_events, summarize_plan, summarize_plan_with_guardrail
from .airtable_client import AirtableClient, config_from_env
from .api_models import CandidateModel, LineCandidateModel, SyncRequest, SyncResponse
from .commit_models import PlanRequest, PlanResult
from .commit_planner import build_plan
from .extract_stub import extract_from_pdf_bytes
from .mcp_zapier import zapier_mcp_from_env
from .reconcile import reconcile

routes = APIRouter()

# Minimal in-memory cache of computed plans, keyed by idempotency_key.
# This enables task-oriented streaming by id.
_TASKS_CACHE: dict[str, PlanResult] = {}


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
                item_candidates=[
                    CandidateModel.model_validate(c.__dict__) for c in lr.item_candidates
                ],
                available_qty=lr.available_qty,
            )
            for lr in rec.lines
        ],
    )


@routes.post("/po/plan", response_model=PlanResult)
async def po_plan(req: PlanRequest) -> PlanResult:
    client = AirtableClient(config_from_env())
    plan = build_plan(client, req)
    try:
        _TASKS_CACHE[plan.idempotency_key] = plan
    except Exception:
        # Best-effort cache; ignore failures in cache write
        pass
    return plan


@routes.post("/po/plan/summary")
async def po_plan_summary(req: PlanRequest) -> dict[str, str]:
    client = AirtableClient(config_from_env())
    plan = build_plan(client, req)
    summary = await summarize_plan(plan)
    return {"summary": summary}


@routes.post("/po/plan/summary/stream")
async def po_plan_summary_stream(req: PlanRequest):
    """Stream semantic events for the summarizer for UI consumption (NDJSON)."""
    client = AirtableClient(config_from_env())
    plan = build_plan(client, req)

    async def _gen() -> AsyncIterator[str]:
        async for ev in stream_summary_events(plan, task_id=req.idempotency_key):
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


@routes.post("/po/plan/summary/guarded")
async def po_plan_summary_guarded(req: PlanRequest) -> dict[str, object]:
    """Run summary with output guardrail and return structured result."""
    client = AirtableClient(config_from_env())
    plan = build_plan(client, req)
    result = await summarize_plan_with_guardrail(plan)
    return result


@routes.get("/tasks.json")
async def list_tasks() -> list[dict[str, object]]:
    """Return a lightweight list of known tasks for the demo UI."""
    out: list[dict[str, object]] = []
    for tid, plan in _TASKS_CACHE.items():
        out.append(
            {
                "id": tid,
                "client": plan.purchase_order.fields.get("Clients"),
                "lines": len(plan.computed_lines),
            }
        )
    return out


@routes.get("/tasks/{task_id}/summary/stream")
async def task_summary_stream(task_id: str):
    """SSE stream of summarizer events for a specific task id (idempotency_key).

    Mirrors the semantics of /po/plan/summary/stream but returns text/event-stream
    for use with EventSource on the frontend.
    """
    plan = _TASKS_CACHE.get(task_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Task not found")

    async def _sse_gen() -> AsyncIterator[str]:
        async for ev in stream_summary_events(plan, task_id=task_id):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@routes.get("/ui")
async def ui_page() -> HTMLResponse:
    """Minimal UI to list tasks and stream summaries via EventSource when clicked."""
    html = """
<!DOCTYPE html>
<html>
  <head>
    <meta charset=\"utf-8\" />
    <title>PO Assistant – Tasks</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 20px; }
      .tasks { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
      .task { border: 1px solid #ddd; border-radius: 8px; padding: 12px; cursor: pointer; }
      .task h3 { margin: 0 0 6px 0; font-size: 16px; }
      .log { background: #fafafa; border: 1px solid #eee; padding: 8px; margin-top: 8px; height: 120px; overflow: auto; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
      .badge { background: #eef; color: #225; padding: 2px 6px; border-radius: 999px; font-size: 12px; }
    </style>
  </head>
  <body>
    <h2>Tasks</h2>
    <div id=\"tasks\" class=\"tasks\"></div>
    <script>
      async function loadTasks() {
        const res = await fetch('/tasks.json');
        const tasks = await res.json();
        const root = document.getElementById('tasks');
        root.innerHTML = '';
        tasks.forEach(t => {
          const tile = document.createElement('div');
          tile.className = 'task';
          tile.innerHTML = `<h3>Task ${t.id}</h3><div><span class="badge">lines: ${t.lines}</span></div><div class="log" id="log-${t.id}"></div>`;
          tile.addEventListener('click', () => openStream(t.id));
          root.appendChild(tile);
        });
      }

      const sources = {};
      function openStream(id) {
        // Close any existing
        if (sources[id]) { sources[id].close(); }
        const log = document.getElementById('log-' + id);
        log.textContent = '';
        const src = new EventSource(`/tasks/${id}/summary/stream`);
        sources[id] = src;
        src.onmessage = (evt) => {
          try {
            const obj = JSON.parse(evt.data);
            const payload = obj.payload || {};
            if (obj.type === 'raw_response_event' && payload.delta) {
              log.textContent += payload.delta;
            } else if (obj.type === 'agent_updated_stream_event') {
              log.textContent += `\n[Agent: ${payload.agent}]\n`;
            } else if (obj.type === 'run_item_stream_event' && payload.name) {
              log.textContent += `\n[event: ${payload.name}]\n`;
            }
            log.scrollTop = log.scrollHeight;
          } catch (e) {
            // ignore parse errors
          }
        };
        src.onerror = () => {
          src.close();
        };
      }

      loadTasks();
    </script>
  </body>
  </html>
    """
    return HTMLResponse(html)


@routes.get("/mcp/tools")
async def mcp_list_tools() -> dict[str, list[str]]:
    async with zapier_mcp_from_env() as server:
        tools = await server.list_tools()
        return {"tools": [t.name for t in tools]}


@routes.post("/airtable/schema")
async def airtable_schema_via_mcp(base_id: Optional[str] = None) -> dict[str, Any]:
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
                parsed = json.loads(text)
                return cast(dict[str, Any], parsed)
            except Exception:
                return {"raw": text}
        return {"raw": str(content)}
