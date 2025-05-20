from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents.usage import Usage


def test_usage_add_aggregates_all_fields():
    u1 = Usage(
        requests=1,
        input_tokens=10,
        input_tokens_details=InputTokensDetails(cached_tokens=3),
        output_tokens=20,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
        total_tokens=30,
    )
    u2 = Usage(
        requests=2,
        input_tokens=7,
        input_tokens_details=InputTokensDetails(cached_tokens=4),
        output_tokens=8,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=6),
        total_tokens=15,
    )

    u1.add(u2)

    assert u1.requests == 3
    assert u1.input_tokens == 17
    assert u1.output_tokens == 28
    assert u1.total_tokens == 45
    assert u1.input_tokens_details.cached_tokens == 7
    assert u1.output_tokens_details.reasoning_tokens == 11


def test_usage_add_aggregates_with_none_values():
    u1 = Usage()
    u2 = Usage(
        requests=2,
        input_tokens=7,
        input_tokens_details=InputTokensDetails(cached_tokens=4),
        output_tokens=8,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=6),
        total_tokens=15,
    )

    u1.add(u2)

    assert u1.requests == 2
    assert u1.input_tokens == 7
    assert u1.output_tokens == 8
    assert u1.total_tokens == 15
    assert u1.input_tokens_details.cached_tokens == 4
    assert u1.output_tokens_details.reasoning_tokens == 6
