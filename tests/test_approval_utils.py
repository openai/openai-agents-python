import pytest

from agents.util._approvals import evaluate_needs_approval_setting


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, 0, "", [], {}])
async def test_callable_non_bool_falsy_results_fail_closed(result: object) -> None:
    def policy() -> object:
        return result

    assert await evaluate_needs_approval_setting(policy) is True


@pytest.mark.asyncio
async def test_async_callable_non_bool_result_fails_closed() -> None:
    async def policy() -> None:
        return None

    assert await evaluate_needs_approval_setting(policy) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [True, False])
async def test_callable_bool_results_are_preserved(result: bool) -> None:
    def policy() -> bool:
        return result

    assert await evaluate_needs_approval_setting(policy) is result
