"""Capture unchanged checkpoint bytes with a specified historical source tree.

Run with that tree's matching Python environment and qualified native artifact.
The model peer comes from the same source tree. Only synthetic data is captured.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def capture(source: Path, output: Path) -> None:
    sys.path[:0] = [
        str(source / "src"),
        str(source / "packages/codex/src"),
        str(source / "packages/codex"),
    ]
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from pydantic import BaseModel

    import _agents_codex.agent as runtime_agent
    import agents
    import agents._native
    from agents import (
        Agent,
        FunctionTool,
        InteractionExpiredError,
        NativeProvider,
        PromptedOutput,
        function_tool,
    )

    runtime_file = Path(sys.modules[Agent.__module__].__file__).resolve()
    assert runtime_file.is_relative_to(source)
    assert Path(agents.__file__).resolve().is_relative_to(source)
    print(
        json.dumps(
            {
                "agents_module": agents.__file__,
                "agent_runtime": str(runtime_file),
                "implementation_module": runtime_agent.__file__,
                "native_module": agents._native.__file__,
            }
        )
    )
    from tests.native_fixtures import ResponsesFixture, assistant_output, function_call

    class LookupArgs(BaseModel):
        key: str

    output.mkdir(parents=True, exist_ok=True)
    workspace = Path("/tmp/agents-checkpoint-capture")
    workspace.mkdir(exist_ok=True)
    os.chdir(workspace)
    peer = ResponsesFixture()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with TestServer(app) as server:
        peer.url = str(server.make_url("/v1"))
        async with NativeProvider(
            base_url=peer.url, api_key="synthetic-checkpoint-producer", supports_websockets=False
        ) as provider:
            idle = Agent(
                instructions="Remember checkpoint history.", model="gpt-5.5", provider=provider
            )
            peer.respond(assistant_output("remembered-before-upgrade"))
            async with idle.session() as session:
                await (await session.submit("idle-before-upgrade")).get_result()
                (output / "idle.json").write_text((await session.save_state()).to_json())

            effects: list[str] = []

            @function_tool
            async def record(key: str) -> str:
                effects.append(key)
                return "recorded-" + key

            lookup = FunctionTool(
                name="lookup", description="Read", input_type=LookupArgs, output_type=str
            )
            manual = Agent(
                instructions="Record and request a decision.",
                model="gpt-5.5",
                provider=provider,
                tools=[record, lookup],
                output_type=PromptedOutput(int),
            )
            peer.respond(
                function_call("record", {"key": "accepted"}, "accepted-record"),
                function_call("lookup", {"key": "pending"}, "pending-lookup"),
            )
            async with manual.session() as session:
                await session.submit("manual-before-upgrade")
                async for event in session.interactions():
                    request = lookup.match(event)
                    if request is not None:
                        break
                else:
                    raise AssertionError("Missing manual request")
                state = await session.suspend()
                assert effects == ["accepted"]
                (output / "manual.json").write_text(state.to_json())
                try:
                    await request.respond("expired")
                except InteractionExpiredError:
                    pass
                else:
                    raise AssertionError("Sealed producer reply authority survived")

            code = Agent(
                instructions="Store and read checkpoint values.",
                model="gpt-5.5",
                provider=provider,
                programmatic_tool_calling=True,
            )
            peer.respond(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "seed-cell",
                        "input": 'store("retained", {"value":29}); text("stored-before-upgrade");',
                    },
                }
            )
            peer.respond(assistant_output("code-before-upgrade"))
            async with code.session() as session:
                await (await session.submit("code-before-upgrade")).get_result()
                state = await session.save_state()
                assert state.root_code_mode is not None
                assert state.root_code_mode["stored_values"]["retained"] == {"value": 29}
                assert state.root_code_mode["next_cell_id"] > 1
                (output / "code.json").write_text(state.to_json())
    assert len(peer.requests) == 4 and not peer.replies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-id", required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    subprocess.run(
        [
            "git",
            "diff",
            "--quiet",
            "HEAD",
            "--",
            "src",
            "packages/codex/src",
            "packages/codex/rust",
        ],
        cwd=source,
        check=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    receipt_hash, artifact_hash = digest(args.receipt), digest(args.artifact)
    asyncio.run(capture(source, output))
    manifest = {
        "format": 1,
        "producer": {
            "kind": "source-snapshot",
            "source_commit": revision,
            "native_artifact_id": args.artifact_id,
            "native_wheel_file": args.artifact.name,
            "native_wheel_sha256": artifact_hash,
            "native_receipt_sha256": receipt_hash,
            "codex_revision": (source / "packages/codex/CODEX_REVISION").read_text().strip(),
        },
        "capture_script_sha256": digest(Path(__file__)),
        "cases": [
            {
                "file": name + ".json",
                "sha256": digest(output / (name + ".json")),
                "sdk_format_version": json.loads((output / (name + ".json")).read_text())[
                    "format_version"
                ],
            }
            for name in ("idle", "manual", "code")
        ],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
