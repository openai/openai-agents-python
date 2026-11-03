"""A guide-only regression must fail at the copied page and source line."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/check_docs.py"
SPEC = importlib.util.spec_from_file_location("check_docs", SCRIPT)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)
TESTING = SCRIPT.parents[2] / "docs/sdk/testing.md"


def test_missing_sdk_import_reports_page_line(tmp_path: Path) -> None:
    page = tmp_path / "broken-import.md"
    page.write_text(
        "# Copied fragment\n\n```python\nfrom agents import MissingDocumentationSymbol\n```\n"
    )
    with pytest.raises(ValueError, match=r"broken-import\.md:4:.*MissingDocumentationSymbol"):
        checker.check_page(page)


def test_missing_import_in_complete_guide_fails(tmp_path: Path) -> None:
    page = tmp_path / "missing-import.md"
    page.write_text(
        TESTING.read_text().replace(
            "from agents import Agent, function_tool", "from agents import Agent"
        )
    )
    with pytest.raises(
        ValueError, match=r"missing-import\.md:\d+:[\s\S]*NameError[\s\S]*function_tool"
    ):
        checker.check_page(page)


def test_wrong_effect_assertion_in_exact_guide_fails(tmp_path: Path) -> None:
    page = tmp_path / "wrong-effect.md"
    original = TESTING.read_text()
    mutation = original.replace(
        'assert peer.tool_output("greeting-1") == "Hello, Ada!"',
        'assert peer.tool_output("greeting-1") == "Wrong effect"',
    )
    assert mutation != original
    page.write_text(mutation)
    with pytest.raises(ValueError, match=r"wrong-effect\.md:\d+:[\s\S]*AssertionError"):
        checker.check_page(page)


def test_exact_complete_no_key_guide() -> None:
    assert checker.check_page(TESTING) == 1


@pytest.mark.parametrize("mode", ["text", "tools", "strict"])
async def test_exact_custom_endpoint_smoke_recipe(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.testing import ScriptedResponses, text, tool_call

    responses = (
        [[tool_call("lookup_label", {}, call_id="label")], [text("atlas")]]
        if mode == "tools"
        else [[text('{"label":"atlas"}' if mode == "strict" else "atlas")]]
    )
    page = SCRIPT.parents[2] / "docs/sdk/providers.md"
    [(line, code, _)] = list(checker.python_fences(page))
    namespace = {"__name__": "documentation_smoke"}
    exec(compile("\n" * (line - 1) + code, str(page), "exec"), namespace)
    async with ScriptedResponses(*responses) as peer:
        monkeypatch.setenv("OPENAI_BASE_URL", peer.provider.base_url)
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
        monkeypatch.setenv("MODEL", "gpt-5.2")
        monkeypatch.setattr("sys.argv", ["check_endpoint.py", mode])
        await namespace["main"]()
        if mode == "tools":
            assert peer.tool_output("label") == "atlas"
        assert len(peer.requests) == (2 if mode == "tools" else 1)


def test_topic_index_rejects_wrong_package_version(tmp_path: Path) -> None:
    import json

    (tmp_path / "docs/sdk").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('version = "2.0"\n')
    (tmp_path / "docs/sdk/topics.json").write_text(
        json.dumps({"package_version": "1.0", "topics": []})
    )
    with pytest.raises(ValueError, match="topics.json: package_version"):
        checker.check_topics(tmp_path)
