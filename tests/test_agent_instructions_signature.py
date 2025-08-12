import pytest
from src.agents.agent import Agent  # adjust if needed

class DummyContext:
    pass

class DummyAgent(Agent):
    def __init__(self, instructions):
        super().__init__(instructions=instructions)

@pytest.mark.asyncio
async def test_valid_signature():
    async def good_instructions(ctx, agent):
        return "valid"
    a = DummyAgent(good_instructions)
    result = await a.get_system_prompt(DummyContext())
    assert result == "valid"

@pytest.mark.asyncio
async def test_invalid_signature_raises():
    async def bad_instructions(ctx):
        return "invalid"
    a = DummyAgent(bad_instructions)
    import pytest
    with pytest.raises(TypeError):
        await a.get_system_prompt(DummyContext())
