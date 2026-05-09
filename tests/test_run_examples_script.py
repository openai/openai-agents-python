from __future__ import annotations

from pathlib import Path

import examples.run_examples as run_examples


def test_default_auto_skip_excludes_prerequisite_bound_examples() -> None:
    expected = {
        "examples/sandbox/docker/mounts/azure_mount_read_write.py",
        "examples/sandbox/docker/mounts/gcs_mount_read_write.py",
        "examples/sandbox/docker/mounts/s3_files_mount_read_write.py",
        "examples/sandbox/docker/mounts/s3_mount_read_write.py",
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


def test_prepare_redis_for_example_uses_existing_local_redis(monkeypatch) -> None:
    env: dict[str, str] = {}
    monkeypatch.setattr(run_examples, "redis_ping_url", lambda url, timeout=0.5: True)

    redis_server, messages = run_examples.prepare_redis_for_example(
        run_examples.REDIS_SESSION_EXAMPLE,
        env,
    )

    assert redis_server is None
    assert env["REDIS_URL"] == run_examples.DEFAULT_REDIS_URL
    assert messages == [f"Using existing Redis server at {run_examples.DEFAULT_REDIS_URL}."]


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
    assert messages == [
        "REDIS_URL is set but not reachable before example start: redis://localhost:6380/2."
    ]


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
