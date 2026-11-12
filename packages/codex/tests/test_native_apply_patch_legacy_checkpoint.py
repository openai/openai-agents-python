"""Existing settled editor receipts and portable approval upgrades."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agents import Agent, NativeProvider, SessionState, apply_patch_tool, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_apply_patch import patch_call
from tests.test_native_manual_tools import next_request


def selected_model():
    catalog = json.loads(
        (Path(__file__).parent / "fixtures/request-control-models.json").read_text()
    )
    return next(row["slug"] for row in catalog["models"] if row.get("apply_patch_tool_type"))


async def legacy_stage(stage, endpoint, directory):
    root = Path(directory)
    edited = asyncio.Event()

    class Editor:
        def create_file(self, operation):
            raise AssertionError("Unexpected create")

        def delete_file(self, operation):
            raise AssertionError("Unexpected delete")

        def update_file(self, operation):
            assert stage == "capture", "Settled patch acquired another execution grant"
            with (root / "effects.txt").open("a") as file:
                file.write("patch\n")
            edited.set()
            return "recorded once"

    @function_tool(needs_approval=True)
    def confirm(value: str) -> str:
        assert stage == "restore"
        with (root / "effects.txt").open("a") as file:
            file.write("confirm\n")
        return value

    async with NativeProvider(base_url=endpoint, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use both configured tools.",
            model=selected_model(),
            provider=provider,
            tools=[apply_patch_tool(Editor()), confirm],
        )
        if stage == "capture":
            async with agent.session(max_function_concurrency=2) as session:
                await session.submit("edit, then confirm")
                request = await next_request(session.interactions(), confirm)
                assert request.call_id == "legacy-confirm"
                await edited.wait()
                async with timeout(10):
                    while not any(
                        item.get("type") == "function_call_output"
                        and item.get("call_id") == "native-patch"
                        for item in [entry async for entry in session.items()]
                    ):
                        await asyncio.sleep(0)
                saved = await session.suspend()
                (root / "legacy.json").write_text(saved.to_json())
        elif stage == "refuse":
            with pytest.raises((RuntimeError, ValueError)):
                saved = SessionState.from_json((root / "legacy.json").read_text())
                async with agent.session(state=saved):
                    raise AssertionError("Invalid legacy frame acquired an execution owner")
        else:
            saved = SessionState.from_json((root / "legacy.json").read_text())
            async with agent.session(state=saved) as session:
                request = await next_request(session.interactions(), confirm)
                assert request.call_id == "legacy-confirm"
                await request.approve()
                assert (await session.restored_turn.get_result()).final_output == "done"
                (root / "completed.json").write_text((await session.save_state()).to_json())


async def run_legacy_stage(stage, endpoint, directory, sdk):
    environment = dict(os.environ)
    paths = [str(Path(__file__).parents[1]), *sys.path]
    if sdk:
        paths.insert(0, sdk)
        environment["LD_LIBRARY_PATH"] = str(Path(sdk) / "openai_agents.libs")
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import asyncio,sys; "
        "from tests.test_native_apply_patch_legacy_checkpoint import legacy_stage; "
        "asyncio.run(legacy_stage(*sys.argv[1:]))",
        stage,
        endpoint,
        str(directory),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, output.decode()


@pytest.mark.parametrize(
    "case",
    ["v5", "unowned", "missing-output", "wrong-turn", "cross-kind", "ordinary-outcome"],
)
async def test_legacy_settled_patch_keeps_pending_function_grant(responses, tmp_path, case):
    responses.respond(
        patch_call({"type": "update_file", "path": "legacy.txt", "diff": "exact settled edit"}),
        function_call("confirm", {"value": "confirmed"}, "legacy-confirm"),
    )
    valid = case == "v5"
    if valid:
        responses.respond(assistant_output("done"))
    old_sdk = os.environ.get("LEGACY_ACCEPTED_SDK")
    candidate_sdk = os.environ.get("LEGACY_CANDIDATE_SDK")
    await run_legacy_stage("capture", responses.url, tmp_path, old_sdk)
    path = tmp_path / "legacy.json"
    raw = json.loads(path.read_text())
    (tmp_path / "producer-original.json").write_text(json.dumps(raw, indent=2))
    frame = raw["native_continuation"]
    legacy = json.loads(
        (Path(__file__).parent / "fixtures/legacy-settled-apply-patch-accounting.json").read_text()
    )
    # A real accepted writer supplied this accounting fixture. CI retains its
    # current canonical history, response anchor and fresh provider endpoint.
    if old_sdk:
        assert raw["format_version"] == legacy["public_format_version"]
        assert frame["seen_call_ids"] == legacy["seen_call_ids"]
        assert frame["reply_order"] == legacy["reply_order"]
        assert frame["tool_outcomes"]["call_ids"] == legacy["tool_outcomes"]["call_ids"]
        assert frame["tool_outcomes"]["outcomes"] == legacy["tool_outcomes"]["outcomes"]
    else:
        assert "native-patch" in frame["seen_call_ids"]
        assert "native-patch" in frame["tool_outcomes"]["call_ids"]
    frame["seen_call_ids"] = legacy["seen_call_ids"]
    assert frame["reply_order"] == legacy["reply_order"]
    outcomes = frame["tool_outcomes"]
    outcomes.update(legacy["tool_outcomes"])
    call = next(
        item for item in raw["history"] if item.get("payload", {}).get("type") == "apply_patch_call"
    )
    output = next(
        item
        for item in raw["history"]
        if item.get("payload", {}).get("type") == "apply_patch_call_output"
    )
    if case == "unowned":
        call["metadata"].pop("sdk_application_tool")
    elif case == "missing-output":
        raw["history"].remove(output)
    elif case == "wrong-turn":
        output["payload"]["internal_chat_message_metadata_passthrough"]["turn_id"] = "another-turn"
    elif case == "cross-kind":
        output["payload"]["type"] = "function_call_output"
        output["payload"].pop("id", None)
        output["payload"].pop("status", None)
    elif case == "ordinary-outcome":
        outcomes["call_ids"] = []
    path.write_text(json.dumps(raw))
    await run_legacy_stage("restore" if valid else "refuse", responses.url, tmp_path, candidate_sdk)
    assert (tmp_path / "effects.txt").read_text().splitlines() == (
        ["patch", "confirm"] if valid else ["patch"]
    )
    assert len(responses.requests) == (2 if valid else 1)
