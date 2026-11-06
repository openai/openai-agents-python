import json

import pytest
from examples.recipes.inspect_requests import function_declarations
from examples.recipes.typed_tools import Ledger, Line, record, run_example
from pydantic import ValidationError

from agents import Agent
from agents.testing import ScriptedResponses, tool_call


async def test_nested_coercion_json_result_and_native_schema():
    ledger, peer = await run_example()
    assert ledger.writes == [Line(sku="synthetic", quantity=2)]
    assert json.loads(peer.tool_output("line")) == [{"sku": "synthetic", "accepted": 2}]
    [declaration] = function_declarations(peer.requests[0])
    parameters = declaration["parameters"]
    assert parameters["properties"]["line"] == {"$ref": "#/$defs/Line"}
    line = parameters["$defs"]["Line"]
    assert line["type"] == "object"
    assert set(line["properties"]) == {"sku", "quantity"}
    assert line["properties"]["quantity"]["type"] == "integer"
    assert "minimum" not in line["properties"]["quantity"]


@pytest.mark.parametrize("quantity,invalid_return,effects", [(0, False, 0), (1, True, 1)])
async def test_validation_boundary_preserves_existing_effects(quantity, invalid_return, effects):
    ledger = Ledger(invalid_return=invalid_return)
    async with ScriptedResponses(
        [tool_call("record", {"line": {"sku": "synthetic", "quantity": quantity}}, call_id="bad")]
    ) as peer:
        error = ValueError if invalid_return else ValidationError
        with pytest.raises(error):
            await Agent(instructions="Record.", tools=[record], provider=peer.provider).run(
                "Record.", context=ledger
            )
        assert len(ledger.writes) == effects
        assert peer.tool_output("bad") is None
