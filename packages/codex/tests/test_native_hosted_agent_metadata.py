from dataclasses import replace
from types import SimpleNamespace

from _agents_codex._hosted_agent_metadata import _bind_hosted_agent_metadata
from _agents_codex.response_facts import _response_fact_record, _summarize_response_fact
from agents import HostedAgentMetadata, ToolCallInfo, ToolRef, get_hosted_agent_metadata


def test_provider_items_expose_only_bounded_agent_name_and_phase():
    value = {
        "agent": {"agent_name": "worker", "namespace": "ignored"},
        "phase": "analysis",
        "text": "private",
    }
    assert get_hosted_agent_metadata(value) == HostedAgentMetadata("worker", "analysis")
    assert get_hosted_agent_metadata(SimpleNamespace(tool_call=value)) == HostedAgentMetadata(
        "worker", "analysis"
    )
    for value in [
        None,
        [],
        {"agent": []},
        {"agent": {"agent_name": ""}},
        {"agent": {"agent_name": 42}},
        {"agent": {"agent_name": "x" * (1024 * 1024 + 1)}},
        {"agent": {"agent_name": "\ud800"}},
    ]:
        assert get_hosted_agent_metadata(value) is None


def test_value_equal_call_ids_keep_exact_admitted_object_metadata():
    first = ToolCallInfo(
        tool=ToolRef("lookup"),
        session_id="session",
        turn_id="turn",
        call_id="reused",
        child_id=None,
        child_incarnation=None,
        role=None,
        model="selected",
        route="direct",
    )
    second = replace(first)
    assert first == second and first is not second
    _bind_hosted_agent_metadata(
        first, {"agent_name": "first", "phase": "analysis", "role": "ignored"}
    )
    _bind_hosted_agent_metadata(second, {"agent_name": "second"})
    assert get_hosted_agent_metadata(SimpleNamespace(call=first)) == HostedAgentMetadata(
        "first", "analysis"
    )
    assert get_hosted_agent_metadata(second) == HostedAgentMetadata("second")
    assert get_hosted_agent_metadata(replace(first)) is None
    assert (first.role, first.model, first.tool.name) == (None, "selected", "lookup")


def test_default_fact_summary_omits_attribution_content():
    record = _response_fact_record(
        {
            "id": "fact",
            "thread_id": "thread",
            "turn_id": "turn",
            "request_id": "native",
            "response_id": "response",
            "fact": {
                "kind": "hosted_agent_attribution",
                "item_fingerprint": "a" * 64,
                "agent_name": "worker",
                "phase": "analysis",
            },
        }
    )
    assert record is not None
    assert _summarize_response_fact(record)["fact"] == {"kind": "hosted_agent_attribution"}
