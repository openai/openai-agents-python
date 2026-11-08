"""Capture and project media descriptors; native qualification runs separately."""

from typing import Any

import pytest

from _agents_codex._history import _response_item
from _agents_codex._projection import EventProjector
from _agents_codex.input import _native_input, _response_content
from agents import InputFile, InputImageFile, ToolOutput, ToolOutputFile, ToolOutputImage


@pytest.mark.parametrize("detail", [None, "auto", "low", "high", "original"])
@pytest.mark.parametrize(
    "source", [{"image_url": "https://image.invalid/a.png"}, {"file_id": "opaque-image"}]
)
def test_image_capture_and_both_projections_preserve_reference_and_detail(
    source: dict[str, str], detail: Any
) -> None:
    image: dict[str, Any] = {"type": "input_image", **source, "detail": detail}
    captured = _native_input({"role": "user", "content": [image]})  # type: ignore[typeddict-item]
    expected = {"type": "input_image", **source, **({"detail": detail} if detail else {})}
    image.clear()
    assert _response_content(captured) == [expected]
    native = {
        "type": "inputImage",
        **{"imageUrl" if key == "image_url" else "fileId": value for key, value in source.items()},
    }
    if detail:
        native["detail"] = detail
    assert _response_content([native]) == [expected]
    assert ToolOutput([native]).to_native()["contentItems"] == [native]


@pytest.mark.parametrize(
    "source",
    [
        {"file_id": "opaque-file"},
        {"file_url": "https://files.invalid/document.pdf?opaque=1"},
        {"file_data": "JVBERi0xLjcK"},
        {"file_data": "data:application/pdf;base64,JVBERi0xLjcK"},
    ],
)
def test_file_capture_is_detached_and_retains_filename_and_order(source: dict[str, str]) -> None:
    file: InputFile = {"type": "input_file", **source, "filename": "report.pdf"}  # type: ignore[typeddict-item]
    image: InputImageFile = {"type": "input_image", "file_id": "opaque-image", "detail": "low"}
    parts = [
        {"type": "input_text", "text": "before"},
        image,
        file,
        {"type": "input_text", "text": "after"},
    ]
    captured = _native_input({"role": "user", "content": parts})  # type: ignore[typeddict-item]
    projected = _response_content(captured)
    assert projected == [
        {"type": "input_text", "text": "before"},
        dict(image),
        dict(file),
        {"type": "input_text", "text": "after"},
    ]
    file["filename"] = "changed"
    projected[2]["filename"] = "also changed"
    assert _response_content(captured)[2]["filename"] == "report.pdf"
    assert ToolOutput.file(**source, filename="report.pdf").to_native() == {
        "success": True,
        "contentItems": [{"type": "inputFile", **source, "filename": "report.pdf"}],
    }


@pytest.mark.parametrize(
    "part",
    [
        {"type": "input_image"},
        {"type": "input_image", "image_url": "https://image.invalid/a", "file_id": None},
        {"type": "input_image", "file_id": ""},
        {"type": "input_image", "file_id": "id", "detail": "maximum"},
        {"type": "input_image", "image_url": "/tmp/image.png"},
        {"type": "input_file"},
        {"type": "input_file", "file_id": "id", "file_url": None},
        {"type": "input_file", "file_url": "http://files.invalid/a"},
        {"type": "input_file", "file_data": "Zh=="},
        {"type": "input_file", "file_data": "not base64!"},
        {"type": "input_file", "file_id": " ", "filename": "a"},
        {"type": "input_file", "file_id": "id", "filename": 1},
    ],
)
def test_bad_media_fails_during_capture(part: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        _native_input({"role": "user", "content": [part]})  # type: ignore[typeddict-item]


def test_old_factories_and_native_text_metadata_keep_their_meaning() -> None:
    image = "data:image/png;base64,AA=="
    assert ToolOutput.image(image).to_native() == ToolOutput([ToolOutputImage(image)]).to_native()
    assert (
        ToolOutput.file(file_id="id").to_native()
        == ToolOutput([ToolOutputFile(file_id="id")]).to_native()
    )
    assert ToolOutput.image(file_id="id", detail="low").to_native()["contentItems"] == [
        {"type": "inputImage", "fileId": "id", "detail": "low"}
    ]
    assert _response_content([{"type": "text", "text": "a", "text_elements": []}]) == [
        {"type": "input_text", "text": "a"}
    ]


def test_raw_history_retains_file_content_without_granting_authority() -> None:
    content = [{"type": "input_file", "file_id": "opaque", "filename": "a.pdf"}]
    item = _response_item(
        {"type": "message", "id": "m", "role": "user", "content": content}, "turn"
    )
    assert item is not None and item["content"] == content


def test_live_native_tool_projection_preserves_ids_detail_and_files() -> None:
    output = [
        {"type": "inputImage", "fileId": "opaque-image", "detail": "low"},
        {"type": "inputFile", "file_id": "opaque", "filename": "a.pdf"},
    ]
    events = EventProjector("session").python_tool(
        {"callId": "call", "tool": "picture", "arguments": {}, "turnId": "turn"},
        {"success": True, "contentItems": output},
    )
    item = next(
        event["item"]
        for event in events
        if event.get("item", {}).get("type") == "function_call_output"
    )
    assert item["output"] == _response_content(output)
