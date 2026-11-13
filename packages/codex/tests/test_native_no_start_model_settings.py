"""Keep a session usable after native settings validation refuses a turn."""

import pytest

from agents import Agent, CodexLogin, NativeError, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_codex_login import (
    codex_endpoint as codex_endpoint,
    saved_login,
    uncompressed_model_requests as uncompressed_model_requests,
)
from tests.test_native_run_model_override import model_names


@pytest.mark.asyncio
async def test_invalid_native_settings_leave_session_usable(codex_endpoint, tmp_path):
    peer = codex_endpoint
    home = saved_login(tmp_path / "synthetic-login", token="synthetic-no-start-token")
    async with NativeProvider(auth=CodexLogin(codex_home=home)) as provider:
        agent = Agent(model=model_names()[0], instructions="Reply briefly.", provider=provider)
        async with timeout(20), agent.session() as session:
            with pytest.raises(
                NativeError, match="store=true requires API-key Responses authentication"
            ) as refused:
                invalid = await session.submit(
                    "invalid settings", model_settings_override={"store": True}
                )
                await invalid.get_result()
            assert not peer.requests
            assert refused.value._effects == "not_started"
            peer.respond(assistant_output("corrected settings complete"))
            corrected = await session.submit("corrected settings", model_settings_override={})
            assert (await corrected.get_result()).final_output == "corrected settings complete"
            assert len(peer.requests) == 1
