from examples.recipes.concurrent_tools import run_example


async def test_two_sessions_cannot_overspend_shared_quota():
    quota, outputs = await run_example()
    assert quota.arrived == 2
    assert quota.remaining == 0
    assert len(quota.ledger) == 1
    assert quota.ledger[0] in {"user-a", "user-b"}
    assert sorted(outputs) == ["full", "reserved"]
