"""Check rollback using two installed wheels, with synthetic provider traffic only.

Run this file with Python 3.10+ and supply fresh wheel-installed environments::

    python checkpoint_wheel_compatibility.py \
        --previous-python /tmp/alpha7/bin/python \
        --candidate-python /tmp/candidate/bin/python \
        --previous-wheel /downloads/openai_agents-0.24.0a7-PLATFORM.whl \
        --candidate-wheel /qualified/openai_agents-0.24.0a8-PLATFORM.whl \
        --previous-sha256 PUBLISHED_SHA256 --candidate-sha256 QUALIFIED_SHA256 \
        --output /tmp/checkpoint-wheel-compatibility.json

The caller authenticates the published and qualified checksums. This gate checks
those archive digests and installed bytes; it never downloads or installs files.
Each phase runs with its wheel's Python in isolated mode, outside the checkout.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import zipfile
from importlib import metadata
from pathlib import Path


def installed_identity(wheel: str, digest: str, expected: str) -> dict:
    import agents

    archive = Path(wheel)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest, "Wheel digest mismatch"
    distribution = metadata.distribution("openai-agents")
    assert distribution.version in {expected, expected.replace("a", "-alpha.")}, (
        distribution.version,
        expected,
    )
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    assert not direct.get("dir_info", {}).get("editable"), "Editable SDK installation"
    owned = {Path(distribution.locate_file(item)).resolve() for item in distribution.files or ()}
    assert Path(agents.__file__).resolve() in owned, "SDK import outside installed distribution"
    count = 0
    with zipfile.ZipFile(archive) as wheel_file:
        for name in wheel_file.namelist():
            if name.endswith("/") or ".dist-info/" in name:
                continue
            assert name.startswith(
                ("agents/", "_agents_codex/", "_agents_shared/", "openai_agents.libs/")
            ), name
            installed = Path(distribution.locate_file(name))
            assert installed.resolve() in owned, f"Unowned installed file: {name}"
            assert installed.read_bytes() == wheel_file.read(name), f"Wheel byte mismatch: {name}"
            count += 1
    assert count > 0
    return {"version": distribution.version, "wheel_sha256": digest, "matched_files": count}


async def exercise(command: dict, directory: Path) -> dict:
    from pydantic import BaseModel, ValidationError

    from agents import Agent, FunctionTool, LocalStorage, RunBudget, SessionState, function_tool
    from agents.testing import ScriptedResponses, text, tool_call

    action = command["action"]
    name = command.get("name", "")
    source = directory / command.get("source", "unused.json")
    destination = directory / command.get("destination", "unused-output.json")
    effects = directory / command.get("effects", "effects.txt")

    @function_tool
    def record(value: str) -> str:
        """Record one synthetic application effect."""
        with effects.open("a", encoding="utf-8") as output:
            output.write(value + "\n")
        return "recorded:" + value

    class LookupInput(BaseModel):
        key: str

    lookup = FunctionTool(
        name="lookup", description="Read a synthetic label", input_type=LookupInput, output_type=str
    )

    def agent(peer, *, tools=False):
        return Agent(
            instructions="Continue the synthetic compatibility check.",
            model="gpt-5.5",
            provider=peer.provider,
            tools=[record, lookup] if tools else [],
        )

    def verify_history(peer):
        assert len(peer.requests) == 1, len(peer.requests)
        texts = [
            part.get("text")
            for item in peer.requests[0]["input"]
            if item.get("type") == "message"
            for part in item.get("content", [])
        ]
        for marker in command.get("markers", []):
            assert texts.count(marker) == 1, (marker, texts)

    if action == "budget-reject":
        try:
            SessionState.from_json(source.read_text())
        except ValidationError as error:
            assert any(entry["loc"] == ("format_version",) for entry in error.errors())
        else:
            raise AssertionError("Older wheel accepted a budgeted checkpoint")
        return {"action": action, "source": source.name}

    if action in {"idle-create", "idle-continue", "local"}:
        answer = "answer:" + name
        async with ScriptedResponses([text(answer)]) as peer:
            options = {}
            if action == "idle-continue":
                options["state"] = SessionState.from_json(source.read_text())
            elif action == "local":
                options["storage"] = LocalStorage(directory / command["profile"])
                identity = directory / command["identity"]
                if identity.exists():
                    options["resume"] = identity.read_text()
            async with agent(peer, tools=command.get("tools", False)).session(**options) as session:
                result = await (await session.submit(name)).get_result()
                assert result.final_output == answer
                verify_history(peer)
                if action == "local":
                    identity.write_text(session.id)
                else:
                    saved = await session.save_state()
                    assert saved.format_version <= 7
                    destination.write_text(saved.to_json())
        return {"action": action, "name": name}

    if action in {"manual-create", "budget-create"}:
        budgeted = action == "budget-create"
        script = [[tool_call("record", {"value": "once"}, call_id="settled-effect")]]
        script.append([tool_call("lookup", {"key": "pending"}, call_id="pending-lookup")])
        async with ScriptedResponses(*script) as peer:
            async with agent(peer, tools=True).session() as session:
                options = {"budget": RunBudget(max_requests=3)} if budgeted else {}
                turn = await session.submit(name, **options)
                pending = await turn.wait_for_request_or_result()
                assert lookup.match(pending.requests[0]) is not None
                saved = await session.suspend()
                assert effects.read_text() == "once\n"
                assert len(peer.requests) == 2
                if budgeted:
                    assert saved.format_version == 8
                    assert saved.run_budget.admitted_requests == 2
                    assert json.loads(saved.to_json())["format_version"] == 8
                else:
                    assert saved.format_version <= 7
                destination.write_text(saved.to_json())
        return {"action": action, "format_version": saved.format_version}

    if action in {"manual-continue", "budget-continue"}:
        saved = SessionState.from_json(source.read_text())
        budgeted = action == "budget-continue"
        options = {"resume_budget": RunBudget(max_requests=3)} if budgeted else {}
        if budgeted:
            assert saved.format_version == 8
            assert saved.run_budget.admitted_requests == 2
        assert effects.read_text() == "once\n"
        async with ScriptedResponses([text("manual-restored")]) as peer:
            async with agent(peer, tools=True).session(state=saved, **options) as session:
                async for event in session.interactions():
                    request = lookup.match(event)
                    if request is not None:
                        assert request.call_id == "pending-lookup"
                        await request.respond("lookup-result")
                        break
                else:
                    raise AssertionError("Missing restored manual request")
                assert session.restored_turn is not None
                result = await session.restored_turn.get_result()
                assert result.final_output == "manual-restored"
                assert effects.read_text() == "once\n", "Settled callback replayed"
                verify_history(peer)
                outputs = [
                    item
                    for item in peer.requests[0]["input"]
                    if item.get("type") == "function_call_output"
                ]
                assert [item["call_id"] for item in outputs].count("settled-effect") == 1
                assert [item["call_id"] for item in outputs].count("pending-lookup") == 1
                assert json.dumps(outputs).count("recorded:once") == 1
                assert json.dumps(outputs).count("lookup-result") == 1
                retained = await session.save_state()
                destination.write_text(retained.to_json())
        return {"action": action, "settled_callback_count": 1}
    raise AssertionError(action)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker")
    parser.add_argument("--previous-python", type=Path)
    parser.add_argument("--candidate-python", type=Path)
    parser.add_argument("--previous-wheel", type=Path)
    parser.add_argument("--candidate-wheel", type=Path)
    parser.add_argument("--previous-sha256")
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        command = json.loads(args.worker)
        if command["action"] == "identity":
            result = installed_identity(command["wheel"], command["sha256"], command["version"])
        else:
            result = asyncio.run(asyncio.wait_for(exercise(command, Path.cwd()), 45))
        print(json.dumps(result))
        return
    for field in (
        "previous_python",
        "candidate_python",
        "previous_wheel",
        "candidate_wheel",
        "previous_sha256",
        "candidate_sha256",
        "output",
    ):
        if getattr(args, field) is None:
            parser.error("--" + field.replace("_", "-") + " is required")
    script = Path(__file__).resolve()
    receipt = {
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "status": "incomplete",
        "checks": [],
    }
    environment = os.environ.copy()
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
        environment.pop(key, None)
    environment.update(OPENAI_AGENTS_DISABLE_TRACING="1", NO_PROXY="127.0.0.1,localhost,::1")
    environment["no_proxy"] = environment["NO_PROXY"]

    with tempfile.TemporaryDirectory(prefix="checkpoint-wheel-compatibility-") as temporary:

        def run(python, action, **options):
            command = {"action": action, **options}
            result = subprocess.run(
                [str(python.absolute()), "-I", str(script), "--worker", json.dumps(command)],
                cwd=temporary,
                env=environment,
                text=True,
                capture_output=True,
                timeout=60,
            )
            assert result.returncode == 0, (command, result.stdout, result.stderr)
            check = json.loads(result.stdout)
            check["python"] = str(python.absolute())
            receipt["checks"].append(check)
            args.output.write_text(json.dumps(receipt, indent=2) + "\n")

        old, new = args.previous_python, args.candidate_python
        for label, python, version in (
            ("previous", old, "0.24.0a7"),
            ("candidate", new, "0.24.0a8"),
        ):
            run(
                python,
                "identity",
                wheel=str(getattr(args, label + "_wheel").resolve()),
                sha256=getattr(args, label + "_sha256"),
                version=version,
            )
        for label, producer, consumer in (("forward", old, new), ("rollback", new, old)):
            run(producer, "idle-create", name=label + "-idle", destination=label + "-idle.json")
            run(
                consumer,
                "idle-continue",
                name=label + "-continued",
                source=label + "-idle.json",
                destination=label + "-continued.json",
                markers=[label + "-idle"],
            )
            run(
                producer,
                "manual-create",
                name=label + "-manual",
                destination=label + "-manual.json",
                effects=label + "-effects.txt",
            )
            run(
                consumer,
                "manual-continue",
                source=label + "-manual.json",
                destination=label + "-settled.json",
                effects=label + "-effects.txt",
            )
            run(
                producer,
                "idle-continue",
                name=label + "-after-manual",
                source=label + "-settled.json",
                destination=label + "-final.json",
                tools=True,
                markers=[label + "-manual", "manual-restored"],
            )
        for label, sequence in (("old-profile", (old, new, old)), ("new-profile", (new, old, new))):
            markers = []
            for index, python in enumerate(sequence):
                name = label + "-" + str(index)
                run(
                    python,
                    "local",
                    name=name,
                    profile=label,
                    identity=label + ".id",
                    markers=markers,
                )
                markers.append(name)
        run(
            new,
            "budget-create",
            name="budgeted-manual",
            destination="budget.json",
            effects="budget-effects.txt",
        )
        run(old, "budget-reject", source="budget.json")
        run(
            new,
            "budget-continue",
            source="budget.json",
            destination="budget-settled.json",
            effects="budget-effects.txt",
        )
    receipt["status"] = "passed"
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Passed {len(receipt['checks'])} wheel compatibility checks; receipt: {args.output}")


if __name__ == "__main__":
    main()
