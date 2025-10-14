from __future__ import annotations

from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall

from agents import ItemHelpers, ToolOutputFileContent, ToolOutputImage, ToolOutputText


def _make_tool_call() -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        id="call-1",
        arguments="{}",
        call_id="call-1",
        name="dummy",
        type="function_call",
    )


def test_tool_call_output_item_text_model() -> None:
    call = _make_tool_call()
    out = ToolOutputText(text="hello")
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert item["type"] == "input_text"
    assert item["text"] == "hello"


def test_tool_call_output_item_image_model() -> None:
    call = _make_tool_call()
    out = ToolOutputImage(image_url="data:image/png;base64,AAAA")
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_image"
    assert item["image_url"] == "data:image/png;base64,AAAA"


def test_tool_call_output_item_file_model() -> None:
    call = _make_tool_call()
    out = ToolOutputFileContent(file_data="ZmFrZS1kYXRh", filename="foo.txt")
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_file"
    assert item["file_data"] == "ZmFrZS1kYXRh"


def test_tool_call_output_item_mixed_list() -> None:
    call = _make_tool_call()
    outputs = [
        ToolOutputText(text="a"),
        ToolOutputImage(image_url="http://example/img.png"),
        ToolOutputFileContent(file_data="ZmlsZS1kYXRh"),
    ]

    payload = ItemHelpers.tool_call_output_item(call, outputs)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    items = payload["output"]
    assert isinstance(items, list) and len(items) == 3

    assert items[0]["type"] == "input_text" and items[0]["text"] == "a"
    assert items[1]["type"] == "input_image" and items[1]["image_url"] == "http://example/img.png"
    assert items[2]["type"] == "input_file" and items[2]["file_data"] == "ZmlsZS1kYXRh"


def test_tool_call_output_item_text_dict_variant() -> None:
    call = _make_tool_call()
    # Dict variant using the pydantic model schema (type="text").
    out = {"type": "text", "text": "hey"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_text"
    assert item["text"] == "hey"


def test_tool_call_output_item_image_forwards_file_id_and_detail() -> None:
    """Ensure image outputs forward provided file_id and detail fields."""
    call = _make_tool_call()
    out = ToolOutputImage(file_id="file_123", detail="high")
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_image"
    assert item["file_id"] == "file_123"
    assert item["detail"] == "high"


def test_tool_call_output_item_file_forwards_file_id_and_filename() -> None:
    """Ensure file outputs forward provided file_id and filename fields."""
    call = _make_tool_call()
    out = ToolOutputFileContent(file_id="file_456", filename="report.pdf")
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_file"
    assert item["file_id"] == "file_456"
    assert item["filename"] == "report.pdf"


def test_tool_call_output_item_file_forwards_file_url() -> None:
    """Ensure file outputs forward provided file_url when present."""
    call = _make_tool_call()
    out = ToolOutputFileContent(file_url="https://example.com/report.pdf")
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_file"
    assert item["file_url"] == "https://example.com/report.pdf"
