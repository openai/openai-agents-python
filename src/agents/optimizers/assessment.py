from __future__ import annotations

from typing import Any

from ..result import RunResult


def extract_prediction_for_metric(result: RunResult) -> Any:
    """Prefer structured final_output if present; fallback to concatenated text.

    This helper ensures evaluation works for structured outputs.
    """
    if result.final_output is not None:
        return result.final_output
    try:
        from ..items import ItemHelpers

        text = ItemHelpers.text_message_outputs(result.new_items)
        return text
    except Exception:
        return None


