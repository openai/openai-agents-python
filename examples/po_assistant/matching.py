from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import List, Optional, Tuple

_non_alnum = re.compile(r"[^A-Za-z0-9]+")


def _norm(s: str) -> str:
    return _non_alnum.sub("", s).lower().strip()


def string_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a_n = _norm(a)
    b_n = _norm(b)
    if a_n and a_n == b_n:
        return 1.0
    return difflib.SequenceMatcher(a=a_n, b=b_n).ratio()


@dataclass
class MatchCandidate:
    id: str
    label: str
    confidence: float


def best_matches(
    query: str, items: Iterable[Tuple[str, str]], top_k: int = 5
) -> List[MatchCandidate]:
    scored: List[Tuple[float, str, str]] = []
    for item_id, label in items:
        conf = string_similarity(query, label)
        if conf > 0:
            scored.append((conf, item_id, label))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [MatchCandidate(id=i, label=lbl, confidence=conf) for conf, i, lbl in scored[:top_k]]


def exact_or_best(
    query: Optional[str], items: Iterable[Tuple[str, str]], top_k: int = 5
) -> List[MatchCandidate]:
    items_list = list(items)
    if not query:
        # If no query, return top labels (alphabetically) without confidence.
        baseline = [(0.0, i, lbl) for i, lbl in items_list]
        baseline.sort(key=lambda x: x[2])
        return [MatchCandidate(id=i, label=lbl, confidence=0.0) for _, i, lbl in baseline[:top_k]]
    # Prefer exact normalized match if present.
    qn = _norm(query)
    for item_id, label in items_list:
        if _norm(label) == qn:
            return [MatchCandidate(id=item_id, label=label, confidence=1.0)]
    return best_matches(query, items_list, top_k=top_k)
