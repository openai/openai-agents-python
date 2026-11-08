"""Exercise the maintained application ports through their public SDK APIs."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agents.classic import RunConfig, Runner
from agents.classic.testing import ScriptedModel, assistant_message, function_call
from agents.testing import ScriptedResponses, text, tool_call
from examples.codex.migration import classic as lookup_classic, native as lookup_native
from examples.codex.migration.advanced import classic, native
from examples.codex.migration.business import PROMPT, Application, Invoice


async def test_classic_application_preserves_business_contract_and_history(tmp_path):
    app, answer, model, specialist = await classic.run_example(tmp_path)
    assert answer.model_dump() == {
        "invoice_id": "INV-42",
        "amount_cents": 4200,
        "case_id": "case-1",
        "label": "atlas",
    }
    assert app.case_writes == ["INV-42"]
    assert len(specialist.calls) == 2
    assert "case-1" in json.dumps(model.calls[-1].input)
    assert "atlas" in json.dumps(model.calls[-1].input)
    app, _, resumed_model, specialist = await classic.run_example(tmp_path, resume=True)
    assert app.case_writes == []
    assert specialist.calls == ()
    assert "case-1" in json.dumps(resumed_model.calls[0].input)


async def test_native_port_runs_allowed_tools_blocks_denied_effect_and_restores_history(tmp_path):
    app, answer, peer, specialist = await native.run_example(tmp_path)
    assert answer.model_dump() == {
        "invoice_id": "INV-42",
        "amount_cents": 4200,
        "case_id": "case-1",
        "label": "atlas",
    }
    assert app.case_writes == ["INV-42"]
    assert peer.tool_output("allowed") == "case-1"
    assert "outside" in peer.tool_output("denied")
    assert json.loads(peer.tool_output("lookup")) == {
        "invoice_id": "INV-42",
        "amount_cents": 4200,
        "status": "paid",
    }
    assert json.loads(peer.tool_output("review")) == {
        "category": "billing",
        "recommendation": "Open a support case.",
    }
    assert specialist.tool_output("policy").startswith("Paid invoices")
    assert "case-1" in json.dumps(peer.requests[-1]["input"])
    assert "atlas" in json.dumps(peer.requests[-1]["input"])


async def test_reviewed_facts_start_fresh_native_history_without_repeating_old_effect(tmp_path):
    await classic.run_example(tmp_path / "classic")
    app, answer, peer = await native.continue_from_reviewed_facts(
        tmp_path / "classic" / "reviewed-facts.json", tmp_path / "native"
    )
    assert app.case_writes == []
    assert answer.case_id == "case-1"
    first_input = json.dumps(peer.requests[0]["input"])
    assert "Reviewed business facts" in first_input
    assert "function_call_output" not in first_input
    assert "case-1" in json.dumps(peer.requests[1]["input"])


@pytest.mark.skipif(
    not os.environ.get("MIGRATION_V0_PYTHON"), reason="Separate released-v0 environment required"
)
@pytest.mark.parametrize("via_classic", [False, True], ids=["direct", "through-classic"])
async def test_released_v0_to_native_with_explicit_state_boundary(tmp_path, via_classic):
    interpreter = os.environ["MIGRATION_V0_PYTHON"]
    root = Path(__file__).resolve().parents[3]
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    env["PYTHONPATH"] = str(root)
    subprocess.run(
        [
            interpreter,
            "-c",
            "from importlib.metadata import version; assert version('openai-agents') == '0.24.0'",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        timeout=30,
    )
    subprocess.run(
        [interpreter, "-m", "examples.codex.migration.advanced.v0", str(tmp_path / "v0")],
        cwd=tmp_path,
        env=env,
        check=True,
        timeout=60,
    )
    if via_classic:
        app, _, model, _ = await classic.run_example(tmp_path / "v0", resume=True)
        assert app.case_writes == []
        assert "case-1" in json.dumps(model.calls[0].input)
    app, answer, _ = await native.continue_from_reviewed_facts(
        tmp_path / "v0" / "reviewed-facts.json", tmp_path / "native"
    )
    assert app.case_writes == []
    assert answer.label == "atlas"


async def test_read_only_native_walkthrough_uses_application_context_and_typed_output():
    invoice = Invoice(invoice_id="INV-42", amount_cents=9900, status="pending")
    app = Application(invoice=invoice)
    async with ScriptedResponses(
        [tool_call("lookup_invoice", {"invoice_id": "INV-42"}, call_id="lookup")],
        [text(invoice.model_dump_json())],
    ) as peer:
        result = await lookup_native.build_agent(peer.provider).run(PROMPT, context=app)
    assert isinstance(result.final_output, Invoice)
    assert result.final_output == invoice
    assert json.loads(peer.tool_output("lookup")) == invoice.model_dump()
    assert "9900" in json.dumps(peer.requests[-1]["input"])
    assert app.invoice == invoice


async def test_read_only_classic_staging_keeps_the_same_tool_and_output_contract():
    invoice = Invoice(invoice_id="INV-42", amount_cents=9900, status="pending")
    model = ScriptedModel(
        [
            [function_call("lookup_invoice", {"invoice_id": "INV-42"}, call_id="lookup")],
            [assistant_message(invoice.model_dump_json())],
        ]
    )
    result = await Runner.run(
        lookup_classic.build_agent(model),
        PROMPT,
        context=Application(invoice=invoice),
        run_config=RunConfig(tracing_disabled=True),
    )
    assert isinstance(result.final_output, Invoice)
    assert result.final_output == invoice
    assert "9900" in json.dumps(model.calls[-1].input)
    model.assert_complete()


def test_read_only_lookup_rejects_an_invoice_outside_application_context():
    from examples.codex.migration.business import read_invoice

    with pytest.raises(PermissionError, match="outside"):
        read_invoice(Application(), "INV-private")


@pytest.mark.skipif(
    not os.environ.get("MIGRATION_V0_PYTHON"), reason="Separate released-v0 environment required"
)
def test_read_only_walkthrough_runs_on_actual_released_v0(tmp_path):
    root = Path(__file__).resolve().parents[3]
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    env["PYTHONPATH"] = str(root)
    subprocess.run(
        [os.environ["MIGRATION_V0_PYTHON"], "-c", RELEASED_LOOKUP_CHECK],
        cwd=tmp_path,
        env=env,
        check=True,
        timeout=60,
    )


RELEASED_LOOKUP_CHECK = """
import asyncio
import json
from importlib.metadata import version
from agents import RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message, function_call
from examples.codex.migration import v0
from examples.codex.migration.business import PROMPT, Application, Invoice

async def check():
    assert version("openai-agents") == "0.24.0"
    invoice = Invoice(invoice_id="INV-42", amount_cents=9900, status="pending")
    model = ScriptedModel([
        [function_call("lookup_invoice", {"invoice_id": "INV-42"}, call_id="lookup")],
        [assistant_message(invoice.model_dump_json())],
    ])
    result = await Runner.run(
        v0.build_agent(model), PROMPT, context=Application(invoice=invoice),
        run_config=RunConfig(tracing_disabled=True),
    )
    assert isinstance(result.final_output, Invoice)
    assert result.final_output == invoice
    assert "9900" in json.dumps(model.calls[-1].input)
    model.assert_complete()

asyncio.run(check())
"""
