from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class URLCitation:
    title: str
    url: str


def get_field(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def extract_url_citations(items: Sequence[Any]) -> list[URLCitation]:
    citations: list[URLCitation] = []
    seen: set[str] = set()

    for item in items:
        raw_item = get_field(item, "raw_item")
        if get_field(raw_item, "type") != "message":
            continue

        content = get_field(raw_item, "content")
        if not isinstance(content, list):
            continue

        for part in content:
            if get_field(part, "type") != "output_text":
                continue
            annotations = get_field(part, "annotations")
            if not isinstance(annotations, list):
                continue

            for annotation in annotations:
                if get_field(annotation, "type") != "url_citation":
                    continue
                url = get_field(annotation, "url")
                title = get_field(annotation, "title")
                if not isinstance(url, str) or url in seen:
                    continue
                seen.add(url)
                citations.append(
                    URLCitation(
                        title=title if isinstance(title, str) else url,
                        url=url,
                    )
                )

    return citations


def extract_web_search_source_urls(items: Sequence[Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    for item in items:
        raw_item = get_field(item, "raw_item")
        if get_field(raw_item, "type") != "web_search_call":
            continue

        action = get_field(raw_item, "action")
        sources = get_field(action, "sources") if action else None
        if not isinstance(sources, list):
            continue

        for source in sources:
            url = get_field(source, "url")
            if not isinstance(url, str) or url in seen:
                continue
            seen.add(url)
            urls.append(url)

    return urls
