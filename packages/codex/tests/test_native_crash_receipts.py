"""Process loss does not turn an external effect into an accepted native receipt."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from agents import Agent, NativeProvider, ToolContext, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


async def _crash_child(url: str, directory: Path, boundary: str) -> None:
    entered = asyncio.Event()
    identity: dict[str, str] = {}

    async def stop(context: ToolContext[None]) -> None:
        identity.update(
            call_id=context.call_id,
            turn_id=context.submission.turn_id,
            session_id=context.submission.session_id,
        )
        entered.set()
        await asyncio.Event().wait()

    async def result(context: ToolContext[None], output: str) -> str:
        if context.call_id == "uncertain-call" and boundary == "before_receipt":
            await stop(context)
        return output

    @function_tool
    async def record(context: ToolContext[None], value: str) -> str:
        if context.call_id == "uncertain-call" and boundary == "before_effect":
            await stop(context)
        with (directory / "effects.jsonl").open("a") as effect:
            effect.write(json.dumps({"call_id": context.call_id, "value": value}) + "\n")
            effect.flush()
            os.fsync(effect.fileno())
        if context.call_id == "uncertain-call" and boundary == "after_effect":
            await stop(context)
        return await result(context, value)

    agent = Agent(
        instructions="Use record when requested.",
        tools=[record],
        provider=NativeProvider(base_url=url, api_key="synthetic-crash-test"),
    )
    async with agent.session() as session:
        completed = await session.submit("record accepted")
        assert (await (completed).get_result()).final_output == "accepted"
        # The only restorable state is taken while idle, before the uncertain call.
        checkpoint = await session.save_state()
        with (directory / "checkpoint.json").open("w") as saved:
            saved.write(checkpoint.to_json())
            saved.flush()
            os.fsync(saved.fileno())
        accepted_items = [entry async for entry in session.items()]
        pending = await session.submit("record uncertain")
        await entered.wait()
        assert pending.turn_id == identity["turn_id"]
        try:
            await session.save_state()
        except RuntimeError as error:
            checkpoint_error = str(error)
        else:
            raise AssertionError("Active checkpoint unexpectedly succeeded")
        # Read the native store through the public API; never fabricate a receipt.
        observed = {
            "boundary": boundary,
            "identity": identity,
            "accepted_items": accepted_items,
            "pending_items": [entry async for entry in session.items()],
            "checkpoint_error": checkpoint_error,
        }
        print(json.dumps(observed), flush=True)
        await asyncio.Event().wait()


def _receipts(items):
    return [item for item in items if item["type"] == "function_call_output"]


def _stored_receipts(state: SessionState):
    return [
        record
        for record in json.loads(state.to_json())["history"]
        if record["type"] == "response_item" and record["payload"]["type"] == "function_call_output"
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="Uses Linux SIGKILL process-loss semantics")
@pytest.mark.parametrize("boundary", ["before_effect", "after_effect", "before_receipt"])
async def test_process_loss_keeps_effects_distinct_from_native_receipts(
    responses, tmp_path, boundary
):
    responses.respond(function_call("record", {"value": "accepted"}, "accepted-call"))
    responses.respond(assistant_output("accepted"))
    responses.respond(function_call("record", {"value": "uncertain"}, "uncertain-call"))
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith(("OPENAI_", "AZURE_OPENAI_")):
            environment.pop(name)
    package = Path(__file__).resolve().parents[1]
    repository = package.parents[1]
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(repository / "src"), str(package / "src"), str(package))
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # SIGKILL cannot run TemporaryDirectory cleanup; keep the child's files in our fixture.
    native_temporary = tmp_path / "native"
    native_temporary.mkdir()
    environment["TMPDIR"] = str(native_temporary)
    async with timeout(40):
        with (tmp_path / "child.stderr").open("wb") as stderr:
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-B",
                "-m",
                "tests.test_native_crash_receipts",
                responses.url,
                str(tmp_path),
                boundary,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
            )
            try:
                assert child.stdout is not None
                line = await child.stdout.readline()
                assert line, (tmp_path / "child.stderr").read_text()
                observed = json.loads(line)
                assert "save_state requires an idle Session" in observed["checkpoint_error"]
                prior_receipts = _receipts(observed["accepted_items"])
                assert [item["call_id"] for item in prior_receipts] == ["accepted-call"]
                assert _receipts(observed["pending_items"]) == prior_receipts
                pending_calls = [
                    item
                    for item in observed["pending_items"]
                    if item["type"] == "function_call" and item["call_id"] == "uncertain-call"
                ]
                assert len(pending_calls) == 1
                assert pending_calls[0]["turn_id"] == observed["identity"]["turn_id"]
                assert pending_calls[0]["status"] == "in_progress"
                child.kill()
                assert await child.wait() == -signal.SIGKILL
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()

        effects = [
            json.loads(line) for line in (tmp_path / "effects.jsonl").read_text().splitlines()
        ]
        expected_ids = ["accepted-call"]
        if boundary != "before_effect":
            expected_ids.append("uncertain-call")
        assert [effect["call_id"] for effect in effects] == expected_ids
        assert len(responses.requests) == 3
        resumed_callbacks: list[str] = []

        @function_tool
        async def record(context: ToolContext[None], value: str) -> str:
            resumed_callbacks.append(context.call_id)
            return value

        saved = SessionState.from_json((tmp_path / "checkpoint.json").read_text())
        stored_receipts = _stored_receipts(saved)
        assert len(stored_receipts) == 1
        assert stored_receipts[0]["payload"]["call_id"] == "accepted-call"
        # The pinned Core omits absent history metadata. client_authored describes
        # app-supplied developer messages, not whether a tool effect was accepted.
        metadata = stored_receipts[0].get("metadata")
        assert metadata is None or metadata.get("client_authored", False) is False
        agent = Agent(
            instructions="Use record when requested.",
            tools=[record],
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-crash-test"),
        )
        async with agent.session(state=saved) as restored:
            assert restored.id == observed["identity"]["session_id"]
            assert [entry async for entry in restored.items()] == observed["accepted_items"]
            assert _stored_receipts(await restored.save_state()) == stored_receipts
            assert len(responses.requests) == 3
            assert resumed_callbacks == []
            responses.respond(assistant_output("continued without replay"))
            continued = await restored.submit("Continue without calling record.")
            settled = await (continued).get_result()
            assert settled.final_output == "continued without replay"
            assert await (continued).get_result() is settled
            restored_items = [entry async for entry in restored.items()]
            assert _receipts(restored_items) == prior_receipts
            assert _stored_receipts(await restored.save_state()) == stored_receipts
            assert resumed_callbacks == []
            assert len(responses.requests) == 4
            assert "uncertain-call" not in json.dumps(responses.requests[-1]["input"])
        assert [
            json.loads(line) for line in (tmp_path / "effects.jsonl").read_text().splitlines()
        ] == effects
        (tmp_path / "evidence.json").write_text(
            json.dumps(
                {**observed, "external_effects": effects, "restored_items": restored_items},
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(_crash_child(sys.argv[1], Path(sys.argv[2]), sys.argv[3]))
