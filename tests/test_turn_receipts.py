"""Applied settings and file sources from the actual model context."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agents import Agent
from agents.turn_receipt import _load_instruction_files, _project_turn_receipt
from tests._asyncio import timeout
from tests.test_native_agent_checkpoints import message, peer
from tests.test_native_instruction_updates import application_texts


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


def test_old_context_or_unknown_receipt_version_is_unavailable():
    assert _project_turn_receipt({"turn_id": "old-turn"}) is None
    assert _project_turn_receipt({"turn_id": "new-turn", "sdk_receipt": {"version": 2}}) is None


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
