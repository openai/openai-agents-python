from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urljoin

from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel

from agents import Agent, ModelSettings, Runner, function_tool
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from examples.sandbox.misc.example_support import text_manifest, tool_call_name
from examples.sandbox.misc.workspace_shell import WorkspaceShellCapability

try:
    from agents.extensions.sandbox import (
        DEFAULT_RUNLOOP_ROOT_WORKSPACE_ROOT,
        DEFAULT_RUNLOOP_WORKSPACE_ROOT,
        RunloopAfterIdle,
        RunloopGatewaySpec,
        RunloopLaunchParameters,
        RunloopMcpSpec,
        RunloopSandboxClient,
        RunloopSandboxClientOptions,
        RunloopSandboxSessionState,
        RunloopTunnelConfig,
        RunloopUserParameters,
    )
except Exception as exc:  # pragma: no cover - import path depends on optional extras
    raise SystemExit(
        "Runloop sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra runloop"
    ) from exc


DEFAULT_MODEL = "gpt-5.5"
DEFAULT_HTTP_PORT = 8123
DEFAULT_AGENT_PROMPT = (
    "Inspect this Runloop sandbox workspace, verify the configuration using the shell tool, "
    "and summarize which Runloop-specific capabilities were exercised."
)
EXAMPLE_RESOURCE_SLUG = "runloop-capabilities-example"
PERSISTENT_SECRET_NAME = "RUNLOOP_CAPABILITIES_EXAMPLE_TOKEN"
PERSISTENT_SECRET_VALUE = "runloop-capabilities-example-token"
PERSISTENT_NETWORK_POLICY_NAME = "runloop-capabilities-example-policy"
HTTP_LOG_PATH = Path(".runloop-http.log")
RUNTIME_CONTEXT_PATH = Path("runtime_context.json")
AGENT_PROOF_PATH = Path("verification/agent-proof.txt")


class RunloopResourceQueryResult(BaseModel):
    resource_type: Literal["secret", "network_policy"]
    name: str
    found: bool
    id: str | None = None
    description: str | None = None


class RunloopResourceBootstrapResult(BaseModel):
    resource_type: Literal["secret", "network_policy"]
    name: str
    action: Literal["created", "reused", "override"]
    id: str | None = None
    found_before_bootstrap: bool


def _phase(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def _require_env(name: str) -> None:
    if os.environ.get(name):
        return
    raise SystemExit(f"{name} must be set before running this example.")


def _run_id() -> str:
    return uuid.uuid4().hex[:8]


def _summarize_resource(item: object, fields: tuple[str, ...]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for field in fields:
        value = getattr(item, field, None)
        if value is not None:
            summary[field] = value
    return summary


async def _collect_async_items(items: Any, *, limit: int) -> list[Any]:
    collected: list[Any] = []
    async for item in items:
        collected.append(item)
        if len(collected) >= limit:
            break
    return collected


def _status_code(exc: BaseException) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _is_not_found(exc: BaseException) -> bool:
    return _status_code(exc) == 404


def _error_message(exc: BaseException) -> str | None:
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        return message
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        body_message = body.get("message")
        if isinstance(body_message, str):
            return body_message
    return None


def _is_conflict(exc: BaseException) -> bool:
    status_code = _status_code(exc)
    if status_code == 409:
        return True
    if status_code == 400:
        message = _error_message(exc)
        return isinstance(message, str) and "already exists" in message.lower()
    return False


async def _collect_maybe_async_items(items: Any, *, limit: int) -> list[Any]:
    if hasattr(items, "__aiter__"):
        return await _collect_async_items(items, limit=limit)
    return list(items)[:limit]


async def _read_text(session: Any, path: Path) -> str:
    data = await session.read(path)
    try:
        payload = data.read()
    finally:
        data.close()
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    return str(payload)


async def _write_json(session: Any, path: Path, payload: dict[str, object]) -> None:
    await session.write(
        path, io.BytesIO(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))
    )


def _build_manifest(*, workspace_root: str, context: dict[str, object]) -> Manifest:
    manifest = text_manifest(
        {
            "README.md": (
                "# Runloop Capabilities Example\n\n"
                "This workspace is used to validate the Runloop-specific sandbox integration end "
                "to end.\n"
            ),
            "checklist.md": (
                "# Checklist\n\n"
                "1. Inspect the workspace.\n"
                "2. Verify the resource discovery results in the context files.\n"
                "3. Confirm the managed secret is available without printing its full value.\n"
                "4. Confirm the HTTP preview server and verification file.\n"
                "5. Summarize what Runloop-native features were exercised and whether persistent "
                "resources were reused or created.\n"
            ),
            "platform_context.json": json.dumps(context, indent=2, sort_keys=True) + "\n",
        }
    )
    return Manifest(root=workspace_root, entries=manifest.entries)


def _build_sandbox_agent(
    *, model: str, manifest: Manifest, managed_secret_name: str
) -> SandboxAgent:
    return SandboxAgent(
        name="Runloop Capabilities Guide",
        model=model,
        instructions=(
            "Inspect the Runloop sandbox workspace carefully before answering. Use the shell tool "
            "to verify what happened in the environment and keep the final response concise. "
            "Follow this sequence:\n"
            "1. Run `pwd` and `find . -maxdepth 3 -type f | sort`.\n"
            "2. Read `README.md`, `checklist.md`, `platform_context.json`, and `runtime_context.json`.\n"
            "3. Report whether the managed secret and network policy existed before bootstrap by "
            "reading the query/bootstrap summaries from the context files.\n"
            f"4. Confirm whether `${managed_secret_name}` is set, but never print the full value. "
            "Only report whether it exists and its character length.\n"
            f"5. Read `{HTTP_LOG_PATH.as_posix()}` and confirm the HTTP server started.\n"
            f"6. Create `{AGENT_PROOF_PATH.as_posix()}` with these exact lines:\n"
            "   runloop_capabilities_verified=true\n"
            "   managed_secret_checked=true\n"
            "   tunnel_verified=true\n"
            "7. Print that verification file from the shell.\n"
            "8. Final answer: 2 short sentences naming the specific Runloop features exercised, "
            "including whether the persistent secret and policy were reused or created.\n"
            "Only mention facts you verified from files, environment inspection, or shell output."
        ),
        default_manifest=manifest,
        capabilities=[WorkspaceShellCapability()],
        model_settings=ModelSettings(tool_choice="required"),
    )


def _build_query_agent(
    *,
    model: str,
    query_secret_tool: Any,
    query_policy_tool: Any,
    managed_secret_name: str,
    network_policy_name: str,
) -> Agent:
    return Agent(
        name="Runloop Resource Discovery Guide",
        model=model,
        instructions=(
            "Use the provided Runloop query tools to check whether the persistent example "
            "resources already exist before any create step. Keep the final answer concise."
        ),
        tools=[query_secret_tool, query_policy_tool],
        model_settings=ModelSettings(tool_choice="required"),
    ).clone(
        instructions=(
            "Use the provided Runloop query tools to check whether the persistent example "
            "resources already exist before any create step. Keep the final answer concise."
        ),
        handoff_description=None,
        output_type=None,
    )


def _stream_event_banner(event_name: str) -> str | None:
    if event_name == "tool_called":
        return "[tool call]"
    if event_name == "tool_output":
        return "[tool output]"
    return None


def _runloop_state(session: Any) -> RunloopSandboxSessionState:
    return cast(RunloopSandboxSessionState, session.state)


async def _run_plain_agent(
    *,
    agent: Agent,
    prompt: str,
    workflow_name: str,
    stream: bool,
) -> str:
    if not stream:
        result = await Runner.run(agent, prompt, run_config=RunConfig(workflow_name=workflow_name))
        print(result.final_output)
        return str(result.final_output)

    stream_result = Runner.run_streamed(
        agent,
        prompt,
        run_config=RunConfig(workflow_name=workflow_name),
    )
    saw_text_delta = False
    saw_any_text = False

    async for event in stream_result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            if not saw_text_delta:
                print("assistant> ", end="", flush=True)
                saw_text_delta = True
            print(event.data.delta, end="", flush=True)
            saw_any_text = True
            continue

        if event.type != "run_item_stream_event":
            continue

        banner = _stream_event_banner(event.name)
        if banner is None:
            continue
        if saw_text_delta:
            print()
            saw_text_delta = False
        print(f"{banner}: {tool_call_name(event.item.raw_item) or 'tool'}", flush=True)

    if saw_text_delta:
        print()
    if not saw_any_text:
        print(stream_result.final_output)
    return str(stream_result.final_output)


async def _run_sandbox_agent(
    *,
    agent: SandboxAgent,
    prompt: str,
    session: Any,
    workflow_name: str,
    stream: bool,
) -> str:
    if not stream:
        result = await Runner.run(
            agent,
            prompt,
            run_config=RunConfig(
                sandbox=SandboxRunConfig(session=session),
                workflow_name=workflow_name,
            ),
        )
        print(result.final_output)
        return str(result.final_output)

    stream_result = Runner.run_streamed(
        agent,
        prompt,
        run_config=RunConfig(
            sandbox=SandboxRunConfig(session=session),
            workflow_name=workflow_name,
        ),
    )
    saw_text_delta = False
    saw_any_text = False

    async for event in stream_result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            if not saw_text_delta:
                print("assistant> ", end="", flush=True)
                saw_text_delta = True
            print(event.data.delta, end="", flush=True)
            saw_any_text = True
            continue

        if event.type != "run_item_stream_event":
            continue

        banner = _stream_event_banner(event.name)
        if banner is None:
            continue
        if saw_text_delta:
            print()
            saw_text_delta = False
        print(f"{banner}: {tool_call_name(event.item.raw_item) or 'tool'}", flush=True)

    if saw_text_delta:
        print()
    if not saw_any_text:
        print(stream_result.final_output)
    return str(stream_result.final_output)


async def _start_http_server(session: Any, *, port: int, workspace_root: str) -> None:
    command = (
        "python -m http.server "
        f"{port} --bind 0.0.0.0 --directory {workspace_root} "
        f"> {HTTP_LOG_PATH.as_posix()} 2>&1 &"
    )
    result = await session.exec(command, shell=True, timeout=10)
    if not result.ok():
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))


def _build_endpoint_url(endpoint: Any) -> str:
    scheme = "https" if endpoint.tls else "http"
    port = endpoint.port
    host = endpoint.host
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return f"{scheme}://{host}/"
    return f"{scheme}://{host}:{port}/"


async def _fetch_text(url: str, *, timeout_s: float) -> str:
    def _fetch() -> str:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            payload = response.read()
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload)

    return await asyncio.to_thread(_fetch)


async def _poll_http_preview(url: str, *, expected_substring: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            body = await _fetch_text(url, timeout_s=5.0)
            if expected_substring in body:
                return body
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        await asyncio.sleep(2)
    if last_error is not None:
        raise RuntimeError(f"HTTP preview never became ready: {last_error}") from last_error
    raise RuntimeError("HTTP preview never returned the expected content.")


async def _preflight_public_resources(client: RunloopSandboxClient) -> dict[str, object]:
    blueprints = await _collect_async_items(
        await client.platform.blueprints.list_public(limit=3),
        limit=3,
    )
    benchmarks = await _collect_async_items(
        await client.platform.benchmarks.list_public(limit=3),
        limit=3,
    )

    blueprint_summaries = [
        _summarize_resource(item, ("id", "name", "status")) for item in blueprints
    ]
    benchmark_summaries = [
        _summarize_resource(item, ("id", "name", "description")) for item in benchmarks
    ]

    if blueprint_summaries:
        print("public blueprints:")
        for summary in blueprint_summaries:
            print(f"  - {summary}")
    else:
        print("public blueprints: none returned")

    if benchmark_summaries:
        print("public benchmarks:")
        for summary in benchmark_summaries:
            print(f"  - {summary}")
    else:
        print("public benchmarks: none returned")

    return {
        "public_blueprints": blueprint_summaries,
        "public_benchmarks": benchmark_summaries,
    }


async def _query_runloop_secret(
    client: RunloopSandboxClient,
    *,
    name: str,
) -> RunloopResourceQueryResult:
    try:
        secret = cast(Any, await client.platform.secrets.get(name))
    except Exception as exc:
        if _is_not_found(exc):
            return RunloopResourceQueryResult(resource_type="secret", name=name, found=False)
        raise

    return RunloopResourceQueryResult(
        resource_type="secret",
        name=name,
        found=True,
        id=cast(str | None, getattr(secret, "id", None)),
    )


async def _query_runloop_network_policy(
    client: RunloopSandboxClient,
    *,
    name: str,
) -> RunloopResourceQueryResult:
    policies = await _collect_maybe_async_items(
        await client.platform.network_policies.list(name=name, limit=10),
        limit=10,
    )
    for policy in policies:
        if getattr(policy, "name", None) != name:
            continue
        info = cast(
            Any, await client.platform.network_policies.get(cast(str, policy.id)).get_info()
        )
        return RunloopResourceQueryResult(
            resource_type="network_policy",
            name=name,
            found=True,
            id=cast(str | None, getattr(policy, "id", None)),
            description=cast(str | None, getattr(info, "description", None)),
        )

    return RunloopResourceQueryResult(resource_type="network_policy", name=name, found=False)


def _build_resource_query_tools(
    client: RunloopSandboxClient,
    *,
    managed_secret_name: str,
    network_policy_name: str,
) -> tuple[list[Any], dict[str, RunloopResourceQueryResult]]:
    query_results: dict[str, RunloopResourceQueryResult] = {}

    @function_tool
    async def query_runloop_secret(name: str) -> RunloopResourceQueryResult:
        """Query whether a Runloop secret exists by name and return non-sensitive metadata."""

        result = await _query_runloop_secret(client, name=name)
        query_results["secret"] = result
        return result

    @function_tool
    async def query_runloop_network_policy(name: str) -> RunloopResourceQueryResult:
        """Query whether a Runloop network policy exists by name and return basic metadata."""

        result = await _query_runloop_network_policy(client, name=name)
        query_results["network_policy"] = result
        return result

    tools = [query_runloop_secret, query_runloop_network_policy]
    _ = (managed_secret_name, network_policy_name)
    return tools, query_results


async def _run_resource_query_phase(
    client: RunloopSandboxClient,
    *,
    model: str,
    stream: bool,
    managed_secret_name: str,
    network_policy_name: str,
) -> tuple[dict[str, RunloopResourceQueryResult], str]:
    tools, query_results = _build_resource_query_tools(
        client,
        managed_secret_name=managed_secret_name,
        network_policy_name=network_policy_name,
    )
    query_agent = Agent(
        name="Runloop Resource Discovery Guide",
        model=model,
        instructions=(
            "Use both query tools before answering. You are checking whether the persistent "
            "Runloop example resources already exist before any create step.\n\n"
            f"1. Call `query_runloop_secret` with `{managed_secret_name}`.\n"
            f"2. Call `query_runloop_network_policy` with `{network_policy_name}`.\n"
            "3. Final answer in 2 short sentences stating whether each resource already exists."
        ),
        tools=tools,
        model_settings=ModelSettings(tool_choice="required"),
    )
    prompt = (
        "Check whether the persistent Runloop secret and network policy for this example already "
        "exist before the script attempts any create or reuse step."
    )
    output = await _run_plain_agent(
        agent=query_agent,
        prompt=prompt,
        workflow_name="Runloop resource query example",
        stream=stream,
    )
    if "secret" not in query_results or "network_policy" not in query_results:
        raise RuntimeError("The query agent did not call both Runloop resource query tools.")
    return query_results, output


async def _bootstrap_persistent_resources(
    client: RunloopSandboxClient,
    *,
    managed_secret_name: str,
    managed_secret_value: str,
    network_policy_name: str,
    network_policy_id_override: str | None,
    query_results: dict[str, RunloopResourceQueryResult],
    axon_name: str | None,
) -> dict[str, object]:
    secret_query = query_results["secret"]
    policy_query = query_results["network_policy"]

    bootstrap: dict[str, object] = {
        "managed_secret_value": managed_secret_value,
        "secret": RunloopResourceBootstrapResult(
            resource_type="secret",
            name=managed_secret_name,
            action="reused" if secret_query.found else "created",
            id=secret_query.id,
            found_before_bootstrap=secret_query.found,
        ),
        "network_policy": RunloopResourceBootstrapResult(
            resource_type="network_policy",
            name=network_policy_name,
            action="override"
            if network_policy_id_override
            else ("reused" if policy_query.found else "created"),
            id=network_policy_id_override or policy_query.id,
            found_before_bootstrap=policy_query.found,
        ),
        "axon_id": None,
        "axon_name": axon_name,
    }

    secret_result = cast(RunloopResourceBootstrapResult, bootstrap["secret"])
    if not secret_query.found:
        created_secret = cast(
            Any,
            await client.platform.secrets.create(
                name=managed_secret_name, value=managed_secret_value
            ),
        )
        secret_result.id = cast(str | None, getattr(created_secret, "id", None))
    print(
        "persistent secret bootstrap:",
        secret_result.model_dump(mode="json"),
    )

    policy_result = cast(RunloopResourceBootstrapResult, bootstrap["network_policy"])
    if network_policy_id_override is None and not policy_query.found:
        try:
            created_policy = cast(
                Any,
                await client.platform.network_policies.create(
                    name=network_policy_name,
                    allow_all=True,
                    description="Persistent network policy for the Runloop capabilities example.",
                ),
            )
        except Exception as exc:
            if not _is_conflict(exc):
                raise
            policy_result.action = "reused"
            policy_result.found_before_bootstrap = True
            refreshed_policy = await _query_runloop_network_policy(client, name=network_policy_name)
            policy_result.id = refreshed_policy.id
        else:
            policy_result.id = cast(str | None, getattr(created_policy, "id", None))
    print(
        "persistent network policy bootstrap:",
        policy_result.model_dump(mode="json"),
    )

    if axon_name is not None:
        axon = cast(Any, await client.platform.axons.create(name=axon_name))
        await client.platform.axons.query_sql(
            cast(str, axon.id),
            sql="CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL)",
        )
        await client.platform.axons.batch_sql(
            cast(str, axon.id),
            statements=[
                {"sql": "INSERT INTO events (kind) VALUES (?)", "params": ["capabilities"]},
                {"sql": "INSERT INTO events (kind) VALUES (?)", "params": ["agent_guided"]},
            ],
        )
        query_result = cast(
            Any,
            await client.platform.axons.query_sql(
                cast(str, axon.id),
                sql="SELECT COUNT(*) AS total_events FROM events",
            ),
        )
        publish_result = cast(
            Any,
            await client.platform.axons.publish(
                cast(str, axon.id),
                event_type="capabilities_example",
                origin="AGENT_EVENT",
                payload=json.dumps({"axon_name": axon_name}),
                source="openai-agents-python",
            ),
        )
        bootstrap["axon_id"] = cast(str, axon.id)
        print(
            "axon demo created:",
            {
                "id": cast(str, axon.id),
                "name": axon_name,
                "rows": query_result.rows,
                "published": getattr(publish_result, "published", None),
            },
        )

    return bootstrap


def _optional_gateways(args: argparse.Namespace) -> dict[str, RunloopGatewaySpec]:
    if not (args.gateway_env_var and args.gateway_name and args.gateway_secret_name):
        return {}
    return {
        args.gateway_env_var: RunloopGatewaySpec(
            gateway=args.gateway_name,
            secret=args.gateway_secret_name,
        )
    }


def _optional_mcp(args: argparse.Namespace) -> dict[str, RunloopMcpSpec]:
    if not (args.mcp_env_var and args.mcp_config and args.mcp_secret_name):
        return {}
    return {
        args.mcp_env_var: RunloopMcpSpec(
            mcp_config=args.mcp_config,
            secret=args.mcp_secret_name,
        )
    }


async def main(args: argparse.Namespace) -> None:
    _require_env("OPENAI_API_KEY")
    _require_env("RUNLOOP_API_KEY")

    workspace_root = (
        DEFAULT_RUNLOOP_ROOT_WORKSPACE_ROOT if args.root else DEFAULT_RUNLOOP_WORKSPACE_ROOT
    )
    run_id = _run_id()
    metadata = {
        "example": "runloop-capabilities",
        "run_id": run_id,
    }

    client = RunloopSandboxClient()
    session = None
    resumed = None
    session_closed = False
    resumed_closed = False

    try:
        _phase("Public Resource Discovery")
        public_context = await _preflight_public_resources(client)

        _phase("Agent Resource Discovery")
        query_results, query_agent_output = await _run_resource_query_phase(
            client,
            model=args.model,
            stream=args.stream,
            managed_secret_name=PERSISTENT_SECRET_NAME,
            network_policy_name=PERSISTENT_NETWORK_POLICY_NAME,
        )
        print(
            "resource query results:",
            {key: value.model_dump(mode="json") for key, value in query_results.items()},
        )

        _phase("Persistent Resource Bootstrap")
        axon_name = f"{EXAMPLE_RESOURCE_SLUG}-axon-{run_id}" if args.with_axon_demo else None
        bootstrap = await _bootstrap_persistent_resources(
            client,
            managed_secret_name=PERSISTENT_SECRET_NAME,
            managed_secret_value=PERSISTENT_SECRET_VALUE,
            network_policy_name=PERSISTENT_NETWORK_POLICY_NAME,
            network_policy_id_override=args.network_policy_id,
            query_results=query_results,
            axon_name=axon_name,
        )
        secret_bootstrap = cast(RunloopResourceBootstrapResult, bootstrap["secret"])
        network_policy_bootstrap = cast(RunloopResourceBootstrapResult, bootstrap["network_policy"])
        network_policy_id = network_policy_bootstrap.id

        context = {
            "example_slug": EXAMPLE_RESOURCE_SLUG,
            "workspace_root": workspace_root,
            "requested_blueprint_name": args.blueprint_name,
            "public_resources": public_context,
            "resource_query_agent_output": query_agent_output,
            "resource_queries": {
                key: value.model_dump(mode="json") for key, value in query_results.items()
            },
            "resource_bootstrap": {
                "secret": secret_bootstrap.model_dump(mode="json"),
                "network_policy": network_policy_bootstrap.model_dump(mode="json"),
                "axon_id": bootstrap["axon_id"],
                "axon_name": bootstrap["axon_name"],
            },
            "managed_secret_env_var": PERSISTENT_SECRET_NAME,
            "network_policy_id": network_policy_id,
            "metadata": metadata,
            "gateway_bindings": sorted(_optional_gateways(args)),
            "mcp_bindings": sorted(_optional_mcp(args)),
        }

        manifest = _build_manifest(workspace_root=workspace_root, context=context)
        agent = _build_sandbox_agent(
            model=args.model,
            manifest=manifest,
            managed_secret_name=PERSISTENT_SECRET_NAME,
        )
        options = RunloopSandboxClientOptions(
            blueprint_name=args.blueprint_name,
            pause_on_exit=True,
            exposed_ports=(args.http_port,),
            user_parameters=(RunloopUserParameters(username="root", uid=0) if args.root else None),
            launch_parameters=RunloopLaunchParameters(
                network_policy_id=network_policy_id,
                resource_size_request=args.resource_size,
                after_idle=RunloopAfterIdle(idle_time_seconds=300, on_idle="suspend"),
                launch_commands=["echo runloop-capabilities-example"],
            ),
            tunnel=RunloopTunnelConfig(
                auth_mode="open",
                http_keep_alive=True,
                wake_on_http=True,
            ),
            gateways=_optional_gateways(args),
            mcp=_optional_mcp(args),
            metadata=metadata,
            managed_secrets={PERSISTENT_SECRET_NAME: PERSISTENT_SECRET_VALUE},
        )

        _phase("Sandbox Create")
        session = await client.create(manifest=manifest, options=options)
        await session.start()
        session_state = _runloop_state(session)
        print(
            "session started:",
            {
                "devbox_id": session_state.devbox_id,
                "secret_refs": session_state.secret_refs,
                "metadata": session_state.metadata,
            },
        )

        _phase("Tunnel Check")
        await _write_json(
            session,
            RUNTIME_CONTEXT_PATH,
            {
                **context,
                "devbox_id": session_state.devbox_id,
                "secret_refs": session_state.secret_refs,
                "runtime_phase": "before_tunnel_check",
            },
        )
        await _start_http_server(session, port=args.http_port, workspace_root=workspace_root)
        endpoint = await session.resolve_exposed_port(args.http_port)
        preview_url = urljoin(_build_endpoint_url(endpoint), "README.md")
        preview_body = await _poll_http_preview(
            preview_url,
            expected_substring="Runloop Capabilities Example",
            timeout_s=45.0,
        )
        print("resolved tunnel:", preview_url)
        await _write_json(
            session,
            RUNTIME_CONTEXT_PATH,
            {
                **context,
                "devbox_id": session_state.devbox_id,
                "secret_refs": session_state.secret_refs,
                "tunnel_url": preview_url,
                "http_preview_contains_readme": "Runloop Capabilities Example" in preview_body,
                "runtime_phase": "before_agent_run",
            },
        )

        _phase("Agent Verification")
        await _run_sandbox_agent(
            agent=agent,
            prompt=args.prompt,
            session=session,
            workflow_name="Runloop capabilities example",
            stream=args.stream,
        )
        proof_text = await _read_text(session, AGENT_PROOF_PATH)
        print("agent proof:")
        print(proof_text.rstrip())

        _phase("Suspend")
        await session.aclose()
        session_closed = True
        print("session persisted and suspended")

        _phase("Resume Check")
        resumed = await client.resume(session.state)
        await resumed.start()
        resumed_state = _runloop_state(resumed)
        resumed_runtime_context = await _read_text(resumed, RUNTIME_CONTEXT_PATH)
        resumed_proof_text = await _read_text(resumed, AGENT_PROOF_PATH)
        print("resumed runtime context bytes:", len(resumed_runtime_context.encode("utf-8")))
        print("resumed proof:")
        print(resumed_proof_text.rstrip())
        resumed_state.pause_on_exit = False
        await resumed.aclose()
        resumed_closed = True
        print("resumed session cleaned up with delete semantics")

        _phase("Persistent Resource Summary")
        print(
            "persistent resources retained:",
            {
                "secret": secret_bootstrap.model_dump(mode="json"),
                "network_policy": network_policy_bootstrap.model_dump(mode="json"),
            },
        )
        if bootstrap["axon_id"] is not None:
            print(
                "axon retained for manual cleanup:",
                {
                    "axon_id": bootstrap["axon_id"],
                    "axon_name": bootstrap["axon_name"],
                },
            )
    finally:
        if resumed is not None and not resumed_closed:
            try:
                _runloop_state(resumed).pause_on_exit = False
                await resumed.aclose()
            except Exception as exc:
                print(f"warning: failed to close resumed session cleanly: {exc}")
        elif session is not None and not session_closed:
            try:
                _runloop_state(session).pause_on_exit = False
                await session.aclose()
            except Exception as exc:
                print(f"warning: failed to close initial session cleanly: {exc}")
        elif session is not None and session_closed and resumed is None:
            try:
                cleanup_session = await client.resume(session.state)
                _runloop_state(cleanup_session).pause_on_exit = False
                await cleanup_session.aclose()
            except Exception as exc:
                print(f"warning: failed to resume suspended session for cleanup: {exc}")

        await client.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name to use.")
    parser.add_argument(
        "--prompt", default=DEFAULT_AGENT_PROMPT, help="Prompt to send to the agent."
    )
    parser.add_argument("--blueprint-name", default=None, help="Optional Runloop blueprint name.")
    parser.add_argument(
        "--resource-size",
        default="MEDIUM",
        choices=["X_SMALL", "SMALL", "MEDIUM", "LARGE", "X_LARGE", "XX_LARGE", "CUSTOM_SIZE"],
        help="Runloop resource size request for the devbox.",
    )
    parser.add_argument(
        "--network-policy-id",
        default=None,
        help="Optional Runloop network policy id override. Without this flag, the example reuses or creates the persistent example policy by name.",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port used by the preview HTTP server.",
    )
    parser.add_argument(
        "--root",
        action="store_true",
        default=False,
        help="Launch the Runloop devbox as root. The workspace root becomes /root.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        default=False,
        help="Stream the agent response and tool activity.",
    )
    parser.add_argument(
        "--with-axon-demo",
        action="store_true",
        default=False,
        help="Also create and use a temporary Axon. This leaves the Axon behind for manual cleanup.",
    )
    parser.add_argument(
        "--gateway-env-var", default=None, help="Env var name for a gateway binding."
    )
    parser.add_argument(
        "--gateway-name", default=None, help="Runloop gateway name for the binding."
    )
    parser.add_argument(
        "--gateway-secret-name",
        default=None,
        help="Runloop secret name used by the gateway binding.",
    )
    parser.add_argument("--mcp-env-var", default=None, help="Env var name for an MCP binding.")
    parser.add_argument(
        "--mcp-config", default=None, help="Runloop MCP config name for the binding."
    )
    parser.add_argument(
        "--mcp-secret-name",
        default=None,
        help="Runloop secret name used by the MCP binding.",
    )
    return parser


if __name__ == "__main__":
    asyncio.run(main(_build_parser().parse_args()))
