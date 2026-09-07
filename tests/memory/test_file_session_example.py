from pathlib import Path

import pytest

from examples.memory.file_session import FileSession


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        pytest.param(None, ["first", "second"], id="unlimited"),
        pytest.param(0, [], id="zero"),
        pytest.param(1, ["second"], id="latest-item"),
        pytest.param(3, ["first", "second"], id="larger-than-history"),
    ],
)
async def test_file_session_get_items_limit(
    tmp_path: Path, limit: int | None, expected: list[str]
) -> None:
    session = FileSession(dir=tmp_path, session_id="limit-test")
    items = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    await session.add_items(items)

    assert [item["content"] for item in await session.get_items(limit=limit)] == expected
    assert await session.get_items() == items
