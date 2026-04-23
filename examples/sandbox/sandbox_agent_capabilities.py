from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from openai.types.responses import ResponseFunctionCallArgumentsDeltaEvent, ResponseTextDeltaEvent
from openai.types.responses.response_prompt_param import ResponsePromptParam

from agents import (
    AgentOutputSchemaBase,
    AgentUpdatedStreamEvent,
    ApplyPatchOperation,
    Handoff,
    ItemHelpers,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    OpenAIProvider,
    RawResponsesStreamEvent,
    RunContextWrapper,
    RunItemStreamEvent,
    Runner,
    RunResultStreaming,
    Tool,
    ToolOutputImage,
)
from agents.items import (
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
    TResponseStreamEvent,
)
from agents.run import RunConfig
from agents.sandbox import LocalFile, Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import (
    Filesystem,
    FilesystemToolSet,
    LocalDirLazySkillSource,
    Skills,
)
from agents.sandbox.capabilities.capabilities import Capabilities
from agents.sandbox.entries import File, LocalDir
from agents.sandbox.errors import WorkspaceReadNotFoundError
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


DEFAULT_MODEL = "gpt-5.4"
COMPACTION_THRESHOLD = 1_000
VERIFICATION_FILE = Path("verification/capabilities.txt")
DELETE_FILE = Path("verification/delete-me.txt")


class RecordingModel(Model):
    def __init__(self, model_name: str) -> None:
        self._model = OpenAIProvider().get_model(model_name)
        self.first_input: str | list[TResponseInputItem] | None = None
        self.first_model_settings: ModelSettings | None = None

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        if self.first_input is None:
            self.first_input = input
            self.first_model_settings = model_settings
        return await self._model.get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        if self.first_input is None:
            self.first_input = input
            self.first_model_settings = model_settings
        return self._model.stream_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )

    async def close(self) -> None:
        await self._model.close()


def _build_manifest() -> Manifest:
    return Manifest(
        entries={
            "README.md": File(
                content=(
                    b"# Capability Smoke Workspace\n\n"
                    b"This workspace is used to verify sandbox capabilities end to end.\n"
                    b"Project code name: atlas.\n"
                )
            ),
            "notes/input.txt": File(content=b"source=filesystem\n"),
            "examples/image.png": LocalFile(
                src=Path(__file__).parent.parent.parent / "docs/assets/images/graph.png"
            ),
        }
    )


def _write_local_skill(skills_root: Path) -> None:
    skill_dir = skills_root / "capability-proof"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: capability-proof",
                "description: Verifies the sandbox skills capability in the smoke example.",
                "---",
                "",
                "# Capability Proof",
                "",
                "When loaded, write a verification file containing these exact lines:",
                "- skill_loaded=true",
                "- codename=atlas",
                "- note_source=filesystem",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _build_agent(model: RecordingModel, skills_root: Path) -> SandboxAgent:
    capabilities = Capabilities.default() + [
        Skills(
            lazy_from=LocalDirLazySkillSource(
                # This is a host path read by the SDK process.
                # Requested skills are copied into `skills_path` in the sandbox.
                source=LocalDir(src=skills_root),
            )
        ),
    ]

    def apply_patch_needs_approval(
        ctx: RunContextWrapper[Any], operation: ApplyPatchOperation, call_id: str
    ):
        return False

    def _configure_filesystem(toolset: FilesystemToolSet):
        toolset.apply_patch.needs_approval = apply_patch_needs_approval

    for capability in capabilities:
        if isinstance(capability, Filesystem):
            capability.configure_tools = _configure_filesystem

    return SandboxAgent(
        name="Sandbox Capabilities Smoke",
        model=model,
        instructions=(
            "Run the sandbox capability smoke test end to end, use the available tools "
            "deliberately, and then give a one-line final summary. "
            "Follow this sequence:\n"
            "1. Inspect the workspace root at `.`.\n"
            "2. Read `README.md`.\n"
            "3. Use `view_image` on `examples/image.png` and confirm it shows a routing diagram "
            "centered on `Triage Agent`.\n"
            "4. Use the `capability-proof` skill.\n"
            f"5. Create `{VERIFICATION_FILE.as_posix()}` with exactly these two lines:\n"
            "   skill_loaded=true\n"
            "   codename=atlas\n"
            "6. Update that file so it has exactly these four lines:\n"
            "   skill_loaded=true\n"
            "   codename=atlas\n"
            "   note_source=filesystem\n"
            "   image_verified=true\n"
            f"7. Create `{DELETE_FILE.as_posix()}`, then delete it.\n"
            f"8. Print `{VERIFICATION_FILE.as_posix()}` from the shell.\n"
            "When referring to the workspace root in any path argument, use `.` exactly. Do not "
            "use an empty string for a path.\n"
            "Keep the final answer to one line: `capability smoke complete`."
        ),
        default_manifest=_build_manifest(),
        capabilities=capabilities,
        model_settings=ModelSettings(tool_choice="required"),
    )


def _initial_input() -> list[TResponseInputItem]:
    return [
        {
            "role": "user",
            "content": (
                "Run the sandbox capability smoke test now. Use the listed tools and then answer "
                "with `capability smoke complete`."
            ),
        },
    ]


def _tool_call_name(item: ToolCallItem) -> str:
    raw_item = item.raw_item
    if isinstance(raw_item, dict):
        if raw_item.get("type") == "apply_patch_call":
            return "apply_patch"
        return cast(str, raw_item.get("name") or raw_item.get("type") or "")
    return cast(str, getattr(raw_item, "name", None) or getattr(raw_item, "type", None) or "")


async def _read_workspace_text(session: BaseSandboxSession, path: Path) -> str:
    handle = await session.read(path)
    try:
        payload = handle.read()
    finally:
        handle.close()
    if isinstance(payload, str):
        return payload
    return bytes(payload).decode("utf-8")


def _format_tool_call_arguments(item: ToolCallItem) -> str | None:
    raw_item = item.raw_item
    if isinstance(raw_item, dict):
        arguments = raw_item.get("arguments")
    else:
        arguments = getattr(raw_item, "arguments", None)
    if not isinstance(arguments, str) or arguments == "":
        return None

    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments
    return json.dumps(parsed, indent=2, sort_keys=True)


def _format_tool_output(output: object) -> str:
    text = str(output)
    if len(text) <= 240:
        return text
    return f"{text[:240]}..."


async def _print_stream_details(result: RunResultStreaming) -> None:
    print("=== Stream starting ===")
    print("Streaming raw text deltas, tool activity, and semantic run events as they arrive.\n")

    active_tool_call: str | None = None
    text_stream_open = False

    async for event in result.stream_events():
        if isinstance(event, AgentUpdatedStreamEvent):
            if text_stream_open:
                print()
                text_stream_open = False
            print(f"[agent] switched to: {event.new_agent.name}")
            continue

        if isinstance(event, RawResponsesStreamEvent):
            data = event.data
            if isinstance(data, ResponseTextDeltaEvent):
                if not text_stream_open:
                    print("[model:text] ", end="", flush=True)
                    text_stream_open = True
                print(data.delta, end="", flush=True)
                continue
            if isinstance(data, ResponseFunctionCallArgumentsDeltaEvent):
                if text_stream_open:
                    print()
                    text_stream_open = False
                if active_tool_call is None:
                    active_tool_call = "tool"
                    print("[model:tool_args] ", end="", flush=True)
                print(data.delta, end="", flush=True)
                continue

            event_type = getattr(data, "type", None)
            if event_type == "response.output_item.done" and active_tool_call is not None:
                print()
                print(f"[model:tool_args] completed for {active_tool_call}")
                active_tool_call = None
            continue

        if text_stream_open:
            print()
            text_stream_open = False
        if active_tool_call is not None:
            print()
            active_tool_call = None

        if not isinstance(event, RunItemStreamEvent):
            continue

        if event.item.type == "tool_call_item":
            tool_name = _tool_call_name(event.item)
            active_tool_call = tool_name
            print(f"[tool:call] {tool_name}")
            arguments = _format_tool_call_arguments(event.item)
            if arguments:
                print(arguments)
        elif event.item.type == "tool_call_output_item":
            print(f"[tool:output] {_format_tool_output(event.item.output)}")
        elif event.item.type == "message_output_item":
            message_text = ItemHelpers.text_message_output(event.item)
            print(f"[message:complete] {len(message_text)} characters")
        elif event.item.type == "reasoning_item":
            print("[reasoning] model emitted a reasoning item")
        else:
            print(f"[event:{event.name}] item_type={event.item.type}")

    if text_stream_open:
        print()
    print("\n=== Stream complete ===")


async def main(model_name: str) -> None:
    model = RecordingModel(model_name)
    with tempfile.TemporaryDirectory(prefix="agents-skills-") as temp_dir:
        skills_root = Path(temp_dir) / "skills"
        _write_local_skill(skills_root)

        agent = _build_agent(model, skills_root)
        client = UnixLocalSandboxClient()
        sandbox = await client.create(manifest=agent.default_manifest)

        try:
            async with sandbox:
                result = Runner.run_streamed(
                    agent,
                    _initial_input(),
                    run_config=RunConfig(
                        sandbox=SandboxRunConfig(session=sandbox),
                        tracing_disabled=True,
                        workflow_name="Sandbox capabilities smoke",
                    ),
                )
                await _print_stream_details(result)

                tool_calls = [
                    _tool_call_name(item)
                    for item in result.new_items
                    if isinstance(item, ToolCallItem)
                ]
                tool_outputs = [
                    item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)
                ]
                vision_outputs = [
                    output for output in tool_outputs if isinstance(output, ToolOutputImage)
                ]
                verification_text = await _read_workspace_text(sandbox, VERIFICATION_FILE)
                delete_file_exists = True
                try:
                    handle = await sandbox.read(DELETE_FILE)
                except WorkspaceReadNotFoundError:
                    delete_file_exists = False
                else:
                    handle.close()

                first_model_settings = model.first_model_settings
                if first_model_settings is None:
                    raise RuntimeError("Model settings were not captured")
                extra_args = first_model_settings.extra_args or {}
                if extra_args.get("context_management") is None:
                    raise RuntimeError(
                        f"Compaction sampling params were not attached: {extra_args!r}"
                    )

                expected_tools = {
                    "load_skill",
                    "apply_patch",
                    "exec_command",
                    "view_image",
                }
                missing_tools = expected_tools - set(tool_calls)
                if missing_tools:
                    raise RuntimeError(
                        "Missing expected tool calls: "
                        f"{sorted(missing_tools)}; observed tool calls: {tool_calls}"
                    )

                expected_verification = (
                    "skill_loaded=true\n"
                    "codename=atlas\n"
                    "note_source=filesystem\n"
                    "image_verified=true\n"
                )
                if verification_text.rstrip("\n") != expected_verification.rstrip("\n"):
                    raise RuntimeError(
                        "Verification file content mismatch:\n"
                        f"expected={expected_verification!r}\n"
                        f"actual={verification_text!r}"
                    )

                if expected_verification.strip() not in "\n".join(
                    str(output) for output in tool_outputs
                ):
                    raise RuntimeError("Shell output did not include the verification file content")

                if not vision_outputs:
                    raise RuntimeError("Expected view_image to produce a ToolOutputImage")

                if not all(
                    isinstance(output.image_url, str) and output.image_url.startswith("data:image/")
                    for output in vision_outputs
                ):
                    raise RuntimeError(
                        f"Expected ToolOutputImage data URLs from view_image, got {vision_outputs!r}"
                    )

                if delete_file_exists:
                    raise RuntimeError(f"Expected {DELETE_FILE.as_posix()} to be deleted")

                print("=== Final summary ===")
                print("final_output:", result.final_output)
                print("tool_calls:", ", ".join(tool_calls))
                print("vision_outputs:", len(vision_outputs))
                print(f"compaction_threshold: {COMPACTION_THRESHOLD}")
                print(f"compaction_extra_args: {extra_args}")
                print(f"verification_file: {VERIFICATION_FILE.as_posix()}")
                print(f"deleted_file_absent: {not delete_file_exists}")
                print(verification_text, end="")
        finally:
            await client.delete(sandbox)
            await model.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    args = parser.parse_args()

    asyncio.run(main(args.model))
