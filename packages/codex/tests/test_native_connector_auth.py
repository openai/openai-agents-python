"""Native Apps OAuth state and proof ownership against synthetic local services."""

import asyncio
import base64
import json
from contextlib import aclosing, asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    ChatGPTAppsMCP,
    ElicitationRequest,
    HostedVerificationMCP,
    InteractionExpiredError,
    InteractionRequired,
    VerificationRequest,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses, tool_output
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, declarations, mcp_call
from tests.test_native_user_verification import Enrollment, encoded


def access_token(account="synthetic-account-A", user="synthetic-user-A"):
    def segment(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return ".".join(
        [
            segment({"alg": "none", "typ": "JWT"}),
            segment(
                {
                    "exp": 4102444800,
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": account,
                        "chatgpt_user_id": user,
                        "chatgpt_plan_type": "plus",
                    },
                }
            ),
            "synthetic-only",
        ]
    )


class ConnectorBackend(Peer):
    def __init__(self, *, reason="reauthentication_required", case="complete", enrollment=None):
        super().__init__(names=("calendar_search",))
        self.reason = reason
        self.case = case
        self.enrollment = enrollment
        self.init_requests = []
        self.redirect_requests = []
        self.poll_requests = []
        self.mcp_headers = []
        self.complete = asyncio.Event()
        self.polled = asyncio.Event()
        self.connected = asyncio.Event()
        self.forged = asyncio.Event()
        self.notifications_queue = asyncio.Queue()
        self.proof_answers = []
        self.proof_answered = asyncio.Event()
        self.proof_accepted = False
        self.challenge = b"synthetic-account-A-action-B-after-auth"
        self.state_id = "synthetic-private-exact-oauth-state-A"
        self.redirect = "https://provider.synthetic.invalid/authorize?state=provider-state"
        self.authorization_complete = False

    def auth_failure(self):
        result = {
            "isError": True,
            "content": [{"type": "text", "text": "synthetic_original_authentication_failure"}],
            "_meta": {
                "_codex_apps": {
                    "connector_auth_failure": {
                        "is_auth_failure": True,
                        "connector_id": "calendar",
                        "auth_reason": self.reason,
                        "link_id": "synthetic_link::calendar"
                        if self.reason == "missing_link"
                        else "legacy-implicit-real-link",
                        "requested_scopes": ["calendar.write"],
                    }
                }
            },
        }

        failure = result["_meta"]["_codex_apps"]["connector_auth_failure"]
        if self.case == "synthetic-link":
            failure["link_id"] = "synthetic_link::calendar"
        elif self.case == "missing-link-id":
            failure.pop("link_id")
        elif self.case == "malformed-scopes":
            failure["requested_scopes"] = ["calendar.write", 1]
        return result

    async def handle(self, request):
        body = await request.json()
        self.mcp_headers.append(dict(request.headers))
        if body.get("id") == 873 and "result" in body:
            self.proof_answers.append(body)
            self.proof_answered.set()
            return web.Response(status=202)
        if body.get("method") == "tools/call":
            self.requests.append(body)
            self.calls.append(body["params"])
            self.call_entered.set()
            await self.connected.wait()
            if self.enrollment is not None and len(self.calls) == 2:
                assert self.authorization_complete
                stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await stream.prepare(request)

                async def send(value):
                    await stream.write(
                        (
                            "event: message\ndata: "
                            + json.dumps({"jsonrpc": "2.0", **value})
                            + "\n\n"
                        ).encode()
                    )

                await send(
                    {
                        "id": 873,
                        "method": "openai/elicitation/create",
                        "params": {
                            "mode": "openai/userVerification",
                            "title": "Approve separate action B",
                            "description": "Authentication alone does not sign this action",
                            "challenge": encoded(self.challenge),
                        },
                    }
                )
                await self.proof_answered.wait()
                reply = self.proof_answers[0]["result"]
                self.proof_accepted = reply.get("action") == "accept" and self.enrollment.verify(
                    self.challenge, reply.get("content")
                )
                await send(
                    {
                        "id": body["id"],
                        "result": {
                            "isError": not self.proof_accepted,
                            "content": [
                                {
                                    "type": "text",
                                    "text": "proof_accepted"
                                    if self.proof_accepted
                                    else "proof_rejected",
                                }
                            ],
                        },
                    }
                )
                return stream
            return web.json_response(
                {"jsonrpc": "2.0", "id": body["id"], "result": self.auth_failure()}
            )
        response = await super().handle(request)
        if body.get("method") == "initialize":
            value = json.loads(response.body)
            value["result"]["protocolVersion"] = "2025-11-25"
            return web.json_response(value, headers={"Mcp-Session-Id": "synthetic-native-apps"})
        if body.get("method") == "tools/list":
            value = json.loads(response.body)
            for tool in value["result"]["tools"]:
                tool["_meta"] = {"connector_id": "calendar", "connector_name": "Calendar"}
            return web.json_response(value)
        return response

    async def notifications(self, request):
        stream = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await stream.prepare(request)
        self.connected.set()
        while (identity := await self.notifications_queue.get()) is not None:
            await stream.write(
                (
                    "event: message\ndata: "
                    + json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/elicitation/complete",
                            "params": {"elicitationId": identity},
                        }
                    )
                    + "\n\n"
                ).encode()
            )
            self.forged.set()
        return stream

    async def initiate(self, request):
        self.init_requests.append((request.path, await request.json(), dict(request.headers)))
        if self.case in {"401", "403", "redirect"}:
            return web.Response(
                status=302 if self.case == "redirect" else int(self.case),
                headers={"Location": str(request.url.origin()) + "/forbidden-redirect"},
            )
        return web.json_response({"oauth_state_id": self.state_id, "redirect_url": self.redirect})

    async def state(self, request):
        self.poll_requests.append((request.raw_path, dict(request.headers)))
        self.polled.set()
        if not self.complete.is_set():
            return web.json_response(
                {"status": "PENDING", "connector_id": "calendar", "link": None}
            )
        value = {
            "status": "COMPLETE",
            "connector_id": "calendar",
            "link": {
                "id": "new-real-link"
                if self.reason == "missing_link"
                else "legacy-implicit-real-link",
                "connector_id": "calendar",
                "auth_status": "ACTIVE",
                "scopes": ["calendar.read", "calendar.write"],
            },
        }
        if self.case == "partial":
            value["link"]["scopes"] = ["calendar.read"]
        elif self.case == "wrong-link":
            value["link"]["id"] = "another-link"
        elif self.case == "wrong-connector":
            value["connector_id"] = "other"
        elif self.case == "reauth-required":
            value["link"]["auth_status"] = "REAUTH_REQUIRED"
        elif self.case == "null-link":
            value["link"] = None
        elif self.case == "unknown-status":
            value["status"] = "not-a-status"
        self.authorization_complete = self.case == "complete"
        return web.json_response(value)

    async def forbidden_redirect(self, request):
        self.redirect_requests.append(dict(request.headers))
        return web.json_response({"oauth_state_id": "untrusted", "redirect_url": self.redirect})

    @asynccontextmanager
    async def serve(self):
        app = web.Application()
        app.router.add_post("/api/codex/ps/mcp", self.handle)
        app.router.add_get("/api/codex/ps/mcp", self.notifications)
        app.router.add_post("/proof-mcp", self.handle)
        app.router.add_get("/proof-mcp", self.notifications)
        app.router.add_route("*", "/forbidden-redirect", self.forbidden_redirect)
        app.router.add_post("/api/aip/connectors/links/oauth", self.initiate)
        app.router.add_post("/api/aip/connectors/links/oauth/reauth", self.initiate)
        app.router.add_get("/api/aip/connectors/links/oauth/state/{state:.*}", self.state)
        server = TestServer(app)
        try:
            await server.start_server(shutdown_timeout=1)
            yield str(server.make_url("/"))
        finally:
            await self.notifications_queue.put(None)
            self.proof_answered.set()
            await server.close()


def synthetic_backend(monkeypatch, base):
    """Redirect only the captured native backend config; production API has no URL knob."""
    import agents._native as native

    original = native.create_engine

    def create(config_json="{}", runtime_json="{}", **kwargs):
        config = json.loads(config_json)
        config["chatgpt_base_url"] = base
        return original(json.dumps(config), runtime_json, **kwargs)

    monkeypatch.setattr(native, "create_engine", create)


def application(responses, declaration=None):
    declaration = declaration or ChatGPTAppsMCP(
        tools=["calendar_search"],
        access_token=access_token(),
        account_id="synthetic-account-A",
        approval_mode="preapproved",
    )
    return Agent(instructions="", provider=provider(responses), tools=[declaration]), declaration


def select(call_id):
    return mcp_call("calendar_search", "mcp__codex_apps", call_id)


@pytest.mark.parametrize(
    "reason", ["missing_link", "reauthentication_required", "oauth_upgrade_required"]
)
async def test_native_apps_requires_exact_backend_state_and_grants(responses, monkeypatch, reason):
    peer = ConnectorBackend(reason=reason)
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("native-auth"))
        responses.respond(assistant_output("finished"))
        app, declaration = application(responses)
        assert declaration.access_token not in repr(declaration)
        assert declaration.access_token not in json.dumps(declaration._config())
        async with app.session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            assert isinstance(question, ElicitationRequest) and question.url == peer.redirect
            await question.accept_url()
            waiter = asyncio.create_task(question.wait_url_completion(timeout=5))
            await peer.polled.wait()
            await peer.notifications_queue.put(question.elicitation_id)
            await peer.forged.wait()
            done, _ = await asyncio.wait({waiter}, timeout=0.1)
            assert not done, "peer completion cannot prove a raw connector flow"
            peer.complete.set()
            await waiter
            assert (await turn.get_result()).final_output == "finished"
            assert len(peer.calls) == 1, "the original tool is never replayed automatically"
            assert len(peer.init_requests) == 1
            path, body, headers = peer.init_requests[0]
            assert path.endswith("/oauth" if reason == "missing_link" else "/oauth/reauth")
            assert body["callback_url"] == "https://chatgpt.com/connector_platform_oauth_redirect"
            assert body["post_auth_url"] == "https://chatgpt.com/apps/calendar/calendar"
            assert body["requested_scopes"] == ["calendar.write"]
            assert (body.get("connector_id"), body.get("link_id")) == (
                ("calendar", None)
                if reason == "missing_link"
                else (None, "legacy-implicit-real-link")
            )
            assert headers["Authorization"] == "Bearer " + declaration.access_token
            assert (
                next(v for k, v in headers.items() if k.lower() == "chatgpt-account-id")
                == declaration.account_id
            )
            assert all(
                h["authorization"] == "Bearer synthetic-native-agent-key"
                for h in responses.request_headers
            )
            assert "Authentication for Calendar completed" in tool_output(
                responses.requests[-1], "native-auth"
            )
            assert peer.state_id not in json.dumps(responses.requests)
            with pytest.raises(InteractionExpiredError):
                await question.accept_url()


@pytest.mark.parametrize(
    "case",
    ["partial", "wrong-link", "wrong-connector", "reauth-required", "null-link", "unknown-status"],
)
async def test_backend_completion_must_not_accept_incomplete_or_mismatched_proof(
    responses, monkeypatch, case
):
    peer = ConnectorBackend(reason="oauth_upgrade_required", case=case)
    peer.complete.set()
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("rejected-auth"))
        responses.respond(assistant_output("failed"))
        app, _ = application(responses)
        async with app.session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            await question.accept_url()
            with pytest.raises(InteractionExpiredError):
                await question.wait_url_completion(timeout=5)
            await turn.get_result()
            assert "synthetic_original_authentication_failure" in tool_output(
                responses.requests[-1], "rejected-auth"
            )
            assert len(peer.calls) == 1


@pytest.mark.parametrize("case", ["401", "403", "redirect"])
async def test_backend_auth_errors_do_not_fallback_or_forward_credentials(
    responses, monkeypatch, case
):
    peer = ConnectorBackend(case=case)
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("backend-denied"))
        responses.respond(assistant_output("failed"))
        app, _ = application(responses)
        callbacks = []
        await app.run("go", on_interaction=lambda question: callbacks.append(question))
        assert not callbacks and len(peer.init_requests) == 1 and not peer.poll_requests
        assert not peer.redirect_requests
        assert "synthetic_original_authentication_failure" in tool_output(
            responses.requests[-1], "backend-denied"
        )


@pytest.mark.parametrize("retirement", ["timeout", "close", "refresh"])
async def test_raw_auth_completion_after_retirement_or_rejected_refresh(
    responses, monkeypatch, retirement
):
    peer = ConnectorBackend()
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("retired-auth"))
        responses.respond(assistant_output("failed"))
        app, _ = application(responses)
        async with app.session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            await question.accept_url()
            await peer.polled.wait()
            if retirement == "timeout":
                with pytest.raises(TimeoutError):
                    await question.wait_url_completion(timeout=0.05)
            elif retirement == "close":
                await session.cancel_all()
            else:
                # Rejected raw refresh cannot retire an SDK-owned operation.
                with pytest.raises(RuntimeError, match="unsupported for replaceable tool sessions"):
                    await session._native.call("refresh_mcp_servers", "{}")
            peer.complete.set()
            if retirement == "refresh":
                await question.wait_url_completion(timeout=3)
                assert peer.authorization_complete
                assert len(peer.init_requests) == 1 and len(peer.calls) == 1
            else:
                with pytest.raises(InteractionExpiredError):
                    await question.wait_url_completion(timeout=3)
            if retirement != "close":
                await turn.get_result()
            with pytest.raises(InteractionExpiredError):
                await question.accept_url()


async def test_caller_verification_host_cannot_initiate_personal_backend_auth(
    responses, monkeypatch
):
    peer = ConnectorBackend()
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        declaration = HostedVerificationMCP(
            url=base + "proof-mcp", tools=["calendar_search"], approval_mode="preapproved"
        )
        responses.respond(select("forged-apps-classification"))
        responses.respond(assistant_output("failed"))
        app, _ = application(responses, declaration)

        async with app.session() as session:
            turn = await session.submit("go")
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            assert isinstance(question, ElicitationRequest)
            with pytest.raises(InteractionRequired, match="no live native completion route"):
                await question.accept_url()
            await question.decline()
            assert (await turn.get_result()).final_output == "failed"
        assert not peer.init_requests and not peer.poll_requests


async def test_auth_completion_does_not_automatically_sign_a_later_challenge(
    responses, monkeypatch, tmp_path
):
    enrollment = Enrollment(tmp_path)
    peer = ConnectorBackend(enrollment=enrollment)
    peer.complete.set()
    async with timeout(25), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("auth-first"))
        responses.respond(select("separate-proof-request"))
        responses.respond(assistant_output("finished"))
        app, declaration = application(responses)
        seen = []

        def trusted_provider(question):
            if isinstance(question, ElicitationRequest):
                assert enrollment.calls == 0 and not peer.proof_answers
                seen.append("url")
                return question.reply_url_accept()
            assert isinstance(question, VerificationRequest)
            assert question._registration is declaration
            assert peer.authorization_complete and not peer.proof_accepted
            seen.append("proof")
            return question.reply(enrollment.sign(question.challenge))

        assert (await app.run("go", on_interaction=trusted_provider)).final_output == "finished"
        assert seen == ["url", "proof"] and enrollment.calls == 1
        assert peer.proof_accepted and len(peer.calls) == 2
        assert len(peer.init_requests) == 1


@pytest.mark.parametrize("rewrite", ["add", "remove"])
async def test_result_callback_cannot_rewrite_authoritative_requested_scopes(
    responses, monkeypatch, rewrite
):
    peer = ConnectorBackend(reason="oauth_upgrade_required")
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("rewritten-scopes"))
        responses.respond(assistant_output("failed"))

        def transform(context, result):
            result = json.loads(json.dumps(result))
            failure = result["_meta"]["_codex_apps"]["connector_auth_failure"]
            if rewrite == "add":
                failure["requested_scopes"].append("calendar.admin")
            else:
                failure.pop("requested_scopes")
            return result

        declaration = ChatGPTAppsMCP(
            tools=["calendar_search"],
            access_token=access_token(),
            account_id="synthetic-account-A",
            approval_mode="preapproved",
            transform_result=transform,
        )
        app, _ = application(responses, declaration)
        callbacks = []
        await app.run("go", on_interaction=lambda question: callbacks.append(question))
        assert not callbacks and not peer.init_requests and not peer.poll_requests
        assert len(peer.calls) == 1


async def test_backend_state_cannot_be_replayed_into_a_new_local_request(responses, monkeypatch):
    peer = ConnectorBackend()
    peer.complete.set()
    async with timeout(25), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("first-state-owner"))
        responses.respond(select("replayed-state-owner"))
        responses.respond(assistant_output("finished"))
        app, _ = application(responses)
        issued = []

        def consent(question):
            issued.append(question)
            return question.reply_url_accept()

        await app.run("go", on_interaction=consent)
        assert len(issued) == 1 and len(peer.init_requests) == 2
        assert "synthetic_original_authentication_failure" in tool_output(
            responses.requests[-1], "replayed-state-owner"
        )
        assert peer.state_id not in json.dumps(responses.requests)


async def test_default_api_key_session_does_not_admit_personal_apps(responses, monkeypatch):
    peer = ConnectorBackend()
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("undeclared-apps"))
        responses.respond(assistant_output("unavailable"))
        await Agent(instructions="", provider=provider(responses)).run("go")
        assert not peer.requests and not peer.init_requests and not peer.poll_requests


@pytest.mark.parametrize("case", ["synthetic-link", "missing-link-id", "malformed-scopes"])
async def test_invalid_reauth_authority_never_starts_a_personal_flow(responses, monkeypatch, case):
    peer = ConnectorBackend(reason="oauth_upgrade_required", case=case)
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        responses.respond(select("invalid-authority"))
        responses.respond(assistant_output("failed"))
        app, _ = application(responses)
        callbacks = []
        await app.run("go", on_interaction=lambda question: callbacks.append(question))
        assert not callbacks and not peer.init_requests and not peer.poll_requests


def test_explicit_apps_declaration_fixes_target_and_hides_credentials():
    options = {
        "tools": ["calendar_search"],
        "access_token": access_token(),
        "account_id": "synthetic-account-A",
    }
    declaration = ChatGPTAppsMCP(**options)
    assert declaration.name == "codex_apps"
    assert declaration.url == "https://chatgpt.com/backend-api/ps/mcp"
    assert declaration.access_token not in repr(declaration)
    assert declaration.account_id not in repr(declaration)
    assert declaration.access_token not in json.dumps(declaration._config())
    assert "Authorization" not in json.dumps(declaration._config())
    for field, value in [
        ("url", "https://other.invalid"),
        ("bearer_token", "another-key"),
        ("name", "ordinary"),
    ]:
        with pytest.raises(TypeError):
            ChatGPTAppsMCP(**options, **{field: value})


@pytest.mark.parametrize("selection", ["update", "submit", "step"])
async def test_fixed_personal_apps_registration_accepts_captured_leaf_selection(
    responses, monkeypatch, selection
):
    peer = ConnectorBackend()
    peer.names = ("calendar_search", "excluded")
    async with timeout(20), peer.serve() as base:
        synthetic_backend(monkeypatch, base)
        opening = ChatGPTAppsMCP(
            tools=["calendar_search", "excluded"],
            access_token=access_token(),
            account_id="synthetic-account-A",
            approval_mode="preapproved",
        )
        app, _ = application(responses, opening)
        async with app.session() as session:
            leaf = opening.tool("calendar_search")
            options = {}
            if selection == "update":
                await session.update(tools=[leaf])
            elif selection == "submit":
                options["tools"] = [leaf]
            else:
                options["before_step"] = lambda turn, step_index: [leaf]
            responses.respond(select("selected-personal-app"))
            responses.respond(assistant_output("finished"))
            turn = await session.submit("Authorize once", **options)
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            assert isinstance(question, ElicitationRequest)
            await question.accept_url()
            peer.complete.set()
            await question.wait_url_completion(timeout=5)
            assert (await turn.get_result()).final_output == "finished"
            assert len(peer.init_requests) == 1 and len(peer.calls) == 1
            _, body, headers = peer.init_requests[0]
            assert body["requested_scopes"] == ["calendar.write"]
            assert headers["Authorization"] == "Bearer " + opening.access_token
            assert (
                next(v for k, v in headers.items() if k.lower() == "chatgpt-account-id")
                == opening.account_id
            )
            assert all(
                ("mcp__codex_apps", "calendar_search") in declarations(request)
                and ("mcp__codex_apps", "excluded") not in declarations(request)
                for request in responses.requests
            )
            assert "Authentication for Calendar completed" in tool_output(
                responses.requests[-1], "selected-personal-app"
            )
