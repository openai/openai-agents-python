from __future__ import annotations

from collections.abc import Iterable


class Retriever:
    """Very small RAG retriever stub."""

    def __init__(self, corpus: Iterable[str] | None = None) -> None:
        self._corpus = list(corpus or [])

    def add(self, document: str) -> None:
        """Add a document to the corpus."""
        self._corpus.append(document)

    def search(self, query: str) -> list[str]:
        """Return documents containing the query string."""
        return [doc for doc in self._corpus if query.lower() in doc.lower()]
