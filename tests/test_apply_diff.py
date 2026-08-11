"""Tests for the V4A diff helper."""

from __future__ import annotations

import pytest

from agents import apply_diff


def test_apply_diff_with_floating_hunk_adds_lines() -> None:
    diff = "\n".join(["@@", "+hello", "+world"])  # no trailing newline
    assert apply_diff("", diff) == "hello\nworld\n"


def test_apply_diff_with_empty_input_and_crlf_diff_preserves_crlf() -> None:
    diff = "\r\n".join(["@@", "+hello", "+world"])
    assert apply_diff("", diff) == "hello\r\nworld\r\n"


def test_apply_diff_create_mode_requires_plus_prefix() -> None:
    diff = "plain line"
    with pytest.raises(ValueError):
        apply_diff("", diff, mode="create")


def test_apply_diff_create_mode_preserves_trailing_newline() -> None:
    diff = "\n".join(["+hello", "+world", "+"])
    assert apply_diff("", diff, mode="create") == "hello\nworld\n"


def test_apply_diff_applies_contextual_replacement() -> None:
    input_text = "line1\nline2\nline3\n"
    diff = "\n".join(["@@ line1", "-line2", "+updated", " line3"])
    assert apply_diff(input_text, diff) == "line1\nupdated\nline3\n"


def test_apply_diff_applies_stacked_anchors_from_the_tool_description() -> None:
    """The worked example the apply_patch tool description gives the model."""
    input_text = (
        "\n".join(
            [
                "class BaseClass",
                "    def search():",
                "        pass",
                "",
                "class Subclass",
                "    def search():",
                "        pass",
            ]
        )
        + "\n"
    )
    diff = "\n".join(
        [
            "@@ class BaseClass",
            "@@     def search():",
            "-        pass",
            "+        raise NotImplementedError()",
            "",
            "@@ class Subclass",
            "@@     def search():",
            "-        pass",
            "+        raise NotImplementedError()",
        ]
    )

    assert (
        apply_diff(input_text, diff)
        == "\n".join(
            [
                "class BaseClass",
                "    def search():",
                "        raise NotImplementedError()",
                "",
                "class Subclass",
                "    def search():",
                "        raise NotImplementedError()",
            ]
        )
        + "\n"
    )


def test_apply_diff_stacked_anchors_narrow_to_the_named_block() -> None:
    """The second anchor skips an earlier matching body inside the selected class."""
    input_text = (
        "\n".join(
            [
                "class First",
                "    def target():",
                "        return 0",
                "",
                "class Second",
                "    def helper():",
                "        pass",
                "",
                "    def target():",
                "        pass",
            ]
        )
        + "\n"
    )
    diff = "\n".join(
        [
            "@@ class Second",
            "@@     def target():",
            "-        pass",
            "+        return 1",
        ]
    )

    assert (
        apply_diff(input_text, diff)
        == "\n".join(
            [
                "class First",
                "    def target():",
                "        return 0",
                "",
                "class Second",
                "    def helper():",
                "        pass",
                "",
                "    def target():",
                "        return 1",
            ]
        )
        + "\n"
    )


def test_apply_diff_stacked_anchors_stay_advisory_when_unmatched() -> None:
    """Same rule a single unmatched anchor already follows: locate by context, don't fail."""
    input_text = "a\nb\n"
    diff = "\n".join(["@@ nope", "@@ also-nope", "-b", "+B"])

    assert apply_diff(input_text, diff) == "a\nB\n"


def test_apply_diff_stacked_anchors_accept_a_trailing_bare_anchor() -> None:
    input_text = "class Only\n    def run():\n        pass\n"
    diff = "\n".join(["@@ class Only", "@@", "-        pass", "+        return 1"])

    assert apply_diff(input_text, diff) == "class Only\n    def run():\n        return 1\n"


def test_apply_diff_raises_on_context_mismatch() -> None:
    input_text = "one\ntwo\n"
    diff = "\n".join(["@@ -1,2 +1,2 @@", " x", "-two", "+2"])
    with pytest.raises(ValueError):
        apply_diff(input_text, diff)


def test_apply_diff_with_crlf_input_and_lf_diff_preserves_crlf() -> None:
    input_text = "line1\r\nline2\r\nline3\r\n"
    diff = "\n".join(["@@ line1", "-line2", "+updated", " line3"])
    assert apply_diff(input_text, diff) == "line1\r\nupdated\r\nline3\r\n"


def test_apply_diff_with_lf_input_and_crlf_diff_preserves_lf() -> None:
    input_text = "line1\nline2\nline3\n"
    diff = "\r\n".join(["@@ line1", "-line2", "+updated", " line3"])
    assert apply_diff(input_text, diff) == "line1\nupdated\nline3\n"


def test_apply_diff_with_crlf_input_and_crlf_diff_preserves_crlf() -> None:
    input_text = "line1\r\nline2\r\nline3\r\n"
    diff = "\r\n".join(["@@ line1", "-line2", "+updated", " line3"])
    assert apply_diff(input_text, diff) == "line1\r\nupdated\r\nline3\r\n"


def test_apply_diff_create_mode_preserves_crlf_newlines() -> None:
    diff = "\r\n".join(["+hello", "+world", "+"])
    assert apply_diff("", diff, mode="create") == "hello\r\nworld\r\n"
