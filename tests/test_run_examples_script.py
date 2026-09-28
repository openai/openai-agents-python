from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import examples.run_examples as run_examples


@pytest.mark.parametrize("auto_source", ["argument", "environment", "manual"])
def test_local_temporal_runner_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    auto_source: str,
) -> None:
    monkeypatch.delenv("EXAMPLES_AUTO_SKIP", raising=False)
    monkeypatch.setenv(
        "EXAMPLES_INTERACTIVE_MODE", "auto" if auto_source == "environment" else "manual"
    )
    monkeypatch.setattr(run_examples, "build_command_path", lambda: "")
    spawn = Mock(side_effect=AssertionError("The runner must not start this example"))
    monkeypatch.setattr(run_examples.subprocess, "Popen", spawn)
    args = [
        "run_examples.py",
        "--filter",
        "local_hello_workflow",
        "--logs-dir",
        str(tmp_path / "logs"),
        "--main-log",
        str(tmp_path / "main.log"),
        "--artifacts-dir",
        str(tmp_path / "artifacts"),
    ]
    if auto_source == "argument":
        args.append("--auto-mode")
    elif auto_source == "manual":
        args.append("--dry-run")
    monkeypatch.setattr(sys, "argv", args)

    assert run_examples.main() == 0

    output = capsys.readouterr().out
    relpath = "examples/sandbox/extensions/temporal/local_hello_workflow.py"
    assert f"- {'RUN ' if auto_source == 'manual' else 'SKIP'} {relpath}" in output
    if auto_source != "manual":
        assert "(skipped: auto-skip)" in output
    spawn.assert_not_called()


@pytest.mark.parametrize("mode", ["auto", "AUTO", "manual"])
@pytest.mark.skipif(sys.platform == "win32", reason="The example requires the Unix-only backend")
@pytest.mark.asyncio
async def test_local_temporal_entrypoint_refuses_auto_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    pytest.importorskip("temporalio")
    from examples.sandbox.extensions.temporal import local_hello_workflow

    monkeypatch.setenv("EXAMPLES_INTERACTIVE_MODE", mode)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    # A custom skip list can select the example, but cannot authorize auto-mode execution.
    monkeypatch.setenv("EXAMPLES_AUTO_SKIP", "examples/basic/hello_world.py")
    start_server = AsyncMock(side_effect=RuntimeError("test server startup boundary"))
    monkeypatch.setattr(
        local_hello_workflow.WorkflowEnvironment, "start_time_skipping", start_server
    )

    if mode.lower() == "auto":
        with pytest.raises(SystemExit, match="cannot run in auto mode"):
            await local_hello_workflow.main()
        start_server.assert_not_awaited()
    else:
        with pytest.raises(RuntimeError, match="test server startup boundary"):
            await local_hello_workflow.main()
        start_server.assert_awaited_once()


def test_default_auto_skip_excludes_prerequisite_bound_examples() -> None:
    expected = {
        "examples/sandbox/docker/mounts/azure_mount_read_write.py",
        "examples/sandbox/docker/mounts/gcs_mount_read_write.py",
        "examples/sandbox/docker/mounts/s3_mount_read_write.py",
        "examples/sandbox/extensions/blaxel_runner.py",
        "examples/sandbox/extensions/cloudflare_runner.py",
        "examples/sandbox/extensions/daytona/usaspending_text2sql/setup_db.py",
        "examples/sandbox/extensions/temporal/temporal_sandbox_agent.py",
        "examples/sandbox/extensions/vercel_runner.py",
        "examples/sandbox/memory_s3.py",
        "examples/sandbox/misc/reference_policy_mcp_server.py",
        "examples/sandbox/sandbox_agent_with_remote_snapshot.py",
        "examples/sandbox/tax_prep.py",
        "examples/sandbox/tutorials/dataroom_metric_extract/evals.py",
        "examples/sandbox/tutorials/dataroom_metric_extract/main.py",
        "examples/sandbox/tutorials/dataroom_qa/main.py",
        "examples/sandbox/tutorials/repo_code_review/evals.py",
        "examples/sandbox/tutorials/repo_code_review/main.py",
        "examples/sandbox/tutorials/vision_website_clone/main.py",
        "examples/tools/codex_same_thread.py",
    }

    assert expected <= run_examples.DEFAULT_AUTO_SKIP


def test_default_auto_skip_keeps_computer_use_example_enabled() -> None:
    assert "examples/tools/computer_use.py" not in run_examples.DEFAULT_AUTO_SKIP


def test_default_auto_skip_keeps_one_turn_auto_examples_enabled() -> None:
    assert "examples/agent_patterns/routing.py" not in run_examples.DEFAULT_AUTO_SKIP
    assert "examples/customer_service/main.py" not in run_examples.DEFAULT_AUTO_SKIP


def test_example_command_runs_python_unbuffered(monkeypatch) -> None:
    monkeypatch.delenv("EXAMPLES_UV_EXTRAS", raising=False)
    example = run_examples.ExampleScript(
        run_examples.ROOT_DIR / Path("examples/basic/hello_world.py")
    )

    assert example.command == ["uv", "run", "python", "-u", "-m", "examples.basic.hello_world"]


def test_example_command_includes_configured_uv_extras(monkeypatch) -> None:
    monkeypatch.setenv("EXAMPLES_UV_EXTRAS", "litellm any-llm")
    example = run_examples.ExampleScript(
        run_examples.ROOT_DIR / Path("examples/basic/hello_world.py")
    )

    assert example.command == [
        "uv",
        "run",
        "--extra",
        "litellm",
        "--extra",
        "any-llm",
        "python",
        "-u",
        "-m",
        "examples.basic.hello_world",
    ]


def test_artifact_dir_for_example_uses_tmp_safe_stem(tmp_path: Path) -> None:
    artifact_dir = run_examples.artifact_dir_for_example(
        "examples/sandbox/tutorials/vision_website_clone/main.py",
        tmp_path,
    )

    assert artifact_dir == tmp_path / "examples__sandbox__tutorials__vision_website_clone__main"


@pytest.mark.parametrize(
    "removed_arguments",
    [
        ["--rerun-file", ".tmp/failed.txt"],
        ["--write-rerun"],
        ["--collect", ".tmp/main.log"],
        ["--output", ".tmp/failed.txt"],
    ],
)
def test_removed_rerun_arguments_are_rejected(
    monkeypatch: pytest.MonkeyPatch, removed_arguments: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["run_examples.py", *removed_arguments])

    with pytest.raises(SystemExit, match="2"):
        run_examples.parse_args()


def test_prepare_redis_for_example_uses_existing_local_redis(monkeypatch) -> None:
    env: dict[str, str] = {}
    monkeypatch.setattr(run_examples, "redis_ping_url", lambda url, timeout=0.5: True)

    redis_server, messages = run_examples.prepare_redis_for_example(
        run_examples.REDIS_SESSION_EXAMPLE,
        env,
    )

    assert redis_server is None
    assert env["REDIS_URL"] == run_examples.DEFAULT_REDIS_URL
    assert messages == ["Using existing local Redis server."]


def test_prepare_redis_for_example_starts_managed_redis(monkeypatch) -> None:
    class DummyRedisServer:
        url = "redis://127.0.0.1:12345/0"

        def close(self) -> None:
            pass

    dummy_server = DummyRedisServer()
    env: dict[str, str] = {}
    monkeypatch.setattr(run_examples, "redis_ping_url", lambda url, timeout=0.5: False)
    monkeypatch.setattr(run_examples, "start_temporary_redis_server", lambda: dummy_server)

    redis_server, messages = run_examples.prepare_redis_for_example(
        run_examples.REDIS_SESSION_EXAMPLE,
        env,
    )

    assert redis_server is not None
    assert redis_server.url == dummy_server.url
    assert env["REDIS_URL"] == dummy_server.url
    assert messages == [f"Started temporary Redis server at {dummy_server.url}."]


def test_prepare_redis_for_example_respects_configured_url(monkeypatch) -> None:
    env = {"REDIS_URL": "redis://localhost:6380/2"}
    monkeypatch.setattr(run_examples, "redis_ping_url", lambda url, timeout=0.5: False)
    monkeypatch.setattr(
        run_examples,
        "start_temporary_redis_server",
        lambda: (_ for _ in ()).throw(AssertionError("should not start Redis")),
    )

    redis_server, messages = run_examples.prepare_redis_for_example(
        run_examples.REDIS_SESSION_EXAMPLE,
        env,
    )

    assert redis_server is None
    assert env["REDIS_URL"] == "redis://localhost:6380/2"
    assert messages == ["Using configured REDIS_URL; local preflight did not confirm availability."]


def test_prerequisite_skip_reasons_skip_dapr_without_sidecar(monkeypatch) -> None:
    monkeypatch.setattr(run_examples, "dapr_sidecar_available", lambda env: False)

    reasons = run_examples.prerequisite_skip_reasons(
        run_examples.DAPR_SESSION_EXAMPLE,
        auto_mode=True,
        env={},
    )

    assert reasons == {"missing-dapr-sidecar"}


def test_prerequisite_skip_reasons_allow_forced_dapr(monkeypatch) -> None:
    monkeypatch.setattr(
        run_examples,
        "dapr_sidecar_available",
        lambda env: (_ for _ in ()).throw(AssertionError("should not probe sidecar")),
    )

    reasons = run_examples.prerequisite_skip_reasons(
        run_examples.DAPR_SESSION_EXAMPLE,
        auto_mode=True,
        env={"EXAMPLES_FORCE_DAPR": "1"},
    )

    assert reasons == set()


def test_prerequisite_skip_reasons_allow_non_dapr_example(monkeypatch) -> None:
    monkeypatch.setattr(
        run_examples,
        "dapr_sidecar_available",
        lambda env: (_ for _ in ()).throw(AssertionError("should not probe sidecar")),
    )

    reasons = run_examples.prerequisite_skip_reasons(
        run_examples.REDIS_SESSION_EXAMPLE,
        auto_mode=True,
        env={},
    )

    assert reasons == set()


@pytest.mark.parametrize("buffered", [True, False])
@pytest.mark.parametrize(
    "url,reachable",
    [
        ("redis://synthetic-user:synthetic-pass@localhost/0?password=query-secret", True),
        ("rediss://synthetic-user:synthetic-pass@remote.example/0?password=query-secret", False),
        ("redis://localhost:query-secret/0", False),
    ],
)
def test_redis_runner_output_and_logs_omit_configured_connection_details(
    monkeypatch, tmp_path, capsys, url, reachable, buffered
):
    monkeypatch.setenv("REDIS_URL", url)
    monkeypatch.setenv("EXAMPLES_BUFFER_OUTPUT", "1")
    monkeypatch.setattr(run_examples, "build_command_path", lambda: "")
    monkeypatch.setattr(run_examples, "redis_ping_url", lambda url: reachable)
    monkeypatch.setattr(
        run_examples,
        "start_temporary_redis_server",
        lambda: pytest.fail("Configured Redis must not be replaced"),
    )
    # Run the actual example entry point in a child. Fail construction after verifying
    # that the complete configured URL survived the runner's environment forwarding.
    child = """
import os
import runpy
from unittest.mock import patch
from agents.extensions.memory import RedisSession

def fail_construction(*args, **kwargs):
    assert kwargs['url'] == os.environ['REDIS_URL']
    raise ValueError('Connection failed: ' + kwargs['url'])

with patch.object(RedisSession, 'from_url', side_effect=fail_construction):
    runpy.run_module('examples.memory.redis_session_example', run_name='__main__')
"""
    monkeypatch.setattr(
        run_examples.ExampleScript,
        "command",
        property(lambda self: [sys.executable, "-c", child]),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_examples.py",
            "--include-external",
            "--logs-dir",
            str(tmp_path / "logs"),
            "--main-log",
            str(tmp_path / "main.log"),
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
            *([] if buffered else ["--no-buffer-output"]),
        ],
    )
    script = run_examples.ExampleScript(run_examples.ROOT_DIR / run_examples.REDIS_SESSION_EXAMPLE)
    assert run_examples.run_examples([script], run_examples.parse_args()) == 1

    captured = capsys.readouterr()
    logs = "".join(path.read_text() for path in tmp_path.rglob("*.log"))
    main_log = (tmp_path / "main.log").read_text()
    assert "FAILED examples/memory/redis_session_example.py exit=1" in main_log
    assert "PASSED" not in main_log
    for output in (captured.out + captured.err, logs):
        assert "[runner]" in output
        assert "Check the Redis configuration and connection." in output
        assert url not in output
        assert "synthetic-user" not in output
        assert "synthetic-pass" not in output
        assert "query-secret" not in output
        assert "Traceback" not in output
