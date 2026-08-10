from __future__ import annotations

import json
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).resolve().parent / "features"
SECURITY_OUTPUT = Path(__file__).resolve().parent / "security"
RESUME_OUTPUT = Path(__file__).resolve().parent / "resume"

BASE = """
import json

from agents import Agent, RunContextWrapper, RunState

agent = Agent(name="compat-agent")
state = RunState(
    context=RunContextWrapper(context={}),
    original_input="historical input",
    starting_agent=agent,
    max_turns=10,
)
"""


@dataclass(frozen=True)
class Scenario:
    version: str
    commit: str
    name: str
    code: str
    provenance: str = "historical_writer"
    emitted_version: str | None = None


SCENARIOS = (
    Scenario(
        "1.2",
        "74e8c1e22d7441bd42c58bcd4270937ccc2dca8c",
        "reasoning_item_id_policy",
        """
from agents.items import ReasoningItem
from openai.types.responses import ResponseReasoningItem

state.set_reasoning_item_id_policy("omit")
state._generated_items = [
    ReasoningItem(
        agent=agent,
        raw_item=ResponseReasoningItem(type="reasoning", id="reasoning-1", summary=[]),
    )
]
""",
    ),
    Scenario(
        "1.3",
        "6814a54711f591712c893f0a8be1cf56c512ae63",
        "resumed_trace_state",
        """
from agents import trace

with trace(
    workflow_name="compatibility trace",
    tracing={"api_key": "fixed-trace-key"},
) as run_trace:
    state.set_trace(run_trace)
""",
    ),
    Scenario(
        "1.4",
        "159beb56130f7d85192acfd593c9168757984dc0",
        "request_id",
        """
from agents import ModelResponse, Usage

state._model_responses = [
    ModelResponse(output=[], usage=Usage(), response_id="response-1", request_id="request-1")
]
""",
    ),
    Scenario(
        "1.5",
        "e0f6a28c20887b83dd4e1532cdfe0b78a01d4961",
        "tool_search_and_display_metadata",
        """
from agents.items import ToolCallItem, ToolSearchCallItem, ToolSearchOutputItem
from openai.types.responses import ResponseFunctionToolCall

state._generated_items = [
    ToolSearchCallItem(
        agent=agent,
        raw_item={
            "type": "tool_search_call",
            "arguments": {"query": "account balance"},
            "execution": "server",
            "status": "completed",
        },
    ),
    ToolSearchOutputItem(
        agent=agent,
        raw_item={
            "type": "tool_search_output",
            "execution": "server",
            "status": "completed",
            "tools": [],
        },
    ),
    ToolCallItem(
        agent=agent,
        raw_item=ResponseFunctionToolCall(
            type="function_call",
            name="lookup",
            call_id="call-display",
            status="completed",
            arguments="{}",
        ),
        title="Lookup account",
        description="Reads the account balance.",
    ),
]
""",
    ),
    Scenario(
        "1.6",
        "86739b1a0f94d73f9a35e68f6f25ddc0beaa2078",
        "approval_rejection_message",
        """
from agents.items import ToolApprovalItem
from openai.types.responses import ResponseFunctionToolCall

approval = ToolApprovalItem(
    agent=agent,
    raw_item=ResponseFunctionToolCall(
        type="function_call",
        name="sensitive_tool",
        call_id="approval-1",
        status="completed",
        arguments="{}",
    ),
)
state.reject(approval, rejection_message="Denied by release reviewer")
""",
    ),
    Scenario(
        "1.7",
        "2d665c9a67fdf3198a0daa0f9978b8239d78e78b",
        "duplicate_agent_identity_and_sandbox",
        """
from agents import handoff

duplicate = Agent(name="compat-agent")
agent.handoffs = [handoff(duplicate)]
state._current_agent = duplicate
state._sandbox = {
    "provider": "compat-provider",
    "session_state": {"session_id": "sandbox-session-1"},
    "requires_rebind": True,
}
""",
        provenance="canonical_compatibility",
        emitted_version="1.9",
    ),
    Scenario(
        "1.8",
        "2d665c9a67fdf3198a0daa0f9978b8239d78e78b",
        "prompt_cache_key",
        """
state._generated_prompt_cache_key = "prompt-cache-key-1"
""",
        provenance="canonical_compatibility",
        emitted_version="1.9",
    ),
    Scenario(
        "1.9",
        "bed924b45d97ea0080655329129075e457d46c6d",
        "custom_tool_call_and_tool_origin",
        """
from agents import ToolOrigin, ToolOriginType
from agents.items import ToolCallItem, ToolCallOutputItem

origin = ToolOrigin(type=ToolOriginType.FUNCTION)
state._generated_items = [
    ToolCallItem(
        agent=agent,
        raw_item={
            "type": "custom_tool_call",
            "call_id": "custom-call-1",
            "name": "custom_lookup",
            "input": "account-1",
        },
        tool_origin=origin,
    ),
    ToolCallOutputItem(
        agent=agent,
        raw_item={
            "type": "custom_tool_call_output",
            "call_id": "custom-call-1",
            "output": "custom result",
        },
        output="custom result",
        tool_origin=origin,
    ),
]
""",
    ),
    Scenario(
        "1.10",
        "a4ba63f7045d27998a0b1bc1ee64a313574ee139",
        "unlimited_max_turns",
        """
state._max_turns = None
""",
    ),
    Scenario(
        "1.11",
        "70c447e14ffabdf29bfaeb4bb3df33bb6dfaaab7",
        "tool_output_custom_data",
        """
from agents.items import ToolCallOutputItem

state._generated_items = [
    ToolCallOutputItem(
        agent=agent,
        raw_item={
            "type": "function_call_output",
            "call_id": "custom-data-1",
            "output": "result",
        },
        output="result",
        custom_data={"ui": {"kind": "chart"}, "ids": ["a", "b"]},
    )
]
""",
    ),
    Scenario(
        "1.12",
        "95df2c99a745655ba71c763b8ac036283e9df87e",
        "input_cache_write_usage",
        """
from agents.usage import InputTokensDetails

state._context.usage.requests = 1
state._context.usage.input_tokens = 10
state._context.usage.input_tokens_details = InputTokensDetails.model_validate(
    {"cache_write_tokens": 7, "cached_tokens": 3}
)
""",
    ),
    Scenario(
        "1.13",
        "ece7b0e5861d6c839041d5f860a2a2cf08bba81e",
        "programmatic_tool_calling",
        """
from agents.items import ModelResponse, ToolCallItem, ToolCallOutputItem
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_function_tool_call import CallerProgram
from openai.types.responses.response_output_item import Program, ProgramOutput

program = Program(
    id="program-item",
    call_id="program-call",
    code="lookup()",
    fingerprint="fingerprint",
    type="program",
)
function_call = ResponseFunctionToolCall(
    id="function-item",
    call_id="function-call",
    name="lookup",
    arguments="{}",
    caller=CallerProgram(type="program", caller_id="program-call"),
    type="function_call",
)
program_output = ProgramOutput(
    id="program-output-item",
    call_id="program-call",
    result="done",
    status="completed",
    type="program_output",
)
state._model_responses = [
    ModelResponse(
        output=[program, function_call, program_output],
        usage=Usage(),
        response_id="response-program",
    )
]
state._generated_items = [
    ToolCallItem(agent=agent, raw_item=program),
    ToolCallItem(agent=agent, raw_item=function_call),
    ToolCallOutputItem(agent=agent, raw_item=program_output, output="done"),
]
""",
    ),
    Scenario(
        "1.13",
        "ece7b0e5861d6c839041d5f860a2a2cf08bba81e",
        "nested_history_ownership",
        """
from agents.items import MessageOutputItem
from agents.run_internal.items import (
    NestedHistoryOwnedItemRef,
    digest_input_item,
    run_item_to_input_item,
)
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

message_item = MessageOutputItem(
    agent=agent,
    raw_item=ResponseOutputMessage(
        id="owned-message",
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(
                type="output_text",
                text="owned history",
                annotations=[],
            )
        ],
    ),
)
input_item = run_item_to_input_item(message_item)
digest = digest_input_item(input_item)
assert input_item is not None and digest is not None
state._original_input = [input_item]
state._session_items = [message_item]
state._generated_items = [message_item]
state._nested_history_owned_session_item_refs = [
    NestedHistoryOwnedItemRef(
        session_index=0,
        digest=digest,
        input_index=0,
        run_item=message_item,
        input_item=input_item,
    )
]
""",
    ),
    Scenario(
        "1.14",
        "0c60a196af1236044a829e39b10f22a9cedaa326",
        "hosted_mcp_approval_scope",
        """
from agents.items import ToolApprovalItem
from openai.types.responses.response_output_item import McpApprovalRequest

approval = ToolApprovalItem(
    agent=agent,
    raw_item=McpApprovalRequest(
        id="mcp-request-1",
        type="mcp_approval_request",
        arguments="{}",
        name="lookup_account",
        server_label="accounts-server",
    ),
)
state.approve(approval, always_approve=True)
""",
    ),
    Scenario(
        "1.15",
        "9c6cadf8201f4908ced206d49ed9f1489dc9db67",
        "canonical_invocation_identity",
        """
from agents.items import ToolApprovalItem
from openai.types.responses import ResponseFunctionToolCall

approval = ToolApprovalItem(
    agent=agent,
    raw_item=ResponseFunctionToolCall(
        type="function_call",
        name="lookup_account",
        call_id="function-request-1",
        status="completed",
        arguments='{"account_id":"account-1"}',
    ),
)
state.approve(approval)
""",
    ),
)


LEGACY_MOUNT_CREDENTIALS = Scenario(
    "1.13",
    "92aa1b905306d7f5a130d911061c44cddeaa6e20",
    "legacy_mount_credentials",
    """
from agents.sandbox import Manifest
from agents.sandbox.entries import DockerVolumeMountStrategy, S3Mount
from agents.sandbox.snapshot import NoopSnapshot

manifest = Manifest(
    entries={
        "remote": S3Mount(
            bucket="compat-bucket",
            access_key_id="RUNSTATE_ACCESS_SENTINEL_42",
            secret_access_key="RUNSTATE_SECRET_SENTINEL_42",
            session_token="RUNSTATE_TOKEN_SENTINEL_42",
            region="us-east-1",
            mount_strategy=DockerVolumeMountStrategy(
                driver="rclone",
                driver_options={"vfs-cache-mode": "off"},
            ),
        )
    }
)
session_state = {
    "type": "unix_local",
    "session_id": "00000000-0000-0000-0000-000000000042",
    "snapshot": NoopSnapshot(id="legacy-snapshot").model_dump(mode="json"),
    "manifest": manifest.model_dump(mode="json"),
    "exposed_ports": [],
    "workspace_root_owned": False,
}
state._sandbox = {
    "backend_id": "unix_local",
    "current_agent_key": agent.name,
    "current_agent_name": agent.name,
    "session_state": session_state,
    "sessions_by_agent": {
        agent.name: {
            "agent_name": agent.name,
            "session_state": session_state,
        }
    },
}
""",
)


PENDING_TOOL_APPROVAL = Scenario(
    "1.13",
    "92aa1b905306d7f5a130d911061c44cddeaa6e20",
    "pending_tool_approval",
    r"""
import asyncio

from agents import Runner, function_tool
from tests.fake_model import FakeModel
from tests.test_responses import get_function_tool_call

@function_tool(needs_approval=True)
def historical_approval(account_id: str) -> str:
    return f"approved:{account_id}"

async def produce_pending_state():
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [[get_function_tool_call(
            "historical_approval",
            '{"account_id":"account-1"}',
            call_id="historical-approval-1",
        )]]
    )
    run_agent = Agent(name="compat-agent", model=model, tools=[historical_approval])
    result = await Runner.run(run_agent, "historical input")
    assert len(result.interruptions) == 1
    return result.to_state()

state = asyncio.run(produce_pending_state())
""",
)


def _extract(commit: str, destination: Path) -> None:
    archive = subprocess.check_output(["git", "archive", commit], cwd=ROOT)
    with tarfile.open(fileobj=BytesIO(archive)) as bundle:
        bundle.extractall(destination, filter="data")


def _generate(scenario: Scenario) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"run-state-{scenario.version}-") as temp:
        tree = Path(temp)
        _extract(scenario.commit, tree)
        env = dict(os.environ)
        env["UV_DEFAULT_INDEX"] = "https://pypi.org/simple"
        for variable in (
            "ALL_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "all_proxy",
            "http_proxy",
            "https_proxy",
        ):
            env.pop(variable, None)
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(tree),
                "--frozen",
                "--no-dev",
                "python",
                "-c",
                BASE + scenario.code + "\nprint(json.dumps(state.to_json(), sort_keys=True))\n",
            ],
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise RuntimeError(
                f"Historical writer {scenario.commit} failed:\n"
                f"{completed.stdout}\n{completed.stderr}"
            )
        payload = json.loads(completed.stdout)
        emitted_version = scenario.emitted_version or scenario.version
        if payload["$schemaVersion"] != emitted_version:
            raise RuntimeError(
                f"Historical writer {scenario.commit} emitted "
                f"{payload['$schemaVersion']}, expected {emitted_version}."
            )
        if scenario.provenance == "canonical_compatibility":
            payload["$schemaVersion"] = scenario.version
        return cast(dict[str, object], payload)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    feature_sources: list[dict[str, str]] = []
    for scenario in SCENARIOS:
        payload = _generate(scenario)
        filename = f"v{scenario.version.replace('.', '_')}_{scenario.name}.json"
        (OUTPUT / filename).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        source = {
            "version": scenario.version,
            "feature": scenario.name,
            "commit": scenario.commit,
            "fixture": f"features/{filename}",
            "provenance": scenario.provenance,
        }
        if scenario.emitted_version is not None:
            source["emitted_version"] = scenario.emitted_version
            source["note"] = (
                "The release-boundary schema renumbering introduced this reader version "
                "without a writer that emitted it. The recorded writer emitted 1.9; only "
                "the schema label is changed to exercise the canonical compatibility branch."
            )
        feature_sources.append(source)

    sources_path = OUTPUT.parent / "sources.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    sources["features"] = feature_sources

    SECURITY_OUTPUT.mkdir(parents=True, exist_ok=True)
    security_payload = _generate(LEGACY_MOUNT_CREDENTIALS)
    security_filename = "v1_13_legacy_mount_credentials.json"
    (SECURITY_OUTPUT / security_filename).write_text(
        json.dumps(security_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sources["security"] = {
        "version": LEGACY_MOUNT_CREDENTIALS.version,
        "feature": LEGACY_MOUNT_CREDENTIALS.name,
        "commit": LEGACY_MOUNT_CREDENTIALS.commit,
        "fixture": f"security/{security_filename}",
        "provenance": LEGACY_MOUNT_CREDENTIALS.provenance,
        "sentinels": [
            "RUNSTATE_ACCESS_SENTINEL_42",
            "RUNSTATE_SECRET_SENTINEL_42",
            "RUNSTATE_TOKEN_SENTINEL_42",
        ],
    }

    RESUME_OUTPUT.mkdir(parents=True, exist_ok=True)
    resume_payload = _generate(PENDING_TOOL_APPROVAL)
    resume_filename = "v1_13_pending_tool_approval.json"
    (RESUME_OUTPUT / resume_filename).write_text(
        json.dumps(resume_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sources["resume"] = {
        "version": PENDING_TOOL_APPROVAL.version,
        "feature": PENDING_TOOL_APPROVAL.name,
        "commit": PENDING_TOOL_APPROVAL.commit,
        "fixture": f"resume/{resume_filename}",
        "provenance": PENDING_TOOL_APPROVAL.provenance,
    }
    sources_path.write_text(
        json.dumps(sources, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
