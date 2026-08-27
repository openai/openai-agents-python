import json
from types import MappingProxyType

from openai.types.responses.response_output_message_param import ResponseOutputMessageParam
from openai.types.responses.response_output_text_param import ResponseOutputTextParam

from agents.util._json import _to_dump_compatible


def test_to_dump_compatible():
    # Given a list of message dictionaries, ensure the returned list is a deep copy.
    input_iter = [
        ResponseOutputMessageParam(
            id="a75654dc-7492-4d1c-bce0-89e8312fbdd7",
            content=[
                ResponseOutputTextParam(
                    type="output_text",
                    text="Hey, what's up?",
                    annotations=[],
                    logprobs=[],
                )
            ].__iter__(),
            role="assistant",
            status="completed",
            type="message",
        )
    ].__iter__()
    # this fails if any of the properties are Iterable objects.
    # result = json.dumps(input_iter)
    result = json.dumps(_to_dump_compatible(input_iter))
    assert (
        result
        == """[{"id": "a75654dc-7492-4d1c-bce0-89e8312fbdd7", "content": [{"type": "output_text", "text": "Hey, what's up?", "annotations": [], "logprobs": []}], "role": "assistant", "status": "completed", "type": "message"}]"""  # noqa: E501
    )


def test_to_dump_compatible_preserves_non_dict_mapping_values():
    # A non-dict Mapping (e.g. MappingProxyType) must be preserved as an object,
    # recursing into its values, instead of collapsing to a list of its keys.
    out = _to_dump_compatible({"config": MappingProxyType({"timeout": 30, "retries": 3})})
    assert out == {"config": {"timeout": 30, "retries": 3}}
    # A top-level mapping is preserved as an object, not flattened to its keys.
    assert _to_dump_compatible(MappingProxyType({"a": 1, "b": 2})) == {"a": 1, "b": 2}
