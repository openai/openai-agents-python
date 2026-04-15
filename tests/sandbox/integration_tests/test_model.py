from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from agents.items import TResponseOutputItem
from tests.fake_model import FakeModel
from tests.test_responses import get_final_output_message, get_function_tool_call

__test__ = False


class TestModel(FakeModel):
    """Reusable queued model for sandbox integration tests."""

    __test__ = False

    def queue_turn(self, *items: TResponseOutputItem) -> None:
        self.set_next_output(list(items))

    def queue_function_call(
        self,
        name: str,
        arguments: Mapping[str, Any] | str | None = None,
        *,
        call_id: str | None = None,
        namespace: str | None = None,
    ) -> None:
        self.queue_turn(
            get_function_tool_call(
                name,
                _serialize_arguments(arguments),
                call_id=call_id,
                namespace=namespace,
            )
        )

    def queue_function_calls(
        self,
        calls: Sequence[tuple[str, Mapping[str, Any] | str | None, str | None]],
    ) -> None:
        self.queue_turn(
            *[
                get_function_tool_call(name, _serialize_arguments(arguments), call_id=call_id)
                for name, arguments, call_id in calls
            ]
        )

    def queue_final_output(self, output: str) -> None:
        self.queue_turn(get_final_output_message(output))


def _serialize_arguments(arguments: Mapping[str, Any] | str | None) -> str:
    if arguments is None:
        return "{}"
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments)
