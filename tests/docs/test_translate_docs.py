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


def test_mismatched_headings_are_left_alone(translate_docs: ModuleType) -> None:
    missing_one_heading = TRANSLATED.replace("\n## 例\n", "\n", 1)

    result = translate_docs.preserve_heading_anchors(SOURCE, missing_one_heading)

    assert result == missing_one_heading
