"""Keep captured validation diagnostics useful and outside ordinary error display."""

from __future__ import annotations

import traceback
from typing import Any

import pytest
from pydantic import BaseModel, field_validator
from pydantic_core import PydanticCustomError

from agents import NativeOutput, OutputValidationError, OutputValidationIssue, PromptedOutput
from agents.schema import _prepare_spec


class Item(BaseModel):
    count: int


class Batch(BaseModel):
    items: list[Item]


@pytest.mark.parametrize("mode", [NativeOutput, PromptedOutput])
def test_diagnostics_identify_nested_application_fields(mode: Any) -> None:
    prepared = _prepare_spec(mode(Batch))
    raw = '{"items":[{"count":"synthetic-private-value"}]}'
    with pytest.raises(OutputValidationError) as failed:
        prepared.validate(raw)
    assert failed.value.issues == (
        OutputValidationIssue(code="int_parsing", path=("items", 0, "count")),
    )
    assert failed.value.raw_output == raw
    assert failed.value.__cause__ is None
    assert failed.value.__context__ is None
    assert "synthetic-private-value" not in str(failed.value)
    assert "synthetic-private-value" not in repr(failed.value)
    assert "synthetic-private-value" not in "".join(traceback.format_exception(failed.value))


@pytest.mark.parametrize(
    "raw,path",
    [('{"response":["wrong"]}', (0,)), ("{}", ()), ('{"response":[1],"extra":2}', ())],
)
def test_native_wrapper_paths_address_application_output(raw: str, path: tuple[Any, ...]) -> None:
    with pytest.raises(OutputValidationError) as failed:
        _prepare_spec(NativeOutput(list[int])).validate(raw)
    assert len(failed.value.issues) == 1
    assert failed.value.issues[0].path == path


def test_application_response_field_is_not_treated_as_a_wrapper() -> None:
    class ApplicationOutput(BaseModel):
        response: list[int]

    with pytest.raises(OutputValidationError) as failed:
        _prepare_spec(NativeOutput(ApplicationOutput)).validate('{"response":["wrong"]}')
    assert failed.value.issues[0].path == ("response", 0)


def test_custom_diagnostics_are_explicit_and_do_not_revalidate() -> None:
    calls: list[str] = []

    class PrivateOutput(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def reject(cls, value: str) -> str:
            calls.append(value)
            raise PydanticCustomError("synthetic-private-code", "synthetic-private-message")

    raw = '{"value":"synthetic-private-input"}'
    with pytest.raises(OutputValidationError) as failed:
        _prepare_spec(PromptedOutput(PrivateOutput)).validate(raw)
    error = failed.value
    assert calls == ["synthetic-private-input"]
    assert error.issues == (OutputValidationIssue(code="synthetic-private-code", path=("value",)),)
    display = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert "synthetic-private" not in display
    assert error.__cause__ is None
    assert error.__context__ is None
    assert all(not isinstance(value, BaseException) for value in vars(error).values())


def test_private_field_names_only_appear_on_diagnostic_access() -> None:
    class PrivateField(BaseModel):
        synthetic_private_field: int

    with pytest.raises(OutputValidationError) as failed:
        _prepare_spec(PromptedOutput(PrivateField)).validate("{}")
    assert failed.value.issues[0].path == ("synthetic_private_field",)
    assert "synthetic_private_field" not in repr(failed.value)


@pytest.mark.parametrize("raw", ["not JSON", '{"response":'])
def test_invalid_json_has_root_diagnostics(raw: str) -> None:
    with pytest.raises(OutputValidationError) as failed:
        _prepare_spec(NativeOutput(int)).validate(raw)
    assert failed.value.issues == (OutputValidationIssue(code="json_invalid", path=()),)


def test_arbitrary_validator_errors_do_not_retain_messages_or_causes() -> None:
    calls: list[str] = []

    class PrivateOutput(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def reject(cls, value: str) -> str:
            calls.append(value)
            raise TypeError("synthetic-private-exception")

    with pytest.raises(OutputValidationError) as failed:
        _prepare_spec(PromptedOutput(PrivateOutput)).validate('{"value":"once"}')
    assert calls == ["once"]
    assert failed.value.issues == (OutputValidationIssue(code="validation_error", path=()),)
    assert failed.value.__cause__ is None
    assert failed.value.__context__ is None
    assert "synthetic-private-exception" not in repr(failed.value)
