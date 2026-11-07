"""Actual child URL, proof and Apps paths retain original native authority."""

import asyncio
import json
from contextlib import aclosing, asynccontextmanager
from dataclasses import replace

import pytest

from agents import (
    ChatGPTAppsMCP,
    ChildInvocation,
    ElicitationRequest,
    HostedVerificationMCP,
    HttpMCP,
    InteractionExpiredError,
    VerificationRequest,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, tool_output
from tests.test_native_child_agents import handles, metadata, trust
from tests.test_native_child_tool_integration import Memory, family_agent, family_peer
from tests.test_native_connector_auth import (
    ConnectorBackend,
    access_token,
    synthetic_backend,
)
from tests.test_native_executor_tools import declaration as environment_for
from tests.test_native_mcp_tools import mcp_call
from tests.test_native_owned_child_prompts import cancel_child, interactive
from tests.test_native_url_completion import CompletionPeer, RequiredPeer
from tests.test_native_user_verification import Enrollment, VerificationPeer


@asynccontextmanager
async def owned_http_child(tmp_path, monkeypatch, registration, *, name="search", followup=False):
    """Drive the existing real family fixture; keep native responders authoritative."""
    memory = Memory()
    async with (
        family_peer(programmatic_tool_calling=False, execute=followup) as model,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        original = model.invoke

        def invoke(child, step):
            if step == 1 or followup and step == 2:
                return (
                    mcp_call(name, "mcp__" + registration.name, f"child-request-{step}")
                    if child == "alpha"
                    else assistant_output("beta complete")
                )
            return original(child, step)

        model.invoke = invoke
        app = interactive(family_agent(model, executor, registration.url, memory))
        app = replace(
            app,
            tools=[registration if isinstance(tool, HttpMCP) else tool for tool in app.tools],
        )
        async with app.session(context=memory, environment=environment_for(executor)) as session:
            root = await session.submit("Start the original native children")
            assert (await root.get_result()).final_output == "root complete"
            await asyncio.gather(*(event.wait() for event in model.entered.values()))
            for release in model.release.values():
                release.set()
            children = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha = children[model.child_metadata["alpha"][0]["thread_id"]]
            yield model, session, alpha, children


async def next_owned_question(session, alpha):
    async with aclosing(session.interactions()) as cursor:
        request = await anext(cursor)
    assert request.subagent_id == alpha.child_id
    assert request.turn_id == alpha.turn_id
    assert request.submission_id == alpha.submission_id
    return request


@pytest.mark.parametrize("early", [False, True])
async def test_actual_child_url_consent_waits_for_its_exact_source(tmp_path, monkeypatch, early):
    peer = CompletionPeer(early=early)
    async with timeout(50), peer.serve() as url:
        registration = HttpMCP(
            name="records", url=url, tools=["search"], approval_mode="preapproved"
        )
        try:
            async with owned_http_child(tmp_path, monkeypatch, registration) as (
                model,
                session,
                alpha,
                children,
            ):
                question = await next_owned_question(session, alpha)
                assert isinstance(question, ElicitationRequest)
                assert question.url == "https://example.invalid/never-open"
                assert question.elicitation_id == "synthetic-challenge-A"
                if early:
                    peer.before_accept.set()
                    await peer.early_sent.wait()
                await question.accept_url()
                await peer.answered.wait()
                waiter = asyncio.create_task(question.wait_url_completion(timeout=8))
                try:
                    if not early:
                        assert not waiter.done()
                    peer.finish.set()
                    await waiter
                finally:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                assert (await alpha.get_result()).final_output == "alpha complete"
                assert len(peer.answers) == 1 and peer.answers[0]["result"] == {"action": "accept"}
                assert "synthetic action complete" in tool_output(
                    model.child_requests["alpha"][-1], "child-request-1"
                )
                assert len(peer.replay_answers) == 1 and peer.replay_answers[0]["result"] == {
                    "action": "cancel"
                }
                with pytest.raises(InteractionExpiredError):
                    await question.accept_url()
                for child in children.values():
                    await child.get_result()
        finally:
            peer.finish.set()


@pytest.mark.parametrize("case", ["accepted", "wrong-challenge", "replay"])
async def test_actual_child_proof_submission_is_distinct_from_verifier_acceptance(
    tmp_path, monkeypatch, case
):
    enrollment = Enrollment(tmp_path)
    peer = VerificationPeer(enrollment, hold=True, replay=case == "replay")
    async with timeout(50), peer.serve() as url:
        registration = HostedVerificationMCP(url=url, tools=["search"], approval_mode="preapproved")
        try:
            async with owned_http_child(tmp_path, monkeypatch, registration) as (
                model,
                session,
                alpha,
                children,
            ):
                question = await next_owned_question(session, alpha)
                assert isinstance(question, VerificationRequest)
                assert (
                    question._registration is registration and question.challenge == peer.challenge
                )
                proof = enrollment.sign(
                    question.challenge, wrong_challenge=case == "wrong-challenge"
                )
                await question.respond(proof)
                await peer.answered.wait()
                assert not peer.accepted
                with pytest.raises(InteractionExpiredError):
                    await question.respond(proof)
                peer.verify_release.set()
                assert (await alpha.get_result()).final_output == "alpha complete"
                assert peer.accepted is (case == "accepted")
                expected = (
                    "verification_accepted" if case == "accepted" else "verification_rejected"
                )
                assert expected in tool_output(model.child_requests["alpha"][-1], "child-request-1")
                assert len(peer.answers) == 1
                assert proof.signature not in json.dumps(model.requests)
                for child in children.values():
                    await child.get_result()
        finally:
            peer.verify_release.set()


async def test_actual_child_hosted_verification_transforms_its_original_result(
    tmp_path, monkeypatch
):
    enrollment = Enrollment(tmp_path)
    peer = VerificationPeer(enrollment)
    contexts = []

    def display(context, value):
        assert isinstance(context.submission, ChildInvocation)
        assert value["isError"] is False
        assert "verification_accepted" in json.dumps(value)
        contexts.append(context)
        return {"content": [{"type": "text", "text": "accepted verified child result"}]}

    async with timeout(50), peer.serve() as url:
        registration = HostedVerificationMCP(
            url=url, tools=["search"], approval_mode="preapproved", transform_result=display
        )
        async with owned_http_child(tmp_path, monkeypatch, registration) as (
            model,
            session,
            alpha,
            children,
        ):
            question = await next_owned_question(session, alpha)
            assert isinstance(question, VerificationRequest)
            assert question._registration is registration
            await question.respond(enrollment.sign(question.challenge))
            assert (await alpha.get_result()).final_output == "alpha complete"
            assert peer.accepted
            assert len(contexts) == 1
            assert contexts[0].submission.child_id == alpha.child_id
            assert contexts[0].submission.turn_id == alpha.turn_id
            assert contexts[0].call_id == "child-request-1"
            output = tool_output(model.child_requests["alpha"][-1], "child-request-1")
            assert "accepted verified child result" in output
            assert "verification_accepted" not in output
            for child in children.values():
                await child.get_result()


@pytest.mark.parametrize(
    "reason", ["missing_link", "reauthentication_required", "oauth_upgrade_required"]
)
async def test_actual_child_apps_completion_retains_original_scopes_and_identity(
    tmp_path, monkeypatch, reason
):
    backend = ConnectorBackend(reason=reason)
    contexts = []

    def displayed_result(context, value):
        assert isinstance(context.submission, ChildInvocation)
        contexts.append(context)
        replacement = json.loads(json.dumps(value))
        replacement["content"] = [{"type": "text", "text": "synthetic redacted auth failure"}]
        return replacement

    registration = ChatGPTAppsMCP(
        tools=["calendar_search"],
        access_token=access_token(),
        account_id="synthetic-account-A",
        approval_mode="preapproved",
        transform_result=displayed_result,
    )
    async with timeout(60), backend.serve() as base:
        synthetic_backend(monkeypatch, base)
        async with owned_http_child(
            tmp_path, monkeypatch, registration, name="calendar_search"
        ) as (model, session, alpha, children):
            question = await next_owned_question(session, alpha)
            assert isinstance(question, ElicitationRequest) and question.url == backend.redirect
            await question.accept_url()
            waiter = asyncio.create_task(question.wait_url_completion(timeout=8))
            try:
                await backend.polled.wait()
                assert not waiter.done()
                backend.complete.set()
                await waiter
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            assert (await alpha.get_result()).final_output == "alpha complete"
            assert len(backend.calls) == 1 and len(backend.init_requests) == 1
            path, body, headers = backend.init_requests[0]
            assert path.endswith("/oauth" if reason == "missing_link" else "/oauth/reauth")
            assert body["requested_scopes"] == ["calendar.write"]
            assert (body.get("connector_id"), body.get("link_id")) == (
                ("calendar", None)
                if reason == "missing_link"
                else (None, "legacy-implicit-real-link")
            )
            assert headers["Authorization"] == "Bearer " + registration.access_token
            assert (
                next(
                    value for name, value in headers.items() if name.lower() == "chatgpt-account-id"
                )
                == registration.account_id
            )
            model_auth = model.request_headers[0]["authorization"]
            assert all(header["authorization"] == model_auth for header in model.request_headers)
            assert model_auth != headers["Authorization"]
            assert contexts and all(
                context.submission.child_id == alpha.child_id for context in contexts
            )
            assert "Authentication for Calendar completed" in tool_output(
                model.child_requests["alpha"][-1], "child-request-1"
            )
            assert backend.state_id not in json.dumps(model.requests)
            for child in children.values():
                await child.get_result()


async def test_actual_child_apps_oauth_does_not_sign_a_separate_proof(tmp_path, monkeypatch):
    enrollment = Enrollment(tmp_path)
    backend = ConnectorBackend(enrollment=enrollment)
    backend.complete.set()
    registration = ChatGPTAppsMCP(
        tools=["calendar_search"],
        access_token=access_token(),
        account_id="synthetic-account-A",
        approval_mode="preapproved",
    )
    async with timeout(60), backend.serve() as base:
        synthetic_backend(monkeypatch, base)
        async with owned_http_child(
            tmp_path, monkeypatch, registration, name="calendar_search", followup=True
        ) as (model, session, alpha, children):
            question = await next_owned_question(session, alpha)
            assert isinstance(question, ElicitationRequest)
            await question.accept_url()
            await question.wait_url_completion(timeout=8)
            proof_request = await next_owned_question(session, alpha)
            assert isinstance(proof_request, VerificationRequest)
            assert proof_request._registration is registration
            assert backend.authorization_complete and not backend.proof_accepted
            assert enrollment.calls == 0 and not backend.proof_answers
            proof = enrollment.sign(proof_request.challenge)
            await proof_request.respond(proof)
            assert (await alpha.get_result()).final_output == "alpha complete"
            assert backend.proof_accepted and enrollment.calls == 1
            assert proof.signature not in json.dumps(model.requests)
            assert len(backend.calls) == 2 and len(backend.init_requests) == 1
            for child in children.values():
                await child.get_result()


@pytest.mark.parametrize("decision", ["accept", "decline"])
async def test_actual_child_url_batch_keeps_original_owner_before_early_notifications(
    tmp_path, monkeypatch, decision
):
    peer = RequiredPeer(ids=("required-A", "required-B"))
    async with timeout(50), peer.serve() as url:
        registration = HttpMCP(
            name="records", url=url, tools=["search"], approval_mode="preapproved"
        )
        async with owned_http_child(tmp_path, monkeypatch, registration) as (
            _model,
            session,
            alpha,
            children,
        ):
            async with aclosing(session.interactions()) as cursor:
                first = await anext(cursor)
                peer.complete.set()
                await peer.notification_sent.wait()
                second = await anext(cursor)
            assert (first.elicitation_id, second.elicitation_id) == (
                "required-A",
                "required-B",
            )
            for question in (first, second):
                assert question.subagent_id == alpha.child_id
                assert question.turn_id == alpha.turn_id
                assert question.submission_id == alpha.submission_id
            if decision == "accept":
                await first.accept_url()
                await second.accept_url()
                await asyncio.gather(
                    first.wait_url_completion(timeout=8),
                    second.wait_url_completion(timeout=8),
                )
            else:
                await second.decline()
                with pytest.raises(InteractionExpiredError):
                    await first.wait_url_completion(timeout=8)
                with pytest.raises(InteractionExpiredError):
                    await first.accept_url()
            assert (await alpha.get_result()).final_output == "alpha complete"
            assert not peer.answers
            for child in children.values():
                await child.get_result()


async def test_original_child_cancellation_rejects_late_application_proof(tmp_path, monkeypatch):
    enrollment = Enrollment(tmp_path)
    peer = VerificationPeer(enrollment, hold=True)
    entered, release = asyncio.Event(), asyncio.Event()
    async with timeout(50), peer.serve() as url:
        registration = HostedVerificationMCP(url=url, tools=["search"], approval_mode="preapproved")
        try:
            async with owned_http_child(tmp_path, monkeypatch, registration) as (
                _model,
                session,
                alpha,
                children,
            ):
                question = await next_owned_question(session, alpha)
                assert isinstance(question, VerificationRequest)

                async def delayed_application_signer():
                    entered.set()
                    await release.wait()
                    await question.respond(enrollment.sign(question.challenge))

                signer = asyncio.create_task(delayed_application_signer())
                try:
                    await entered.wait()
                    await cancel_child(session, alpha)
                    release.set()
                    with pytest.raises(InteractionExpiredError):
                        await signer
                    assert all(
                        answer.get("result", {}).get("action") != "accept"
                        for answer in peer.answers
                    )
                    assert not peer.accepted
                    for child in children.values():
                        if child is not alpha:
                            assert (await child.get_result()).final_output == "beta complete"
                finally:
                    release.set()
                    signer.cancel()
                    await asyncio.gather(signer, return_exceptions=True)
        finally:
            peer.verify_release.set()


async def test_one_shot_cancel_joins_original_child_signer_and_discards_late_proof(
    tmp_path, monkeypatch
):
    enrollment = Enrollment(tmp_path)
    peer = VerificationPeer(enrollment, hold=True)
    entered, cancelled, release, joined, root_release = (asyncio.Event() for _ in range(5))
    received = []
    memory = Memory()
    async with (
        timeout(60),
        peer.serve() as url,
        family_peer(programmatic_tool_calling=False) as model,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        registration = HostedVerificationMCP(url=url, tools=["search"], approval_mode="preapproved")
        original_invoke, original_response, original_hold = (
            model.invoke,
            model.response,
            model.before_event,
        )

        def invoke(child, step):
            if step == 1:
                return (
                    mcp_call("search", "mcp__codex_apps", "held-child-proof")
                    if child == "alpha"
                    else assistant_output("beta complete")
                )
            return original_invoke(child, step)

        def response():
            path = metadata(model.requests[-1], model.request_headers[-1])["agent_name"]
            events = original_response()
            if path == "/root":
                for event in events:
                    if event.get("item", {}).get("type") == "message":
                        event["item"]["id"] = "held-root-final-output"
            return events

        async def hold(event):
            if event.get("item", {}).get("id") == "held-root-final-output":
                await root_release.wait()
            elif original_hold is not None:
                await original_hold(event)

        async def delayed_provider(question):
            assert isinstance(question, VerificationRequest)
            assert question.subagent_id == model.child_metadata["alpha"][0]["thread_id"]
            assert question.turn_id == model.child_metadata["alpha"][0]["turn_id"]
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

        model.invoke, model.response, model.before_event = invoke, response, hold
        for ready in model.release.values():
            ready.set()
        app = interactive(family_agent(model, executor, url, memory))
        app = replace(
            app, tools=[registration if isinstance(tool, HttpMCP) else tool for tool in app.tools]
        )
        task = asyncio.create_task(
            app.run(
                "Wait for the original child proof",
                context=memory,
                environment=environment_for(executor),
                on_interaction=delayed_provider,
            )
        )
        entered_waiter = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait(
                (entered_waiter, task), return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                await task
                raise AssertionError("The run ended before the original child signer started")
            await entered_waiter
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
            assert not peer.accepted
            with pytest.raises(InteractionExpiredError):
                await received[0].respond(enrollment.sign(peer.challenge))
        finally:
            release.set()
            root_release.set()
            peer.verify_release.set()
            entered_waiter.cancel()
            task.cancel()
            await asyncio.gather(task, entered_waiter, return_exceptions=True)
