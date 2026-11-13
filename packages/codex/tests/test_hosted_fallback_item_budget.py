import json

import pytest

from _agents_codex import _hosted_responses_websocket as h


class Raw:
    def __init__(self):
        self.frames = []
        self.sent = []

    async def send(self, p):
        self.sent.append(json.loads(p))

    async def recv(self):
        return self.frames.pop(0)

    def add(self, v):
        self.frames.append(json.dumps(v))


@pytest.mark.parametrize("cycles", [1, 3])
async def test_unstored_fallback_bounds_accumulated_replay(cycles):
    raw = Raw()
    accounted = []

    def account(operation):
        accounted.append(operation["operation"])
        return "lineage" if operation["operation"] == "create" else None

    run = h._Run(account=account)
    w = h._Socket(raw, run, {}, None)
    frame = {"type": "response.create", "model": "gpt-5.5", "store": False, "input": []}
    await w.send(json.dumps(frame))
    for i in range(cycles):
        cid = f"call_{i}"
        rid = f"response_{i}"
        call = {
            "type": "function_call",
            "id": cid,
            "call_id": cid,
            "name": "lookup",
            "arguments": "{}",
        }
        raw.add({"type": "response.created", "response": {"id": rid}})
        raw.add({"type": "response.output_item.done", "item": call})
        for _unused in range(3):
            await w.recv()
        await w.send(
            json.dumps(
                {
                    **frame,
                    "input": [{"type": "function_call_output", "call_id": cid, "output": "done"}],
                }
            )
        )
        raw.add({"type": "response.inject.failed", "error": {"code": "response_already_completed"}})
        raw.add(
            {
                "type": "response.completed",
                "response": {
                    "id": rid,
                    "status": "completed",
                    "store": False,
                    "output": [
                        call,
                        {
                            "type": "program",
                            "call_id": f"program_{i}",
                            "code": "pass",
                            "provider_fields": {"dense": [{}] * 24000},
                        },
                    ],
                },
            }
        )
        if i == 2:
            sent_before = len(raw.sent)
            creates_before = accounted.count("create")
            with pytest.raises(ValueError, match="node limit"):
                await w.recv()
            assert len(raw.sent) == sent_before
            assert accounted.count("create") == creates_before
        else:
            assert (await w.recv())["item"]["type"] == "program"
            assert len(run.active.create["input"]) == (i + 1) * 3
            assert raw.sent[-1]["type"] == "response.create"
            h._check_socket_json_structure(json.dumps(run.active.create))
