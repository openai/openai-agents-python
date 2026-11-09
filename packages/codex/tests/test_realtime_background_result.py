"""Background controls retain the declared application result schema."""

from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from _agents_codex.realtime_result import BackgroundResult, background_result
from agents import function_tool


class Record(BaseModel):
    count: int


def test_background_result_validates_payload_and_preserves_ordinary_union():
    @function_tool
    def lookup() -> str | BackgroundResult[Record]:
        return background_result(Record(count=2))

    assert lookup._bind is not None
    adapter = lookup._bind(None, None, lookup).output_adapter
    assert adapter.validate_python("ordinary") == "ordinary"
    accepted = adapter.validate_python(background_result(Record(count=2)))
    assert isinstance(accepted, BackgroundResult)
    assert accepted.value == Record(count=2)
    with pytest.raises(ValidationError):
        adapter.validate_python(background_result({"count": "invalid"}))


def test_background_result_is_immutable_and_revalidates_its_generic_payload():
    @function_tool
    def lookup() -> BackgroundResult[int]:
        return background_result(7)

    assert lookup._bind is not None
    adapter = lookup._bind(None, None, lookup).output_adapter
    result = background_result(7)
    assert adapter.validate_python(result).value == 7
    with pytest.raises(ValidationError):
        adapter.validate_python(background_result("invalid"))
    with pytest.raises(FrozenInstanceError):
        result.value = 8  # type: ignore[misc]


def test_arbitrary_control_shaped_json_remains_application_content():
    @function_tool
    def lookup() -> Any:
        return {"value": "ordinary", "start_response": False}

    assert lookup._bind is not None
    adapter = lookup._bind(None, None, lookup).output_adapter
    value = adapter.validate_python(lookup._bind(None, None, lookup).callback())
    assert not isinstance(value, BackgroundResult)
    assert value == {"value": "ordinary", "start_response": False}
