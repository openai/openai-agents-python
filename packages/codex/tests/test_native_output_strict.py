import pytest
from pydantic import BaseModel

from _agents_codex.session import _output_model_settings
from agents.schema import NativeOutput, OutputValidationError, _prepare_spec
from tests.native_fixtures import responses as responses


class OptionalOutput(BaseModel):
    label: str = "default"


def test_provider_nonstrict_preserves_defaults_and_local_validation():
    output = _prepare_spec(NativeOutput(OptionalOutput, strict=False))
    assert "label" not in output.schema.get("required", [])
    assert output.schema["properties"]["label"]["default"] == "default"
    assert output.validate("{}") == OptionalOutput()
    with pytest.raises(OutputValidationError):
        output.validate('{"label":{}}')


def test_provider_nonstrict_supports_native_open_map_schema():
    output = _prepare_spec(NativeOutput(dict[str, int], strict=False))
    assert output.schema["additionalProperties"] == {"type": "integer"}
    assert output.validate('{"count":4}') == {"count": 4}
    with pytest.raises(OutputValidationError):
        output.validate('{"count":"invalid"}')


def test_default_true_fingerprint_stays_stable_and_false_differs():
    default = _prepare_spec(NativeOutput(OptionalOutput))
    explicit = _prepare_spec(NativeOutput(OptionalOutput, strict=True))
    assert default.schema == explicit.schema
    assert default.fingerprint == explicit.fingerprint
    assert (
        _prepare_spec(NativeOutput(OptionalOutput, strict=False)).fingerprint != default.fingerprint
    )


def test_scoped_output_settings_preserve_retry_and_reset_prior_false():
    settings = {
        "request_controls": {"retry": {"max_retries": 2}, "output_schema_strict": False},
        "reasoning_effort": "low",
    }
    output = _prepare_spec(NativeOutput(OptionalOutput))
    assert _output_model_settings(output, settings) == {
        "request_controls": {"retry": {"max_retries": 2}, "output_schema_strict": True},
        "reasoning_effort": "low",
    }
    assert settings["request_controls"]["output_schema_strict"] is False


@pytest.mark.parametrize("strict", [None, 0, "false"])
def test_invalid_strictness_fails_before_resources(strict):
    with pytest.raises(TypeError, match="strict must be a boolean"):
        NativeOutput(OptionalOutput, strict=strict)


async def test_native_provider_wire_keeps_nonstrict_schema_and_local_defaults(responses):
    from agents import Agent, NativeProvider
    from tests.native_fixtures import assistant_output

    responses.respond(assistant_output("{}"))
    agent = Agent(
        instructions="Return JSON.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-output-strictness"),
        output_type=NativeOutput(OptionalOutput, strict=False),
    )
    async with agent.session() as session:
        turn = await session.submit("Reply.")
        assert (await turn.get_result()).final_output.label == "default"
    assert len(responses.requests) == 1
    output = responses.requests[0]["text"]["format"]
    assert output["strict"] is False
    assert "label" not in output["schema"].get("required", [])
    assert output["schema"]["properties"]["label"]["default"] == "default"
