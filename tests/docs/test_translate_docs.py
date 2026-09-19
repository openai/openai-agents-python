from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
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


def test_ref_pages_are_skipped_with_windows_separators(
    translate_docs: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    translated: list[tuple[str, str, str]] = []
    monkeypatch.setattr(translate_docs.os.path, "relpath", lambda *_args: r"ref\voice\model.md")
    monkeypatch.setattr(translate_docs.os, "makedirs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        translate_docs,
        "translate_file",
        lambda file_path, target_path, lang_code: translated.append(
            (file_path, target_path, lang_code)
        ),
    )

    translate_docs.translate_single_source_file(
        r"docs\ref\voice\model.md",
        check_translation_outdated=False,
    )

    assert translated == []


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")


def test_rebase_relative_target_adds_parent_for_untranslated_ref(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    english_dir = tmp_path / "docs"
    locale_dir = tmp_path / "docs" / "ja"
    _touch(english_dir / "ref" / "lifecycle.md")
    _touch(english_dir / "agents.md")
    _touch(locale_dir / "agents.md")

    assert (
        translate_docs.rebase_relative_target(
            "ref/lifecycle.md",
            source_page_dir=english_dir,
            locale_page_dir=locale_dir,
        )
        == "../ref/lifecycle.md"
    )


def test_rebase_relative_target_adds_parent_for_nested_asset(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    docs = tmp_path / "docs"
    english_dir = docs / "sandbox"
    locale_dir = docs / "ja" / "sandbox"
    _touch(docs / "assets" / "images" / "harness_with_compute.png")
    _touch(english_dir / "guide.md")
    _touch(locale_dir / "guide.md")

    assert (
        translate_docs.rebase_relative_target(
            "../assets/images/harness_with_compute.png",
            source_page_dir=english_dir,
            locale_page_dir=locale_dir,
        )
        == "../../assets/images/harness_with_compute.png"
    )


def test_rebase_relative_target_keeps_a_translated_sibling(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    english_dir = tmp_path / "docs"
    locale_dir = tmp_path / "docs" / "ja"
    _touch(english_dir / "tools.md")
    _touch(locale_dir / "tools.md")

    assert (
        translate_docs.rebase_relative_target(
            "tools.md",
            source_page_dir=english_dir,
            locale_page_dir=locale_dir,
        )
        == "tools.md"
    )


def test_rebase_relative_target_keeps_translatable_sibling_before_locale_exists(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    english_dir = tmp_path / "docs"
    locale_dir = tmp_path / "docs" / "ja"
    _touch(english_dir / "agents.md")
    _touch(english_dir / "tools.md")
    locale_dir.mkdir(parents=True)

    assert (
        translate_docs.rebase_relative_target(
            "tools.md",
            source_page_dir=english_dir,
            locale_page_dir=locale_dir,
        )
        == "tools.md"
    )


def test_rebase_relative_target_sibling_result_is_order_independent(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    english_dir = tmp_path / "docs"
    locale_dir = tmp_path / "docs" / "ja"
    locale_sibling = locale_dir / "tools.md"
    _touch(english_dir / "agents.md")
    _touch(english_dir / "tools.md")
    locale_dir.mkdir(parents=True)

    def rebase() -> str:
        return translate_docs.rebase_relative_target(
            "tools.md",
            source_page_dir=english_dir,
            locale_page_dir=locale_dir,
        )

    before = rebase()
    results: list[str] = []

    def rebase_many() -> None:
        for _ in range(32):
            results.append(rebase())

    with ThreadPoolExecutor(max_workers=2) as executor:
        rebase_future = executor.submit(rebase_many)
        write_future = executor.submit(_touch, locale_sibling)
        rebase_future.result()
        write_future.result()

    after = rebase()
    assert before == "tools.md"
    assert after == "tools.md"
    assert results
    assert set(results) == {"tools.md"}


def test_rebase_relative_target_is_idempotent_and_keeps_fragments(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    english_dir = tmp_path / "docs"
    locale_dir = tmp_path / "docs" / "ja"
    _touch(english_dir / "ref" / "lifecycle.md")

    rebased = translate_docs.rebase_relative_target(
        "ref/lifecycle.md#agent-hooks",
        source_page_dir=english_dir,
        locale_page_dir=locale_dir,
    )
    assert rebased == "../ref/lifecycle.md#agent-hooks"
    assert (
        translate_docs.rebase_relative_target(
            rebased,
            source_page_dir=english_dir,
            locale_page_dir=locale_dir,
        )
        == rebased
    )


def test_rebase_relative_target_leaves_external_and_anchor_targets(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    english_dir = tmp_path / "docs"
    locale_dir = tmp_path / "docs" / "ja"
    english_dir.mkdir()
    locale_dir.mkdir(parents=True)

    for target in (
        "https://example.com/ref/lifecycle.md",
        "#lifecycle-events-hooks",
        "/absolute/path.md",
        "mailto:docs@example.com",
    ):
        assert (
            translate_docs.rebase_relative_target(
                target,
                source_page_dir=english_dir,
                locale_page_dir=locale_dir,
            )
            == target
        )


def test_rebase_relative_links_rewrites_markdown_and_images(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    docs = tmp_path / "docs"
    english_dir = docs / "sandbox"
    locale_dir = docs / "ja" / "sandbox"
    _touch(docs / "ref" / "lifecycle.md")
    _touch(docs / "assets" / "images" / "harness_with_compute.png")
    _touch(docs / "tools.md")
    _touch(docs / "ja" / "tools.md")
    _touch(english_dir / "guide.md")
    _touch(locale_dir / "guide.md")

    markdown = (
        "See the [Lifecycle API](../ref/lifecycle.md#hooks) and [Tools](../tools.md).\n"
        "![Harness](../assets/images/harness_with_compute.png)\n"
        "```python\n"
        "# [Lifecycle](../ref/lifecycle.md)\n"
        'agent = Agent[VoiceContext](name="Voice assistant")\n'
        "```\n"
        "Stay on this page: [Hooks](#hooks).\n"
    )

    result = translate_docs.rebase_relative_links(
        markdown,
        source_page_dir=english_dir,
        locale_page_dir=locale_dir,
    )

    assert "[Lifecycle API](../../ref/lifecycle.md#hooks)" in result
    assert "![Harness](../../assets/images/harness_with_compute.png)" in result
    assert "[Tools](../tools.md)" in result
    assert "# [Lifecycle](../ref/lifecycle.md)\n" in result
    assert 'agent = Agent[VoiceContext](name="Voice assistant")' in result
    assert "[Hooks](#hooks)" in result


def test_rebase_relative_links_rewrites_targets_with_inline_code_labels(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    english_dir = tmp_path / "docs"
    locale_dir = tmp_path / "docs" / "ja"
    _touch(english_dir / "ref" / "testing.md")
    _touch(english_dir / "ref" / "memory" / "openai_conversations_session.md")
    _touch(english_dir / "testing.md")
    _touch(locale_dir / "testing.md")

    markdown = (
        "- [`agents.testing`](ref/testing.md)\n"
        "- [`OpenAIConversationsSession`](ref/memory/openai_conversations_session.md)\n"
    )

    result = translate_docs.rebase_relative_links(
        markdown,
        source_page_dir=english_dir,
        locale_page_dir=locale_dir,
    )

    assert "[`agents.testing`](../ref/testing.md)" in result
    assert "[`OpenAIConversationsSession`](../ref/memory/openai_conversations_session.md)" in result


def test_rebase_relative_links_preserves_literal_inline_code_markdown_examples(
    translate_docs: ModuleType, tmp_path: Path
) -> None:
    english_dir = tmp_path / "docs"
    locale_dir = tmp_path / "docs" / "ja"
    _touch(english_dir / "ref" / "x.md")
    _touch(english_dir / "ref" / "testing.md")
    _touch(english_dir / "agents.md")
    _touch(locale_dir / "agents.md")

    markdown = (
        "Document the syntax with `[API](ref/x.md)` and `` `[API](ref/x.md)` ``.\n"
        "Keep a real link with a code label: [`agents.testing`](ref/testing.md).\n"
    )

    result = translate_docs.rebase_relative_links(
        markdown,
        source_page_dir=english_dir,
        locale_page_dir=locale_dir,
    )

    assert "`[API](ref/x.md)`" in result
    assert "`` `[API](ref/x.md)` ``" in result
    assert "`[API](../ref/x.md)`" not in result
    assert "[`agents.testing`](../ref/testing.md)" in result
