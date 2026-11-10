"""Verify a copied Codex login through concurrent native refresh and restart."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
import sys
from contextlib import AsyncExitStack, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agents import Turn, TurnStoppedError
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_core_schema_transport import connect_proxy
from tests.test_native_codex_login import ACCOUNT, codex_agent, saved_login, submit

AUTH_HOST = "auth.openai.com"
SESSION_COUNT = 3


def refresh_server_tls(tmp_path: Path) -> tuple[Path, ssl.SSLContext]:
    """Create a test-only CA and certificate for the fixed OAuth hostname."""
    now = datetime.now(timezone.utc)
    validity = (
        x509.CertificateBuilder()
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
    )
    ca_key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic refresh CA")])
    ca = (
        validity.subject_name(issuer)
        .issuer_name(issuer)
        .serial_number(x509.random_serial_number())
        .public_key(ca_key.public_key())
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        validity.subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, AUTH_HOST)]))
        .issuer_name(issuer)
        .serial_number(x509.random_serial_number())
        .public_key(key.public_key())
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(AUTH_HOST)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_file = tmp_path / "refresh-ca.pem"
    cert_file = tmp_path / "refresh-cert.pem"
    key_file = tmp_path / "refresh-key.pem"
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    key_file.unlink()
    return ca_file, context


async def run_after_restart(home: Path, endpoint: str) -> None:
    script = """
import asyncio
import sys
from _agents_codex import session as session_module
from agents import Agent, CodexLogin, NativeProvider

async def main():
    home, endpoint, account_id = sys.argv[1:]
    session_module._CODEX_RESPONSES_BASE_URL = endpoint
    async with NativeProvider(auth=CodexLogin(codex_home=home, account_id=account_id)) as provider:
        agent = Agent(instructions="Answer briefly.", model="gpt-5.1-codex", provider=provider)
        async with agent.session() as session:
            turn = await session.submit("Continue after restart.")
            assert (await turn.get_result()).final_output == "Accepted rotated login."

asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(home),
        endpoint,
        ACCOUNT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await process.communicate()
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    assert process.returncode == 0, (stdout.decode(), stderr.decode())


@pytest.mark.serial
async def test_sessions_share_one_refresh_of_a_copied_login_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _agents_codex import session as session_module

    source = saved_login(tmp_path / "browser-host") / "auth.json"
    home = tmp_path / "headless-host"
    home.mkdir()
    cache = home / "auth.json"
    shutil.copyfile(source, cache)
    original = json.loads(source.read_text(encoding="utf-8"))
    model_tokens: list[str] = []
    refresh_tokens: list[str] = []
    all_expired = asyncio.Event()
    duplicate_refresh = asyncio.Event()

    async def responses(request: web.Request) -> web.Response:
        assert request.headers["ChatGPT-Account-ID"] == ACCOUNT
        token = request.headers["Authorization"]
        model_tokens.append(token)
        if token == "Bearer saved-token":
            # Each Session reloads from disk on its first 401 and asks for
            # refresh after its second 401 if the cached login has not changed.
            if model_tokens.count("Bearer saved-token") == 2 * SESSION_COUNT:
                all_expired.set()
            return web.json_response({"error": {"message": "Expired"}}, status=401)
        assert token == "Bearer rotated-token"
        events = [
            assistant_output("Accepted rotated login."),
            {"type": "response.completed", "response": {"id": "resp_rotated"}},
        ]
        return web.Response(
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
            content_type="text/event-stream",
        )

    async def refresh(request: web.Request) -> web.Response:
        grant = await request.json()
        assert grant["grant_type"] == "refresh_token"
        refresh_tokens.append(grant["refresh_token"])
        if len(refresh_tokens) > 1:
            duplicate_refresh.set()
            return web.json_response({"error": "invalid_grant"}, status=400)
        await all_expired.wait()
        # Keep rotation pending while the other engines handle their final 401s.
        with suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(duplicate_refresh.wait(), timeout=0.25)
        return web.json_response(
            {"access_token": "rotated-token", "refresh_token": "rotated-refresh-token"}
        )

    model_app = web.Application(handler_args={"auto_decompress": False})
    model_app.router.add_post("/v1/responses", responses)
    auth_app = web.Application()
    auth_app.router.add_post("/oauth/token", refresh)
    ca_file, tls = refresh_server_tls(tmp_path)
    authority = f"{AUTH_HOST}:443"
    async with timeout(30), AsyncExitStack() as peers:
        model = await peers.enter_async_context(TestServer(model_app))
        auth = TestServer(auth_app, scheme="https")
        peers.push_async_callback(auth.close)
        await auth.start_server(ssl=tls)
        proxy, connects = await peers.enter_async_context(
            connect_proxy(str(auth.make_url("/")), authority=authority)
        )
        for variable in ("HTTPS_PROXY", "https_proxy"):
            monkeypatch.setenv(variable, proxy)
        for variable in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(variable, "127.0.0.1")
        for variable in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            monkeypatch.delenv(variable, raising=False)
        monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(ca_file))
        endpoint = str(model.make_url("/v1"))
        monkeypatch.setattr(session_module, "_CODEX_RESPONSES_BASE_URL", endpoint)

        agent = codex_agent(home)
        assert agent.provider is not None
        async with agent.provider, AsyncExitStack() as sessions:
            opened = [
                await sessions.enter_async_context(agent.session()) for _ in range(SESSION_COUNT)
            ]
            results = await asyncio.gather(
                *(submit(session, "Recover from an expired login.") for session in opened)
            )
        assert [result.final_output for result in results] == [
            "Accepted rotated login."
        ] * SESSION_COUNT
        assert model_tokens.count("Bearer rotated-token") == SESSION_COUNT
        persisted = json.loads(cache.read_text(encoding="utf-8"))
        assert persisted["tokens"] == {
            **original["tokens"],
            "access_token": "rotated-token",
            "refresh_token": "rotated-refresh-token",
        }
        assert persisted["last_refresh"] != original["last_refresh"]
        assert json.loads(source.read_text(encoding="utf-8")) == original

        await run_after_restart(home, endpoint)
        assert model_tokens == ["Bearer saved-token"] * (2 * SESSION_COUNT) + [
            "Bearer rotated-token"
        ] * (SESSION_COUNT + 1)
        assert refresh_tokens == ["synthetic-refresh-token"]
        connect_request = f"CONNECT {authority} HTTP/1.1\r\n".encode()
        assert any(request.startswith(connect_request) for request in connects)


@pytest.mark.serial
async def test_saved_login_survives_cancelled_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _agents_codex import session as session_module

    source = saved_login(tmp_path / "browser-host") / "auth.json"
    home = tmp_path / "headless-host"
    home.mkdir()
    cache = home / "auth.json"
    shutil.copyfile(source, cache)
    original_bytes = source.read_bytes()
    original = json.loads(original_bytes)
    replacement = {"access_token": "rotated-token", "refresh_token": "rotated-refresh-token"}
    api_key = "unused-synthetic-api-key"
    admitted = asyncio.Event()
    release_reply = asyncio.Event()
    both_expired = asyncio.Event()
    oauth_calls = replays = expired = rotated = fallback = unexpected = 0

    def cache_has_rotation() -> bool:
        try:
            state = json.loads(cache.read_bytes())
        except (OSError, ValueError):
            return False
        return (
            isinstance(state, dict)
            and state.get("tokens") == {**original["tokens"], **replacement}
            and state.get("auth_mode") == original["auth_mode"]
            and state.get("OPENAI_API_KEY") is None
            and isinstance(state.get("last_refresh"), str)
            and bool(state["last_refresh"])
            and state["last_refresh"] != original["last_refresh"]
        )

    async def responses(request: web.Request) -> web.Response:
        nonlocal expired, rotated, fallback, unexpected
        bearer = request.headers.get("Authorization")
        if bearer == f"Bearer {api_key}":
            fallback += 1
        elif request.headers.get("ChatGPT-Account-ID") == ACCOUNT:
            if bearer == "Bearer saved-token":
                expired += 1
                # Each turn reloads on its first 401 and reaches refresh on its second.
                if expired == 4:
                    both_expired.set()
                return web.json_response({"error": {"message": "Expired"}}, status=401)
            if bearer == "Bearer rotated-token":
                rotated += 1
                events = [
                    assistant_output("Accepted rotated login."),
                    {"type": "response.completed", "response": {"id": "resp_rotated"}},
                ]
                return web.Response(
                    text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
                    content_type="text/event-stream",
                )
        unexpected += 1
        return web.json_response({"error": {"message": "Unexpected login"}}, status=403)

    async def refresh(request: web.Request) -> web.Response:
        nonlocal oauth_calls, replays
        oauth_calls += 1
        grant = await request.json()
        if (
            grant.get("grant_type") != "refresh_token"
            or grant.get("refresh_token") != original["tokens"]["refresh_token"]
        ):
            return web.json_response({"error": "invalid_grant"}, status=400)
        if admitted.is_set():
            replays += 1
            return web.json_response({"error": "invalid_grant"}, status=400)
        admitted.set()
        await release_reply.wait()
        return web.json_response(replacement)

    async def force_cancel(turn: Turn) -> bool:
        await turn.cancel(mode="force")
        try:
            await turn.get_result()
        except TurnStoppedError:
            return True
        return False

    model_app = web.Application(handler_args={"auto_decompress": False})
    model_app.router.add_post("/v1/responses", responses)
    auth_app = web.Application()
    auth_app.router.add_post("/oauth/token", refresh)
    ca_file, tls = refresh_server_tls(tmp_path)
    authority = f"{AUTH_HOST}:443"
    async with timeout(60), AsyncExitStack() as peers:
        model = await peers.enter_async_context(TestServer(model_app))
        auth = TestServer(auth_app, scheme="https")
        peers.push_async_callback(auth.close)
        await auth.start_server(ssl=tls)
        proxy, connects = await peers.enter_async_context(
            connect_proxy(str(auth.make_url("/")), authority=authority)
        )
        for variable in ("HTTPS_PROXY", "https_proxy"):
            monkeypatch.setenv(variable, proxy)
        for variable in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(variable, "127.0.0.1")
        for variable in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            monkeypatch.setenv(variable, api_key)
        monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(ca_file))
        endpoint = str(model.make_url("/v1"))
        monkeypatch.setenv("OPENAI_BASE_URL", endpoint)
        monkeypatch.setattr(session_module, "_CODEX_RESPONSES_BASE_URL", endpoint)

        agent = codex_agent(home)
        provider = agent.provider
        assert provider is not None
        async with provider, AsyncExitStack() as sessions:
            original_file = sessions.enter_context(cache.open("rb")) if os.name == "posix" else None
            owner = await sessions.enter_async_context(agent.session())
            peer = await sessions.enter_async_context(agent.session())
            tasks: list[asyncio.Task[bool] | asyncio.Task[None]] = []

            async def close_refresh_owner() -> bool:
                await owner.aclose()
                return cache_has_rotation()

            try:
                first = await owner.submit("Recover the saved login.")
                async with timeout(10):
                    await admitted.wait()
                second = await peer.submit("Recover the same saved login.")
                async with timeout(10):
                    await both_expired.wait()
                    cancelling = [
                        asyncio.create_task(force_cancel(turn)) for turn in (first, second)
                    ]
                    tasks.extend(cancelling)
                    results = await asyncio.gather(*cancelling, return_exceptions=True)
                cancelled = sum(result is True for result in results)
                assert cancelled == 2
                assert expired == 4

                closing_owner = asyncio.create_task(close_refresh_owner())
                closing_peer = asyncio.create_task(peer.aclose())
                tasks.extend((closing_owner, closing_peer))
                # The peer can retire while the refresh owner is still waiting.
                await closing_peer
                assert not closing_owner.done()
                release_reply.set()
                cache_rotated_at_session_close = await closing_owner
                assert cache_rotated_at_session_close
                await provider.aclose()
                cache_rotated_at_provider_close = cache_has_rotation()
                assert cache_rotated_at_provider_close
                if original_file is not None:
                    old_record_preserved = original_file.read() == original_bytes
                    assert old_record_preserved
                    assert cache.stat().st_mode & 0o077 == 0
            finally:
                release_reply.set()
                await asyncio.gather(*tasks, return_exceptions=True)

        assert rotated == 0
        try:
            async with timeout(15):
                await run_after_restart(home, endpoint)
        except AssertionError:
            restarted = False
        else:
            restarted = True
        assert restarted
        assert expired == 4
        assert rotated == 1
        assert oauth_calls == 1
        assert replays == fallback == unexpected == 0
        source_preserved = source.read_bytes() == original_bytes
        assert source_preserved
        connect_request = f"CONNECT {authority} HTTP/1.1\r\n".encode()
        connected_to_authority = any(request.startswith(connect_request) for request in connects)
        assert connected_to_authority
