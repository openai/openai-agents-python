from __future__ import annotations

from typing import Any

from litellm.types.utils import Message

from agents.extensions.models.litellm_model import LitellmConverter


def _message_with_annotations(annotations: list[dict[str, Any]]) -> Message:
    # Pass raw provider-shaped dicts intentionally; these mirror the partial payloads
    # LiteLLM forwards and would not satisfy the strict ChatCompletionAnnotation type.
    return Message.model_construct(role="assistant", content="hi", annotations=annotations)


def test_convert_annotations_returns_none_when_absent() -> None:
    message = Message(role="assistant", content="hi")
    assert LitellmConverter.convert_annotations_to_openai(message) is None


def test_convert_annotations_maps_full_citation() -> None:
    message = _message_with_annotations(
        [
            {
                "type": "url_citation",
                "url_citation": {
                    "start_index": 1,
                    "end_index": 4,
                    "url": "https://example.com",
                    "title": "Example",
                },
            }
        ]
    )
    result = LitellmConverter.convert_annotations_to_openai(message)
    assert result is not None
    assert len(result) == 1
    citation = result[0].url_citation
    assert citation.start_index == 1
    assert citation.end_index == 4
    assert citation.url == "https://example.com"
    assert citation.title == "Example"


def test_convert_annotations_defaults_missing_title() -> None:
    # Providers reached through LiteLLM may omit fields the OpenAI schema marks as
    # required; hard indexing those fields previously raised KeyError and aborted
    # the whole turn instead of degrading gracefully.
    message = _message_with_annotations(
        [
            {
                "type": "url_citation",
                "url_citation": {
                    "start_index": 0,
                    "end_index": 5,
                    "url": "https://example.com",
                },
            }
        ]
    )
    result = LitellmConverter.convert_annotations_to_openai(message)
    assert result is not None
    assert len(result) == 1
    assert result[0].url_citation.url == "https://example.com"
    assert result[0].url_citation.title == ""


def test_convert_annotations_defaults_missing_indices_and_title() -> None:
    message = _message_with_annotations(
        [
            {
                "type": "url_citation",
                "url_citation": {
                    "url": "https://example.com",
                    "title": None,
                    "start_index": None,
                    "end_index": None,
                },
            }
        ]
    )
    result = LitellmConverter.convert_annotations_to_openai(message)
    assert result is not None
    assert len(result) == 1
    citation = result[0].url_citation
    assert citation.start_index == 0
    assert citation.end_index == 0
    assert citation.url == "https://example.com"
    assert citation.title == ""


def test_convert_annotations_skips_entries_without_url_citation_payload() -> None:
    # LiteLLM enforces type == "url_citation" but allows the url_citation payload to be
    # absent; such an entry carries no citation data, so it is skipped rather than
    # emitted as an empty citation.
    message = _message_with_annotations(
        [
            {"type": "url_citation"},
            {
                "type": "url_citation",
                "url_citation": {
                    "start_index": 0,
                    "end_index": 2,
                    "url": "https://example.com",
                    "title": "Kept",
                },
            },
        ]
    )
    result = LitellmConverter.convert_annotations_to_openai(message)
    assert result is not None
    assert len(result) == 1
    assert result[0].url_citation.title == "Kept"


def test_convert_annotations_returns_none_when_no_usable_citations() -> None:
    message = _message_with_annotations(
        [
            {"type": "url_citation"},
            {"type": "url_citation", "url_citation": {"title": "Missing URL"}},
        ]
    )

    assert LitellmConverter.convert_annotations_to_openai(message) is None
