"""Equivalent canonical write grants share a checkpoint policy identity."""

import hashlib
import sys
from dataclasses import replace

import pytest

from agents import Agent, Environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_provider_websockets import peer as model_peer


@pytest.mark.skipif(sys.platform == "win32", reason="Strict POSIX writable-root aliases")
@pytest.mark.parametrize("sampled", [False, True], ids=["before-first-turn", "after-turn"])
async def test_checkpoint_write_root_order_is_canonical(tmp_path, monkeypatch, sampled):
    first, last = tmp_path / "a-target", tmp_path / "z-target"
    first.mkdir()
    last.mkdir()
    alias_first, alias_last = tmp_path / "a-link", tmp_path / "z-link"
    alias_first.symlink_to(last, target_is_directory=True)
    alias_last.symlink_to(first, target_is_directory=True)
    async with (
        timeout(40),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        model_peer() as model,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(executor.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty-cert-directory"))
        agent = Agent(instructions="Answer.", provider=model.provider(websocket=False))
        selected = Environment(
            url=executor.url,
            cwd=str(executor.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + executor.token},
            writable_roots=[alias_first, alias_last],
        )
        async with agent.session(environment=selected) as session:
            if sampled:
                model.respond(assistant_output("ready"))
                await (await session.submit("Start.")).get_result()
            saved = await session.save_state()
            # Reproduce the current supported producer's original opaque digest
            # without rewriting native history or claiming an older SDK capture.
            legacy_digest = hashlib.sha256(
                ("strict-writes:" + await session._native._executor_policies()).encode("utf-8")
            ).hexdigest()
        legacy = saved.model_copy(update={"executor_policy_digest": legacy_digest})
        equivalent = replace(selected, writable_roots=[first, last])
        assert legacy.executor_policy_digest != saved.executor_policy_digest
        # This old opaque identity retains its original-spelling requirement.
        with pytest.raises(ValueError, match="agree on the applied executor policy"):
            async with agent.session(environment=equivalent, state=legacy):
                pytest.fail("An old ordered digest accepted different root spellings")
        async with agent.session(environment=selected, state=legacy) as restored:
            resaved = await restored.save_state()
            assert resaved.source_session_id == saved.source_session_id
            assert resaved.executor_policy_digest == saved.executor_policy_digest
        # Saving the restored state upgrades its identity through the public API.
        async with agent.session(environment=equivalent, state=resaved) as restored:
            model.respond(assistant_output("continued"))
            result = await (await restored.submit("Continue.")).get_result()
            assert result.final_output == "continued"
            continued = await restored.save_state()
            assert continued.source_session_id == saved.source_session_id
            assert continued.executor_policy_digest == resaved.executor_policy_digest
        # Different physical roots still require a different current grant.
        for roots in ([first], [first, last, tmp_path / "extra"]):
            if len(roots) == 3:
                roots[-1].mkdir()
            with pytest.raises(ValueError, match="agree on the applied executor policy"):
                async with agent.session(
                    environment=replace(selected, writable_roots=roots), state=saved
                ):
                    pytest.fail("Changed writable grants restored the checkpoint")
