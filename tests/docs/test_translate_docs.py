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


@pytest.mark.parametrize("stale_lang", ["ja", "ko", "zh"])
@pytest.mark.parametrize("stale_timestamp", [0, 99])
def test_translation_freshness_checks_every_language(
    translate_docs: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    stale_lang: str,
    stale_timestamp: int,
) -> None:
    source_path = translate_docs.os.path.join("docs", "agents.md")
    timestamps = {source_path: 100}
    for lang_code in translate_docs.languages:
        timestamps[translate_docs.os.path.join("docs", lang_code, "agents.md")] = 100
    timestamps[translate_docs.os.path.join("docs", stale_lang, "agents.md")] = stale_timestamp

    monkeypatch.setattr(translate_docs.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        translate_docs,
        "git_last_commit_timestamp",
        lambda path: timestamps.get(path, 0),
    )

    assert translate_docs.should_translate_based_on_translation(source_path) is True


@pytest.mark.parametrize("missing_lang", ["ja", "ko", "zh"])
def test_translation_freshness_checks_filesystem_for_deleted_translation(
    translate_docs: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    missing_lang: str,
) -> None:
    source_path = translate_docs.os.path.join("docs", "agents.md")
    missing_path = translate_docs.os.path.join("docs", missing_lang, "agents.md")
    timestamps = {source_path: 100}
    for lang_code in translate_docs.languages:
        timestamps[translate_docs.os.path.join("docs", lang_code, "agents.md")] = 101

    # Git still reports the deletion commit for a missing tracked file. The filesystem
    # check must win even though that timestamp is newer than the English source.
    monkeypatch.setattr(translate_docs.os.path, "exists", lambda path: path != missing_path)
    monkeypatch.setattr(
        translate_docs,
        "git_last_commit_timestamp",
        lambda path: timestamps.get(path, 0),
    )

    assert translate_docs.should_translate_based_on_translation(source_path) is True


def test_translation_freshness_skips_when_all_languages_are_current(
    translate_docs: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = translate_docs.os.path.join("docs", "agents.md")
    timestamps = {source_path: 100}
    for lang_code in translate_docs.languages:
        timestamps[translate_docs.os.path.join("docs", lang_code, "agents.md")] = 100

    monkeypatch.setattr(translate_docs.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(
        translate_docs,
        "git_last_commit_timestamp",
        lambda path: timestamps.get(path, 0),
    )

    assert translate_docs.should_translate_based_on_translation(source_path) is False
