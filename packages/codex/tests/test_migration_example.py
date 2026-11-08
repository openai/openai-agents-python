"""Qualify the small invoice ports and the explicit boundary between their state."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agents import SessionState
from agents.classic import RunConfig, Runner, SQLiteSession
from agents.classic.testing import ScriptedModel, assistant_message, function_call
from agents.testing import ScriptedResponses, text, tool_call
from examples.codex.migration import classic, native
from examples.codex.migration.business import PROMPT, Application, Invoice


async def test_read_only_native_walkthrough_uses_application_context_and_typed_output():
    invoice = Invoice(invoice_id="INV-42", amount_cents=9900, status="pending")
    app = Application(invoice=invoice)
    async with ScriptedResponses(
        [tool_call("lookup_invoice", {"invoice_id": "INV-42"}, call_id="lookup")],
        [text(invoice.model_dump_json())],
    ) as peer:
        result = await native.build_agent(peer.provider).run(PROMPT, context=app)
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
        classic.build_agent(model),
        PROMPT,
        context=Application(invoice=invoice),
        run_config=RunConfig(tracing_disabled=True),
    )
    assert isinstance(result.final_output, Invoice)
    assert result.final_output == invoice
    assert "9900" in json.dumps(model.calls[-1].input)
    model.assert_complete()


async def test_native_lookup_rejects_an_invoice_outside_application_context():
    async with ScriptedResponses(
        [tool_call("lookup_invoice", {"invoice_id": "INV-private"}, call_id="denied")]
    ) as peer:
        with pytest.raises(PermissionError, match="outside"):
            await native.build_agent(peer.provider).run(PROMPT, context=Application())
    assert peer.tool_output("denied") is None


@pytest.mark.skipif(
    not os.environ.get("MIGRATION_V0_PYTHON"), reason="Separate released-v0 environment required"
)
@pytest.mark.parametrize("via_classic", [False, True], ids=["direct", "through-classic"])
async def test_released_v0_state_stays_with_its_engine(tmp_path, monkeypatch, via_classic):
    root = Path(__file__).resolve().parents[3]
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    env["PYTHONPATH"] = str(root)
    subprocess.run(
        [os.environ["MIGRATION_V0_PYTHON"], "-c", RELEASED_HISTORY_CHECK],
        cwd=tmp_path,
        env=env,
        check=True,
        timeout=60,
    )
    assert (tmp_path / "v0.sqlite").is_file()
    current = Invoice.model_validate_json((tmp_path / "facts.json").read_text(encoding="utf-8"))

    def unexpected_lookup(*args):
        pytest.fail("A completed old lookup must not run again during continuation or cutover.")

    monkeypatch.setattr(classic, "read_invoice", unexpected_lookup)
    monkeypatch.setattr(native, "read_invoice", unexpected_lookup)
    if via_classic:
        updated = current.model_copy(update={"status": "refunded"})
        model = ScriptedModel([[assistant_message(updated.model_dump_json())]])
        store = SQLiteSession("invoice", tmp_path / "v0.sqlite")
        try:
            result = await Runner.run(
                classic.build_agent(model),
                "The invoice status is now refunded. "
                "Return current fields from history without tools.",
                context=Application(),
                session=store,
                run_config=RunConfig(tracing_disabled=True),
            )
        finally:
            store.close()
        assert result.final_output == updated
        current = result.final_output
        history = json.dumps(model.calls[0].input)
        assert "v0-history-only" in history
        assert "function_call_output" in history and "4200" in history
        model.assert_complete()

    # Select the validated result; execution history stays with the engine that owns it.
    prompt = "Current invoice facts: " + current.model_dump_json() + " Repeat without tools."
    async with ScriptedResponses(
        [text(current.model_dump_json())], [text(current.model_dump_json())]
    ) as peer:
        agent = native.build_agent(peer.provider)
        async with agent.session(context=Application(invoice=current)) as session:
            assert not [item async for item in session.items()]
            turn = await session.submit(prompt)
            assert (await turn.get_result()).final_output == current
            checkpoint = tmp_path / "native.json"
            checkpoint.write_text((await session.save_state()).to_json(), encoding="utf-8")
        state = SessionState.from_json(checkpoint.read_text(encoding="utf-8"))
        async with agent.session(context=Application(invoice=current), state=state) as restored:
            turn = await restored.submit("Repeat the invoice facts without tools.")
            assert (await turn.get_result()).final_output == current
    first_input = json.dumps(peer.requests[0]["input"])
    assert current.status in first_input
    if via_classic:
        assert "refunded" in first_input and "paid" not in first_input
    assert "v0-history-only" not in first_input
    assert "function_call_output" not in first_input
    assert "Current invoice facts" in json.dumps(peer.requests[1]["input"])


RELEASED_HISTORY_CHECK = """
import asyncio
import json
from importlib.metadata import version
from pathlib import Path
from agents import RunConfig, Runner, SQLiteSession
from agents.testing import ScriptedModel, assistant_message, function_call
from examples.codex.migration import v0
from examples.codex.migration.business import PROMPT, Application, Invoice

async def check():
    assert version("openai-agents") == "0.24.0"
    invoice = Application().invoice
    model = ScriptedModel([
        [function_call("lookup_invoice", {"invoice_id": "INV-42"}, call_id="lookup")],
        [assistant_message(invoice.model_dump_json())],
    ])
    store = SQLiteSession("invoice", "v0.sqlite")
    try:
        result = await Runner.run(
            v0.build_agent(model), PROMPT + " v0-history-only", context=Application(),
            session=store, run_config=RunConfig(tracing_disabled=True),
        )
        assert isinstance(result.final_output, Invoice)
        assert result.final_output == invoice
        Path("facts.json").write_text(result.final_output.model_dump_json(), encoding="utf-8")
        outputs = [item for item in await store.get_items()
                   if item.get("type") == "function_call_output"]
        assert len(outputs) == 1
        assert outputs[0]["output"] == str(invoice)
        model.assert_complete()
    finally:
        store.close()

asyncio.run(check())
"""
