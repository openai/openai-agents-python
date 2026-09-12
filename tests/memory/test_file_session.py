from pathlib import Path

import pytest

from examples.memory.file_session import FileSession


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit, expected_contents",
    [
        pytest.param(0, [], id="zero"),
        pytest.param(1, ["third"], id="one"),
        pytest.param(2, ["second", "third"], id="latest-in-order"),
        pytest.param(5, ["first", "second", "third"], id="larger-than-history"),
        pytest.param(None, ["first", "second", "third"], id="unlimited"),
    ],
)
async def test_get_items_respects_limit_without_changing_stored_history(
    tmp_path: Path, limit: int | None, expected_contents: list[str]
) -> None:
    session = FileSession(dir=tmp_path, session_id="history")
    items = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    await session.add_items(items)

    resumed = FileSession(dir=tmp_path, session_id="history")
    retrieved = await resumed.get_items(limit=limit)

    assert [item["content"] for item in retrieved] == expected_contents
    assert await resumed.get_items() == items
