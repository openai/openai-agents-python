from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents.usage import RequestUsage, Usage


def test_usage_add_copies_preexisting_request_entries() -> None:
    entry = RequestUsage(
        input_tokens=3,
        output_tokens=4,
        total_tokens=7,
        input_tokens_details=InputTokensDetails.model_validate(
            {"cache_write_tokens": 0, "cached_tokens": 1}
        ),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=2),
    )
    source = Usage(
        requests=1,
        input_tokens=3,
        output_tokens=4,
        total_tokens=7,
        request_usage_entries=[entry],
    )
    aggregate = Usage()

    aggregate.add(source)

    copied_entry = aggregate.request_usage_entries[0]
    assert copied_entry is not entry

    entry.input_tokens = 99
    entry.input_tokens_details.cached_tokens = 99
    copied_entry.output_tokens_details.reasoning_tokens = 77

    assert copied_entry.input_tokens == 3
    assert copied_entry.input_tokens_details.cached_tokens == 1
    assert entry.output_tokens_details.reasoning_tokens == 2


def test_usage_add_copies_synthesized_request_details() -> None:
    source = Usage(
        requests=1,
        input_tokens=3,
        output_tokens=4,
        total_tokens=7,
        input_tokens_details=InputTokensDetails.model_validate(
            {"cache_write_tokens": 0, "cached_tokens": 1}
        ),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=2),
    )
    aggregate = Usage()

    aggregate.add(source)

    copied_entry = aggregate.request_usage_entries[0]
    source.input_tokens_details.cached_tokens = 99
    copied_entry.output_tokens_details.reasoning_tokens = 77

    assert copied_entry.input_tokens_details.cached_tokens == 1
    assert source.output_tokens_details.reasoning_tokens == 2
