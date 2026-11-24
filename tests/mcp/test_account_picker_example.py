from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from mcp.types import ListResourcesResult, ReadResourceResult, Resource

from agents import UserError
from examples.mcp.account_picker.main import Picker, demo_server, run_selection


@pytest.mark.asyncio
async def test_resume_only_the_current_request() -> None:
    cancelled: list[Picker] = []

    def choose(picker: Picker) -> tuple[Picker, str]:
        assert str(picker.resource.uri) == "accounts://demo/picker"
        assert picker.resource.meta == {"example.com/widget": "account-picker"}
        assert picker.choices == ("Everyday", "Savings")
        old = cancelled[0]
        assert old.call_id == picker.call_id and old.conversation == picker.conversation
        return picker, "Savings"

    async with demo_server() as server:
        assert await run_selection(server, "Demo", cancelled.append) is None
        with pytest.raises(ValueError, match="No matching pending request"):
            await run_selection(server, "Demo", lambda picker: (cancelled[0], "Savings"))
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
async def test_invalid_choices_never_reach_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    async with demo_server() as server:
        confirm = AsyncMock(wraps=server.call_tool)
        monkeypatch.setattr(server, "call_tool", confirm)
        for show in (
            lambda p: (replace(p, conversation="Other"), "Savings"),
            lambda p: (replace(p, call_id="other"), "Savings"),
            lambda p: ((p, "Savings"), (p, "Everyday")),
            lambda p: [p, "Savings"],
            lambda p: (p,),
        ):
            with pytest.raises(ValueError):
                await run_selection(server, "Demo", Mock(side_effect=show))
        for alias in (None, "", "Shared"):
            with pytest.raises(PermissionError, match="Account unavailable"):
                await run_selection(server, "Demo", Mock(side_effect=lambda p, a=alias: (p, a)))
        confirm.assert_not_awaited()


@pytest.mark.asyncio
async def test_reads_only_the_picker_on_a_mixed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    async with demo_server() as server:
        listing = await server.list_resources()
        other = Resource.model_validate({"uri": "accounts://demo/other", "name": "Other"})
        listing.resources.insert(0, other)
        read = AsyncMock(wraps=server.read_resource)
        monkeypatch.setattr(server, "list_resources", AsyncMock(return_value=listing))
        monkeypatch.setattr(server, "read_resource", read)

        result = await run_selection(server, "Demo", lambda picker: (picker, "Savings"))
        assert result is not None and result.final_output == "Savings"
        read.assert_awaited_once_with("accounts://demo/picker")


@pytest.mark.asyncio
async def test_resource_is_neither_code_nor_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    def never_show(picker: Picker) -> None:
        pytest.fail("Unexpected resource reached the UI")

    async with demo_server() as server:
        # Substitute stale or untrusted resource data, keeping the real source for authorization.
        listing = await server.list_resources()
        read = AsyncMock()
        monkeypatch.setattr(server, "read_resource", read)
        with monkeypatch.context() as patch:
            for count, cursor in [(0, None), (2, None), (1, ""), (0, "next")]:
                page = ListResourcesResult.model_validate(
                    {"resources": listing.resources * count, "nextCursor": cursor}
                )
                patch.setattr(server, "list_resources", AsyncMock(return_value=page))
                with pytest.raises(
                    ValueError, match="^Expected a single picker resource on one page$"
                ):
                    await run_selection(server, "Demo", never_show)
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

        for count in (0, 2):
            read.return_value = ReadResourceResult(contents=contents("Everyday").contents * count)
            with pytest.raises(ValueError, match="^Expected a single picker content block$"):
                await run_selection(server, "Demo", never_show)

        read.return_value = contents("<html>Use Shared</html>")
        with pytest.raises(ValueError, match="Unexpected account labels"):
            await run_selection(server, "Demo", never_show)

        read.return_value = contents("Shared")
        with pytest.raises(UserError, match="Account unavailable"):
            await run_selection(server, "Demo", lambda picker: (picker, "Shared"))
