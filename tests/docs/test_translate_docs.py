from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "docs" / "scripts" / "translate_docs.py"

SOURCE = """# Agents

## Dynamic instructions

Text.

## Example

```python
# not a heading
```

## Example
"""

TRANSLATED = """# エージェント

## 動的な指示

本文。

## 例

```python
# not a heading
```

## 例
"""


@pytest.fixture
def translate_docs(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    # The script builds an OpenAI client at import time; nothing here sends a request.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    spec = importlib.util.spec_from_file_location("translate_docs", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_translated_headings_carry_the_english_ids(translate_docs: ModuleType) -> None:
    result = translate_docs.preserve_heading_anchors(SOURCE, TRANSLATED)

    assert "## 動的な指示 {#dynamic-instructions}\n" in result
    assert "## 例 {#example}\n" in result
    assert "## 例 {#example_1}\n" in result
    # The H1 is left for mkdocs to read the page title from.
    assert result.startswith("# エージェント\n")
    # A comment inside a fenced block is not a heading.
    assert "# not a heading\n" in result
    assert "# not a heading {#" not in result


def test_heading_ids_come_from_the_rendered_english_headings(translate_docs: ModuleType) -> None:
    source = (
        "## Using `Agent` with [tools](tools.md)\n\n"
        "## [API][ref]\n\n"
        "## A &amp; B\n\n"
        "## <code>run</code> loop\n\n"
        "[ref]: https://example.com\n"
    )
    translated = "## `Agent` とツール\n\n## API\n\n## A と B\n\n## 実行ループ\n"

    result = translate_docs.preserve_heading_anchors(source, translated)

    assert result == (
        "## `Agent` とツール {#using-agent-with-tools}\n\n"
        "## API {#api}\n\n"
        "## A と B {#a-b}\n\n"
        "## 実行ループ {#run-loop}\n"
    )


def test_preserve_heading_anchors_is_idempotent(translate_docs: ModuleType) -> None:
    once = translate_docs.preserve_heading_anchors(SOURCE, TRANSLATED)

    assert translate_docs.preserve_heading_anchors(SOURCE, once) == once


def test_an_id_written_earlier_follows_the_english_heading(translate_docs: ModuleType) -> None:
    result = translate_docs.preserve_heading_anchors("## Alpha\n", "## アルファ {#old}\n")

    assert result == "## アルファ {#alpha}\n"


def test_mismatched_headings_are_left_alone(translate_docs: ModuleType) -> None:
    missing_one_heading = TRANSLATED.replace("\n## 例\n", "\n", 1)

    result = translate_docs.preserve_heading_anchors(SOURCE, missing_one_heading)

    assert result == missing_one_heading


def test_an_english_setext_heading_still_yields_its_id(translate_docs: ModuleType) -> None:
    # The English side goes through the parser, so setext is just another heading there.
    source = "Alpha\n-----\n\n## Beta\n"
    translated = "## アルファ\n\n## ベータ\n"

    result = translate_docs.preserve_heading_anchors(source, translated)

    assert result == "## アルファ {#alpha}\n\n## ベータ {#beta}\n"


def test_a_setext_heading_in_the_translation_is_outside_the_contract(
    translate_docs: ModuleType,
) -> None:
    source = "## Alpha\n\n## Beta\n"
    translated = "アルファ\n-----\n\n## ベータ\n"

    assert translate_docs.preserve_heading_anchors(source, translated) == translated


def test_a_heading_with_its_own_attribute_list_is_not_rewritten(translate_docs: ModuleType) -> None:
    source = "## Alpha\n\n## Beta\n"
    translated = "## アルファ {.lead}\n\n## ベータ\n"

    assert translate_docs.preserve_heading_anchors(source, translated) == translated
