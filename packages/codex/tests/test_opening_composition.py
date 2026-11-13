"""Private opening producers retain canonical capture and caller precedence."""

from typing import Any

import pytest

from _agents_codex import _opening as opening
from agents import Agent, HttpMCP, function_tool


def capture(agent: Agent[Any, Any], **overrides: Any) -> Agent[Any, Any]:
    options = {"provider": None, "model": None, "model_settings": None, "tool_selection": None}
    options.update(overrides)
    return opening._capture_opening_agent(agent, **options)


@pytest.mark.parametrize(
    "declared,override,provided",
    [
        (None, None, False),
        ([], None, True),
        (None, [], True),
    ],
)
def test_default_producer_preserves_omitted_and_empty_tools(declared, override, provided):
    agent = Agent(instructions="Answer", tools=declared)
    captured = capture(agent, tool_selection=override)
    assert captured.tools == ()
    assert captured._hosted_tools_provided is provided
    assert captured.before_complete is None


def test_private_producer_selects_typed_tools_and_one_policy_without_binding(monkeypatch):
    effects = []
    producers = []

    def make_function(label):
        @function_tool(name="echo")
        def echo(value: str) -> str:
            """Return a synthetic value."""
            effects.append(label)
            return label + value

        return echo

    first, second = make_function("A"), make_function("B")
    first_mcp = HttpMCP(name="docs", url="http://127.0.0.1:1/A", tools=["lookup"])
    second_mcp = HttpMCP(name="docs", url="http://127.0.0.1:1/B", tools=["lookup"])

    def first_policy(request):
        effects.append("policy-A")

    def second_policy(request):
        effects.append("policy-B")

    agent = Agent(instructions="Answer", tools=[first, first_mcp], before_complete=first_policy)

    def forbidden_binding(*args, **kwargs):
        raise AssertionError("Opening selection bound a callback")

    def producer(source):
        assert source is agent
        producers.append("B")
        return opening._OpeningContributions((second, second_mcp), second_policy)

    real_capture = type(first)._capture
    monkeypatch.setattr(type(first), "_capture", forbidden_binding)
    configured = capture(agent)
    assert configured.tools == (first, first_mcp)
    assert configured.before_complete is first_policy
    assert effects == []
    monkeypatch.setattr(opening, "_select_opening_contributions", producer)
    captured = capture(agent)
    selected = captured
    assert producers == ["B"]
    assert captured.tools == (second, second_mcp)
    assert captured.before_complete is second_policy
    assert first._spec_json() == second._spec_json()
    assert agent.tools == (first, first_mcp)
    assert agent.before_complete is first_policy
    assert effects == []

    # Canonical capture retains the explicit empty override after private selection.
    captured = capture(agent, tool_selection=[])
    assert captured.tools == ()
    assert captured._hosted_tools_provided is True
    assert captured.before_complete is second_policy
    assert producers == ["B", "B"]
    assert effects == []

    # Bind only through the existing opening owner, without invoking callbacks.
    from _agents_codex.session import _OpeningTools

    binding_calls = []

    def selected_binding(tool, context):
        assert tool is second, "The replaced A binding was captured"
        binding_calls.append(tool)
        return real_capture(tool, context)

    monkeypatch.setattr(type(first), "_capture", selected_binding)
    context = {"binding": "B"}
    declarations = _OpeningTools.capture(selected.tools, False)
    declarations.validate_collisions()
    catalog = declarations.bind(context)
    assert declarations.python == (second,)
    assert declarations.mcp == (second_mcp,)
    assert len(binding_calls) == 1 and binding_calls[0] is second
    assert len(catalog.tools) == 1
    assert catalog.tools[0].context is context
    assert catalog.tools[0].spec_json == second._spec_json()
    assert effects == []

    # Invalid selected MCP aliases retain the canonical collision owner.
    monkeypatch.setattr(
        opening,
        "_select_opening_contributions",
        lambda source: opening._OpeningContributions((first_mcp, second_mcp), second_policy),
    )
    conflicting = capture(agent)
    with pytest.raises(ValueError, match="MCP server aliases have colliding native namespaces"):
        _OpeningTools.capture(conflicting.tools, False).validate_collisions()
    assert len(binding_calls) == 1
    assert effects == []


def test_model_settings_failure_precedes_private_selection(monkeypatch):
    agent = Agent(instructions="Answer")
    failure = ValueError("synthetic settings failure")
    selections = []

    def reject_settings(value):
        raise failure

    def producer(source):
        selections.append(source)
        raise AssertionError("Selection preceded model-settings validation")

    monkeypatch.setattr(opening, "_copy_model_settings", reject_settings)
    monkeypatch.setattr(opening, "_select_opening_contributions", producer)
    with pytest.raises(ValueError) as caught:
        capture(agent)
    assert caught.value is failure
    assert selections == []
