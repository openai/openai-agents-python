"""MongoDB adapter checks use a real local server when mongod is available."""

import json
import shutil
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pymongo import MongoClient
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.errors import PyMongoError

from agents import Agent, CustomStorage, MongoDBSessionStore, NativeProvider
from tests.native_fixtures import assistant_output, responses as responses

# Run the local database tests after the Windows pytest workers exit.
pytestmark = pytest.mark.serial if sys.platform == "win32" else []


def test_mongodb_adapter_accepts_sync_client_without_connecting_and_rejects_async() -> None:
    client = MongoClient("mongodb://127.0.0.1:1", connect=False)
    asynchronous = AsyncMongoClient("mongodb://127.0.0.1:1", connect=False)
    try:
        store = MongoDBSessionStore(client, database="synthetic")
        assert CustomStorage(store).backend is store
        assert store.collection.write_concern.document == {"j": True, "w": "majority"}
        assert store.collection.read_concern.document == {"level": "majority"}
        with pytest.raises(TypeError, match="synchronous"):
            MongoDBSessionStore(asynchronous, database="synthetic")
        store.close()
        with pytest.raises(RuntimeError, match="closed"):
            store.acquire("thread")
    finally:
        client.close()


@pytest.fixture
def mongodb_client(tmp_path: Path, unused_tcp_port):
    executable = shutil.which("mongod")
    if executable is None:
        pytest.skip("mongod is required for real MongoDB adapter checks")
    directory = tmp_path / "mongodb"
    directory.mkdir(mode=0o700)
    log_path = tmp_path / "mongod.log"
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            [
                executable,
                "--bind_ip",
                "127.0.0.1",
                "--port",
                str(unused_tcp_port),
                "--dbpath",
                str(directory),
                "--quiet",
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    uri = f"mongodb://127.0.0.1:{unused_tcp_port}"
    try:
        with MongoClient(
            uri,
            serverSelectionTimeoutMS=100,
            socketTimeoutMS=1000,
            connectTimeoutMS=1000,
        ) as probe:
            started = time.monotonic()
            startup_timeout = 20
            while process.poll() is None and time.monotonic() - started < startup_timeout:
                try:
                    probe.admin.command("ping")
                    break
                except PyMongoError:
                    time.sleep(0.05)
            else:
                exit_code = process.poll()
                if exit_code is None:
                    reason = f"Local MongoDB not ready after {startup_timeout}s (still running)"
                else:
                    reason = f"Local MongoDB exited with code {exit_code} before readiness"
                with log_path.open("rb") as output:
                    output.seek(max(0, log_path.stat().st_size - 4096))
                    tail = output.read(4096).decode("utf-8", errors="replace").strip()
                pytest.fail(f"{reason}\nMongoDB log tail:\n{tail or '(empty)'}", pytrace=False)
            elapsed = time.monotonic() - started
            if elapsed > 5:
                # Identify runs that would have hit the original five-second limit.
                warnings.warn(f"Local MongoDB became ready after {elapsed:.1f}s", stacklevel=1)
        # Durable reads and writes need a separate budget from readiness probes.
        with MongoClient(
            uri,
            serverSelectionTimeoutMS=5000,
            socketTimeoutMS=5000,
            connectTimeoutMS=5000,
        ) as client:
            yield client
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def snapshot(revision=1, write_id="one"):
    return json.dumps({"revision": revision, "write_id": write_id, "items": ["saved"]})


def test_mongodb_snapshot_cas_reopen_and_competing_owner(mongodb_client) -> None:
    first = MongoDBSessionStore(mongodb_client, database="synthetic")
    second = MongoDBSessionStore(mongodb_client, database="synthetic")
    token = first.acquire("thread")
    try:
        assert first.read("thread", token) is None
        with pytest.raises(RuntimeError, match="already has a writer"):
            second.acquire("thread")
        first.commit("thread", token, 0, "one", snapshot())
        first.commit("thread", token, 0, "one", snapshot())
        with pytest.raises(RuntimeError, match="revision conflict"):
            first.commit("thread", token, 0, "other", snapshot(write_id="other"))
        first.flush("thread", token)
    finally:
        first.release("thread", token)
    successor = second.acquire("thread")
    try:
        first.release("thread", token)
        assert second.read("thread", successor) == snapshot()
    finally:
        second.release("thread", successor)


def test_mongodb_expired_lease_fences_old_writer_and_retains_snapshot(mongodb_client) -> None:
    first = MongoDBSessionStore(mongodb_client, database="synthetic", lease_seconds=1)
    token = first.acquire("thread")
    first.commit("thread", token, 0, "one", snapshot())
    first.collection.update_one(
        {"_id": "thread"}, {"$set": {"lease_until": datetime(2000, 1, 1, tzinfo=timezone.utc)}}
    )
    second = MongoDBSessionStore(mongodb_client, database="synthetic")
    successor = second.acquire("thread")
    try:
        with pytest.raises(RuntimeError, match="fenced|stale or closed"):
            first.commit("thread", token, 1, "two", snapshot(2, "two"))
        first.release("thread", token)
        assert second.read("thread", successor) == snapshot()
    finally:
        first.release("thread", token)
        second.release("thread", successor)


async def test_mongodb_native_session_restores_canonical_context(mongodb_client, responses) -> None:
    store = MongoDBSessionStore(mongodb_client, database="synthetic_native")
    responses.respond(assistant_output("first answer"))
    responses.respond(assistant_output("second answer"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-native-store")
    try:
        async with provider:
            agent = Agent(instructions="Answer.", provider=provider)
            async with agent.session(storage=CustomStorage(store)) as session:
                identity = session.id
                result = await (await session.submit("remember this input")).get_result()
                assert result.final_output == "first answer"
            async with agent.session(storage=CustomStorage(store), resume=identity) as restored:
                assert restored.id == identity
                result = await (await restored.submit("Continue.")).get_result()
                assert result.final_output == "second answer"
        assert len(responses.requests) == 2
        assert "remember this input" in json.dumps(responses.requests[1])
        assert "first answer" in json.dumps(responses.requests[1])
    finally:
        store.close()


def test_mongodb_commit_without_ttl_clears_previous_expiry(mongodb_client) -> None:
    expiring = MongoDBSessionStore(mongodb_client, database="synthetic", ttl=2)
    token = expiring.acquire("thread")
    expiring.commit("thread", token, 0, "one", snapshot())
    expiring.release("thread", token)
    persistent = MongoDBSessionStore(mongodb_client, database="synthetic")
    token = persistent.acquire("thread")
    try:
        before = persistent.collection.find_one({"_id": "thread"})
        assert before is not None and "expires_at" in before
        with pytest.raises(RuntimeError, match="revision conflict"):
            persistent.commit("thread", token, 0, "stale", snapshot(1, "stale"))
        rejected = persistent.collection.find_one({"_id": "thread"})
        assert rejected is not None and rejected["expires_at"] == before["expires_at"]
        persistent.commit("thread", token, 1, "permanent", snapshot(2, "permanent"))
        after = persistent.collection.find_one({"_id": "thread"})
        assert after is not None and "expires_at" not in after
        time.sleep(2.1)
        assert persistent.read("thread", token) == snapshot(2, "permanent")
        assert (
            persistent.collection.count_documents(
                {"_id": "thread", "expires_at": {"$exists": True}}
            )
            == 0
        )
    finally:
        persistent.release("thread", token)
        persistent.close()
        expiring.close()
