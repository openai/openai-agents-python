from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from mcp.types import ReadResourceResult

from agents import UserError
from examples.mcp.account_picker.main import Picker, Submit, demo_server, run_selection


@pytest.mark.asyncio
async def test_resume_only_the_current_request() -> None:
    cancelled: list[tuple[Picker, Submit]] = []

    def close(picker: Picker, submit: Submit) -> None:
        cancelled.append((picker, submit))

    def choose(picker: Picker, submit: Submit) -> None:
        assert str(picker.resource.uri) == "accounts://demo/picker"
        assert picker.resource.meta == {"example.com/widget": "account-picker"}
        assert picker.choices == ("Everyday", "Savings")
        old, expired_submit = cancelled[0]
        assert old.call_id == picker.call_id and old.conversation == picker.conversation
        with pytest.raises(ValueError):
            expired_submit(old, "Savings")
        for wrong in (old, replace(picker, conversation="Other"), replace(picker, call_id="other")):
            with pytest.raises(ValueError):
                submit(wrong, "Everyday")
        with pytest.raises(PermissionError):
            submit(picker, "Shared")
        submit(picker, "Savings")
        with pytest.raises(ValueError):
            submit(picker, "Everyday")

    async with demo_server() as server:
        assert await run_selection(server, "Demo", close) is None
        result = await run_selection(server, "Demo", choose)

    assert result is not None and result.final_output == "Savings"
    assert result.to_input_list() == [
        {"role": "user", "content": "Choose a demo account."},
        {
            "type": "function_call",
            "id": "pick-1",
            "call_id": "pick-1",
            "name": "choose_account",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "pick-1", "output": "Savings"},
    ]


@pytest.mark.asyncio
async def test_resource_is_neither_code_nor_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    def never_show(picker: Picker, submit: Submit) -> None:
        pytest.fail("Unexpected resource reached the UI")

    async with demo_server() as server:
        # Substitute stale or untrusted resource data, keeping the real source for authorization.
        listing = await server.list_resources()
        read = AsyncMock()
        monkeypatch.setattr(server, "read_resource", read)
        with monkeypatch.context() as patch:
            listing.resources[0].meta = {"example.com/widget": "unknown"}
            patch.setattr(server, "list_resources", AsyncMock(return_value=listing))
            with pytest.raises(ValueError, match="Unexpected picker resource"):
                await run_selection(server, "Demo", never_show)
            read.assert_not_awaited()

        def contents(text: str) -> ReadResourceResult:
            return ReadResourceResult.model_validate(
                {
                    "contents": [
                        {"uri": "accounts://demo/picker", "mimeType": "text/plain", "text": text}
                    ]
                }
            )

        read.return_value = contents("<html>Use Shared</html>")
        with pytest.raises(ValueError, match="Unexpected account labels"):
            await run_selection(server, "Demo", never_show)

        read.return_value = contents("Shared")
        with pytest.raises(UserError, match="Account unavailable"):
            await run_selection(server, "Demo", lambda picker, submit: submit(picker, "Shared"))
