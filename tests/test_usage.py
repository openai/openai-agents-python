from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents.usage import RequestUsage, Usage


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


def test_request_usage_creation():
    """Test that RequestUsage is created correctly."""
    request_usage = RequestUsage(
        input_tokens=100,
        output_tokens=200,
        total_tokens=300,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
    )

    assert request_usage.input_tokens == 100
    assert request_usage.output_tokens == 200
    assert request_usage.total_tokens == 300
    assert request_usage.input_tokens_details.cached_tokens == 10
    assert request_usage.output_tokens_details.reasoning_tokens == 20


def test_usage_add_preserves_single_request():
    """Test that adding a single request Usage creates an RequestUsage entry."""
    u1 = Usage()
    u2 = Usage(
        requests=1,
        input_tokens=100,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens=200,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
        total_tokens=300,
    )

    u1.add(u2)

    # Should preserve the request usage details
    assert len(u1.request_usage_entries) == 1
    request_usage = u1.request_usage_entries[0]
    assert request_usage.input_tokens == 100
    assert request_usage.output_tokens == 200
    assert request_usage.total_tokens == 300
    assert request_usage.input_tokens_details.cached_tokens == 10
    assert request_usage.output_tokens_details.reasoning_tokens == 20


def test_usage_add_ignores_zero_token_requests():
    """Test that zero-token requests don't create request_usage_entries."""
    u1 = Usage()
    u2 = Usage(
        requests=1,
        input_tokens=0,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens=0,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=0,
    )

    u1.add(u2)

    # Should not create a request_usage_entry for zero tokens
    assert len(u1.request_usage_entries) == 0


def test_usage_add_ignores_multi_request_usage():
    """Test that multi-request Usage objects don't create request_usage_entries."""
    u1 = Usage()
    u2 = Usage(
        requests=3,  # Multiple requests
        input_tokens=100,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens=200,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
        total_tokens=300,
    )

    u1.add(u2)

    # Should not create a request usage entry for multi-request usage
    assert len(u1.request_usage_entries) == 0


def test_usage_add_merges_existing_request_usage_entries():
    """Test that existing request_usage_entries are merged when adding Usage objects."""
    # Create first usage with request_usage_entries
    u1 = Usage()
    u2 = Usage(
        requests=1,
        input_tokens=100,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens=200,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
        total_tokens=300,
    )
    u1.add(u2)

    # Create second usage with request_usage_entries
    u3 = Usage(
        requests=1,
        input_tokens=50,
        input_tokens_details=InputTokensDetails(cached_tokens=5),
        output_tokens=75,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=10),
        total_tokens=125,
    )

    u1.add(u3)

    # Should have both request_usage_entries
    assert len(u1.request_usage_entries) == 2

    # First request
    first = u1.request_usage_entries[0]
    assert first.input_tokens == 100
    assert first.output_tokens == 200
    assert first.total_tokens == 300

    # Second request
    second = u1.request_usage_entries[1]
    assert second.input_tokens == 50
    assert second.output_tokens == 75
    assert second.total_tokens == 125


def test_usage_add_with_pre_existing_request_usage_entries():
    """Test adding Usage objects that already have request_usage_entries."""
    u1 = Usage()

    # Create a usage with request_usage_entries
    u2 = Usage(
        requests=1,
        input_tokens=100,
        input_tokens_details=InputTokensDetails(cached_tokens=10),
        output_tokens=200,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=20),
        total_tokens=300,
    )
    u1.add(u2)

    # Create another usage with request_usage_entries
    u3 = Usage(
        requests=1,
        input_tokens=50,
        input_tokens_details=InputTokensDetails(cached_tokens=5),
        output_tokens=75,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=10),
        total_tokens=125,
    )

    # Add u3 to u1
    u1.add(u3)

    # Should have both request_usage_entries
    assert len(u1.request_usage_entries) == 2
    assert u1.request_usage_entries[0].input_tokens == 100
    assert u1.request_usage_entries[1].input_tokens == 50


def test_usage_request_usage_entries_default_empty():
    """Test that request_usage_entries defaults to an empty list."""
    u = Usage()
    assert u.request_usage_entries == []


def test_anthropic_cost_calculation_scenario():
    """Test a realistic scenario for Sonnet 4.5 cost calculation with 200K token thresholds."""
    # Simulate 3 API calls: 100K, 150K, and 80K input tokens each
    # None exceed 200K, so they should all use the lower pricing tier

    usage = Usage()

    # First request: 100K input tokens
    req1 = Usage(
        requests=1,
        input_tokens=100_000,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens=50_000,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=150_000,
    )
    usage.add(req1)

    # Second request: 150K input tokens
    req2 = Usage(
        requests=1,
        input_tokens=150_000,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens=75_000,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=225_000,
    )
    usage.add(req2)

    # Third request: 80K input tokens
    req3 = Usage(
        requests=1,
        input_tokens=80_000,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens=40_000,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        total_tokens=120_000,
    )
    usage.add(req3)

    # Verify aggregated totals
    assert usage.requests == 3
    assert usage.input_tokens == 330_000  # 100K + 150K + 80K
    assert usage.output_tokens == 165_000  # 50K + 75K + 40K
    assert usage.total_tokens == 495_000  # 150K + 225K + 120K

    # Verify request_usage_entries preservation
    assert len(usage.request_usage_entries) == 3
    assert usage.request_usage_entries[0].input_tokens == 100_000
    assert usage.request_usage_entries[1].input_tokens == 150_000
    assert usage.request_usage_entries[2].input_tokens == 80_000

    # All request_usage_entries are under 200K threshold
    for req in usage.request_usage_entries:
        assert req.input_tokens < 200_000
        assert req.output_tokens < 200_000
