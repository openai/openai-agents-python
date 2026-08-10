from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from agents import RunConfig, Runner
from agents.items import ToolCallOutputItem, TResponseOutputItem
from agents.sandbox import Manifest, SandboxAgent, SandboxPathGrant, SandboxRunConfig
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import InContainerMountStrategy, S3Mount
from agents.sandbox.entries.mounts.patterns import RcloneMountPattern
from agents.sandbox.errors import ErrorCode, MountConfigError
from agents.sandbox.session import (
    CallbackSink,
    Instrumentation,
    SandboxSession,
    SandboxSessionEvent,
)
from agents.sandbox.snapshot import NoopSnapshotSpec
from integration_tests._contract_support import _redaction_observables
from integration_tests._fake_model import QueuedFakeModel
from integration_tests.conftest import skip_or_fail

pytestmark = pytest.mark.security

_INSPECTION_SCRIPT = r"""
set -eu
printf '%s\n' '--- environment ---'
env
printf '%s\n' '--- filesystem ---'
mkdir -p /workspace /run/agents-security-probe
dd if=/dev/zero bs=4096 count=2 2>/dev/null \
    | tr '\000' x > /workspace/.agents-inspection-tail-probe
printf '%s\n' 'FILESYSTEM_TAIL_PROBE_AFTER_4096' \
    >> /workspace/.agents-inspection-tail-probe
printf '%s\n' 'FILESYSTEM_OUTSIDE_WORKSPACE_PROBE' \
    > /run/agents-security-probe/readable-file

inspection_list=/.agents-readable-files
: > "$inspection_list"
for root in /workspace /tmp /run /var /etc /root /home /app /opt; do
    if [ -d "$root" ]; then
        find "$root" -type f -print0 2>/dev/null >> "$inspection_list" || true
    fi
done

file_count=0
total_bytes=0
while IFS= read -r -d '' path; do
    if [ -r "$path" ]; then
        size=$(wc -c < "$path") || exit 70
        file_count=$((file_count + 1))
        total_bytes=$((total_bytes + size))
        if [ "$file_count" -gt 1024 ] || [ "$total_bytes" -gt 16777216 ]; then
            printf '%s\n' 'filesystem inspection bound exceeded'
            exit 70
        fi
    fi
done < "$inspection_list"

while IFS= read -r -d '' path; do
    if [ -r "$path" ]; then
        printf '%s\n' "$path"
        cat "$path" || exit 70
        printf '\n'
    fi
done < "$inspection_list"
rm -f "$inspection_list"
printf '%s\n' '--- filesystem complete ---'
printf '%s\n' '--- processes ---'
for process in /proc/[0-9]*; do
    if [ -r "$process/cmdline" ]; then
        tr '\000' '\n' < "$process/cmdline" || true
    fi
    if [ -r "$process/environ" ]; then
        tr '\000' '\n' < "$process/environ" || true
    fi
done
"""

_TAIL_PROBE_EVIDENCE = (
    "/workspace/.agents-inspection-tail-probe\n" + "x" * 8192 + "FILESYSTEM_TAIL_PROBE_AFTER_4096\n"
)
_OUTSIDE_WORKSPACE_PROBE_EVIDENCE = (
    "/run/agents-security-probe/readable-file\nFILESYSTEM_OUTSIDE_WORKSPACE_PROBE\n"
)
_NESTED_MOUNT_PROBE_EVIDENCE = (
    "/run/agents-security-mounted-probe/readable-file\nFILESYSTEM_NESTED_MOUNT_PROBE\n"
)


def _assert_complete_filesystem_inspection(output: str) -> None:
    assert "Process exited with code 0" in output
    assert "--- environment ---" in output
    assert "--- filesystem ---" in output
    assert _TAIL_PROBE_EVIDENCE in output
    assert _OUTSIDE_WORKSPACE_PROBE_EVIDENCE in output
    assert _NESTED_MOUNT_PROBE_EVIDENCE in output
    assert "--- filesystem complete ---\n--- processes ---" in output


@pytest.mark.parametrize("fail_after_inspection", [False, True], ids=["success", "model-failure"])
async def test_runner_owned_local_sandbox_cannot_inspect_trusted_client_credential(
    caplog: pytest.LogCaptureFixture,
    fail_after_inspection: bool,
    tmp_path: Path,
) -> None:
    import docker  # type: ignore[import-untyped]

    from agents.sandbox.sandboxes import DockerSandboxClient, DockerSandboxClientOptions

    sentinel = "LOCAL_SANDBOX_CREDENTIAL_SENTINEL_42"
    image = os.environ.get("OPENAI_AGENTS_INTEGRATION_SECURITY_IMAGE", "busybox:1.36.1")
    events: list[SandboxSessionEvent] = []
    created_container_ids: list[str] = []
    deleted_container_ids: list[str] = []
    try:
        docker_client = docker.from_env()
    except docker.errors.DockerException:
        skip_or_fail("Local sandbox security requires a reachable Docker daemon.")
        return
    try:
        try:
            docker_client.ping()
            try:
                docker_client.images.get(image)
            except docker.errors.ImageNotFound:
                docker_client.images.pull(image)
        except docker.errors.DockerException:
            skip_or_fail("Local sandbox security requires the configured Docker image.")

        class _CredentialOwningDockerClient(DockerSandboxClient):
            def __init__(self, *, trusted_credential: str) -> None:
                super().__init__(
                    docker_client=docker_client,
                    instrumentation=Instrumentation(
                        sinks=[CallbackSink(lambda event, _session: events.append(event))]
                    ),
                )
                self.trusted_credential = trusted_credential

            async def _create_container(
                self,
                image: str,
                *,
                manifest: Manifest | None = None,
                exposed_ports: tuple[int, ...] = (),
                session_id: uuid.UUID | None = None,
            ) -> Any:
                container = await super()._create_container(
                    image,
                    manifest=manifest,
                    exposed_ports=exposed_ports,
                    session_id=session_id,
                )
                container_id = container.id
                assert container_id is not None
                created_container_ids.append(container_id)
                return container

            async def delete(self, session: SandboxSession) -> SandboxSession:
                deleted_container_ids.append(cast(Any, session.state).container_id)
                return await super().delete(session)

        tool_call = ResponseFunctionToolCall(
            type="function_call",
            name="exec_command",
            call_id="inspect-sandbox-boundary",
            status="completed",
            arguments=json.dumps(
                {
                    "cmd": _INSPECTION_SCRIPT,
                    "shell": "sh",
                    "login": False,
                    "yield_time_ms": 10_000,
                }
            ),
        )
        final_message = ResponseOutputMessage(
            id="security-complete",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(
                    type="output_text",
                    text="inspection complete",
                    annotations=[],
                    logprobs=[],
                )
            ],
        )
        turns: list[list[TResponseOutputItem]] = [[tool_call]]
        if not fail_after_inspection:
            turns.append([final_message])
        model = QueuedFakeModel(turns)
        client = _CredentialOwningDockerClient(trusted_credential=sentinel)
        nested_mount_source = tmp_path / "nested-mount-probe"
        nested_mount_source.mkdir()
        (nested_mount_source / "readable-file").write_text(
            "FILESYSTEM_NESTED_MOUNT_PROBE\n", encoding="utf-8"
        )
        manifest = Manifest(
            root="/workspace",
            extra_path_grants=(
                SandboxPathGrant(
                    path="/run/agents-security-mounted-probe",
                    host_path=str(nested_mount_source),
                    read_only=True,
                ),
            ),
        )
        agent = SandboxAgent(
            name="security-inspector",
            model=model,
            default_manifest=manifest,
            capabilities=[Shell()],
        )
        assert client.trusted_credential == sentinel
        assert sentinel not in manifest.model_dump_json()
        result = None
        run_error: BaseException | None = None
        with caplog.at_level(logging.DEBUG):
            if fail_after_inspection:
                with pytest.raises(
                    AssertionError,
                    match="QueuedFakeModel received an unexpected model request",
                ) as exc_info:
                    await Runner.run(
                        agent,
                        "Inspect every model-visible credential surface.",
                        run_config=RunConfig(
                            sandbox=SandboxRunConfig(
                                client=client,
                                options=DockerSandboxClientOptions(image=image),
                                snapshot=NoopSnapshotSpec(),
                            )
                        ),
                    )
                run_error = exc_info.value
            else:
                result = await Runner.run(
                    agent,
                    "Inspect every model-visible credential surface.",
                    run_config=RunConfig(
                        sandbox=SandboxRunConfig(
                            client=client,
                            options=DockerSandboxClientOptions(image=image),
                            snapshot=NoopSnapshotSpec(),
                        )
                    ),
                )

        assert len(created_container_ids) == 1
        assert deleted_container_ids == created_container_ids
        for container_id in created_container_ids:
            with pytest.raises(docker.errors.NotFound):
                docker_client.containers.get(container_id)
    finally:
        for container_id in created_container_ids:
            try:
                docker_client.containers.get(container_id).remove(force=True)
            except docker.errors.NotFound:
                pass
            except docker.errors.DockerException:
                pass
        docker_client.close()

    expected_model_request_fields = {
        "system_instructions",
        "input",
        "model_settings",
        "tools",
        "output_schema",
        "handoffs",
        "tracing",
        "previous_response_id",
        "conversation_id",
        "prompt",
    }
    assert model.requests
    assert all(set(request) == expected_model_request_fields for request in model.requests)
    model_requests = repr(model.requests)
    model_visible_tool_outputs: list[object] = []
    for request in model.requests:
        model_input = request["input"]
        if not isinstance(model_input, list):
            continue
        model_visible_tool_outputs.extend(
            item.get("output")
            for item in model_input
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
    assert model_visible_tool_outputs
    model_visible_tool_output = "\n".join(str(output) for output in model_visible_tool_outputs)
    _assert_complete_filesystem_inspection(model_visible_tool_output)
    assert not any(
        record.getMessage() == "Failed to clean up sandbox resources after run"
        for record in caplog.records
    )
    tool_outputs: list[Any] = []
    serialized_state: dict[str, Any] = {}
    if result is not None:
        tool_outputs = [
            item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)
        ]
        assert len(tool_outputs) == 1
        _assert_complete_filesystem_inspection(str(tool_outputs[0]))
        serialized_state = result.to_state().to_json()
    observables = "\n".join(
        (
            str(result.final_output if result is not None else None),
            model_requests,
            model_visible_tool_output,
            repr(tool_outputs),
            json.dumps(serialized_state, sort_keys=True),
            *(event.model_dump_json() for event in events),
            _redaction_observables(run_error, caplog.records),
        )
    )
    assert sentinel not in observables


async def test_credential_bearing_in_container_mount_is_rejected_before_side_effects(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from agents.sandbox.sandboxes import DockerSandboxClient, DockerSandboxClientOptions

    class _SideEffectTrackingDockerClient(DockerSandboxClient):
        create_container_called = False

        async def _create_container(self, *args: object, **kwargs: object) -> Any:
            _ = (args, kwargs)
            self.create_container_called = True
            raise AssertionError(
                "container creation must not run for an unsafe credential boundary"
            )

    sentinels = (
        "LOCAL_ACCESS_SENTINEL_42",
        "LOCAL_SECRET_SENTINEL_42",
        "LOCAL_TOKEN_SENTINEL_42",
    )
    client = _SideEffectTrackingDockerClient(docker_client=cast(Any, object()))
    manifest = Manifest(
        entries={
            "remote": S3Mount(
                bucket="compat-bucket",
                access_key_id=sentinels[0],
                secret_access_key=sentinels[1],
                session_token=sentinels[2],
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(
            MountConfigError,
            match="mount-scoped credentials cannot be exposed to a helper",
        ) as exc_info:
            await client.create(
                manifest=manifest,
                options=DockerSandboxClientOptions(image="unused"),
            )

    assert client.create_container_called is False
    error = exc_info.value
    assert error.error_code is ErrorCode.MOUNT_CONFIG_INVALID
    assert error.op == "materialize"
    assert error.retryable is False
    assert error.context == {}
    observables = _redaction_observables(error, caplog.records)
    assert all(sentinel not in observables for sentinel in sentinels)
