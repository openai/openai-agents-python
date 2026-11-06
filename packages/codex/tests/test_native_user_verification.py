"""A host-enrolled signer and actual MCP verifier exercise the proof boundary."""

import asyncio
import base64
import json
import shutil
import subprocess
import time
import traceback
from contextlib import aclosing
from dataclasses import replace

import pytest
from aiohttp import web

from agents import (
    Agent,
    HostedVerificationMCP,
    HttpMCP,
    InteractionExpiredError,
    InteractionRequired,
    Session,
    VerificationProof,
    VerificationRequest,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_mcp_tools import declarations, mcp_call
from tests.test_native_scoped_policies import AskingPeer, provider


def encoded(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class Enrollment:
    """Synthetic P-256 enrollment. OpenSSL owns signing and verification."""

    def __init__(self, directory):
        assert shutil.which("openssl"), "Native proof qualification requires OpenSSL"
        self.directory = directory
        self.key = directory / "enrolled-key.pem"
        self.other = directory / "unenrolled-key.pem"
        self.public = directory / "enrolled-public.pem"
        for key in (self.key, self.other):
            subprocess.run(
                [
                    "openssl",
                    "genpkey",
                    "-algorithm",
                    "EC",
                    "-pkeyopt",
                    "ec_paramgen_curve:P-256",
                    "-out",
                    str(key),
                ],
                check=True,
                capture_output=True,
            )
        subprocess.run(
            ["openssl", "pkey", "-in", str(self.key), "-pubout", "-out", str(self.public)],
            check=True,
            capture_output=True,
        )
        self.calls = 0

    def sign(self, challenge, *, wrong_key=False, wrong_challenge=False):
        self.calls += 1
        source = self.directory / f"challenge-{self.calls}"
        signature = self.directory / f"signature-{self.calls}"
        source.write_bytes(b"another challenge" if wrong_challenge else challenge)
        subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-sign",
                str(self.other if wrong_key else self.key),
                "-out",
                str(signature),
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        return VerificationProof("synthetic-enrolled-credential", encoded(signature.read_bytes()))

    def verify(self, challenge, content):
        if (
            not isinstance(content, dict)
            or content.get("credentialId") != "synthetic-enrolled-credential"
        ):
            return False
        source = self.directory / "verifier-challenge"
        signature = self.directory / "verifier-signature"
        source.write_bytes(challenge)
        raw = content.get("signature", "")
        signature.write_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return (
            subprocess.run(
                [
                    "openssl",
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(self.public),
                    "-signature",
                    str(signature),
                    str(source),
                ],
                capture_output=True,
            ).returncode
            == 0
        )


class VerificationPeer(AskingPeer):
    def __init__(self, enrollment, *, replay=False, hold=False, expired=False):
        super().__init__()
        self.enrollment = enrollment
        self.challenge = b"synthetic-private-challenge-for-account-and-action-A"
        self.consumed = {self.challenge} if replay else set()
        self.deadline = time.monotonic() + (-1 if expired else 60)
        self.verify_release = asyncio.Event()
        if not hold:
            self.verify_release.set()
        self.accepted = False

    async def handle(self, request):
        body = await request.json()
        if body.get("method") != "tools/call":
            return await super().handle(request)
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)

        async def send(value):
            await stream.write(
                (
                    "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", **value}) + "\n\n"
                ).encode()
            )

        try:
            await send(
                {
                    "id": 873,
                    "method": "openai/elicitation/create",
                    "params": {
                        "mode": "openai/userVerification",
                        "title": "Approve synthetic action",
                        "description": "Synthetic test only",
                        "challenge": encoded(self.challenge),
                    },
                }
            )
            self.prompted.set()
            await self.answered.wait()
            await self.verify_release.wait()
            response = self.answers[0].get("result", {})
            # Verify and consume the exact account/action challenge before reporting acceptance.
            self.accepted = (
                response.get("action") == "accept"
                and time.monotonic() < self.deadline
                and self.challenge not in self.consumed
                and self.enrollment.verify(self.challenge, response.get("content"))
            )
            if self.accepted:
                self.consumed.add(self.challenge)
            await send(
                {
                    "id": body["id"],
                    "result": {
                        "isError": not self.accepted,
                        "content": [
                            {
                                "type": "text",
                                "text": "verification_accepted"
                                if self.accepted
                                else "verification_rejected",
                            }
                        ],
                    },
                }
            )
            return stream
        finally:
            self.finished.set()


def application(responses, url, *, trusted=True):
    declaration = (
        HostedVerificationMCP(url=url, tools=["search"], approval_mode="approve")
        if trusted
        else HttpMCP(name="ordinary", url=url, tools=["search"], approval_mode="approve")
    )
    return Agent(instructions="", provider=provider(responses), tools=[declaration]), declaration


def select(call_id, *, trusted=True):
    return mcp_call("search", "mcp__codex_apps" if trusted else "mcp__ordinary", call_id)


@pytest.mark.parametrize("case", ["accepted", "wrong-key", "wrong-challenge", "replay", "expired"])
async def test_exact_challenge_is_verified_by_the_enrolled_peer(
    responses: ResponsesFixture, tmp_path, case
):
    enrollment = Enrollment(tmp_path)
    peer = VerificationPeer(
        enrollment, replay=case == "replay", hold=True, expired=case == "expired"
    )
    async with timeout(20), peer.serve() as url:
        responses.respond(select("device-proof"))
        responses.respond(assistant_output("finished"))
        app, _ = application(responses, url)
        try:
            async with app.session() as session:
                turn = await session.submit("go")
                async with aclosing(session.interactions()) as cursor:
                    question = await anext(cursor)
                assert isinstance(question, VerificationRequest)
                assert question.challenge == peer.challenge
                assert question.server_name == "codex_apps"
                assert question.turn_id is None and question.call is None
                proof = enrollment.sign(
                    question.challenge,
                    wrong_key=case == "wrong-key",
                    wrong_challenge=case == "wrong-challenge",
                )
                assert proof.credential_id not in repr(proof) and proof.signature not in repr(proof)
                assert encoded(peer.challenge) not in repr(question)
                for invalid in (
                    VerificationProof("", "BAUG"),
                    VerificationProof("credential", "BAUG="),
                    VerificationProof("credential", "AAB"),
                ):
                    with pytest.raises(ValueError, match="Invalid verification proof") as failure:
                        await question.respond(invalid)
                    assert failure.value.__context__ is None and failure.value.__cause__ is None
                    assert await session.pending_interactions() == (question,)
                    assert not peer.answers
                await question.respond(proof)
                await peer.answered.wait()
                assert not peer.accepted  # Submission is not authoritative verifier acceptance.
                with pytest.raises(InteractionExpiredError):
                    await question.respond(proof)
                peer.verify_release.set()
                assert (await turn.get_result()).final_output == "finished"
                assert peer.accepted is (case == "accepted")
                expected = (
                    "verification_accepted" if case == "accepted" else "verification_rejected"
                )
                assert expected in json.dumps(responses.requests[-1])
                assert proof.signature not in json.dumps(responses.requests)
                assert len(peer.answers) == 1
        finally:
            peer.verify_release.set()


async def test_existing_interaction_callback_owns_the_provider_reply(
    responses: ResponsesFixture, tmp_path
):
    enrollment = Enrollment(tmp_path)
    peer = VerificationPeer(enrollment)
    received = []

    def trusted_provider(question):
        received.append(question)
        return question.reply(enrollment.sign(question.challenge))

    async with timeout(20), peer.serve() as url:
        responses.respond(select("device-callback"))
        responses.respond(assistant_output("finished"))
        app, _ = application(responses, url)
        assert (await app.run("go", on_interaction=trusted_provider)).final_output == "finished"
        assert peer.accepted and len(received) == 1 and enrollment.calls == 1


async def test_proof_callback_cannot_reply_directly_or_reenter_its_own_session(
    responses: ResponsesFixture, tmp_path, monkeypatch
):
    enrollment = Enrollment(tmp_path)
    peer = VerificationPeer(enrollment)
    opened = []
    original_reader = Session._read_events

    async def observe_session(session):
        # Capture the real owner without changing native configuration or event data.
        opened.append(session)
        await original_reader(session)

    monkeypatch.setattr(Session, "_read_events", observe_session)

    async def trusted_provider(question):
        assert isinstance(question, VerificationRequest)
        assert len(opened) == 1 and opened[0].id == question.session_id
        before = len(responses.requests)
        with pytest.raises(RuntimeError, match="return its reply"):
            await question.respond(VerificationProof("synthetic", "BAUG"))
        with pytest.raises(RuntimeError, match="callback"):
            await opened[0].submit("Forbidden nested operation")
        assert not peer.answers and len(responses.requests) == before
        return question.reply(enrollment.sign(question.challenge))

    async with timeout(25), peer.serve() as url:
        responses.respond(select("proof-callback-reentry"))
        responses.respond(assistant_output("finished"))
        app, _ = application(responses, url)
        assert (await app.run("go", on_interaction=trusted_provider)).final_output == "finished"
        assert peer.accepted and len(peer.answers) == 1 and enrollment.calls == 1
        assert len(responses.requests) == 2


async def test_ordinary_server_cannot_activate_the_signer(responses: ResponsesFixture, tmp_path):
    peer = VerificationPeer(Enrollment(tmp_path))
    calls = []
    async with timeout(20), peer.serve() as url:
        responses.respond(select("ordinary-verification", trusted=False))
        responses.respond(assistant_output("rejected"))
        app, _ = application(responses, url, trusted=False)
        await app.run("go", on_interaction=lambda question: calls.append(question))
        initialize = next(
            message for message in peer.requests if message.get("method") == "initialize"
        )
        assert "userVerification" not in initialize["params"]["capabilities"]["extensions"].get(
            "openai/elicitation", {}
        )
        assert not calls and not peer.accepted
        with pytest.raises(ValueError):
            HttpMCP(name="codex_apps", url=url, tools=["search"])


async def test_provider_failure_does_not_expose_private_data_or_submit_a_proof(
    responses: ResponsesFixture, tmp_path
):
    peer = VerificationPeer(Enrollment(tmp_path))

    def failing_provider(question):
        raise RuntimeError("synthetic-private-provider-" + question.challenge.decode())

    async with timeout(20), peer.serve() as url:
        responses.respond(select("provider-failure"))
        app, _ = application(responses, url)
        with pytest.raises(InteractionRequired, match="Verification provider failed") as failure:
            await app.run("go", on_interaction=failing_provider)
        assert failure.value.__context__ is None and failure.value.__cause__ is None
        assert "synthetic-private" not in "".join(traceback.format_exception(failure.value))
        assert all(answer.get("result", {}).get("action") != "accept" for answer in peer.answers)


async def test_cancel_joins_delayed_provider_and_rejects_its_late_reply(
    responses: ResponsesFixture, tmp_path
):
    enrollment = Enrollment(tmp_path)
    peer = VerificationPeer(enrollment)
    entered, cancelled, release, joined = (asyncio.Event() for _ in range(4))
    received = []

    async def delayed_provider(question):
        received.append(question)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finally:
            joined.set()
        return question.reply(enrollment.sign(question.challenge))

    async with timeout(20), peer.serve() as url:
        responses.respond(select("provider-cancel"))
        app, _ = application(responses, url)
        task = asyncio.create_task(app.run("go", on_interaction=delayed_provider))
        try:
            await entered.wait()
            task.cancel()
            await cancelled.wait()
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert joined.is_set()
            assert all(
                answer.get("result", {}).get("action") != "accept" for answer in peer.answers
            )
            with pytest.raises(InteractionExpiredError):
                await received[0].respond(enrollment.sign(peer.challenge))
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_host_registration_cannot_be_replaced_in_a_live_session(
    responses: ResponsesFixture, tmp_path
):
    enrollment = Enrollment(tmp_path)
    first = VerificationPeer(enrollment)
    replacement = VerificationPeer(enrollment)
    async with timeout(20), first.serve() as url, replacement.serve() as other_url:
        responses.respond(select("original-registration"))
        responses.respond(assistant_output("declined"))
        app, _ = application(responses, url)
        async with app.session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                original = await anext(cursor)
            with pytest.raises((ValueError, RuntimeError)):
                await session.update(
                    tools=[
                        HostedVerificationMCP(
                            url=other_url, tools=["search"], approval_mode="approve"
                        )
                    ]
                )
            assert not replacement.requests
            await original.decline()
            await turn.get_result()
            with pytest.raises(ValueError):
                await session.update(
                    tools=[
                        HostedVerificationMCP(
                            url=other_url, tools=["search"], approval_mode="approve"
                        )
                    ]
                )
            assert not replacement.requests
            with pytest.raises(InteractionExpiredError):
                await original.respond(enrollment.sign(original.challenge))


@pytest.mark.parametrize("selection", ["update", "submit", "step"])
async def test_fixed_verification_registration_accepts_captured_leaf_selection(
    responses: ResponsesFixture, tmp_path, selection
):
    enrollment = Enrollment(tmp_path)
    peer = VerificationPeer(enrollment)
    peer.names = ("search", "excluded")
    async with timeout(20), peer.serve() as url:
        opening = HostedVerificationMCP(
            url=url, tools=["search", "excluded"], approval_mode="approve"
        )
        app = Agent(instructions="", provider=provider(responses), tools=[opening])
        async with app.session() as session:
            # A copied declaration is not the captured registration, even with equal fields.
            with pytest.raises(ValueError, match="Apps registration is fixed"):
                await session.update(tools=[replace(opening)])
            leaf = opening.tool("search")
            options = {}
            if selection == "update":
                await session.update(tools=[leaf])
            elif selection == "submit":
                options["tools"] = [leaf]
            else:
                options["before_step"] = lambda turn, step_index: [leaf]
            responses.respond(select("selected-proof"))
            responses.respond(assistant_output("finished"))
            turn = await session.submit("Verify once", **options)
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            assert isinstance(question, VerificationRequest)
            assert question._registration is opening and question.challenge == peer.challenge
            await question.respond(enrollment.sign(question.challenge))
            assert (await turn.get_result()).final_output == "finished"
            assert peer.accepted and len(peer.answers) == 1
            assert "verification_accepted" in json.dumps(responses.requests[-1])
            assert all(
                ("mcp__codex_apps", "search") in declarations(request)
                and ("mcp__codex_apps", "excluded") not in declarations(request)
                for request in responses.requests
            )
            with pytest.raises(InteractionExpiredError):
                await question.respond(enrollment.sign(question.challenge))
