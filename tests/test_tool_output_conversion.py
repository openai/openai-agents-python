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


def test_tool_call_output_item_text_dict_variant() -> None:
    """Dict with type='text' and text field should be treated as structured output."""
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


def test_tool_call_output_item_image_dict_variant() -> None:
    """Dict with type='image' and image_url field should be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "image", "image_url": "http://example.com/img.png", "detail": "auto"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_image"
    assert item["image_url"] == "http://example.com/img.png"
    assert item["detail"] == "auto"


def test_tool_call_output_item_image_dict_variant_with_file_id() -> None:
    """Dict with type='image' and image_url field should be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "image", "file_id": "file_123"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_image"
    assert item["file_id"] == "file_123"


def test_tool_call_output_item_file_dict_variant_with_file_data() -> None:
    """Dict with type='file' and file_data field should be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "file", "file_data": "foobar", "filename": "report.pdf"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_file"
    assert item["file_data"] == "foobar"
    assert item["filename"] == "report.pdf"


def test_tool_call_output_item_file_dict_variant_with_file_url() -> None:
    """Dict with type='file' and file_url field should be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "file", "file_url": "https://example.com/report.pdf", "filename": "report.pdf"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_file"
    assert item["file_url"] == "https://example.com/report.pdf"
    assert item["filename"] == "report.pdf"


def test_tool_call_output_item_file_dict_variant_with_file_id() -> None:
    """Dict with type='file' and file_id field should be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "file", "file_id": "file_123", "filename": "report.pdf"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_file"
    assert item["file_id"] == "file_123"
    assert item["filename"] == "report.pdf"


def test_tool_call_output_item_image_with_extra_fields() -> None:
    """Dict with type='image', image_url, and extra fields should still be converted."""
    call = _make_tool_call()
    out = {"type": "image", "image_url": "http://example.com/img.png", "foobar": 213}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 1
    item = payload["output"][0]
    assert isinstance(item, dict)
    assert item["type"] == "input_image"
    assert item["image_url"] == "http://example.com/img.png"
    # Extra field should be ignored by Pydantic
    assert "foobar" not in item


def test_tool_call_output_item_mixed_list_with_valid_dicts() -> None:
    """List with valid dict variants (with type field) should be converted."""
    call = _make_tool_call()
    out = [
        {"type": "text", "text": "hello"},
        {"type": "image", "image_url": "http://example.com/img.png"},
        {"type": "file", "file_id": "file_123"},
    ]
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], list) and len(payload["output"]) == 3

    assert payload["output"][0]["type"] == "input_text"
    assert payload["output"][0]["text"] == "hello"
    assert payload["output"][1]["type"] == "input_image"
    assert payload["output"][1]["image_url"] == "http://example.com/img.png"
    assert payload["output"][2]["type"] == "input_file"
    assert payload["output"][2]["file_id"] == "file_123"


def test_tool_call_output_item_text_type_only_not_converted() -> None:
    """Dict with only type='text' should NOT be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "text"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    # Should be converted to string since it doesn't have required fields
    assert isinstance(payload["output"], str)
    assert payload["output"] == "{'type': 'text'}"


def test_tool_call_output_item_image_type_only_not_converted() -> None:
    """Dict with only type='image' should NOT be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "image"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    # Should be converted to string since it doesn't have required fields
    assert isinstance(payload["output"], str)
    assert payload["output"] == "{'type': 'image'}"


def test_tool_call_output_item_file_type_only_not_converted() -> None:
    """Dict with only type='file' should NOT be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "file"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], str)
    assert payload["output"] == "{'type': 'file'}"


def test_tool_call_output_item_empty_dict_not_converted() -> None:
    """Empty dict should NOT be treated as structured output."""
    call = _make_tool_call()
    out: dict[str, str] = {}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    assert isinstance(payload["output"], str)
    assert payload["output"] == "{}"


def test_tool_call_output_item_dict_without_type_not_converted() -> None:
    """Dict without 'type' field should NOT be treated as structured output."""
    call = _make_tool_call()
    out = {"msg": "1234"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    # Should be converted to string since it lacks 'type' field
    assert isinstance(payload["output"], str)
    assert payload["output"] == "{'msg': '1234'}"


def test_tool_call_output_item_image_dict_variant_with_location_not_converted() -> None:
    """Dict with type='image' and location field should NOT be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "image", "location": "/path/to/img.png"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    # Should be converted to string since it lacks required fields (image_url or file_id)
    assert isinstance(payload["output"], str)
    assert payload["output"] == "{'type': 'image', 'location': '/path/to/img.png'}"


def test_tool_call_output_item_file_dict_variant_with_path_not_converted() -> None:
    """Dict with type='file' and path field should NOT be treated as structured output."""
    call = _make_tool_call()
    out = {"type": "file", "path": "/path/to/file.txt"}
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    # Should be converted to string since it lacks required fields (file_data, file_url, or file_id)
    assert isinstance(payload["output"], str)
    assert payload["output"] == "{'type': 'file', 'path': '/path/to/file.txt'}"


def test_tool_call_output_item_list_without_type_not_converted() -> None:
    """List with dicts lacking 'type' field should NOT be treated as structured output."""
    call = _make_tool_call()
    out = [{"msg": "foobar"}]
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    # Should be converted to string since list items lack 'type' field
    assert isinstance(payload["output"], str)
    assert payload["output"] == "[{'msg': 'foobar'}]"


def test_tool_call_output_item_mixed_list_partial_invalid_not_converted() -> None:
    """List with mix of valid and invalid dicts should NOT be treated as structured output."""
    call = _make_tool_call()
    out = [
        {"type": "text", "text": "hello"},  # Valid
        {"msg": "foobar"},  # Invalid
    ]
    payload = ItemHelpers.tool_call_output_item(call, out)

    assert payload["type"] == "function_call_output"
    assert payload["call_id"] == call.call_id
    # All-or-nothing: if any item is invalid, convert entire list to string
    assert isinstance(payload["output"], str)
    assert payload["output"] == "[{'type': 'text', 'text': 'hello'}, {'msg': 'foobar'}]"
