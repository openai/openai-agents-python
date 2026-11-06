import json

from examples.recipes.inspect_requests import function_declarations, lookup, run_example


async def test_native_schema_and_effective_arguments():
    inspection, peer = await run_example()
    [declaration] = function_declarations(peer.requests[0])
    assert declaration["name"] == lookup.name == "lookup"
    assert declaration["description"] == lookup.description
    field = declaration["parameters"]["properties"]["limit"]
    assert field == {"type": "integer"}  # Native conversion omits Python min/max.
    python_field = lookup.input_type.model_json_schema()["properties"]["limit"]
    assert python_field["minimum"] == 1 and python_field["maximum"] == 3
    assert inspection.calls == [
        {"original": {"limit": 3}, "effective": {"limit": 2}, "python_limit": 2}
    ]
    assert json.loads(peer.tool_output("lookup")) == ["record-a", "record-b"]
