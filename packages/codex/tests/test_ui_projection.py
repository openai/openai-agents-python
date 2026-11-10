"""Canonical display checks shared by the Vercel and AG-UI examples."""

from types import SimpleNamespace

import pytest

from examples.codex._ui_projection import _complete_display


def message(item_id, text, *, turn="last", phase=None, **extra):
    return {
        "id": item_id,
        "turn_id": turn,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "phase": phase,
        "content": [{"type": "output_text", "text": text}],
        **extra,
    }


async def complete(items, shown, output, *, plain=True):
    async def history(*, subagent_id):
        assert subagent_id == "child"
        for item in items:
            yield item

    return await _complete_display(
        SimpleNamespace(items=history), "child", {"last"}, "last", shown, plain, output
    )


@pytest.mark.parametrize("output", ["", "answer"])
async def test_every_nonempty_canonical_message_needs_display(output):
    items = [message("commentary", "working", phase="commentary"), message("final", output)]
    shown = {"final": [output]}
    assert not await complete(items, shown, output)
    assert await complete(items, {"commentary": ["working"], **shown}, output)
    assert not await complete(items, {"commentary": ["partial"], **shown}, output)
    assert not await complete(items, {"missing": ["working"], **shown}, output)


async def test_typed_final_is_optional_but_commentary_and_shown_text_still_match():
    items = [
        message("commentary", "working", phase="commentary"),
        message("final", '{"answer":42}'),
    ]
    assert not await complete(items, {}, {"answer": 42}, plain=False)
    assert await complete(items, {"commentary": ["working"]}, {"answer": 42}, plain=False)
    assert not await complete(
        items, {"commentary": ["working"], "final": ["wrong"]}, {}, plain=False
    )


async def test_visible_text_and_exact_turn_filter():
    items = [
        message("old", "earlier", turn="earlier"),
        message("final", "markup", display_text="visible"),
    ]
    assert await complete(items, {"final": ["visible"]}, "visible")
    assert not await complete(items, {"final": ["markup"]}, "visible")
    assert await complete([message("final", "markup", display_text="")], {}, "")


async def test_empty_output_still_needs_available_history():
    assert await complete([], {}, "")

    async def unavailable(**options):
        raise RuntimeError("Unavailable history")
        yield

    assert not await _complete_display(
        SimpleNamespace(items=unavailable), None, {"last"}, "last", {}, True, ""
    )


@pytest.mark.parametrize("text, blank", [("\u0085", True), ("\ufeff", False), ("\x1c", False)])
async def test_native_whitespace_does_not_change_display_identity(text, blank):
    assert await complete([message("final", text)], {"final": [text]}, "") is blank
    assert not await complete([message("final", text)], {"final": [" "]}, "")
