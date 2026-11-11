"""Web search configuration checks before provider acquisition."""

from unittest.mock import Mock

import pytest

from agents import Agent, NativeProvider, WebSearch


@pytest.mark.parametrize("max_results", [1.5, -0.5, -1, 2**63, 2**64, True])
async def test_image_count_rejected_before_provider_acquisition(max_results, monkeypatch):
    acquire = Mock(side_effect=AssertionError("Provider acquired for invalid image count"))
    monkeypatch.setattr(NativeProvider, "_acquire_lease", acquire)
    provider = NativeProvider(api_key="image-count-fixture")
    agent = Agent(
        instructions="Act",
        model="gpt-5.5",
        provider=provider,
        tools=[WebSearch(route="hosted", image_settings={"max_results": max_results})],
    )
    try:
        with pytest.raises(ValueError, match="non-negative integer"):
            async with agent.session():
                pytest.fail("Invalid image count admitted")
        acquire.assert_not_called()
    finally:
        await provider.aclose()
