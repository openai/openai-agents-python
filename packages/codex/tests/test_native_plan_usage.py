"""Plan usage through the saved-login Session and native account client."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections import deque
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    ApiRateLimits,
    CodexPlanCredits,
    CodexPlanLimit,
    CodexPlanSpendLimit,
    CodexPlanUsage,
    CodexPlanWindow,
    NativeProvider,
    rate_limits,
)
from tests._asyncio import timeout
from tests.test_native_codex_login import ACCOUNT, USER, codex_agent, saved_login


@pytest_asyncio.fixture(loop_scope="function")
async def plan_endpoint(monkeypatch):
    replies = deque()
    requests = []
    transports = []

    async def handle(request):
        transports.append(request.transport)
        requests.append(
            (
                request.path,
                request.headers.get("Authorization"),
                request.headers.get("ChatGPT-Account-ID"),
            )
        )
        reply = replies.popleft()
        return await reply() if callable(reply) else reply

    app = web.Application()
    app.router.add_get("/backend-api/wham/usage", handle)
    app.router.add_get("/unexpected", handle)
    async with TestServer(app) as server:
        session = importlib.import_module("_agents_codex.session")
        account_url = str(server.make_url("/backend-api"))
        monkeypatch.setattr(session, "_CODEX_ACCOUNT_BASE_URL", account_url)
        monkeypatch.setattr(session, "_CODEX_RESPONSES_BASE_URL", str(server.make_url("/v1")))
        native = importlib.import_module("agents._native")
        create_engine = native.create_engine

        def create(config, *args, **kwargs):
            config = json.loads(config)
            config["chatgpt_base_url"] = account_url
            return create_engine(json.dumps(config), *args, **kwargs)

        monkeypatch.setattr(native, "create_engine", create)
        yield replies, requests, str(server.make_url("/v1")), transports


def _window(used_percent, seconds, reset_at):
    return {
        "used_percent": used_percent,
        "limit_window_seconds": seconds,
        "reset_after_seconds": 60,
        "reset_at": reset_at,
    }


async def test_read_codex_plan_usage_before_dispatch(plan_endpoint, monkeypatch, tmp_path):
    replies, requests, _, transports = plan_endpoint
    monkeypatch.setenv("OPENAI_API_KEY", "unused-synthetic-api-key")
    replies.extend(
        [
            web.json_response(
                {
                    "account_id": ACCOUNT,
                    "user_id": USER,
                    "plan_type": "team",
                    "rate_limit": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary_window": _window(100, 18000, 2000000000),
                        "secondary_window": _window(5, 604800, 2000000100),
                    },
                    "credits": {"has_credits": True, "unlimited": False, "balance": "12.50"},
                    "rate_limit_reset_credits": {"available_count": 2},
                    "spend_control": {
                        "reached": False,
                        "individual_limit": {
                            "limit": "25000",
                            "used": "8000",
                            "remaining": "17000",
                            "used_percent": 32,
                            "remaining_percent": 68,
                            "reset_after_seconds": 3600,
                            "reset_at": 2000000300,
                        },
                    },
                    "additional_rate_limits": [
                        {
                            "metered_feature": "extra-codex",
                            "limit_name": "Extra Codex",
                            "normal_model_slug": "extra-model",
                            "rate_limit": {
                                "allowed": True,
                                "limit_reached": False,
                                "primary_window": _window(88, 3600, 2000000200),
                            },
                        }
                    ],
                }
            ),
            web.json_response(
                {
                    "account_id": ACCOUNT,
                    "plan_type": "team",
                    "rate_limit": {"allowed": False, "limit_reached": True},
                }
            ),
        ]
    )
    async with timeout(30), codex_agent(saved_login(tmp_path / "login")).session() as session:
        assert requests == []
        start = datetime.now(timezone.utc)
        usage = await session.plan_usage()
        assert isinstance(usage, CodexPlanUsage)
        assert usage.account_id == ACCOUNT
        assert usage.identity_verified
        assert start <= usage.observed_at <= datetime.now(timezone.utc)
        assert usage.observed_at.utcoffset() == timezone.utc.utcoffset(None)
        assert usage.plan_type == "team"
        assert usage.ordinary_usage_allowed is True
        assert usage.reset_credits_available == 2
        assert usage.limits == (
            CodexPlanLimit(
                limit_id="codex",
                limit_name=None,
                normal_model_slug=None,
                primary=CodexPlanWindow(
                    used_percent=100,
                    window_minutes=300,
                    resets_at=datetime.fromtimestamp(2000000000, timezone.utc),
                ),
                secondary=CodexPlanWindow(
                    used_percent=5,
                    window_minutes=10080,
                    resets_at=datetime.fromtimestamp(2000000100, timezone.utc),
                ),
                credits=CodexPlanCredits(has_credits=True, unlimited=False, balance="12.50"),
                spend_control_reached=False,
                rate_limit_reached_type=None,
                individual_limit=CodexPlanSpendLimit(
                    limit="25000",
                    used="8000",
                    remaining_percent=68,
                    resets_at=datetime.fromtimestamp(2000000300, timezone.utc),
                ),
            ),
            CodexPlanLimit(
                limit_id="extra-codex",
                limit_name="Extra Codex",
                normal_model_slug="extra-model",
                primary=CodexPlanWindow(
                    used_percent=88,
                    window_minutes=60,
                    resets_at=datetime.fromtimestamp(2000000200, timezone.utc),
                ),
                secondary=None,
                credits=None,
                spend_control_reached=None,
                rate_limit_reached_type=None,
            ),
        )

        partial = await session.plan_usage()
        assert partial is not None
        assert partial.account_id == ACCOUNT
        assert not partial.identity_verified
        assert partial.observed_at >= usage.observed_at
        assert partial.ordinary_usage_allowed is None
        assert partial.reset_credits_available is None
        assert len(partial.limits) == 1
        assert partial.limits[0].primary is None
        assert partial.limits[0].secondary is None
        assert partial.limits[0].credits is None
        assert partial.limits[0].spend_control_reached is None
        assert partial.limits[0].individual_limit is None
        assert session.active_turn is None
        assert session.rate_limits() == ApiRateLimits()

    assert requests == [("/backend-api/wham/usage", "Bearer saved-token", ACCOUNT)] * 2
    assert transports[0] is not None
    assert transports[0] is transports[1]


async def test_plan_usage_without_saved_workspace_id(plan_endpoint, tmp_path):
    replies, requests, _, _ = plan_endpoint
    replies.append(
        web.json_response(
            {
                "account_id": ACCOUNT,
                "user_id": USER,
                "plan_type": "pro",
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": _window(9, 18000, 2000000000),
                },
            }
        )
    )
    home = saved_login(tmp_path / "login", account=None)
    async with timeout(30), codex_agent(home).session() as session:
        usage = await session.plan_usage()
        assert usage is not None
        assert usage.account_id is None
        assert not usage.identity_verified
        assert usage.ordinary_usage_allowed is None
        assert usage.limits[0].primary == CodexPlanWindow(
            used_percent=9,
            window_minutes=300,
            resets_at=datetime.fromtimestamp(2000000000, timezone.utc),
        )
    assert requests == [("/backend-api/wham/usage", "Bearer saved-token", None)]


@pytest.mark.parametrize("field", ["account_id", "user_id"])
async def test_plan_usage_rejects_conflicting_identity(plan_endpoint, tmp_path, field):
    replies, requests, _, _ = plan_endpoint
    replies.append(
        web.json_response(
            {"plan_type": "team", "account_id": ACCOUNT, "user_id": USER}
            | {field: "other-synthetic-identity"}
        )
    )
    async with timeout(30), codex_agent(saved_login(tmp_path / "login")).session() as session:
        with pytest.raises(RuntimeError, match="did not match the Session's saved login") as caught:
            await session.plan_usage()
        assert "other-synthetic-identity" not in str(caught.value)
    assert len(requests) == 1


async def test_plan_usage_does_not_follow_redirects_or_expose_failure_body(plan_endpoint, tmp_path):
    replies, requests, _, _ = plan_endpoint
    replies.append(
        web.Response(
            status=302,
            headers={"Location": "/unexpected"},
            text="synthetic-private-error",
        )
    )
    async with timeout(30), codex_agent(saved_login(tmp_path / "login")).session() as session:
        with pytest.raises(RuntimeError, match="^Unable to read Codex plan usage$") as caught:
            await session.plan_usage()
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
    assert requests == [("/backend-api/wham/usage", "Bearer saved-token", ACCOUNT)]


async def test_plan_usage_rejects_a_managed_account_endpoint(plan_endpoint, monkeypatch, tmp_path):
    _, requests, _, _ = plan_endpoint
    unexpected_requests = []

    async def unexpected(request):
        unexpected_requests.append(request.path)
        return web.Response(status=500)

    app = web.Application()
    app.router.add_get("/backend-api/wham/usage", unexpected)
    async with TestServer(app) as managed:
        requirements = tmp_path / "requirements.toml"
        requirements.write_text(f'chatgpt_base_url = "{managed.make_url("/backend-api")}"\n')
        native = importlib.import_module("agents._native")
        create_engine = native.create_engine

        def create(config, runtime, **kwargs):
            runtime = json.loads(runtime)
            runtime.setdefault("loader", {}).update(
                ignore_managed_requirements=False,
                system_requirements_path=str(requirements),
            )
            return create_engine(config, json.dumps(runtime), **kwargs)

        monkeypatch.setattr(native, "create_engine", create)
        async with timeout(30), codex_agent(saved_login(tmp_path / "login")).session() as session:
            with pytest.raises(RuntimeError, match="requires the first-party account endpoint"):
                await session.plan_usage()

    assert requests == unexpected_requests == []


async def test_expired_access_token_requires_login_renewal(plan_endpoint, tmp_path):
    replies, requests, _, _ = plan_endpoint
    replies.append(web.json_response({"error": {"code": "token_expired"}}, status=401))
    home = saved_login(tmp_path / "login", token="expired-synthetic-token")
    auth_file = home / "auth.json"
    original_login = auth_file.read_bytes()

    async with timeout(30), codex_agent(home).session() as session:
        with pytest.raises(RuntimeError, match="^Unable to read Codex plan usage$"):
            await session.plan_usage()
        assert session.active_turn is None

    assert requests == [("/backend-api/wham/usage", "Bearer expired-synthetic-token", ACCOUNT)]
    assert auth_file.read_bytes() == original_login


async def test_closing_session_retires_in_flight_plan_usage(plan_endpoint, tmp_path):
    replies, requests, _, _ = plan_endpoint
    requested = asyncio.Event()
    release = asyncio.Event()

    async def respond():
        if len(requests) == 2:
            requested.set()
        await release.wait()
        return web.json_response({"plan_type": "team", "account_id": ACCOUNT, "user_id": USER})

    replies.extend([respond, respond])
    async with timeout(30), codex_agent(saved_login(tmp_path / "login")).session() as session:
        reads = [asyncio.create_task(session.plan_usage()) for _ in range(2)]
        try:
            await requested.wait()
            await session.aclose()
            for read in reads:
                assert read.done()
                with pytest.raises(asyncio.CancelledError):
                    await read
            with pytest.raises(RuntimeError, match="Session is closed"):
                await session.plan_usage()
        finally:
            release.set()


async def test_plan_usage_cancelled_before_poll_does_not_send_get(
    plan_endpoint, monkeypatch, tmp_path
):
    replies, requests, _, _ = plan_endpoint
    payload = {"plan_type": "team", "account_id": ACCOUNT, "user_id": USER}
    replies.extend(web.json_response(payload) for _ in range(3))
    bridge = importlib.import_module("agents._native_async")
    await_completion = bridge._await_native_completion

    async def cancel_before_poll(call):
        # Signal native cancellation before the bridge can start the request.
        call.cancel()
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return await await_completion(call)

    async with timeout(30), codex_agent(saved_login(tmp_path / "login")).session() as session:
        assert await session.plan_usage() is not None
        with monkeypatch.context() as patch:
            patch.setattr(bridge, "_await_native_completion", cancel_before_poll)
            with pytest.raises(asyncio.CancelledError):
                await session.plan_usage()
        assert await session.plan_usage() is not None

    assert requests == [("/backend-api/wham/usage", "Bearer saved-token", ACCOUNT)] * 2


async def test_already_cancelled_plan_usage_does_not_start_in_eager_task(
    plan_endpoint, monkeypatch, tmp_path
):
    eager_factory = getattr(asyncio, "eager_task_factory", None)
    if eager_factory is None:
        pytest.skip("asyncio.eager_task_factory requires Python 3.12 or later")
    replies, requests, _, _ = plan_endpoint
    payload = {"plan_type": "team", "account_id": ACCOUNT, "user_id": USER}
    replies.extend(web.json_response(payload) for _ in range(3))
    bridge = importlib.import_module("agents._native_async")
    await_completion = bridge._await_native_completion
    started = []

    async def observe_start(call):
        started.append(True)
        return await await_completion(call)

    async with timeout(30), codex_agent(saved_login(tmp_path / "login")).session() as session:
        assert await session.plan_usage() is not None

        async def cancel_then_read():
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await session.plan_usage()

        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        with monkeypatch.context() as patch:
            patch.setattr(bridge, "_await_native_completion", observe_start)
            loop.set_task_factory(eager_factory)
            try:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.create_task(cancel_then_read())
            finally:
                loop.set_task_factory(previous_factory)

        assert not started
        assert await session.plan_usage() is not None

    assert requests == [("/backend-api/wham/usage", "Bearer saved-token", ACCOUNT)] * 2


async def test_plan_usage_is_unavailable_with_api_key(plan_endpoint, monkeypatch, tmp_path):
    _, requests, model_url, _ = plan_endpoint
    monkeypatch.setenv("CODEX_HOME", str(saved_login(tmp_path / "login")))
    agent = Agent(
        instructions="Test API-key plan usage.",
        provider=NativeProvider(api_key="synthetic-key", base_url=model_url),
    )
    native = importlib.import_module("agents._native")
    begin = native.NativeThread._begin_context_change
    entered, release = asyncio.Event(), asyncio.Event()
    retiring, finish_retirement = asyncio.Event(), asyncio.Event()

    async def hold(thread):
        change = await begin(thread)
        retire = type(change).retire

        async def hold_retirement(change):
            retiring.set()
            await finish_retirement.wait()
            await retire(change)

        monkeypatch.setattr(type(change), "retire", hold_retirement)
        entered.set()
        await release.wait()
        return change

    monkeypatch.setattr(native.NativeThread, "_begin_context_change", hold)
    async with timeout(30), agent.session() as session:
        assert await session.plan_usage() is None
        native_read = type(session._engine)._read_codex_plan_usage

        async def delay_native_result(engine, base_url):
            result = await native_read(engine, base_url)
            await retiring.wait()
            return result

        monkeypatch.setattr(type(session._engine), "_read_codex_plan_usage", delay_native_result)
        admitted = asyncio.Event()

        async def read_plan():
            admitted.set()
            return await session.plan_usage()

        compaction = asyncio.create_task(session.compact(summary="Keep this context."))
        try:
            await entered.wait()
            read = asyncio.create_task(read_plan())
            await admitted.wait()
            release.set()
            await retiring.wait()
            assert await read is None
        finally:
            release.set()
            finish_retirement.set()
            await compaction
        assert await session.plan_usage() is None
    assert requests == []


def test_plan_usage_types_are_available_through_public_imports():
    assert rate_limits.CodexPlanUsage is CodexPlanUsage
    assert rate_limits.CodexPlanLimit is CodexPlanLimit
    assert rate_limits.CodexPlanWindow is CodexPlanWindow
    assert rate_limits.CodexPlanCredits is CodexPlanCredits
    assert rate_limits.CodexPlanSpendLimit is CodexPlanSpendLimit
