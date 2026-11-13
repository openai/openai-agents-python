import json
import os

import pytest

from agents import Agent, SessionState
from agents.classic.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxClient,
    UnixLocalSandboxClientOptions,
)
from agents.sandbox import LocalSnapshotSpec, Manifest, SandboxMemoryOptions, sandbox_environment
from agents.testing import ScriptedResponses, text
from tests._asyncio import timeout
from tests.test_native_sandbox_memory import _workspace_gateway

pytestmark = pytest.mark.skipif(os.name == "nt", reason="UnixLocal memory fixture")


@pytest.mark.parametrize(
    ("trusted", "capture"), [(False, "save_state"), (False, "fork"), (True, "suspend_workspace")]
)
async def test_memory_retains_prior_admission_only_with_authenticated_restore(
    tmp_path, monkeypatch, trusted, capture
):
    workspace = tmp_path / "workspace"
    prior = "SYNTHETIC_PRIOR_SOURCE"
    fresh = "SYNTHETIC_FRESH_SOURCE"
    extract = {
        "rollout_slug": "restore",
        "rollout_summary": "Remember source.",
        "raw_memory": "Remember source.",
    }
    consolidate = {"summary": "Remember source.", "memory": "Remember source."}
    replies = [[text("Prior answer.")]]
    if trusted:
        replies += [[text(json.dumps(extract))], [text(json.dumps(consolidate))]]
    replies += [
        [text("Fresh answer.")],
        [text(json.dumps(extract))],
        [text(json.dumps(consolidate))],
    ]
    async with (
        timeout(40),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(*replies) as peer,
    ):

        def environment(saved=None):
            return sandbox_environment(
                UnixLocalSandboxClient(),
                manifest=Manifest(root=str(workspace)),
                options=UnixLocalSandboxClientOptions(),
                connect=connect,
                authorized_resume_state=UnixLocalSandboxClient().deserialize_session_state(
                    saved.workspace["provider_state"]
                )
                if trusted and saved is not None
                else None,
                authorized_resume_checkpoint=saved if trusted else None,
                managed_workspace=trusted,
                snapshot=LocalSnapshotSpec(base_path=tmp_path / "archives")
                if trusted and saved is None
                else None,
                memory=SandboxMemoryOptions(
                    phase_one=Agent(instructions="Extract.", provider=peer.provider),
                    phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
                ),
            )

        agent = Agent(instructions="Answer.", provider=peer.provider)
        async with agent.session(environment=environment()) as session:
            await (await session.submit(prior)).get_result()
            if trusted:
                # The executor fixture keeps temporary helper symlinks inside its
                # test CODEX_HOME. They are not application workspace content.
                for helper in (workspace / ".fixture-config" / "tmp").rglob("*"):
                    if helper.is_symlink():
                        helper.unlink()
                saved = await session.suspend_workspace()
            else:
                original = (await getattr(session, capture)()).to_json()
                prior = "SYNTHETIC_TAMPERED_PRIOR_SOURCE"
                saved = SessionState.from_json(original.replace("SYNTHETIC_PRIOR_SOURCE", prior))
        async with agent.session(environment=environment(saved), state=saved) as restored:
            await (await restored.submit(fresh)).get_result()
            history = json.dumps((await restored.history_snapshot()).items)
            assert prior in history and fresh in history
            await restored.flush_memory()
            records = (workspace / "sessions" / f"{restored.id}.jsonl").read_text()
            rollout = json.loads(records.splitlines()[-1])
            originals = json.dumps(rollout["items"])
            assert fresh in originals
            assert (prior in originals) is trusted
            assert rollout["terminal_metadata"]["terminal_state"] == "completed"


@pytest.mark.parametrize("write_mode", ["throw", "missing"])
async def test_missing_admission_write_preserves_source_and_stays_ineligible_after_restore(
    tmp_path, monkeypatch, write_mode
):
    workspace = tmp_path / "workspace"
    extraction = {
        "rollout_slug": "proof",
        "rollout_summary": "Fresh source.",
        "raw_memory": "Fresh source.",
    }
    consolidation = {"summary": "Fresh source.", "memory": "Fresh source."}
    failure = OSError("SYNTHETIC_ADMISSION_WRITE_FAILURE")
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [text("Unproved source answer.")],
            [text("Fresh answer.")],
            [text(json.dumps(extraction))],
            [text(json.dumps(consolidation))],
        ) as peer,
    ):

        def environment():
            return sandbox_environment(
                UnixLocalSandboxClient(),
                manifest=Manifest(root=str(workspace)),
                options=UnixLocalSandboxClientOptions(),
                connect=connect,
                memory=SandboxMemoryOptions(
                    phase_one=Agent(instructions="Extract.", provider=peer.provider),
                    phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
                ),
            )

        agent = Agent(instructions="Answer.", provider=peer.provider)
        async with agent.session(environment=environment()) as session:
            native = session._native

            class MissingAdmission:
                def __getattr__(self, name):
                    return getattr(native, name)

                async def _record_memory_input_admission(self, turn_id):
                    if write_mode == "throw":
                        raise failure

            session._native = MissingAdmission()
            turn = await session.submit("SYNTHETIC_UNPROVED_OLD_SOURCE")
            assert (await turn.get_result()).final_output == "Unproved source answer."
            with pytest.raises(Exception) as refused:
                await session.flush_memory()
            if write_mode == "throw":
                assert refused.value is failure
            assert not (workspace / "sessions").exists()
            assert len(peer.requests) == 1
            saved = await session.save_state()
        async with agent.session(environment=environment(), state=saved) as restored:
            await (await restored.submit("SYNTHETIC_FRESH_PROVED_SOURCE")).get_result()
            await restored.flush_memory()
            records = (workspace / "sessions" / f"{restored.id}.jsonl").read_text()
            assert "SYNTHETIC_UNPROVED_OLD_SOURCE" not in records
            assert "SYNTHETIC_FRESH_PROVED_SOURCE" in records
            assert len(peer.requests) == 4


async def test_approval_verification_does_not_authorize_imported_memory(tmp_path, monkeypatch):
    from agents import function_tool
    from agents.testing import tool_call
    from tests.test_native_manual_tools import next_request

    workspace = tmp_path / "workspace"
    calls, verified = [], []

    @function_tool(needs_approval=True, approval_key="synthetic.memory.approval")
    def approved_lookup(value: str) -> str:
        calls.append(value)
        return value

    extraction = {
        "rollout_slug": "approval",
        "rollout_summary": "Fresh source.",
        "raw_memory": "Fresh source.",
    }
    consolidation = {"summary": "Fresh source.", "memory": "Fresh source."}
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [tool_call("approved_lookup", {"value": "once"}, call_id="approval-source")],
            [text("Prior answer.")],
            [text("Fresh answer.")],
            [text(json.dumps(extraction))],
            [text(json.dumps(consolidation))],
        ) as peer,
    ):

        def environment():
            return sandbox_environment(
                UnixLocalSandboxClient(),
                manifest=Manifest(root=str(workspace)),
                options=UnixLocalSandboxClientOptions(),
                connect=connect,
                memory=SandboxMemoryOptions(
                    phase_one=Agent(instructions="Extract.", provider=peer.provider),
                    phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
                ),
            )

        agent = Agent(instructions="Answer.", tools=[approved_lookup], provider=peer.provider)
        async with agent.session(environment=environment()) as session:
            turn = await session.submit("SYNTHETIC_UNVERIFIED_PRIOR_MEMORY")
            await (await next_request(session.interactions(), approved_lookup)).approve(always=True)
            await turn.get_result()
            saved = await session.save_state()
            assert len(saved.remembered_approvals) == 1

        def verify_decisions_only(checkpoint):
            verified.append(True)
            return all(decision.approve for decision in checkpoint.remembered_approvals)

        async with agent.session(
            environment=environment(),
            state=saved,
            verify_remembered_approvals=verify_decisions_only,
        ) as restored:
            await (await restored.submit("SYNTHETIC_VERIFIED_FRESH_MEMORY")).get_result()
            await restored.flush_memory()
            records = (workspace / "sessions" / f"{restored.id}.jsonl").read_text()
            assert "SYNTHETIC_UNVERIFIED_PRIOR_MEMORY" not in records
            assert "SYNTHETIC_VERIFIED_FRESH_MEMORY" in records
            assert verified == [True]
            assert calls == ["once"]
            assert len((await restored.save_state()).remembered_approvals) == 1
            assert len(peer.requests) == 5
