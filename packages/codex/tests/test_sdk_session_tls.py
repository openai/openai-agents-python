"""Real local HTTPS proves Session credentials and the native pooled TLS lifetime."""

from __future__ import annotations

import asyncio
import hashlib
import os
import ssl
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import copy
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeError, NativeProvider, TurnStoppedError
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output
from tests.test_buffered_responses import json_peer, json_response
from tests.test_native_provider_websockets import WebSocketPeer
from tests.test_sdk_session_transport import disconnected, http_peer

pytestmark = pytest.mark.serial

# Synthetic certificates only. The localhost/127.0.0.1/::1 server certs and
# test-only server keys were generated offline for these tests. No CI tools or
# extra dependencies are needed. Certificate validity ends in October 2036.
TLS_FIXTURES = {
    "ca-A.pem": """-----BEGIN CERTIFICATE-----
MIIBujCCAWCgAwIBAgIUV7vEHrfuosFQwDwXjVtapSwWqjMwCgYIKoZIzj0EAwIw
KTEnMCUGA1UEAwweQWdlbnRzIFNESyBzeW50aGV0aWMgdGVzdCBDQSBBMB4XDTI2
MTAxOTA1MzQwNFoXDTM2MTAxNjA1MzQwNFowKTEnMCUGA1UEAwweQWdlbnRzIFNE
SyBzeW50aGV0aWMgdGVzdCBDQSBBMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE
rcBK1nW+S7MJC0vn5Q2BIBRMeW7kcs91jamsX18qAjDaqfUOQG+xo+0dnqTV6ivA
j1lTb/WxPvQpbRPs3QFReqNmMGQwHQYDVR0OBBYEFO53LaWlx7SKHr4aO6QxhsEB
x/20MB8GA1UdIwQYMBaAFO53LaWlx7SKHr4aO6QxhsEBx/20MBIGA1UdEwEB/wQI
MAYBAf8CAQAwDgYDVR0PAQH/BAQDAgEGMAoGCCqGSM49BAMCA0gAMEUCID2yzokb
wwuotAxrnfteNqq8jXHpgoBgY7kwYyec2bWJAiEAwuQ5CLMoymnRofFFzfSIvABX
OEPgfxsKCv3wtq59RpI=
-----END CERTIFICATE-----
""",
    "ca-B.pem": """-----BEGIN CERTIFICATE-----
MIIBuzCCAWCgAwIBAgIUcMkFs5ivpR3Sn+fMPvf5CU6BpaswCgYIKoZIzj0EAwIw
KTEnMCUGA1UEAwweQWdlbnRzIFNESyBzeW50aGV0aWMgdGVzdCBDQSBCMB4XDTI2
MTAxOTA1MzQwNFoXDTM2MTAxNjA1MzQwNFowKTEnMCUGA1UEAwweQWdlbnRzIFNE
SyBzeW50aGV0aWMgdGVzdCBDQSBCMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE
SamD9z5wPwF07thtHk/WX6uYgr/g3bfVssOmhsvEn9fMz/dQorrlfhWYxIjPucXe
5g1DhqNAH8mMa67oDA+CbqNmMGQwHQYDVR0OBBYEFKtjhqSfCjIiHRAIiv6mzyv4
LJjnMB8GA1UdIwQYMBaAFKtjhqSfCjIiHRAIiv6mzyv4LJjnMBIGA1UdEwEB/wQI
MAYBAf8CAQAwDgYDVR0PAQH/BAQDAgEGMAoGCCqGSM49BAMCA0kAMEYCIQCLV/RM
9cf7NNtFUd42lLDyonoiJy52qqlm9AJ/Ca2G2gIhAOcrDIHJmEMWajKOY/pcUokq
Z03rz+kNdVGe+e9U7NH+
-----END CERTIFICATE-----
""",
    "server-A.pem": """-----BEGIN CERTIFICATE-----
MIIB5TCCAYqgAwIBAgIUBSc+2vbx07vJjnOzAC/2TRmjVkIwCgYIKoZIzj0EAwIw
KTEnMCUGA1UEAwweQWdlbnRzIFNESyBzeW50aGV0aWMgdGVzdCBDQSBBMB4XDTI2
MTAxOTA1MzQwNFoXDTM2MTAxNjA1MzQwNFowFDESMBAGA1UEAwwJbG9jYWxob3N0
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEKHBqiZyBbS/4wbxeQebh7vQZypmL
VU/TguR2Lzi3D6HvyN975v6497C9osQFDRRouZ2eXA3Lv9xGS7kcHxuxmqOBpDCB
oTAMBgNVHRMBAf8EAjAAMA4GA1UdDwEB/wQEAwIDiDATBgNVHSUEDDAKBggrBgEF
BQcDATAsBgNVHREEJTAjgglsb2NhbGhvc3SHBH8AAAGHEAAAAAAAAAAAAAAAAAAA
AAEwHQYDVR0OBBYEFHxwQV/CFEs0g5DP9x0G23wGDWs4MB8GA1UdIwQYMBaAFO53
LaWlx7SKHr4aO6QxhsEBx/20MAoGCCqGSM49BAMCA0kAMEYCIQDrLneN/YFdUyZn
Vqzezn2wX8IrsAvbQcYqAKMc3qawkAIhAPIsNe2b/pkbp08ngf9qsvVahBJ4Auj8
R41WbKJEvgrv
-----END CERTIFICATE-----
""",
    "server-A.key": """-----BEGIN EC PRIVATE KEY-----
MHcCAQEEIPDx+KrJ37yPW56MQItCe6Xxw4uaRVYYVxw2N9ySucyPoAoGCCqGSM49
AwEHoUQDQgAEKHBqiZyBbS/4wbxeQebh7vQZypmLVU/TguR2Lzi3D6HvyN975v64
97C9osQFDRRouZ2eXA3Lv9xGS7kcHxuxmg==
-----END EC PRIVATE KEY-----
""",
    "server-B.pem": """-----BEGIN CERTIFICATE-----
MIIB4zCCAYqgAwIBAgIUSpOQ+LoTU4u0k83qcGlAJNAlvWAwCgYIKoZIzj0EAwIw
KTEnMCUGA1UEAwweQWdlbnRzIFNESyBzeW50aGV0aWMgdGVzdCBDQSBCMB4XDTI2
MTAxOTA1MzQwNFoXDTM2MTAxNjA1MzQwNFowFDESMBAGA1UEAwwJbG9jYWxob3N0
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAExHzoCuR7GLSPbgHf+IdrQ0ZRM7yi
cT0Vm335+n87Jh0OMf9Dq65jtQT92VXMKIm/sjtbzt6g0BsQRWLDQO9NVqOBpDCB
oTAMBgNVHRMBAf8EAjAAMA4GA1UdDwEB/wQEAwIDiDATBgNVHSUEDDAKBggrBgEF
BQcDATAsBgNVHREEJTAjgglsb2NhbGhvc3SHBH8AAAGHEAAAAAAAAAAAAAAAAAAA
AAEwHQYDVR0OBBYEFHxaGwANxlnxbWROIsIQtPQtPWVHMB8GA1UdIwQYMBaAFKtj
hqSfCjIiHRAIiv6mzyv4LJjnMAoGCCqGSM49BAMCA0cAMEQCICdcjl55SAZOqgFb
3kiUZzdd3riafi+6bC1QcN2KI/0FAiAtQUwv1MC+v52QzMl5TomBh6W6Pm/52LwV
USfOEf8khw==
-----END CERTIFICATE-----
""",
    "server-B.key": """-----BEGIN EC PRIVATE KEY-----
MHcCAQEEIHa6sUoe0b0fPyXwhKL7a1E9d9hNT6NFFT9zNH9GklSmoAoGCCqGSM49
AwEHoUQDQgAExHzoCuR7GLSPbgHf+IdrQ0ZRM7yicT0Vm335+n87Jh0OMf9Dq65j
tQT92VXMKIm/sjtbzt6g0BsQRWLDQO9NVg==
-----END EC PRIVATE KEY-----
""",
}


@dataclass
class HTTPSPeer(ResponsesFixture):
    tls_context: ssl.SSLContext | None = None
    transports: list[asyncio.Transport] = field(default_factory=list)
    socket_filenos: list[Callable[[], int]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        transport = request.transport
        assert transport is not None
        assert transport.get_extra_info("ssl_object") is not None
        self.transports.append(transport)
        sock = transport.get_extra_info("socket")
        assert sock is not None and sock.fileno() >= 0
        # The bound method retains this exact socket, even after transport cleanup.
        self.socket_filenos.append(sock.fileno)
        return await super().handle(request)

    def agent(self, *, key: str | None = "synthetic-tls-key") -> Agent[None, str]:
        return Agent(
            instructions="Answer with the requested marker.",
            model="gpt-5.2",
            provider=NativeProvider(base_url=self.url, api_key=key, supports_websockets=False),
        )


@pytest.fixture(autouse=True)
def isolated_tls_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each test mutates process environment serially and restores every value.
    for name in ("CODEX_CA_CERTIFICATE", "SSL_CERT_FILE", "SSL_CERT_DIR", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")


@pytest.fixture
def tls_files(tmp_path: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for name, value in TLS_FIXTURES.items():
        path = tmp_path / name
        path.write_text(value)
        if name.endswith(".key"):
            path.chmod(0o600)
        result[name] = path
    return result


@asynccontextmanager
async def https_peer(
    files: dict[str, Path],
    issuer: str = "A",
    *,
    finite_http_response: bool = False,
) -> AsyncIterator[HTTPSPeer]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(files[f"server-{issuer}.pem"], files[f"server-{issuer}.key"])
    peer = HTTPSPeer(tls_context=context, finite_http_response=finite_http_response)
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    server = TestServer(app, scheme="https")
    await server.start_server(ssl=context)
    try:
        peer.url = str(server.make_url("/v1"))
        yield peer
    finally:
        await server.close()


async def completed(peer: HTTPSPeer, agent: Agent[None, str], marker: str) -> None:
    peer.respond(assistant_output(marker))
    result = await agent.run(marker)
    assert result.final_output == marker
    assert result.thread_usage is not None
    assert result.thread_usage.total_token_usage.input_tokens == 11
    assert result.thread_usage.total_token_usage.output_tokens == 3
    assert result.thread_usage.total_token_usage.total_tokens == 14


async def wait_for_socket_close(fileno: Callable[[], int]) -> None:
    # TLS transport flags can lag peer EOF on Python 3.10. Observe the socket
    # before test server cleanup can close it.
    async with timeout(5):
        while fileno() != -1:
            await asyncio.sleep(0.001)


async def rejected(
    peer: HTTPSPeer | WebSocketPeer,
    agent: Agent[None, str],
    marker: str,
    *,
    schannel_verdict: asyncio.Future[int] | None = None,
) -> None:
    # Require a certificate alert or Apple's actual fixture trust verdict.
    # Connection resets, absent HTTP and watchdogs never establish rejection.
    loop = asyncio.get_running_loop()
    assert peer.tls_context is not None
    context = peer.tls_context
    previous_callback = context._msg_callback
    rejection: asyncio.Future[bytes | str] | asyncio.Future[int] = (
        loop.create_future() if schannel_verdict is None else schannel_verdict
    )
    before = len(peer.requests)
    native_trust = sys.platform == "darwin" and "OPENAI_AGENTS_TEST_TLS_VERDICTS" in os.environ

    async def observe_macos_trust() -> None:
        path = Path(os.environ["OPENAI_AGENTS_TEST_TLS_VERDICTS"])
        expected = os.environ["OPENAI_AGENTS_TEST_TLS_LEAF_SHA256"]
        while not rejection.done():
            for line in path.read_text().splitlines():
                digest, api, status, verdict = line.split()
                assert digest == expected
                # Legacy trust failures are deny, recoverable and fatal. Modern
                # errors must identify an untrusted certificate or failed chain.
                certificate_rejected = (
                    api == "legacy" and int(status) == 0 and int(verdict) in {3, 5, 6}
                ) or (
                    api == "modern"
                    and int(verdict) == 0
                    and int(status) in {-25318, -67654, -67843}
                )
                assert certificate_rejected, f"Unexpected fixture trust verdict: {line}"
                rejection.set_result(line)
                return
            await asyncio.sleep(0.01)

    def on_tls_message(
        connection: ssl.SSLSocket | ssl.SSLObject,
        direction: str,
        version: int,
        content_type: int,
        msg_type: int,
        data: bytes,
    ) -> None:
        # Observe only an incoming fatal bad_certificate, certificate_unknown,
        # or unknown_ca alert, sent by the client to this server. These are the
        # same reasons as the SSLError oracle, but Proactor does not report a
        # failed inbound SSL handshake through its loop exception handler.
        if previous_callback is not None:
            previous_callback(connection, direction, version, content_type, msg_type, data)
        if (
            not native_trust
            and schannel_verdict is None
            and direction == "read"
            and content_type == 21
            and data in (b"\x02\x2a", b"\x02\x2e", b"\x02\x30")
            and not rejection.done()
        ):
            rejection.set_result(data)

    context._msg_callback = on_tls_message
    observer = asyncio.create_task(observe_macos_trust()) if native_trust else None
    try:
        async with timeout(12), agent.session() as session:
            handle = await session.submit(marker)
            waiter = asyncio.create_task((handle).get_result())
            try:
                watched = (waiter, rejection) if observer is None else (waiter, rejection, observer)
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                if observer is not None and observer in done:
                    await observer
                assert len(peer.requests) == before
                if rejection in done:
                    rejection.result()
                    # If completion raced the trust verdict, cancel may already
                    # be a no-op. Accept only the same certificate terminal.
                    await handle.cancel()
                try:
                    await waiter
                except NativeError:
                    # Core can finish before the independent certificate proof.
                    if observer is not None:
                        await observer
                    await rejection
                except TurnStoppedError:
                    assert rejection in done
                else:
                    pytest.fail("Untrusted TLS completed without certificate rejection")
            finally:
                if not waiter.done():
                    await handle.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
        assert len(peer.requests) == before
    finally:
        if observer is not None:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        context._msg_callback = previous_callback


@pytest.mark.parametrize("selection", ["codex", "empty-codex-ssl", "codex-and-other-native-root"])
async def test_https_real_text_usage_and_native_ca_selection(
    tls_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    if selection == "empty-codex-ssl":
        monkeypatch.setenv("CODEX_CA_CERTIFICATE", "")
        monkeypatch.setenv("SSL_CERT_FILE", str(tls_files["ca-A.pem"]))
    else:
        monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-A.pem"]))
        if selection == "codex-and-other-native-root":
            # SSL_CERT_FILE may also contribute platform roots. Only require
            # the selected Codex root A to continue working.
            monkeypatch.setenv("SSL_CERT_FILE", str(tls_files["ca-B.pem"]))
    async with timeout(40), https_peer(tls_files) as peer:
        await completed(peer, peer.agent(), "accepted-A")
        assert len(peer.requests) == 1
        assert peer.request_headers[0]["authorization"] == "Bearer synthetic-tls-key"


@pytest.mark.parametrize("trust", ["other-ca", "system-roots"])
async def test_https_untrusted_server_rejected_before_provider_request(
    tls_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    trust: str,
) -> None:
    if (
        trust == "system-roots"
        and sys.platform == "win32"
        and os.environ.get("OPENAI_AGENTS_TEST_SCHANNEL_CHILD") != "1"
    ):
        # Schannel, too, can fail its local trust check without alerting the
        # peer. Isolate the observer in a child containing this one test.
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            f"{__file__}::test_https_untrusted_server_rejected_before_provider_request[system-roots]",
            env=dict(os.environ, OPENAI_AGENTS_TEST_SCHANNEL_CHILD="1"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with timeout(45):
                output, _ = await child.communicate()
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        assert child.returncode == 0, output.decode()
        return
    if (
        trust == "system-roots"
        and sys.platform == "darwin"
        and "OPENAI_AGENTS_TEST_TLS_VERDICTS" not in os.environ
    ):
        # Secure Transport can reject trust without sending a TLS alert. Observe
        # that exact native call in a child; keep the real Session unchanged.
        directory = tls_files["server-A.pem"].parent
        library = directory / "trust-observer.dylib"
        build = await asyncio.create_subprocess_exec(
            "clang",
            "-dynamiclib",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-deprecated-declarations",
            "-framework",
            "Security",
            "-framework",
            "CoreFoundation",
            str(Path(__file__).with_name("macos_trust_observer.c")),
            "-o",
            str(library),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with timeout(30):
                output, _ = await build.communicate()
        finally:
            if build.returncode is None:
                build.kill()
                await build.wait()
        assert build.returncode == 0, output.decode()
        verdicts = directory / "trust-verdicts.txt"
        verdicts.touch(mode=0o600)
        leaf = ssl.PEM_cert_to_DER_cert(tls_files["server-A.pem"].read_text())
        environment = dict(os.environ)
        environment.update(
            DYLD_INSERT_LIBRARIES=str(library),
            OPENAI_AGENTS_TEST_TLS_VERDICTS=str(verdicts),
            OPENAI_AGENTS_TEST_TLS_LEAF_SHA256=hashlib.sha256(leaf).hexdigest(),
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            f"{__file__}::test_https_untrusted_server_rejected_before_provider_request[system-roots]",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            async with timeout(45):
                output, _ = await child.communicate()
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        assert child.returncode == 0, output.decode()
        assert verdicts.read_text(), "The native certificate verdict was not observed"
        return
    if trust == "other-ca":
        monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-B.pem"]))
    async with timeout(50), https_peer(tls_files) as peer:
        if trust == "system-roots" and sys.platform == "win32":
            from tests.windows_trust_observer import fixture_schannel_rejection

            leaf = ssl.PEM_cert_to_DER_cert(tls_files["server-A.pem"].read_text())
            # rejected() closes its Session before the import is restored.
            with fixture_schannel_rejection(leaf) as verdict:
                await rejected(
                    peer, peer.agent(), "must not reach provider", schannel_verdict=verdict
                )
        else:
            await rejected(peer, peer.agent(), "must not reach provider")
        assert peer.requests == []
        assert peer.request_headers == []
        assert peer.transports == []


@pytest.mark.parametrize(
    "failure,alert",
    [("protocol", 70), ("peer-certificate-alert", 48), ("reset", None)],
    ids=["protocol-version", "peer-unknown-ca", "reset"],
)
async def test_noncertificate_tls_failures_still_retry_and_cancel(
    failure: str,
    alert: int | None,
) -> None:
    second_connection = asyncio.Event()
    hellos: list[bytes] = []
    clients: set[asyncio.Task[None]] = set()

    async def decline(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        clients.add(task)
        try:
            header = await reader.readexactly(5)
            message = await reader.readexactly(int.from_bytes(header[3:5], "big"))
            hellos.append(header + message)
            if alert is None:
                writer.transport.abort()
            else:
                # TLS fatal alert: protocol_version or peer unknown_ca.
                # Neither says that the client rejected a server certificate.
                writer.write(bytes((21, 3, 3, 0, 2, 2, alert)))
                await writer.drain()
            if len(hellos) >= 2:
                second_connection.set()
        finally:
            writer.close()
            await writer.wait_closed()
            clients.discard(task)

    server = await asyncio.start_server(decline, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        agent = Agent(
            model="gpt-5.2",
            instructions="Exercise native TLS without delivering HTTP.",
            provider=NativeProvider(
                base_url=f"https://127.0.0.1:{port}/v1",
                api_key="synthetic-noncertificate-key",
                supports_websockets=False,
            ),
        )
        async with (
            timeout(12),
            agent.session() as session,
        ):
            handle = await session.submit(f"must not be a certificate failure: {failure}")
            waiter = asyncio.create_task((handle).get_result())
            retried = asyncio.create_task(second_connection.wait())
            try:
                done, _ = await asyncio.wait((waiter, retried), return_when=asyncio.FIRST_COMPLETED)
                if waiter in done:
                    # Surface any erroneous certificate terminal, with no
                    # conversion of an early failure into a successful test.
                    await waiter
                    pytest.fail("Expected a retryable TLS failure")
                assert retried in done
                assert all(hello[0] == 22 and hello[5] == 1 for hello in hellos)
                assert b"synthetic-noncertificate-key" not in b"".join(hellos)
                await session._cancel(handle)
                with pytest.raises(TurnStoppedError):
                    await waiter
            finally:
                retried.cancel()
                if not waiter.done():
                    await session._cancel(handle)
                await asyncio.gather(retried, waiter, return_exceptions=True)
    finally:
        server.close()
        await server.wait_closed()
        if clients:
            await asyncio.gather(*clients)


async def test_https_same_path_rewrite_reloads_roots_for_new_session(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trust = tmp_path / "selected-roots.pem"
    trust.write_text(tls_files["ca-A.pem"].read_text())
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(trust))
    async with timeout(80), https_peer(tls_files) as peer:
        agent = peer.agent()
        await completed(peer, agent, "before-rewrite")
        await wait_for_socket_close(peer.socket_filenos[-1])
        trust.write_text(tls_files["ca-B.pem"].read_text())
        await rejected(peer, agent, "same-agent-after-rewrite")
        assert len(peer.requests) == 1
        await rejected(peer, peer.agent(), "new-agent-does-not-trust-A")
        assert len(peer.requests) == 1


async def test_https_shared_and_equal_providers_reload_roots_in_fresh_sessions(
    tls_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trust = tmp_path / "shared-provider-roots.pem"
    trust.write_text(tls_files["ca-A.pem"].read_text())
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(trust))
    async with timeout(40), https_peer(tls_files) as peer:
        shared = NativeProvider(
            base_url=peer.url, api_key="synthetic-shared-tls-key", supports_websockets=False
        )
        equal = NativeProvider(
            base_url=peer.url, api_key="synthetic-shared-tls-key", supports_websockets=False
        )
        original_repr = repr(shared)
        original_hash = hash(shared)
        providers = {shared: "lookup still works"}
        assert shared is not equal and shared == equal
        assert hash(equal) == original_hash
        assert peer.url not in original_repr
        assert "synthetic-shared-tls-key" not in original_repr

        await completed(
            peer,
            Agent(model="gpt-5.2", instructions="Agent one.", provider=shared),
            "first-agent",
        )
        await wait_for_socket_close(peer.socket_filenos[-1])
        trust.write_text(tls_files["ca-B.pem"].read_text())
        await rejected(
            peer,
            Agent(model="gpt-5.2", instructions="Agent two.", provider=shared),
            "second-agent",
        )
        assert len(peer.requests) == 1
        assert shared == equal and hash(shared) == original_hash
        assert repr(shared) == original_repr
        assert providers[equal] == "lookup still works"
        assert all(
            headers["authorization"] == "Bearer synthetic-shared-tls-key"
            for headers in peer.request_headers
        )
        # Equality preserves descriptor identity, not a prior Session's roots.
        await rejected(
            peer,
            Agent(model="gpt-5.2", instructions="Agent three.", provider=equal),
            "untrusted",
        )
        assert len(peer.requests) == 1
        assert shared == equal and hash(shared) == original_hash == hash(equal)
        assert repr(shared) == repr(equal) == original_repr
        assert providers[shared] == providers[equal] == "lookup still works"


async def test_https_changed_root_selector_rejects_old_server_and_new_agent_accepts_new_server(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-A.pem"]))
    async with (
        timeout(80),
        https_peer(tls_files, "A") as first,
        https_peer(tls_files, "B") as second,
    ):
        agent = first.agent()
        await completed(first, agent, "trusted-A")
        monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-B.pem"]))
        await rejected(first, agent, "A-rejected-after-selector-change")
        assert len(first.requests) == 1
        await completed(second, second.agent(), "trusted-B")
        assert len(second.requests) == 1


async def test_https_fresh_ambient_key_and_assigned_provider_reach_only_the_current_destination(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-A.pem"]))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-first-tls-key")
    async with timeout(40), https_peer(tls_files) as first, https_peer(tls_files) as second:
        agent = first.agent(key=None)
        await completed(first, agent, "first-key")
        original_transport = first.transports[-1]
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-next-tls-key")
        await completed(first, agent, "next-key")
        assert first.transports[-1] is not original_transport
        agent = replace(
            agent,
            provider=NativeProvider(
                base_url=second.url, api_key="synthetic-explicit-tls-key", supports_websockets=False
            ),
        )
        await completed(second, agent, "new-provider")
        assert [headers["authorization"] for headers in first.request_headers] == [
            "Bearer synthetic-first-tls-key",
            "Bearer synthetic-next-tls-key",
        ]
        assert [headers["authorization"] for headers in second.request_headers] == [
            "Bearer synthetic-explicit-tls-key"
        ]
        assert len(first.requests) == 2
        assert len(second.requests) == 1


async def test_https_equal_ca_material_at_new_path_and_no_proxy_change_use_new_connections(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-A.pem"]))
    async with timeout(45), https_peer(tls_files) as peer:
        agent = peer.agent()
        await completed(peer, agent, "first")
        original = peer.transports[-1]
        copy = tmp_path / "same-ca-new-path.pem"
        copy.write_text(tls_files["ca-A.pem"].read_text())
        monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(copy))
        await completed(peer, agent, "new-ca-selector")
        selected = peer.transports[-1]
        assert selected is not original
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1,example.invalid")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1,example.invalid")
        await completed(peer, agent, "new-no-proxy")
        assert peer.transports[-1] is not selected
        assert len(peer.requests) == 3


async def test_restored_buffered_session_keeps_http1_when_server_offers_http2(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-A.pem"]))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls_files["server-A.pem"], tls_files["server-A.key"])
    context.set_alpn_protocols(["h2", "http/1.1"])
    async with (
        timeout(40),
        json_peer(ssl_context=context) as peer,
        NativeProvider(
            base_url=peer.url,
            api_key="synthetic-buffered-restore-key",
            supports_websockets=False,
            response_mode="buffered",
            request_max_retries=0,
            stream_max_retries=0,
        ) as provider,
    ):
        agent = Agent(model="gpt-5.2", instructions="Keep checkpoint history.", provider=provider)
        peer.json_replies.append(json_response("before restore", "resp_source", "msg_source"))
        async with agent.session() as source:
            assert (await (await source.submit("original input")).get_result()).final_output == (
                "before restore"
            )
            state = SessionState.from_json((await source.save_state()).to_json())
        peer.json_replies.append(json_response("after restore", "resp_restored", "msg_restored"))
        async with agent.session(state=state) as restored:
            assert restored.id == state.source_session_id
            assert (await (await restored.submit("continued input")).get_result()).final_output == (
                "after restore"
            )
            assert len(peer.requests) == 2
            assert all(request["stream"] is False for request in peer.requests)
            assert "original input" in str(peer.requests[-1]["input"])
            assert "continued input" in str(peer.requests[-1]["input"])
            assert peer.alpn_protocols == ["http/1.1", "http/1.1"]


@pytest.mark.skipif(sys.platform != "linux", reason="Provider HTTP pooling is Linux-only")
async def test_provider_http_pool_reuses_socket_with_fresh_auth_and_independent_history(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_files["ca-A.pem"]))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-pooled-first-key")
    async with timeout(40), https_peer(tls_files, finite_http_response=True) as peer:
        provider = NativeProvider(base_url=peer.url, supports_websockets=False)
        agent = Agent(model="gpt-5.2", instructions="Fixed pooled instructions.", provider=provider)
        try:
            await completed(peer, agent, "pooled-first-input")
            original_transport = peer.transports[-1]
            original_socket = peer.socket_filenos[-1]
            assert original_socket() >= 0
            monkeypatch.setenv("OPENAI_API_KEY", "synthetic-pooled-second-key")
            await completed(peer, agent, "pooled-second-input")
            assert len(peer.requests) == 2
            assert peer.transports[-1] is original_transport
            assert original_socket() >= 0
            assert [headers["authorization"] for headers in peer.request_headers] == [
                "Bearer synthetic-pooled-first-key",
                "Bearer synthetic-pooled-second-key",
            ]
            first_input = str(peer.requests[0]["input"])
            second_input = str(peer.requests[1]["input"])
            assert "pooled-first-input" in first_input
            assert "pooled-second-input" not in first_input
            assert "pooled-second-input" in second_input
            assert "pooled-first-input" not in second_input
        finally:
            await provider.aclose()
        await wait_for_socket_close(original_socket)


@pytest.mark.skipif(sys.platform != "linux", reason="Provider HTTP pooling is Linux-only")
async def test_provider_http_pool_reloads_same_path_ca_and_rejects_stale_trust(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trust = tmp_path / "pooled-roots.pem"
    trust.write_bytes(tls_files["ca-A.pem"].read_bytes())
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(trust))
    async with timeout(65), https_peer(tls_files, finite_http_response=True) as peer:
        provider = NativeProvider(
            base_url=peer.url, api_key="synthetic-pooled-rotation-key", supports_websockets=False
        )
        agent = Agent(model="gpt-5.2", instructions="Fixed pooled instructions.", provider=provider)
        try:
            await completed(peer, agent, "pooled-trusted-A")
            original_transport = peer.transports[-1]
            original_socket = peer.socket_filenos[-1]
            assert original_socket() >= 0
            assert peer.tls_context is not None
            peer.tls_context.load_cert_chain(tls_files["server-B.pem"], tls_files["server-B.key"])
            trust.write_bytes(tls_files["ca-B.pem"].read_bytes())
            await completed(peer, agent, "pooled-trusted-B")
            second_transport = peer.transports[-1]
            second_socket = peer.socket_filenos[-1]
            assert second_transport is not original_transport
            await wait_for_socket_close(original_socket)
            assert second_socket() >= 0
            assert len(peer.requests) == 2

            # An established trusted-B connection must not bypass the next
            # Session's changed trust file. Require an actual certificate alert.
            trust.write_bytes(tls_files["ca-A.pem"].read_bytes())
            await rejected(peer, agent, "pooled-A-must-reject-B")
            assert len(peer.requests) == 2
            await wait_for_socket_close(second_socket)

            trust.write_bytes(tls_files["ca-B.pem"].read_bytes())
            await completed(peer, agent, "pooled-restored-B")
            assert len(peer.requests) == 3
            assert peer.transports[-1] is not second_transport
            assert all(
                headers["authorization"] == "Bearer synthetic-pooled-rotation-key"
                for headers in peer.request_headers
            )
        finally:
            await provider.aclose()
        await wait_for_socket_close(peer.socket_filenos[-1])


@pytest.mark.skipif(sys.platform != "linux", reason="Provider HTTP pooling is Linux-only")
@pytest.mark.parametrize("duplicate", ["equal", "copy"])
async def test_provider_http_pool_close_and_duplicate_ownership(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch, duplicate: str
) -> None:
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_files["ca-A.pem"]))
    async with timeout(45), https_peer(tls_files, finite_http_response=True) as peer:
        first = NativeProvider(
            base_url=peer.url, api_key="synthetic-independent-pool-key", supports_websockets=False
        )
        second = None
        try:
            first_agent = Agent(model="gpt-5.2", instructions="First owner.", provider=first)
            await completed(peer, first_agent, "first-pool-owner")
            first_transport = peer.transports[-1]
            first_socket = peer.socket_filenos[-1]
            # Copy a used owner, so accidentally sharing an existing native
            # pool fails the same observed-connection assertion as equality.
            second = (
                copy(first)
                if duplicate == "copy"
                else NativeProvider(
                    base_url=peer.url,
                    api_key="synthetic-independent-pool-key",
                    supports_websockets=False,
                )
            )
            assert first is not second and first == second
            second_agent = Agent(model="gpt-5.2", instructions="Second owner.", provider=second)
            await completed(peer, second_agent, "second-pool-owner")
            second_transport = peer.transports[-1]
            second_socket = peer.socket_filenos[-1]
            assert second_transport is not first_transport
            assert first_socket() >= 0 and second_socket() >= 0

            await first.aclose()
            await wait_for_socket_close(first_socket)
            assert second_socket() >= 0
            await completed(peer, second_agent, "second-owner-still-usable")
            assert peer.transports[-1] is second_transport
            assert len(peer.requests) == 3
            await second.aclose()
            await wait_for_socket_close(second_socket)
        finally:
            try:
                await first.aclose()
            finally:
                if second is not None:
                    await second.aclose()


@pytest.mark.skipif(sys.platform != "linux", reason="Provider HTTP pooling is Linux-only")
@pytest.mark.parametrize("invalid", ["malformed", "malformed-der", "missing"])
async def test_provider_http_pool_invalid_ca_retires_warm_socket(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid: str
) -> None:
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    trust = tmp_path / "invalidated-pooled-roots.pem"
    trust.write_bytes(tls_files["ca-A.pem"].read_bytes())
    monkeypatch.setenv("SSL_CERT_FILE", str(trust))
    async with timeout(50), https_peer(tls_files, finite_http_response=True) as peer:
        provider = NativeProvider(
            base_url=peer.url, api_key="synthetic-invalid-ca-key", supports_websockets=False
        )
        agent = Agent(model="gpt-5.2", instructions="Answer.", provider=provider)
        try:
            await completed(peer, agent, "valid-before-invalid-ca")
            original_transport = peer.transports[-1]
            original_socket = peer.socket_filenos[-1]
            assert original_socket() >= 0
            if invalid == "malformed":
                trust.write_text("not a PEM certificate\n")
            elif invalid == "malformed-der":
                trust.write_text("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
            else:
                trust.unlink()
            async with agent.session() as session:
                handle = await session.submit("invalid-file-cannot-reuse-trusted-A")
                waiter = asyncio.create_task(handle.get_result())
                try:
                    # Invalid local CA material can fail before a TLS handshake.
                    # Observe retirement before cancellation can close the socket.
                    await wait_for_socket_close(original_socket)
                    assert len(peer.requests) == 1
                    await handle.cancel()
                    with pytest.raises((NativeError, TurnStoppedError)):
                        await waiter
                finally:
                    if not waiter.done():
                        await handle.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
            assert len(peer.requests) == 1
            trust.write_bytes(tls_files["ca-A.pem"].read_bytes())
            await completed(peer, agent, "valid-after-invalid-ca")
            assert len(peer.requests) == 2
            assert peer.transports[-1] is not original_transport
        finally:
            await provider.aclose()
        await wait_for_socket_close(peer.socket_filenos[-1])


@pytest.mark.skipif(sys.platform != "linux", reason="Provider HTTP pooling is Linux-only")
async def test_provider_http_pool_no_ca_http_reuses_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    # The module's isolated environment leaves all CA overrides absent.
    async with timeout(40), http_peer() as peer:
        peer.finite_http_response = True
        provider = NativeProvider(
            base_url=peer.url, api_key="synthetic-no-ca-http-key", supports_websockets=False
        )
        agent = Agent(model="gpt-5.2", instructions="Answer.", provider=provider)
        try:
            for marker in ["no-ca-first", "no-ca-second"]:
                peer.respond(assistant_output(marker))
                result = await agent.run(marker)
                assert result.final_output == marker
            assert len(peer.requests) == 2
            assert peer.transports[0] is peer.transports[1]
            assert not peer.transports[0].is_closing()
        finally:
            await provider.aclose()
        await disconnected(peer.transports)


@pytest.mark.skipif(sys.platform != "linux", reason="Provider HTTP pooling is Linux-only")
async def test_provider_http_pool_redirect_strips_auth_and_evicts_before_close(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_files["ca-A.pem"]))
    async with timeout(55), https_peer(tls_files, finite_http_response=True) as target:
        source_requests = []
        source_sockets = []
        source_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        source_context.load_cert_chain(tls_files["server-A.pem"], tls_files["server-A.key"])

        async def redirect(request: web.Request) -> web.StreamResponse:
            transport = request.transport
            assert transport is not None
            assert transport.get_extra_info("ssl_object") is not None
            sock = transport.get_extra_info("socket")
            assert sock is not None and sock.fileno() >= 0
            source_sockets.append(sock.fileno)
            source_requests.append(
                (
                    request.method,
                    {key.lower(): value for key, value in request.headers.items()},
                    await request.json(),
                )
            )
            raise web.HTTPTemporaryRedirect(location=target.url + "/responses")

        app = web.Application()
        app.router.add_post("/v1/responses", redirect)
        server = TestServer(app, scheme="https")
        await server.start_server(ssl=source_context)
        provider = NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-redirect-secret",
            supports_websockets=False,
        )
        agent = Agent(model="gpt-5.2", instructions="Answer.", provider=provider)
        try:
            await completed(target, agent, "redirect-preserves-post")
            assert len(source_requests) == len(target.requests) == 1
            method, headers, body = source_requests[0]
            assert method == "POST"
            assert headers["authorization"] == "Bearer synthetic-redirect-secret"
            assert body == target.requests[0]
            assert "authorization" not in target.request_headers[0]
            # Provider is deliberately still open: redirect retirement, not
            # final owner cleanup, must release both origins' idle sockets.
            await wait_for_socket_close(source_sockets[0])
            await wait_for_socket_close(target.socket_filenos[0])
        finally:
            try:
                await provider.aclose()
            finally:
                await server.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Provider HTTP pooling is Linux-only")
async def test_provider_http_pool_bounds_live_idle_connections(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_files["ca-A.pem"]))
    async with timeout(120), https_peer(tls_files) as peer:
        provider = NativeProvider(
            base_url=peer.url, api_key="synthetic-bounded-idle-key", supports_websockets=False
        )
        agent = Agent(model="gpt-5.2", instructions="Answer.", provider=provider)
        release = asyncio.Event()
        all_arrived = asyncio.Event()
        tasks = []

        async def hold(event):
            if event["type"] == "response.created":
                if len(peer.requests) == 34:  # one warm request plus 33 active
                    all_arrived.set()
                await release.wait()

        try:
            await completed(peer, agent, "warm-idle-limit")
            peer.before_event = hold
            peer.progress_while_before_event = True
            tasks = [
                asyncio.create_task(completed(peer, agent, "identical-idle-limit"))
                for _ in range(33)
            ]
            async with timeout(80):
                await all_arrived.wait()
            assert len(peer.requests) == 34
            concurrent_transports = {id(transport) for transport in peer.transports[1:]}
            assert len(concurrent_transports) == 33
            # One deadline covers completion AND observation, below the pool's
            # 30-second idle expiry so age eviction cannot conceal a broken cap.
            async with timeout(20):
                release.set()
                await asyncio.gather(*tasks)
                # A warm request can reuse one of the later 33 sockets.
                # Deduplicate exact owners before observing live idle count.
                sockets = {
                    id(transport): fileno
                    for transport, fileno in zip(peer.transports, peer.socket_filenos, strict=True)
                }
                while sum(fileno() >= 0 for fileno in sockets.values()) > 32:
                    await asyncio.sleep(0.001)
                assert 0 < sum(fileno() >= 0 for fileno in sockets.values()) <= 32
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                peer.before_event = None
                await provider.aclose()
        for fileno in {
            id(transport): fileno
            for transport, fileno in zip(peer.transports, peer.socket_filenos, strict=True)
        }.values():
            await wait_for_socket_close(fileno)
