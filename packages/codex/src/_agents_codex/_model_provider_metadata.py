"""Receipt-bound provider data, separate from model input and tool authority."""

from __future__ import annotations

import json
from typing import Any

_LIMIT = 8 * 1024 * 1024
_ITEMS = 4096
_FIELD = "__sdk_model_provider_metadata"


def _encoded(value: Any) -> str:
    encoded = None
    try:
        try:
            encoded = json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > _LIMIT:
                encoded = None
        except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
            pass
        if encoded is None:
            raise ValueError("Model provider metadata exceeds its JSON capacity")
        return encoded
    finally:
        del value, encoded


def _retain_output(
    response: Any, provider: str, model: str, values: list[tuple[int, dict]]
) -> None:
    rows = captured = index = value = None
    snapshot = None
    try:
        if not values:
            return
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or not isinstance(model, str)
            or not model.strip()
            or len(response.output) > _ITEMS
        ):
            raise ValueError("Model provider metadata needs a current provider and model")
        if len(values) > _ITEMS or len({index for index, _ in values}) != len(values):
            raise ValueError("Model provider metadata needs exact output occurrences")
        rows = []
        for index, value in values:
            if (
                type(index) is not int
                or not 0 <= index < len(response.output)
                or not isinstance(value, dict)
            ):
                raise ValueError("Model provider metadata needs exact output occurrences")
            rows.append(
                {
                    "output_index": index,
                    "metadata": {"provider": provider, "model": model, "value": value},
                }
            )
        snapshot = _encoded({"output": response.output, "metadata": rows})
        captured = json.loads(snapshot)
        object.__setattr__(
            response,
            "_provider_output_snapshot",
            (_encoded(captured["output"]), captured["metadata"]),
        )
    finally:
        del response, provider, model, values, rows, snapshot, captured, index, value


def _capture_output(response: Any) -> list[dict] | None:
    captured = None
    try:
        captured = getattr(response, "_provider_output_snapshot", None)
        if captured is None:
            return None
        if _encoded(response.output) != captured[0]:
            raise ValueError("Model provider output changed before settlement")
        return json.loads(_encoded(captured[1]))
    finally:
        del response, captured


def _bind_input(request: Any, raw: Any) -> None:
    rows = row = metadata = None
    try:
        if raw is None:
            return
        rows = json.loads(_encoded(raw))
        if not isinstance(rows, list) or len(rows) > _ITEMS:
            raise ValueError("Invalid native model provider metadata")
        indices = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid native model provider metadata")
            index = row.get("input_index")
            metadata = row.get("metadata")
            if (
                type(index) is not int
                or index < 0
                or index in indices
                or not isinstance(row.get("item"), dict)
                or not isinstance(metadata, dict)
                or not isinstance(metadata.get("provider"), str)
                or not metadata["provider"].strip()
                or not isinstance(metadata.get("model"), str)
                or not metadata["model"].strip()
                or not isinstance(metadata.get("value"), dict)
            ):
                raise ValueError("Invalid native model provider metadata occurrence")
            indices.add(index)
        object.__setattr__(request, "_provider_input_snapshot", rows)
    finally:
        del request, raw, rows, row, metadata


def _replay_input(request: Any, index: int, item: dict, provider: str, model: str) -> dict | None:
    rows = row = metadata = None
    try:
        rows = getattr(request, "_provider_input_snapshot", ())
        row = next((row for row in rows if row["input_index"] == index), None)
        if row is None:
            return None
        metadata = row["metadata"]
        if (
            metadata["provider"] != provider
            or metadata["model"] != model
            or _encoded(row["item"]) != _encoded(item)
        ):
            return None
        return json.loads(_encoded(metadata["value"]))
    finally:
        del request, index, item, provider, model, rows, row, metadata


def _has_input(request: Any, index: int) -> bool:
    return any(
        row["input_index"] == index for row in getattr(request, "_provider_input_snapshot", ())
    )


def _retain_frame(event: Any, response: Any, index: int) -> None:
    rows = row = expected = None
    try:
        rows = _capture_output(response)
        row = next((row for row in rows or () if row["output_index"] == index), None)
        if row is None:
            return
        expected = response.output[index]
        if "id" not in expected:
            expected = {**expected, "id": event["item"].get("id")}
        if (
            event.get("type") != "response.output_item.done"
            or event.get("output_index") != index
            or _encoded(event["item"]) != _encoded(expected)
        ):
            raise ValueError("Model provider stream output changed before settlement")
        object.__setattr__(event, "_provider_frame_snapshot", (_encoded(event), row))
    finally:
        del event, response, index, rows, row, expected


def _capture_frame(encoded: str, event: Any) -> str:
    captured = None
    try:
        captured = getattr(event, "_provider_frame_snapshot", None)
        if _FIELD in event:
            raise ValueError("Reserved model provider metadata field")
        if captured is None:
            return encoded
        if _encoded(event) != captured[0]:
            raise ValueError("Model provider stream output changed before settlement")
        return encoded[:-1] + ',"' + _FIELD + '":' + _encoded(captured[1]) + "}"
    finally:
        del encoded, event, captured


def _chat_reasoning(message: dict) -> dict:
    value = blocks = block = None
    try:
        value = {
            key: message[key]
            for key in ("reasoning_content", "reasoning", "thinking_blocks")
            if message.get(key) is not None
        }
        if not any(value.values()):
            return {}
        if "reasoning_content" in value and not isinstance(value["reasoning_content"], str):
            raise ValueError("Chat reasoning content must contain text")
        blocks = value.get("thinking_blocks", [])
        if not isinstance(blocks, list):
            raise ValueError("Chat thinking blocks must contain objects")
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError("Chat thinking blocks must contain objects")
            if any(
                key in block and not isinstance(block[key], str)
                for key in ("thinking", "signature")
            ):
                raise ValueError("Chat thinking text and signatures must contain text")
        return json.loads(_encoded(value))
    finally:
        del message, value, blocks, block


def _fold_chat_reasoning(target: dict, delta: dict) -> None:
    block = current = fields = blocks = None
    try:
        fields = _chat_reasoning(delta)
        for key in ("reasoning_content", "reasoning"):
            if key in fields:
                if not isinstance(fields[key], str):
                    raise ValueError("Streamed chat reasoning must contain text")
                target[key] = target.get(key, "") + fields[key]
        for block in fields.get("thinking_blocks", []):
            blocks = target.setdefault("thinking_blocks", [])
            if block.get("type", "thinking") == "redacted_thinking":
                blocks.append(block)
                continue
            if block.get("type", "thinking") != "thinking":
                raise ValueError("Unsupported streamed thinking block")
            if not any(block.get(key) for key in ("thinking", "signature")) and not any(
                key not in ("type", "thinking", "signature") for key in block
            ):
                continue
            if blocks and blocks[-1].get("type") == "thinking" and "signature" not in blocks[-1]:
                current = blocks[-1]
            else:
                current = {"type": "thinking", "thinking": ""}
                blocks.append(current)
            current.update(
                {
                    key: value
                    for key, value in block.items()
                    if key not in ("type", "thinking", "signature")
                }
            )
            current["thinking"] += block.get("thinking", "")
            if block.get("signature"):
                current["signature"] = block["signature"]
    finally:
        del target, delta, block, current, fields, blocks
