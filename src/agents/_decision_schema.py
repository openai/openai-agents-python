"""The bounded subset of JSON Schema expressible as Decisions questions."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


def _key(value: Any) -> tuple[type, Any]:
    if type(value) not in (str, bool, int, float, type(None)) or (
        type(value) is float and not math.isfinite(value)
    ):
        raise ValueError("Expected a finite JSON scalar")
    return (int if type(value) is float else type(value)), value


def _number(value: Any, low: float, high: float) -> float:
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError("Invalid decision number")
    return float(value)


def _choices(node: dict[str, Any]) -> list[Any]:
    keys = set(node) - {"title", "description", "type", "$defs"}
    kind = node.get("type")
    if keys == {"anyOf"} and kind is None:
        values = [v for branch in node["anyOf"] for v in _choices(branch)]
        if not 1 <= len(values) <= 255 or len({_key(v) for v in values}) != len(values):
            raise ValueError("Expected distinct finite choices")
        return values
    if not keys and kind == "null":
        return [None]
    if not keys and kind == "boolean":
        return [True, False]
    if keys in ({"enum"}, {"const"}):
        values = node.get("enum", [node.get("const")])
        if (
            not isinstance(values, list)
            or not 1 <= len(values) <= 255
            or len({_key(v) for v in values}) != len(values)
        ):
            raise ValueError("Expected distinct finite choices")
        types = {
            "string": (str,),
            "boolean": (bool,),
            "null": (type(None),),
            "integer": (int,),
            "number": (int, float),
        }
        if kind is not None and any(type(v) not in types.get(kind, ()) for v in values):
            raise ValueError("Incompatible Decisions choice type")
        return list(values)
    raise ValueError("Expected a finite scalar schema")


@dataclass
class _Leaf:
    kind: str
    values: list[Any]
    minimum: int = 0
    width: int = 0

    def validate(self, value: Any) -> None:
        if self.kind == "choice":
            if _key(value) not in {_key(item) for item in self.values}:
                raise ValueError("Unknown choice")
        elif self.kind == "boolean":
            if type(value) is not bool:
                raise ValueError("Expected a boolean")
        else:
            _number(value, self.minimum, self.minimum + self.width)

    def decode(self, question: dict[str, Any], answer: dict[str, Any]) -> Any:
        if question["type"] == "predicate":
            p = _number(answer["probability"], 0, 1)
            return p >= 0.5 if self.kind == "boolean" else p
        _number(answer["confidence"], 0, 1)
        wire = (
            [v["value"] for v in question["choices"]]
            if self.kind == "choice"
            else list(range(self.width + 1))
        )
        entries = answer["probabilities"]
        if not isinstance(entries, list) or len(entries) != len(wire):
            raise ValueError("Invalid probability list")
        remaining = {_key(v) for v in wire}
        total = 0.0
        for entry in entries:
            remaining.remove(_key(entry["value"]))
            total += _number(entry["probability"], 0, 1)
            if (
                self.kind == "score"
                and entry["label"] != question["levels"][entry["value"]]["label"]
            ):
                raise ValueError("Invalid score level")
        if abs(total - 1.0) > 1e-5:
            raise ValueError("Invalid probability total")
        if self.kind == "choice":
            return self.values[[_key(v) for v in wire].index(_key(answer["choice"]))]
        return self.minimum + _number(answer["score"], 0, self.width)


class _Plan:
    def __init__(self, question: str, schema: dict[str, Any]):
        self.questions: list[dict[str, Any]] = []
        self.leaves: dict[tuple[str, ...], _Leaf] = {}
        self.records: dict[tuple[str, ...], set[str]] = {}
        definitions = schema.get("$defs", {})
        visited = 0

        def bounded(value: Any, depth: int) -> None:
            nonlocal visited
            visited += 1
            if depth > 32 or visited > 10000:
                raise ValueError("Decisions schema is too large")
            if isinstance(value, (dict, list)):
                for item in value.values() if isinstance(value, dict) else value:
                    bounded(item, depth + 1)

        bounded(schema, 0)
        visited = 0

        def visit(node: dict[str, Any], path: tuple[str, ...], depth: int) -> None:
            nonlocal visited
            visited += 1
            if depth > 32 or visited > 10000 or not isinstance(node, dict):
                raise ValueError("Unsupported Decisions output schema")
            keys = set(node) - {"title", "description", "type", "$defs"}
            kind = node.get("type")
            if keys == {"$ref"} and kind is None:
                ref = node["$ref"]
                if (
                    not isinstance(ref, str)
                    or not ref.startswith("#/$defs/")
                    or ref[8:] not in definitions
                ):
                    raise ValueError("Unsupported Decisions reference")
                visit(definitions[ref[8:]], path, depth + 1)
                return
            if kind == "object" and keys <= {"properties", "required", "additionalProperties"}:
                fields = node.get("properties", {})
                required = node.get("required", [])
                if (
                    not fields
                    or node.get("additionalProperties", False) is not False
                    or len(fields) != len(required)
                    or set(fields) != set(required)
                ):
                    raise ValueError("Decisions records require fixed, required fields")
                self.records[path] = set(fields)
                for name, field_schema in fields.items():
                    visit(field_schema, (*path, name), depth + 1)
                return
            if keys in ({"enum"}, {"const"}, {"anyOf"}) or (kind == "null" and not keys):
                leaf = _Leaf("choice", _choices(node))
            elif kind == "boolean" and not keys:
                leaf = _Leaf("boolean", [])
            elif kind in ("integer", "number") and keys == {"minimum", "maximum"}:
                lo, hi = node["minimum"], node["maximum"]
                if any(
                    type(v) not in (int, float) or not abs(v) <= 2**53 - 1 or int(v) != v
                    for v in (lo, hi)
                ) or not 0 < hi - lo <= (254 if kind == "integer" else 9):
                    raise ValueError("Unsupported Decisions bounds")
                if kind == "integer":
                    leaf = _Leaf("choice", list(range(int(lo), int(hi) + 1)))
                else:
                    leaf = _Leaf(
                        "probability" if (lo, hi) == (0, 1) else "score", [], int(lo), int(hi - lo)
                    )
            else:
                raise ValueError("Unsupported Decisions output schema")
            self.leaves[path] = leaf
            if len(leaf.values) == 1:
                return
            if len(self.questions) >= 400:
                raise ValueError("Decisions supports at most 400 questions")
            instructions = question
            if path:
                instructions += "\nField " + json.dumps(path)
            if node.get("description"):
                instructions += "\n" + node["description"]
            q: dict[str, Any] = {
                "name": f"field_{len(self.questions) + 1}" if path else "decision",
                "instructions": instructions,
            }
            if leaf.kind == "choice":
                plain = all(type(v) in (bool, str) for v in leaf.values)
                q.update(
                    type="choice",
                    choices=[
                        {"value": v}
                        if plain
                        else {"value": f"value_{i}", "description": json.dumps(v)}
                        for i, v in enumerate(leaf.values)
                    ],
                )
            elif leaf.kind == "score":
                q.update(
                    type="score",
                    levels=[{"label": str(leaf.minimum + i)} for i in range(leaf.width + 1)],
                )
            else:
                q["type"] = "predicate"
            self.questions.append(q)

        visit(schema, (), 0)

    def validate(self, value: Any) -> None:
        def check(item: Any, path: tuple[str, ...]) -> None:
            if path in self.records:
                if type(item) is not dict or item.keys() != self.records[path]:
                    raise ValueError("Invalid decision record")
                for name, child in item.items():
                    check(child, (*path, name))
            else:
                self.leaves[path].validate(item)

        check(value, ())

    def choice(self, value: Any) -> Any:
        return next(v for v in self.leaves[()].values if _key(v) == _key(value))

    def decode(self, answers: list[Any]) -> Any:
        result: dict[str, Any] = {}
        pairs = iter(zip(self.questions, answers, strict=True))
        for path, leaf in self.leaves.items():
            value = leaf.values[0] if len(leaf.values) == 1 else leaf.decode(*next(pairs))
            if not path:
                return value
            node = result
            for part in path[:-1]:
                node = node.setdefault(part, {})
            node[path[-1]] = value
        return result
