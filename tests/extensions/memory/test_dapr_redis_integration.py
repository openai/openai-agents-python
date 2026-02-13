"""
Integration tests for DaprSession with real Dapr sidecar and Redis using testcontainers.

These tests use Docker containers for both Redis and Dapr, with proper networking.
Tests are automatically skipped if dependencies (dapr, testcontainers, docker) are not available.

Run with: pytest tests/extensions/memory/test_dapr_redis_integration.py -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import urllib.request

import docker  # type: ignore[import-untyped]
import pytest
from docker.errors import DockerException  # type: ignore[import-untyped]

# Skip tests if dependencies are not available
pytest.importorskip("dapr")  # Skip tests if Dapr is not installed
pytest.importorskip("testcontainers")  # Skip if testcontainers is not installed
if shutil.which("docker") is None:
    pytest.skip(
        "Docker executable is not available; skipping Dapr integration tests",
        allow_module_level=True,
    )
try:
    client = docker.from_env()
    client.ping()
except DockerException:
    pytest.skip(
        "Docker daemon is not available; skipping Dapr integration tests", allow_module_level=True
    )
else:
    client.close()

from testcontainers.core.container import DockerContainer  # type: ignore[import-untyped]
from testcontainers.core.network import Network  # type: ignore[import-untyped]
from testcontainers.core.waiting_utils import wait_for_logs  # type: ignore[import-untyped]

from agents import Agent, Runner, TResponseInputItem
from agents.extensions.memory import (
    DAPR_CONSISTENCY_EVENTUAL,
    DAPR_CONSISTENCY_STRONG,
    DaprSession,
)
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message

# Mark all tests as async
pytestmark = pytest.mark.asyncio


def wait_for_dapr_health(host: str, port: int, timeout: int = 60) -> bool:
    """
    Wait for Dapr sidecar to become healthy by checking the HTTP health endpoint.

    Args:
        host: The host where Dapr is running
        port: The HTTP port (typically 3500)
        timeout: Maximum time to wait in seconds

    Returns:
        True if Dapr becomes healthy, False otherwise
    """
    health_url = f"http://{host}:{port}/v1.0/healthz/outbound"
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            with urllib.request.urlopen(health_url, timeout=5) as response:
                if 200 <= response.status < 300:
                    print(f"✓ Dapr health check passed on {health_url}")
                    return True
        except Exception:
            pass

        time.sleep(1)

    print(f"✗ Dapr health check timed out after {timeout}s on {health_url}")
    return False


@pytest.fixture(scope="module")
def docker_network():
    """Create a Docker network for container-to-container communication."""
    with Network() as network:
        yield network


@pytest.fixture(scope="module")
def redis_container(docker_network):
    """Start Redis container on the shared network."""
    container = (
        DockerContainer("redis:7-alpine")
        .with_network(docker_network)
        .with_network_aliases("redis")
        .with_exposed_ports(6379)
    )
    container.start()
    wait_for_logs(container, "Ready to accept connections", timeout=30)
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def dapr_container(redis_container, docker_network):
    """Start Dapr sidecar container with Redis state store configuration."""
    # Create temporary components directory
    temp_dir = tempfile.mkdtemp()
    components_path = os.path.join(temp_dir, "components")
    os.makedirs(components_path, exist_ok=True)

    # Write Redis state store component configuration
    # KEY: Use 'redis:6379' (network alias), NOT localhost!
    state_store_config = """
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.redis
  version: v1
  metadata:
  - name: redisHost
    value: redis:6379
  - name: redisPassword
    value: ""
  - name: actorStateStore
    value: "false"
"""
    with open(os.path.join(components_path, "statestore.yaml"), "w") as f:
        f.write(state_store_config)

    # Create Dapr container
    container = DockerContainer("daprio/daprd:latest")
    container = container.with_network(docker_network)  # Join the same network
    container = container.with_volume_mapping(components_path, "/components", mode="ro")
    container = container.with_command(
        [
            "./daprd",
            "-app-id",
            "test-app",
            "-dapr-http-port",
            "3500",  # HTTP API port for health checks
            "-dapr-grpc-port",
            "50001",
            "-components-path",
            "/components",
            "-log-level",
            "info",
        ]
    )
    container = container.with_exposed_ports(3500, 50001)  # Expose both ports

    container.start()

    # Get the exposed HTTP port and host
    http_host = container.get_container_host_ip()
    http_port = container.get_exposed_port(3500)

    # Wait for Dapr to become healthy
    if not wait_for_dapr_health(http_host, http_port, timeout=60):
        container.stop()
        pytest.fail("Dapr container failed to become healthy")

    # Set environment variables for Dapr SDK health checks
    # The Dapr SDK checks these when creating a client
    os.environ["DAPR_HTTP_PORT"] = str(http_port)
    os.environ["DAPR_RUNTIME_HOST"] = http_host

    yield container

    # Cleanup environment variables
    os.environ.pop("DAPR_HTTP_PORT", None)
    os.environ.pop("DAPR_RUNTIME_HOST", None)

    container.stop()

    # Cleanup
    import shutil

    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def agent() -> Agent:
    """Fixture for a basic agent with a fake model."""
    return Agent(name="test", model=FakeModel())


async def test_dapr_redis_integration(dapr_container, monkeypatch):
    """Test DaprSession with real Dapr sidecar and Redis backend."""
    # Get Dapr gRPC address (exposed to host)
    dapr_host = dapr_container.get_container_host_ip()
    dapr_port = dapr_container.get_exposed_port(50001)
    dapr_address = f"{dapr_host}:{dapr_port}"

    # Monkeypatch the Dapr health check since we already verified it in the fixture
    from dapr.clients.health import DaprHealth

    monkeypatch.setattr(DaprHealth, "wait_until_ready", lambda: None)

    # Create session using from_address
    session = DaprSession.from_address(
        session_id="integration_test_session",
        state_store_name="statestore",
        dapr_address=dapr_address,
    )

    try:
        # Test connectivity
        is_connected = await session.ping()
        assert is_connected is True

        # Clear any existing data
        await session.clear_session()

        # Test add_items
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "Hello from integration test"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        await session.add_items(items)

        # Test get_items
        retrieved = await session.get_items()
        assert len(retrieved) == 2
        assert retrieved[0].get("content") == "Hello from integration test"
        assert retrieved[1].get("content") == "Hi there!"

        # Test get_items with limit
        latest_1 = await session.get_items(limit=1)
        assert len(latest_1) == 1
        assert latest_1[0].get("content") == "Hi there!"

        # Test pop_item
        popped = await session.pop_item()
        assert popped is not None
        assert popped.get("content") == "Hi there!"

        remaining = await session.get_items()
        assert len(remaining) == 1
        assert remaining[0].get("content") == "Hello from integration test"

        # Test clear_session
        await session.clear_session()
        cleared = await session.get_items()
        assert len(cleared) == 0

    finally:
        await session.close()


async def test_dapr_runner_integration(agent: Agent, dapr_container, monkeypatch):
    """Test DaprSession with agent Runner using real Dapr sidecar."""
    from dapr.clients.health import DaprHealth

    monkeypatch.setattr(DaprHealth, "wait_until_ready", lambda: None)

    dapr_host = dapr_container.get_container_host_ip()
    dapr_port = dapr_container.get_exposed_port(50001)
    dapr_address = f"{dapr_host}:{dapr_port}"

    session = DaprSession.from_address(
        session_id="runner_integration_test",
        state_store_name="statestore",
        dapr_address=dapr_address,
    )

    try:
        await session.clear_session()

        # First turn
        assert isinstance(agent.model, FakeModel)
        agent.model.set_next_output([get_text_message("San Francisco")])
        result1 = await Runner.run(
            agent,
            "What city is the Golden Gate Bridge in?",
            session=session,
        )
        assert result1.final_output == "San Francisco"

        # Second turn - should remember context
        agent.model.set_next_output([get_text_message("California")])
        result2 = await Runner.run(agent, "What state is it in?", session=session)
        assert result2.final_output == "California"

        # Verify history
        last_input = agent.model.last_turn_args["input"]
        assert len(last_input) > 1
        assert any("Golden Gate Bridge" in str(item.get("content", "")) for item in last_input)

    finally:
        await session.close()


async def test_dapr_session_isolation(dapr_container, monkeypatch):
    """Test that different session IDs are isolated with real Dapr."""
    from dapr.clients.health import DaprHealth

    monkeypatch.setattr(DaprHealth, "wait_until_ready", lambda: None)

    dapr_host = dapr_container.get_container_host_ip()
    dapr_port = dapr_container.get_exposed_port(50001)
    dapr_address = f"{dapr_host}:{dapr_port}"

    session1 = DaprSession.from_address(
        session_id="isolated_session_1",
        state_store_name="statestore",
        dapr_address=dapr_address,
    )
    session2 = DaprSession.from_address(
        session_id="isolated_session_2",
        state_store_name="statestore",
        dapr_address=dapr_address,
    )

    try:
        # Clear both sessions
        await session1.clear_session()
        await session2.clear_session()

        # Add different data to each session
        await session1.add_items([{"role": "user", "content": "session 1 data"}])
        await session2.add_items([{"role": "user", "content": "session 2 data"}])

        # Verify isolation
        items1 = await session1.get_items()
        items2 = await session2.get_items()

        assert len(items1) == 1
        assert len(items2) == 1
        assert items1[0].get("content") == "session 1 data"
        assert items2[0].get("content") == "session 2 data"

    finally:
        await session1.clear_session()
        await session2.clear_session()
        await session1.close()
        await session2.close()


async def test_dapr_ttl_functionality(dapr_container, monkeypatch):
    """Test TTL functionality with real Dapr and Redis (if supported by state store)."""
    from dapr.clients.health import DaprHealth

    monkeypatch.setattr(DaprHealth, "wait_until_ready", lambda: None)

    dapr_host = dapr_container.get_container_host_ip()
    dapr_port = dapr_container.get_exposed_port(50001)
    dapr_address = f"{dapr_host}:{dapr_port}"

    # Create session with short TTL
    session = DaprSession.from_address(
        session_id="ttl_test_session",
        state_store_name="statestore",
        dapr_address=dapr_address,
        ttl=2,  # 2 seconds TTL
    )

    try:
        await session.clear_session()

        # Add items with TTL
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "This should expire soon"},
        ]
        await session.add_items(items)

        # Verify items exist immediately
        retrieved = await session.get_items()
        assert len(retrieved) == 1

        # Note: Actual expiration testing depends on state store TTL support
        # Redis state store supports TTL via ttlInSeconds metadata

    finally:
        await session.clear_session()
        await session.close()


async def test_dapr_consistency_levels(dapr_container, monkeypatch):
    """Test different consistency levels with real Dapr."""
    from dapr.clients.health import DaprHealth

    monkeypatch.setattr(DaprHealth, "wait_until_ready", lambda: None)

    dapr_host = dapr_container.get_container_host_ip()
    dapr_port = dapr_container.get_exposed_port(50001)
    dapr_address = f"{dapr_host}:{dapr_port}"

    # Test eventual consistency
    session_eventual = DaprSession.from_address(
        session_id="eventual_consistency_test",
        state_store_name="statestore",
        dapr_address=dapr_address,
        consistency=DAPR_CONSISTENCY_EVENTUAL,
    )

    # Test strong consistency
    session_strong = DaprSession.from_address(
        session_id="strong_consistency_test",
        state_store_name="statestore",
        dapr_address=dapr_address,
        consistency=DAPR_CONSISTENCY_STRONG,
    )

    try:
        await session_eventual.clear_session()
        await session_strong.clear_session()

        # Both should work correctly
        items: list[TResponseInputItem] = [{"role": "user", "content": "Consistency test"}]

        await session_eventual.add_items(items)
        retrieved_eventual = await session_eventual.get_items()
        assert len(retrieved_eventual) == 1

        await session_strong.add_items(items)
        retrieved_strong = await session_strong.get_items()
        assert len(retrieved_strong) == 1

    finally:
        await session_eventual.clear_session()
        await session_strong.clear_session()
        await session_eventual.close()
        await session_strong.close()


async def test_dapr_unicode_and_special_chars(dapr_container, monkeypatch):
    """Test unicode and special characters with real Dapr and Redis."""
    from dapr.clients.health import DaprHealth

    monkeypatch.setattr(DaprHealth, "wait_until_ready", lambda: None)

    dapr_host = dapr_container.get_container_host_ip()
    dapr_port = dapr_container.get_exposed_port(50001)
    dapr_address = f"{dapr_host}:{dapr_port}"

    session = DaprSession.from_address(
        session_id="unicode_test_session",
        state_store_name="statestore",
        dapr_address=dapr_address,
    )

    try:
        await session.clear_session()

        # Test unicode content
        items: list[TResponseInputItem] = [
            {"role": "user", "content": "こんにちは"},
            {"role": "assistant", "content": "😊👍"},
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": '{"nested": "json"}'},
            {"role": "user", "content": "Line1\nLine2\tTabbed"},
        ]
        await session.add_items(items)

        # Retrieve and verify
        retrieved = await session.get_items()
        assert len(retrieved) == 5
        assert retrieved[0].get("content") == "こんにちは"
        assert retrieved[1].get("content") == "😊👍"
        assert retrieved[2].get("content") == "Привет"
        assert retrieved[3].get("content") == '{"nested": "json"}'
        assert retrieved[4].get("content") == "Line1\nLine2\tTabbed"

    finally:
        await session.clear_session()
        await session.close()


async def test_dapr_concurrent_writes_resolution(dapr_container, monkeypatch):
    """
    Concurrent writes from multiple session instances should resolve via
    optimistic concurrency.
    """
    from dapr.clients.health import DaprHealth

    monkeypatch.setattr(DaprHealth, "wait_until_ready", lambda: None)

    dapr_host = dapr_container.get_container_host_ip()
    dapr_port = dapr_container.get_exposed_port(50001)
    dapr_address = f"{dapr_host}:{dapr_port}"

    # Use two different session objects pointing to the same logical session_id
    # to create real contention.
    session_id = "concurrent_integration_session"
    s1 = DaprSession.from_address(
        session_id=session_id,
        state_store_name="statestore",
        dapr_address=dapr_address,
    )
    s2 = DaprSession.from_address(
        session_id=session_id,
        state_store_name="statestore",
        dapr_address=dapr_address,
    )

    try:
        # Clean slate.
        await s1.clear_session()

        # Fire multiple parallel add_items calls from two different session instances.
        tasks: list[asyncio.Task[None]] = []
        for i in range(10):
            tasks.append(
                asyncio.create_task(
                    s1.add_items(
                        [
                            {"role": "user", "content": f"A-{i}"},
                        ]
                    )
                )
            )
            tasks.append(
                asyncio.create_task(
                    s2.add_items(
                        [
                            {"role": "assistant", "content": f"B-{i}"},
                        ]
                    )
                )
            )

        await asyncio.gather(*tasks)

        # Validate all messages were persisted.
        # Use a fresh session object for readback to avoid any local caching
        # (none expected, but explicit).
        s_read = DaprSession.from_address(
            session_id=session_id,
            state_store_name="statestore",
            dapr_address=dapr_address,
        )
        try:
            items = await s_read.get_items()
            contents = [item.get("content") for item in items]
            # We expect 20 total messages: A-0..9 and B-0..9 (order unspecified).
            assert len(contents) == 20
            for i in range(10):
                assert f"A-{i}" in contents
                assert f"B-{i}" in contents
        finally:
            await s_read.close()
    finally:
        await s1.close()
        await s2.close()
