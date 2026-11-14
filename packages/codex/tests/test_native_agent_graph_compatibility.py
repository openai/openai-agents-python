"""Preserve a prior native declaration graph across additive workspace support."""

from __future__ import annotations

import json
from pathlib import Path


def _legacy_graph() -> tuple[list[dict[str, object]], str, str]:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/native-legacy-agent-graph.json").read_text()
    )
    return fixture["roster"], fixture["root_id"], fixture["expected_fingerprint"]


def test_prior_agent_graph_fingerprint_remains_compatible() -> None:
    from agents import _native

    roster, root, expected = _legacy_graph()
    assert _native._agent_profiles_fingerprint(json.dumps(roster), root) == expected


def test_explicit_false_workspace_preparation_keeps_prior_fingerprint() -> None:
    from agents import _native

    roster, root, expected = _legacy_graph()
    for profile in roster:
        profile["workspace_preparation"] = False
    assert _native._agent_profiles_fingerprint(json.dumps(roster), root) == expected


def test_workspace_preparation_remains_bound_when_enabled() -> None:
    from agents import _native

    roster, root, expected = _legacy_graph()
    roster[0]["workspace_preparation"] = True
    assert _native._agent_profiles_fingerprint(json.dumps(roster), root) != expected
