from __future__ import annotations

import pytest
from openai import omit

from _agents_codex.session import _native_model_settings
from agents import Agent


def test_public_transport_settings_detach_and_project():
    headers = {"x-route": "synthetic", "x-remove": omit}
    query = {"count": 2, "nested": {"flag": None}, "tags": ["one", "two"]}
    agent = Agent(
        instructions="Reply briefly.",
        model_settings={
            "extra_headers": headers,
            "extra_query": query,
        },
    )
    headers["x-route"] = "changed"
    query["nested"]["flag"] = "changed"
    result = _native_model_settings(None, agent.model_settings)
    assert result == {
        "request_controls": {
            "request_transport": {
                "headers": {"x-route": "synthetic", "x-remove": None},
                "query": {"count": 2, "nested": {"flag": None}, "tags": ["one", "two"]},
                "query_nulls_as_empty": False,
            }
        }
    }


def test_omitted_transport_settings_do_not_add_a_carrier():
    assert _native_model_settings(None, {}) == {}


@pytest.mark.parametrize("value", [None, {}])
def test_explicit_transport_reset_and_empty_are_distinct(value):
    agent = Agent(
        instructions="Reply briefly.",
        model_settings={
            "extra_headers": value,
            "extra_query": value,
        },
    )
    assert _native_model_settings(None, agent.model_settings) == {
        "request_controls": {
            "request_transport": {
                "headers": value,
                "query": value,
                "query_nulls_as_empty": False,
            }
        }
    }


@pytest.mark.parametrize(
    "settings",
    [
        {"extra_headers": {"x-route": 3}},
        {"extra_headers": [("x-route", "value")]},
        {"extra_query": {"value": float("nan")}},
        {"extra_query": {3: "value"}},
    ],
)
def test_invalid_transport_settings_reject_at_public_capture(settings):
    with pytest.raises((TypeError, ValueError)):
        Agent(instructions="Reply briefly.", model_settings=settings)


@pytest.mark.parametrize(
    ("settings", "carrier"),
    [
        ({"extra_headers": {"x-route": "value"}}, {"headers": {"x-route": "value"}}),
        ({"extra_query": {"flag": None}}, {"query": {"flag": None}, "query_nulls_as_empty": False}),
    ],
)
def test_transport_field_projection_stays_sparse(settings, carrier):
    agent = Agent(instructions="Reply briefly.", model_settings=settings)
    assert _native_model_settings(None, agent.model_settings) == {
        "request_controls": {"request_transport": carrier}
    }
