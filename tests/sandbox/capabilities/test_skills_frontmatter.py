from __future__ import annotations

import pytest

from agents.sandbox.capabilities.skills import _parse_frontmatter


def _frontmatter(block: str) -> dict[str, str]:
    return _parse_frontmatter(f"---\nname: triage\n{block}\n---\n# Skill\n")


class TestParseFrontmatter:
    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            ("description: Use for triage.", "Use for triage."),
            ('description: "Use for triage."', "Use for triage."),
            ("description: 'Use for triage.'", "Use for triage."),
            ("description:", ""),
        ],
    )
    def test_single_line_scalars(self, block: str, expected: str) -> None:
        assert _frontmatter(block)["description"] == expected

    def test_folded_block_scalar_joins_lines_with_spaces(self) -> None:
        parsed = _frontmatter(
            "description: >\n  Use for GitHub issue triage.\n  Triggers: /triage, bug report"
        )

        assert (
            parsed["description"] == "Use for GitHub issue triage. Triggers: /triage, bug report\n"
        )

    def test_folded_block_scalar_does_not_leak_continuation_keys(self) -> None:
        parsed = _frontmatter(
            "description: >\n  Use for GitHub issue triage.\n  Triggers: /triage, bug report"
        )

        assert set(parsed) == {"name", "description"}

    def test_literal_block_scalar_keeps_newlines(self) -> None:
        parsed = _frontmatter("description: |\n  first line\n  second line")

        assert parsed["description"] == "first line\nsecond line\n"

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (">", "first line second line\n"),
            (">-", "first line second line"),
            ("|", "first line\nsecond line\n"),
            ("|-", "first line\nsecond line"),
        ],
    )
    def test_chomping_indicators(self, header: str, expected: str) -> None:
        parsed = _frontmatter(f"description: {header}\n  first line\n  second line")

        assert parsed["description"] == expected

    def test_folded_block_scalar_treats_blank_line_as_paragraph_break(self) -> None:
        parsed = _frontmatter("description: >\n  para one\n\n  para two")

        assert parsed["description"] == "para one\npara two\n"

    def test_wrapped_plain_scalar_is_joined(self) -> None:
        parsed = _frontmatter("description: Use for GitHub issue\n  triage, not for PR review.")

        assert parsed["description"] == "Use for GitHub issue triage, not for PR review."

    def test_wrapped_plain_scalar_does_not_swallow_the_next_key(self) -> None:
        parsed = _parse_frontmatter(
            "---\ndescription: Use for GitHub issue\n  triage.\nname: triage\n---\n# Skill\n"
        )

        assert parsed == {
            "description": "Use for GitHub issue triage.",
            "name": "triage",
        }

    def test_hash_inside_a_value_is_preserved(self) -> None:
        assert _frontmatter("description: use the #triage tag")["description"] == (
            "use the #triage tag"
        )

    def test_comment_lines_are_ignored(self) -> None:
        parsed = _frontmatter("# a comment\ndescription: Use for triage.")

        assert parsed == {"name": "triage", "description": "Use for triage."}

    @pytest.mark.parametrize("header", [">", "|"])
    def test_block_scalar_with_no_body_does_not_swallow_the_next_key(self, header: str) -> None:
        parsed = _parse_frontmatter(f"---\ndescription: {header}\nname: triage\n---\n# Skill\n")

        assert parsed == {"description": "", "name": "triage"}

    @pytest.mark.parametrize("header", [">", "|"])
    def test_block_scalar_stops_at_the_next_key(self, header: str) -> None:
        parsed = _parse_frontmatter(
            f"---\ndescription: {header}\n  text here\nname: triage\n---\n# Skill\n"
        )

        assert parsed == {"description": "text here\n", "name": "triage"}

    @pytest.mark.parametrize("header", ["|-", ">-", "|", ">"])
    def test_quotes_inside_a_block_scalar_are_content(self, header: str) -> None:
        parsed = _frontmatter(f'description: {header}\n  "Use for triage"')

        assert parsed["description"].startswith('"Use for triage"')

    def test_wrapped_plain_scalar_continues_across_a_blank_line(self) -> None:
        parsed = _frontmatter("description: Use for triage.\n\n  Avoid PR review.")

        assert parsed["description"] == "Use for triage.\nAvoid PR review."

    def test_wrapped_plain_scalar_stops_at_a_blank_line_before_a_new_key(self) -> None:
        parsed = _parse_frontmatter(
            "---\ndescription: Use for triage.\n\nname: triage\n---\n# Skill\n"
        )

        assert parsed == {"description": "Use for triage.", "name": "triage"}

    def test_indented_document_marker_stays_inside_a_block_scalar(self) -> None:
        parsed = _parse_frontmatter(
            "---\ndescription: |\n  first\n  ---\n  last\nname: triage\n---\n# Skill\n"
        )

        assert parsed == {"description": "first\n---\nlast\n", "name": "triage"}

    def test_folded_scalar_keeps_breaks_around_a_more_indented_line(self) -> None:
        parsed = _frontmatter("description: >\n  first\n    indented\n  last")

        assert parsed["description"] == "first\n  indented\nlast\n"

    def test_missing_frontmatter_returns_empty(self) -> None:
        assert _parse_frontmatter("# Skill\nno frontmatter here\n") == {}

    def test_unterminated_frontmatter_returns_empty(self) -> None:
        assert _parse_frontmatter("---\nname: triage\ndescription: >\n  text\n") == {}
