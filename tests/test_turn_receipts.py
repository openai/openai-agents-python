"""Applied settings and file sources from the actual model context."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from dataclasses import replace
from errno import ELOOP
from pathlib import Path

import pytest

import agents.turn_receipt as turn_receipt
from agents import Agent, NativeAgents
from agents.state import SessionState
from agents.turn_receipt import (
    _load_instruction_files,
    _project_turn_receipt,
    _redact_checkpoint_executor_receipts,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_agent_checkpoints import message, peer
from tests.test_native_child_agents import (
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_child_tool_integration import Memory, family_agent, family_peer
from tests.test_native_executor_tools import declaration
from tests.test_native_instruction_updates import application_texts
from tests.test_native_mcp_tools import Peer


def test_file_loader_hashes_the_exact_bytes_in_order(tmp_path):
    first = tmp_path / "method.md"
    first_content = "Café\r\n".encode()
    first.write_bytes(first_content)
    second = tmp_path / "checks.md"
    second_content = b"Check the artifact.\n"
    second.write_bytes(second_content)
    instructions, sources = _load_instruction_files("Developer", [first, second])
    assert instructions.encode() == b"Developer\n\n" + first_content + b"\n\n" + second_content
    assert sources == [
        {
            "path": str(path.resolve()),
            "source": "application_file",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in [(first, first_content), (second, second_content)]
    ]
    second.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="must be UTF-8") as raised:
        _load_instruction_files("", [second])
    assert raised.value.__context__ is None


@pytest.mark.parametrize("symlink", [False, True])
def test_file_loader_rejects_a_replacement_before_reading(tmp_path, monkeypatch, symlink):
    if symlink and sys.platform == "win32":
        pytest.skip("Creating symlinks can require special privileges on Windows")
    path = tmp_path.resolve() / "selected.md"
    other = path.with_name("other.md")
    path.write_bytes(b"Selected instructions.")
    other.write_bytes(b"Different instructions.")
    open_file = io.open
    opened = None

    def replace_then_open(name, *args, **kwargs):
        nonlocal opened
        if name != path:
            return open_file(name, *args, **kwargs)
        # Replace the path at the precise boundary before the real open.
        path.rename(path.with_name("original.md"))
        if symlink:
            path.symlink_to(other)
        else:
            other.rename(path)
        opened = open_file(name, *args, **kwargs)
        return opened

    with monkeypatch.context() as patch:
        patch.setattr(io, "open", replace_then_open)
        patch.setattr(turn_receipt, "open", replace_then_open, raising=False)
        with pytest.raises((OSError, ValueError)) as raised:
            _load_instruction_files("", [path])
    if isinstance(raised.value, OSError):
        assert raised.value.errno == ELOOP
    else:
        assert "Instruction file changed" in str(raised.value)
    if opened is not None:
        assert opened.closed


@pytest.mark.skipif(sys.platform == "win32", reason="Creating symlinks can require privileges")
def test_file_loader_preserves_a_stable_selected_symlink(tmp_path):
    path = tmp_path / "selected.md"
    content = b"Selected instructions."
    path.write_bytes(content)
    link = tmp_path / "link.md"
    link.symlink_to(path)

    instructions, sources = _load_instruction_files("", [link])
    assert instructions.encode() == content
    assert sources == [
        {
            "path": str(path.resolve()),
            "source": "application_file",
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]


def test_old_context_or_unknown_receipt_version_is_unavailable():
    assert _project_turn_receipt({"turn_id": "old-turn"}) is None
    assert _project_turn_receipt({"turn_id": "new-turn", "sdk_receipt": {"version": 2}}) is None


@pytest.mark.parametrize(
    "approval_policy",
    [
        "on-failure",
        {
            "granular": {
                "sandbox_approval": True,
                "rules": True,
                "mcp_elicitations": False,
            },
        },
    ],
    ids=["legacy-alias", "granular-defaults"],
)
def test_executor_receipt_projects_only_the_public_policy_fields(approval_policy):
    public_executor = {
        "environment_id": "test-executor",
        "transport": "local",
        "cwd": "file:///synthetic/work",
        "writable_roots": ["file:///synthetic/work"],
        "network_sandbox": "restricted",
        "network_policy": {
            "requires_proxy": True,
            "domains": {"allowed.example.invalid": "allow"},
            "allow_all_unix_sockets": False,
            "allow_local_binding": None,
        },
    }
    permission_profile = {"native_policy": "internal"}
    native_executor = {**public_executor, "permission_profile": permission_profile}
    projected = _project_turn_receipt(
        {
            "turn_id": "test-turn",
            "sdk_receipt": {
                "version": 1,
                "thread_id": "test-thread",
                "model": "test-model",
                "reasoning_effort": None,
                "instruction_revision": None,
                "developer_instructions_sha256": None,
                "instruction_files": [],
                "user_config_applied": False,
                "codex_revision": "test-revision",
                "codex_source_tree": "test-tree",
                "approval_policy": approval_policy,
                "strict_executor_writes": True,
                "executors": [native_executor],
            },
        }
    )

    assert projected is not None
    assert projected["approval_policy"] == approval_policy
    assert "strict_executor_writes" not in projected
    assert projected["executors"] == [public_executor]
    assert native_executor["permission_profile"] == permission_profile


def test_portable_checkpoint_omits_descendant_and_repair_executor_details():
    private = {"executors": [{"cwd": "/synthetic/private"}], "model": "test-model"}
    descendant = {
        "type": "turn_context",
        "payload": {"sdk_receipt": {**private, "strict_executor_writes": True}},
    }
    unknown = {"type": "turn_context", "payload": {"sdk_receipt": "unknown shape"}}
    repair = {"receipt": private}
    payload = {
        "history": [unknown],
        "descendants": [{"history": [descendant]}],
        "repair_turns": [repair],
    }

    _redact_checkpoint_executor_receipts(payload)

    assert descendant["payload"]["sdk_receipt"] == {**private, "executors": None}
    assert unknown["payload"]["sdk_receipt"] == "unknown shape"
    assert repair["receipt"] == {**private, "executors": None}
    assert private["executors"] == [{"cwd": "/synthetic/private"}]


async def test_native_receipt_matches_provider_input_and_survives_restore(tmp_path):
    from agents import _native

    source = tmp_path / "method.md"
    contents = b"Follow this method.\r\n"
    source.write_bytes(contents)
    async with timeout(30), peer() as provider:
        agent = Agent(
            instructions="Give a concise answer.",
            provider=provider.provider(),
            model_settings={"reasoning_effort": "high"},
        )
        async with agent.session(instruction_files=[source]) as original:
            # Preserve an unused publication as well as receipts from sampled turns.
            unopened = await original.save_state()
            source.write_text("An edit after the session opened.", encoding="utf-8")
            provider.replies.append(message("first"))
            await (await original.submit("Start")).get_result()
            sampled = await original.save_state()
            [first] = [row["receipt"] async for row in original.turns() if "receipt" in row]
            assert first["thread_id"] == original.id
        request = provider.requests[-1]
        [accepted] = application_texts(request)
        assert accepted == agent.instructions + "\n\n" + contents.decode()
        assert first["instruction_revision"]
        assert (
            first["developer_instructions_sha256"] == hashlib.sha256(accepted.encode()).hexdigest()
        )
        assert first["instruction_files"] == [
            {
                "path": str(source.resolve()),
                "source": "application_file",
                "sha256": hashlib.sha256(contents).hexdigest(),
            }
        ]
        assert first["codex_revision"] == _native.CODEX_REVISION
        series = Path(__file__).resolve().parents[1] / "packages/codex/patches/series.json"
        assert first["codex_source_tree"] == json.loads(series.read_text())["result_tree"]
        assert first["model"] == request["model"]
        assert first["reasoning_effort"] == request["reasoning"]["effort"]
        assert not first["user_config_applied"]
        assert first["executors"] == []
        assert str(source.resolve()) not in json.dumps(request)

        with pytest.raises(ValueError, match="latest accepted instructions"):
            async with agent.session(state=sampled, instruction_files=[source]):
                pytest.fail("Restore accepted a changed instruction file")
        source.write_bytes(contents)

        for state in [unopened, sampled]:
            async with agent.session(state=state, instruction_files=[source]) as restored:
                provider.replies.append(message("resumed"))
                await (await restored.submit("Resume")).get_result()
                receipts = [row["receipt"] async for row in restored.turns() if "receipt" in row]
                assert receipts[-1]["instruction_files"] == first["instruction_files"]
                assert receipts[-1]["instruction_revision"] == first["instruction_revision"]
                if state is sampled:
                    assert receipts[0] == first

                await restored.update(instructions="Use the new instructions.")
                provider.replies.append(message("updated"))
                await (await restored.submit("Continue")).get_result()
                receipts = [row["receipt"] async for row in restored.turns() if "receipt" in row]
                assert receipts[-1]["instruction_revision"] != first["instruction_revision"]
                assert receipts[-1]["instruction_files"] == []
                assert (
                    receipts[-1]["developer_instructions_sha256"]
                    == hashlib.sha256(b"Use the new instructions.").hexdigest()
                )


@pytest.mark.parametrize(
    "invalid_fields",
    [{"model": None}, {"instruction_files": 5}],
    ids=["model", "instruction-files"],
)
async def test_invalid_latest_sample_is_unavailable_until_a_valid_sample(
    invalid_fields: dict[str, object],
) -> None:
    async with timeout(30), peer() as provider:
        agent = Agent(instructions="Remember atlas.", provider=provider.provider())
        async with agent.session() as source:
            provider.replies.append(message("Remembered.", "first"))
            turn = await source.submit("Remember atlas.")
            await turn.get_result()
            raw = json.loads((await source.save_state()).to_json())

        [index] = [
            index
            for index, record in enumerate(raw["history"])
            if record["type"] == "turn_context"
            and record["payload"].get("turn_id") == turn.turn_id
            and record["payload"].get("sdk_receipt") is not None
        ]
        observed = raw["history"][index]
        context = observed["payload"]
        receipt = context["sdk_receipt"]
        raw["history"].insert(
            index + 1,
            {**observed, "payload": {**context, "sdk_receipt": {**receipt, **invalid_fields}}},
        )

        state = SessionState.from_json(json.dumps(raw))
        async with agent.session(state=state) as restored:
            [recorded] = [row async for row in restored.turns() if row["id"] == turn.turn_id]
            assert "receipt" not in recorded

        latest = {**receipt, "model": "next-sampled-model"}
        raw["history"].insert(
            index + 2,
            {**observed, "payload": {**context, "sdk_receipt": latest}},
        )
        state = SessionState.from_json(json.dumps(raw))
        async with agent.session(state=state) as restored:
            [recorded] = [row async for row in restored.turns() if row["id"] == turn.turn_id]
            assert recorded["receipt"]["model"] == "next-sampled-model"


@pytest.mark.parametrize("receipt_kind", ["malformed", "foreign"])
async def test_portable_receipt_metadata_cannot_replace_the_canonical_context(
    receipt_kind: str,
) -> None:
    async with timeout(30), peer() as provider:
        agent = Agent(instructions="Remember atlas.", model="gpt-5.5", provider=provider.provider())
        async with agent.session() as source:
            provider.replies.append(message("Remembered.", "first"))
            turn = await source.submit("Remember atlas.")
            await turn.get_result()
            raw = json.loads((await source.save_state()).to_json())

        contexts = [
            record["payload"]
            for record in raw["history"]
            if record["type"] == "turn_context" and record["payload"].get("turn_id") == turn.turn_id
        ]
        [canonical] = [context for context in contexts if context.get("sdk_receipt") is None]
        [sample] = [context["sdk_receipt"] for context in contexts if context.get("sdk_receipt")]
        # Distinguish the last turn's settings from the earlier startup context.
        previous_comp_hash = "previous-model-computation"
        for context in contexts:
            context["comp_hash"] = previous_comp_hash
        if receipt_kind == "malformed":
            canonical["sdk_receipt"] = {"version": 1}
        else:
            canonical["sdk_receipt"] = {
                **sample,
                "thread_id": "0ba91311-1b76-4322-aa56-32d55b6c35e7",
            }

        state = SessionState.from_json(json.dumps(raw))
        async with agent.session(state=state) as resumed:
            provider.replies.append(message("A native summary of atlas.", "summary"))
            await resumed.compact()
            history = (await resumed.save_state()).history
        latest = next(item["payload"] for item in reversed(history) if item["type"] == "compacted")
        assert (
            latest["resume_metadata"]["previous_turn_settings"]["comp_hash"] == previous_comp_hash
        )


async def test_native_child_receipt_records_only_sampled_instruction_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "method.md"
    content = b"Use the supplied application method.\r\n"
    source.write_bytes(content)
    memory = Memory()
    vault = Peer(names=("find-record",))
    async with (
        timeout(30),
        family_peer(programmatic_tool_calling=False, grandchildren=True) as provider,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        root_response = provider.response

        def response():
            request = provider.requests[-1]
            identity = metadata(request, provider.request_headers[-1])
            if identity["agent_name"] == "/root":
                return root_response()
            name = identity["agent_name"].rsplit("/", 1)[-1]
            requests = provider.child_requests.setdefault(name, [])
            requests.append(request)
            provider.child_metadata.setdefault(name, []).append(identity)
            if name == "alpha" and len(requests) == 1:
                return [
                    collaboration(
                        "spawn_agent",
                        {"task_name": "grandchild", "message": "Read.", "fork_turns": "none"},
                        "spawn-grandchild",
                    )
                ]
            return [assistant_output("Finished")]

        monkeypatch.setattr(provider, "response", response)
        agent = family_agent(provider, executor, url, memory)
        instructions = agent.instructions
        assert isinstance(instructions, str)
        inherited = instructions + "\n\n" + content.decode().strip()
        (native_agents,) = (tool for tool in agent.tools if isinstance(tool, NativeAgents))
        (role,) = native_agents.additional_roles
        # A role that supplies the same text still has its own provenance.
        agent = replace(
            agent,
            tools=[
                *(tool for tool in agent.tools if tool is not native_agents),
                replace(
                    native_agents,
                    max_children=3,
                    max_depth=2,
                    additional_roles=[replace(role, instructions=inherited)],
                ),
            ],
        )
        async with agent.session(
            context=memory, environment=declaration(executor), instruction_files=[source]
        ) as session:
            await (await session.submit("Delegate the work")).get_result()
            for child in await handles(session, 3):
                await child.get_result()

            receipts = {}
            for name, requests in provider.child_requests.items():
                child_id = provider.child_metadata[name][0]["thread_id"]
                [receipt] = [row["receipt"] async for row in session.turns(subagent_id=child_id)]
                developer_parts = [
                    part["text"]
                    for item in requests[0]["input"]
                    if item.get("role") == "developer"
                    for part in item.get("content", [])
                    if part.get("type") == "input_text"
                ]
                receipts[name] = (receipt, developer_parts)

            files = [
                {
                    "path": str(source.resolve()),
                    "source": "application_file",
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ]
            for name in ("alpha", "grandchild"):
                receipt, parts = receipts[name]
                assert inherited in parts
                assert (
                    receipt["developer_instructions_sha256"]
                    == hashlib.sha256(inherited.encode()).hexdigest()
                )
                assert receipt["instruction_revision"] is None
                assert receipt["instruction_files"] == files

            role_receipt, role_parts = receipts["beta"]
            assert inherited in role_parts
            assert role_receipt["instruction_files"] == []
