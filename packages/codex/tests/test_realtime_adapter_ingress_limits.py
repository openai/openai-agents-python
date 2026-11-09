"""Ingress rejects unbounded text before allocating an encoded copy."""

from typing import Any

import pytest

from tests._asyncio import timeout
from tests.test_realtime_adapter_driver import (
    Adapter,
    Connection,
    Handle,
    connect,
    driver,
)


class OversizedText(str):
    def encode(self, *args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("Oversized text must not be encoded")


@pytest.mark.parametrize(
    "frame",
    [
        OversizedText("\U0001f600" * (8 * 1024 * 1024 + 1)),
        "\U0001f600" * (2 * 1024 * 1024 + 1),
    ],
    ids=["character-limit", "utf8-byte-limit"],
)
async def test_oversized_adapter_ingress_is_rejected_before_native(frame: str) -> None:
    handle, connection, errors = Handle(), Connection(), []
    owner = driver(Adapter(connection), handle, errors)
    async with timeout(2):
        assert await connect(handle)
        connection.frames.put_nowait(frame)
        assert not await handle.ended
        await owner.aclose()
    assert handle.frames == []
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert str(errors[0]) == "Realtime adapter frame exceeds the native limit"
