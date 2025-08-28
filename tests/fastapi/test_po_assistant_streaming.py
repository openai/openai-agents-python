from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from agents import Agent, Runner
from examples.po_assistant.airtable_client import AirtableConfig
from examples.po_assistant.app import app
from examples.po_assistant.commit_models import (
    PlanLineComputed,
    PlanResult,
    PurchaseOrderCreate,
)


@pytest.mark.asyncio
async def test_streamed_summary_endpoint(monkeypatch):
    # Arrange a fake agent output by swapping the model on the summarizer agent at build time.
    # We'll override Runner.run_streamed to yield a minimal sequence of events.

    class DummyEvent:
        def __init__(
            self,
            type: str,
            data: Any | None = None,
            name: str | None = None,
            item: Any | None = None,
        ):
            self.type = type
            self.data = data
            self.name = name
            self.item = item

    class DummyData:
        def __init__(self, type: str, delta: str | None = None):
            self.type = type
            self.delta = delta

    class DummyStream:
        def __init__(self):
            pass

        async def stream_events(self):
            yield DummyEvent("agent_updated_stream_event", None)
            yield DummyEvent(
                "raw_response_event", DummyData("response.output_text.delta", delta="hello")
            )
            yield DummyEvent(
                "run_item_stream_event",
                None,
                name="message_output_created",
                item=type("I", (), {"type": "message_output_item"})(),
            )

    def fake_run_streamed(agent: Agent[Any], input: Any, **kwargs):
        return DummyStream()

    monkeypatch.setattr(Runner, "run_streamed", staticmethod(fake_run_streamed))

    # Stub Airtable env/config and planning to avoid external deps.
    def fake_config_from_env() -> AirtableConfig:
        return AirtableConfig(base_id="base", pat="pat")

    def fake_build_plan(_client, req):
        polines = [
            PlanLineComputed(
                product_option_id=req["lines"][0]["product_option_id"]
                if isinstance(req, dict)
                else req.lines[0].product_option_id,
                requested_qty=2,
                available_qty=5,
                reserve_now=2,
                backorder_qty=0,
            )
        ]
        return PlanResult(
            idempotency_key=req["idempotency_key"]
            if isinstance(req, dict)
            else req.idempotency_key,
            purchase_order=PurchaseOrderCreate(fields={"Clients": ["recClient"]}),
            computed_lines=polines,
            notes=None,
        )

    import examples.po_assistant.routes as routes_mod

    monkeypatch.setattr(routes_mod, "config_from_env", fake_config_from_env)
    monkeypatch.setattr(routes_mod, "build_plan", fake_build_plan)

    transport = ASGITransport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "idempotency_key": "abc123",
            "client_id": "recClient",
            "lines": [{"product_option_id": "opt1", "requested_qty": 2}],
        }
        async with ac.stream("POST", "/po/plan/summary/stream", json=payload) as r:
            assert r.status_code == 200
            body = (await r.aread()).decode("utf-8")
            lines = [json.loads(line) for line in body.splitlines() if line]
            # Validate we saw our three dummy events types.
            assert [ln["type"] for ln in lines] == [
                "agent_updated_stream_event",
                "raw_response_event",
                "run_item_stream_event",
            ]


@pytest.mark.asyncio
async def test_guarded_summary_pass_and_fail(monkeypatch):
    # Make Runner.run return a short unsafe summary first, then a safe one.
    class DummyResult:
        def __init__(self, text: str):
            self.final_output = text

    calls: list[str] = []

    async def fake_run(agent: Agent[Any], input: Any, **kwargs):
        calls.append("run")
        # Return output attached to result-like object
        return DummyResult("All-in 100% of capital")

    monkeypatch.setattr(Runner, "run", staticmethod(fake_run))

    # Stub Airtable env/config and planning to avoid external deps.
    def fake_config_from_env() -> AirtableConfig:
        return AirtableConfig(base_id="base", pat="pat")

    def fake_build_plan(_client, req):
        polines = [
            PlanLineComputed(
                product_option_id=req["lines"][0]["product_option_id"]
                if isinstance(req, dict)
                else req.lines[0].product_option_id,
                requested_qty=2,
                available_qty=5,
                reserve_now=2,
                backorder_qty=0,
            )
        ]
        return PlanResult(
            idempotency_key=req["idempotency_key"]
            if isinstance(req, dict)
            else req.idempotency_key,
            purchase_order=PurchaseOrderCreate(fields={"Clients": ["recClient"]}),
            computed_lines=polines,
            notes=None,
        )

    import examples.po_assistant.routes as routes_mod

    monkeypatch.setattr(routes_mod, "config_from_env", fake_config_from_env)
    monkeypatch.setattr(routes_mod, "build_plan", fake_build_plan)

    transport = ASGITransport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        payload = {
            "idempotency_key": "abc123",
            "client_id": "recClient",
            "lines": [{"product_option_id": "opt1", "requested_qty": 2}],
        }
        # Expect violations for unsafe language
        r1 = await ac.post("/po/plan/summary/guarded", json=payload)
        assert r1.status_code == 200
        j1 = r1.json()
        assert j1["ok"] is False or (
            "violations" in j1 and j1["violations"]
        )  # guardrail may return ok=False or violations
        assert "violations" in j1 and j1["violations"]

        # Now return a safe, longer message
        async def fake_run_safe(agent: Agent[Any], input: Any, **kwargs):
            return DummyResult(
                "Plan preview: reserve partial quantities; no guarantees or leverage."
            )

        monkeypatch.setattr(Runner, "run", staticmethod(fake_run_safe))

        r2 = await ac.post("/po/plan/summary/guarded", json=payload)
        assert r2.status_code == 200
        j2 = r2.json()
        assert j2["ok"] is True and isinstance(j2.get("summary"), str)
