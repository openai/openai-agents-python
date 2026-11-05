"""Exercise worker supervision with the native SDK and synthetic account facts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from agents import (
    ActionRequested,
    Agent,
    Completed,
    FunctionTool,
    LocalStorage,
    NativeError,
    NativeProvider,
    PendingRequests,
    SteeringReceipt,
    Turn,
    TurnNeedsAttention,
    UserInput,
    _native,
    function_tool,
)
from agents.rate_limits import ApiRateLimits, ApiRateLimitWindow
from examples.supervised_worker import __main__ as worker, quota
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_provider_errors import ProviderErrorPeer, provider_peer as provider_peer

NO_CREDITS: quota.Credits = {"hasCredits": False, "unlimited": False, "balance": "0.00"}
USABLE_CREDITS: quota.Credits = {"hasCredits": True, "unlimited": False, "balance": "2.50"}


class Feed:
    """A synthetic application observation clock and raw read payloads."""

    now = 1_790_000_000.0

    def stamp(self, binding: quota.AccountBinding, ttl: float = 20) -> quota.Observation:
        self.now += 1
        return quota.Observation(binding, self.now, self.now + ttl)

    def read(
        self,
        watch: quota.QuotaWatch,
        binding: quota.AccountBinding,
        credits: quota.Credits | None,
        *,
        ttl: float = 20,
    ) -> None:
        watch.observe_read(self.stamp(binding, ttl), raw_read(binding.account_id, credits))


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> Feed:
    clock = Feed()
    monkeypatch.setattr(quota, "time", lambda: clock.now)
    return clock


def raw_read(account_id: str | None, credits: quota.Credits | None) -> quota.AccountRead:
    return {
        "accountId": account_id,
        "ordinaryUsageAllowed": False,
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": None},
            "credits": credits,
        },
    }


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(
        base_url=responses.url, api_key="synthetic-worker", supports_websockets=False
    )


@asynccontextmanager
async def hold_final_text(
    responses: ResponsesFixture,
) -> AsyncIterator[tuple[asyncio.Event, asyncio.Event]]:
    entered, release = asyncio.Event(), asyncio.Event()

    async def before(event: dict[str, Any]) -> None:
        if event.get("item", {}).get("type") == "message":
            entered.set()
            await release.wait()

    responses.before_event = before
    try:
        yield entered, release
    finally:
        release.set()
        responses.before_event = None


async def assert_waiting(task: asyncio.Task[Any]) -> None:
    # The local peer has confirmed that it is holding the turn open.
    done, _ = await asyncio.wait((task,), timeout=0.02)
    assert not done


def trace() -> worker.TurnTrace:
    return worker.TurnTrace(monotonic())


def test_account_view_preserves_raw_values_and_individual_expiry(feed: Feed) -> None:
    watch = quota.QuotaWatch()
    assert watch.account_view() is None
    for missing_account in ("", " \t"):
        with pytest.raises(ValueError, match="account_id"):
            watch.attach(missing_account)
    binding = watch.attach("account-a")
    primary: quota.Window = {
        "usedPercent": 100,
        "windowDurationMins": 300,
        "resetsAt": 1_790_001_800,
    }
    secondary: quota.Window = {"usedPercent": 63, "windowDurationMins": None, "resetsAt": None}
    raw = raw_read(binding.account_id, USABLE_CREDITS)
    raw["rateLimits"]["primary"] = primary
    raw["rateLimits"]["secondary"] = secondary
    raw["rateLimitsByLimitId"] = {"another-bucket": {"primary": {"usedPercent": 12}}}
    reading = feed.stamp(binding, ttl=3)
    watch.observe_read(reading, raw)
    original = watch.account_view()
    assert original is not None and original.read is not None
    assert original.binding == binding
    assert original.read.source == "read"
    assert original.read.is_current and original.read.value == raw
    assert original.rate_limits["secondary"].value == secondary
    assert original.rate_limits["primary"].value == primary
    assert original.rate_limits["primary"].observation == reading

    update = feed.stamp(binding)
    watch.observe_update(update, {"rateLimits": {"credits": NO_CREDITS, "primary": None}})
    feed.now += 3
    merged = watch.account_view()
    assert merged is not None and merged.read is not None
    assert not merged.read.is_current
    assert merged.read.value["rateLimitsByLimitId"] == raw["rateLimitsByLimitId"]
    assert not merged.rate_limits["primary"].is_current
    assert merged.rate_limits["primary"].observation == reading
    assert merged.rate_limits["credits"].is_current
    assert merged.rate_limits["credits"].source == "update"
    assert merged.rate_limits["credits"].observation == update
    assert merged.rate_limits["credits"].value == NO_CREDITS
    assert "spendControlReached" not in merged.rate_limits

    del raw["rateLimits"]["primary"]
    del merged.read.value["rateLimits"]["primary"]
    retained = watch.account_view()
    assert retained is not None and retained.read is not None
    assert retained.read.value["rateLimits"]["primary"] == primary

    replacement = watch.attach(binding.account_id)
    watch.observe_update(feed.stamp(binding), {"rateLimits": {"credits": NO_CREDITS}})
    cleared = watch.account_view()
    assert cleared is not None and cleared.binding == replacement
    assert cleared.read is None and not cleared.rate_limits


async def test_tool_progress_and_sparse_api_headers_do_not_wake_worker(
    responses: ResponsesFixture, tmp_path: Path, feed: Feed
) -> None:
    @function_tool
    async def lookup() -> str:
        return "native callback result"

    responses.respond(
        {
            "type": "response.in_progress",
            "response": {"headers": {"X-RateLimit-Remaining-Requests": "0"}},
        },
        function_call("lookup", {}, "call-lookup"),
    )
    responses.respond(assistant_output("done"))
    watch = quota.QuotaWatch()
    binding = watch.attach("account-a")
    # Neither rounded usage with missing credits nor explicit usable credits blocks.
    feed.read(watch, binding, None)
    connection = provider(responses)
    agent = Agent(instructions="Look up a value.", tools=[lookup], provider=connection)
    async with (
        timeout(15),
        connection,
        agent.session() as session,
        hold_final_text(responses) as (entered, release),
    ):
        turn = await session.submit("Look up a value.")
        report = trace()
        task = asyncio.create_task(
            worker.supervise(worker.Worker(tmp_path, False), turn, report, timeout=12, quota=watch)
        )
        try:
            await entered.wait()
            assert tool_output(responses.requests[1], "call-lookup") == "native callback result"
            assert session.rate_limits() == ApiRateLimits(requests=ApiRateLimitWindow(remaining=0))
            await assert_waiting(task)
            watch.observe_read(
                feed.stamp(binding),
                {**raw_read(binding.account_id, NO_CREDITS), "ordinaryUsageAllowed": None},
            )
            await assert_waiting(task)
            feed.read(watch, binding, USABLE_CREDITS)
            await assert_waiting(task)
            feed.read(watch, binding, {"hasCredits": False, "unlimited": True})
            await assert_waiting(task)
        finally:
            release.set()
        result = await task
        assert isinstance(result, Completed) and result.final_output == "done"
        assert report.first_tool_result_ms is not None


async def test_settled_native_result_wins_without_an_earlier_result_wait(
    responses: ResponsesFixture, feed: Feed
) -> None:
    responses.respond(assistant_output("done"))
    watch = quota.QuotaWatch()
    feed.read(watch, watch.attach("account-a"), NO_CREDITS)
    connection = provider(responses)
    agent = Agent(instructions="Reply.", provider=connection)
    async with timeout(15), connection, agent.session() as session:
        turn = await session.submit("Reply.")
        async for _ in turn.events():
            pass
        result = await worker.wait_for_result_with_quota(turn, trace(), watch)
        assert isinstance(result, Completed) and result.final_output == "done"


async def test_current_read_and_sparse_credit_update_can_wake_an_active_turn(
    responses: ResponsesFixture, tmp_path: Path, feed: Feed
) -> None:
    responses.respond(assistant_output("still ran"))
    watch = quota.QuotaWatch()
    binding = watch.attach("account-a")
    feed.read(watch, binding, USABLE_CREDITS, ttl=3)
    connection = provider(responses)
    agent = Agent(instructions="Reply.", provider=connection)
    async with (
        timeout(15),
        connection,
        agent.session() as session,
        hold_final_text(responses) as (entered, release),
    ):
        turn = await session.submit("Reply.")
        task = asyncio.create_task(
            worker.supervise(worker.Worker(tmp_path, False), turn, trace(), timeout=12, quota=watch)
        )
        await entered.wait()
        feed.read(watch, binding, {"hasCredits": False}, ttl=3)
        await assert_waiting(task)

        # The update has fresh credits but the read's permission is now expired.
        feed.now += 3
        watch.observe_update(feed.stamp(binding), {"rateLimits": {"credits": NO_CREDITS}})
        await assert_waiting(task)
        feed.read(watch, binding, USABLE_CREDITS)
        watch.observe_update(
            feed.stamp(binding),
            {"rateLimits": {"limitId": "another-bucket", "credits": NO_CREDITS}},
        )
        await assert_waiting(task)
        # The display balance does not override the service's usability flags.
        watch.observe_update(
            feed.stamp(binding), {"rateLimits": {"credits": {**NO_CREDITS, "balance": "1.25"}}}
        )
        watch.observe_update(
            feed.stamp(binding), {"rateLimits": {"credits": None, "spendControlReached": None}}
        )
        result = await task
        assert result == quota.AccountQuotaAttention(binding, "ordinaryUsageAllowed", False)
        assert result.turn_may_be_active
        release.set()
        assert (await turn.get_result()).final_output == "still ran"


@pytest.mark.parametrize("during_steering", [False, True], ids=["before-brief", "during-steering"])
async def test_worker_continues_brief_after_quota_handoff(
    responses: ResponsesFixture,
    tmp_path: Path,
    feed: Feed,
    monkeypatch: pytest.MonkeyPatch,
    during_steering: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    if during_steering:
        steer_exact = Turn.steer_exact

        async def hold_receipt(
            turn: Turn[Any], input: UserInput, *, expected_turn_id: str
        ) -> SteeringReceipt:
            receipt = await steer_exact(turn, input, expected_turn_id=expected_turn_id)
            if input == worker.CORRECTIONS[0]:
                entered.set()
                await release.wait()
            return receipt

        monkeypatch.setattr(Turn, "steer_exact", hold_receipt)
    else:

        async def hold_tool_call(event: dict[str, Any]) -> None:
            if event.get("item", {}).get("call_id") == "call-brief":
                entered.set()
                await release.wait()

        responses.before_event = hold_tool_call

    responses.respond(function_call("brief", {}, "call-brief"))
    responses.respond(assistant_output("done"))
    watch = quota.QuotaWatch()
    binding = watch.attach("account-a")
    active_worker = worker.Worker(tmp_path, False)
    connection = provider(responses)
    agent = Agent(instructions="Call brief and reply.", tools=[worker.brief], provider=connection)
    async with timeout(15), connection, agent.session(context=active_worker) as session:
        turn = await session.submit("Call brief and reply.")
        report = trace()
        waiting = asyncio.create_task(
            worker.supervise(active_worker, turn, report, timeout=12, quota=watch)
        )
        try:
            await entered.wait()
            feed.read(watch, binding, NO_CREDITS)
            assert isinstance(await waiting, quota.AccountQuotaAttention)
            assert not active_worker.brief_release.is_set()
            feed.read(watch, binding, USABLE_CREDITS)
            release.set()
            result = await worker.wait_for_result_with_quota(turn, report, watch)
            assert isinstance(result, Completed) and result.final_output == "done"
            assert len(report.steering) == len(worker.CORRECTIONS)
            assert tool_output(responses.requests[1], "call-brief") == active_worker.expected_text
            next_request = json.dumps(responses.requests[1])
            assert all(next_request.count(correction) == 1 for correction in worker.CORRECTIONS)
        finally:
            release.set()
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.parametrize("close_session", [False, True], ids=["cancel-turn", "close-session"])
async def test_direct_worker_stop_releases_handed_off_observer(
    responses: ResponsesFixture, tmp_path: Path, feed: Feed, close_session: bool
) -> None:
    responses.respond(assistant_output("held"))
    watch = quota.QuotaWatch()
    feed.read(watch, watch.attach("account-a"), NO_CREDITS)
    active_worker = worker.Worker(tmp_path, False)
    connection = provider(responses)
    agent = Agent(instructions="Reply.", provider=connection)
    report = trace()
    async with (
        timeout(15),
        connection,
        hold_final_text(responses) as (entered, _),
        AsyncExitStack() as sessions,
    ):
        session = await sessions.enter_async_context(agent.session())
        turn = await session.submit("Reply.")
        await entered.wait()
        outcome = await worker.supervise(active_worker, turn, report, timeout=10, quota=watch)
        assert isinstance(outcome, quota.AccountQuotaAttention)
        attachment = report._observation
        assert attachment is not None and attachment[0] is turn
        observer = attachment[1]
        stopped = asyncio.Event()
        observer.add_done_callback(lambda _: stopped.set())
        if close_session:
            await sessions.aclose()
        else:
            await turn.cancel()
        await stopped.wait()
        observer.result()
        assert report._observation is None


async def test_handed_off_observer_failure_surfaces_on_passive_resume(
    responses: ResponsesFixture,
    tmp_path: Path,
    feed: Feed,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fail = asyncio.Event()
    failure = RuntimeError("synthetic observer failure")

    async def failing_observe(
        _worker: worker.Worker, _turn: Turn[Any], _trace: worker.TurnTrace, *, interrupt: bool
    ) -> None:
        await fail.wait()
        raise failure

    monkeypatch.setattr(worker, "observe", failing_observe)
    responses.respond(assistant_output("held"))
    watch = quota.QuotaWatch()
    feed.read(watch, watch.attach("account-a"), NO_CREDITS)
    connection = provider(responses)
    agent = Agent(instructions="Reply.", provider=connection)
    async with (
        timeout(15),
        connection,
        agent.session() as session,
        hold_final_text(responses) as (entered, _),
    ):
        turn = await session.submit("Reply.")
        await entered.wait()
        report = trace()
        outcome = await worker.supervise(
            worker.Worker(tmp_path, False), turn, report, timeout=10, quota=watch
        )
        assert isinstance(outcome, quota.AccountQuotaAttention)
        attachment = report._observation
        assert attachment is not None
        stopped = asyncio.Event()
        attachment[1].add_done_callback(lambda _: stopped.set())
        fail.set()
        await stopped.wait()
        assert "The handed-off worker observer failed (RuntimeError)." in caplog.messages
        with pytest.raises(RuntimeError) as caught:
            await worker.wait_for_result_with_quota(turn, report, watch)
        assert caught.value is failure


@pytest.mark.parametrize(
    ("snapshot", "field", "value"),
    [
        ({"spendControlReached": True}, "spendControlReached", True),
        (
            {"rateLimitReachedType": "workspace_member_usage_limit_reached"},
            "rateLimitReachedType",
            "workspace_member_usage_limit_reached",
        ),
    ],
)
async def test_explicit_service_block_survives_a_sparse_update(
    responses: ResponsesFixture,
    feed: Feed,
    snapshot: quota.RateLimitSnapshot,
    field: str,
    value: bool | str,
) -> None:
    responses.respond(assistant_output("done"))
    watch = quota.QuotaWatch()
    binding = watch.attach("account-a")
    watch.observe_read(
        feed.stamp(binding),
        {
            "accountId": binding.account_id,
            "ordinaryUsageAllowed": True,
            "rateLimits": {"credits": USABLE_CREDITS},
        },
    )
    connection = provider(responses)
    agent = Agent(instructions="Reply.", provider=connection)
    async with (
        timeout(15),
        connection,
        agent.session() as session,
        hold_final_text(responses) as (entered, release),
    ):
        turn = await session.submit("Reply.")
        task = asyncio.create_task(worker.wait_for_result_with_quota(turn, trace(), watch))
        await entered.wait()
        watch.observe_update(feed.stamp(binding), {"rateLimits": snapshot})
        watch.observe_update(
            feed.stamp(binding),
            {"rateLimits": {"spendControlReached": None, "rateLimitReachedType": None}},
        )
        result = await task
        assert isinstance(result, quota.AccountQuotaAttention)
        assert (result.field, result.value) == (field, value)
        release.set()
        assert (await turn.get_result()).final_output == "done"


async def test_passive_cancel_and_reattach_fence_outdated_account_observations(
    responses: ResponsesFixture, feed: Feed
) -> None:
    responses.respond(assistant_output("done"))
    watch = quota.QuotaWatch()
    first = watch.attach("account-a")
    feed.read(watch, first, USABLE_CREDITS)
    connection = provider(responses)
    agent = Agent(instructions="Reply.", provider=connection)
    async with (
        timeout(15),
        connection,
        agent.session() as session,
        hold_final_text(responses) as (entered, release),
    ):
        turn = await session.submit("Reply.")
        initial = asyncio.create_task(worker.wait_for_result_with_quota(turn, trace(), watch))
        await entered.wait()
        initial.cancel()
        with pytest.raises(asyncio.CancelledError):
            await initial

        current_wait = asyncio.create_task(worker.wait_for_result_with_quota(turn, trace(), watch))
        await assert_waiting(current_wait)
        feed.read(watch, first, NO_CREDITS)
        current = watch.attach("account-b")
        watch.observe_update(feed.stamp(first), {"rateLimits": {"spendControlReached": True}})
        watch.observe_update(feed.stamp(current), {"rateLimits": {"spendControlReached": True}})
        await assert_waiting(current_wait)

        valid = feed.stamp(current)
        watch.observe_read(valid, raw_read(current.account_id, USABLE_CREDITS))
        expired = feed.stamp(current)
        future = replace(feed.stamp(current), observed_at=feed.now + 1)
        for observation, identity in (
            (replace(valid, observed_at=valid.observed_at - 0.5), current.account_id),
            (replace(expired, expires_at=expired.observed_at), current.account_id),
            (future, current.account_id),
        ):
            watch.observe_read(observation, raw_read(identity, NO_CREDITS))
            await assert_waiting(current_wait)
        for other_identity in ("account-a", None):
            watch.observe_read(feed.stamp(current), raw_read(other_identity, NO_CREDITS))
            await assert_waiting(current_wait)

        same_account = watch.attach(current.account_id)
        assert same_account.generation > current.generation > first.generation
        feed.read(watch, same_account, USABLE_CREDITS)
        watch.observe_update(feed.stamp(current), {"rateLimits": {"credits": NO_CREDITS}})
        await assert_waiting(current_wait)
        feed.read(watch, same_account, NO_CREDITS)
        result = await current_wait
        assert result == quota.AccountQuotaAttention(same_account, "ordinaryUsageAllowed", False)
        release.set()
        assert (await turn.get_result()).final_output == "done"
        assert len(responses.requests) == 1


class Question(BaseModel):
    topic: str


async def test_supervise_returns_a_live_native_question(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    ask = FunctionTool(name="ask", description="Ask the app", input_type=Question, output_type=str)
    responses.respond(function_call("ask", {"topic": "color"}, "call-question"))
    responses.respond(assistant_output("answered"))
    watch = quota.QuotaWatch()
    connection = provider(responses)
    agent = Agent(instructions="Ask the application.", tools=[ask], provider=connection)
    async with timeout(15), connection, agent.session() as session:
        turn = await session.submit("Ask a question.")
        report = trace()
        result = await worker.supervise(
            worker.Worker(tmp_path, False), turn, report, timeout=10, quota=watch
        )
        assert isinstance(result, PendingRequests) and result.turn is turn
        (request,) = result.requests
        assert isinstance(request, ActionRequested)
        assert request.call_id == "call-question" and request.arguments.topic == "color"
        await request.respond("blue")
        completed = await worker.wait_for_result_with_quota(turn, report, watch)
        assert isinstance(completed, Completed) and completed.final_output == "answered"
        assert tool_output(responses.requests[1], "call-question") == "blue"


@pytest.mark.parametrize("cancel_owner", [False, True], ids=["interrupt-turn", "cancel-supervisor"])
async def test_opt_in_owner_cancellation_joins_the_native_turn(
    responses: ResponsesFixture, tmp_path: Path, cancel_owner: bool
) -> None:
    responses.respond(assistant_output("held"))
    watch = quota.QuotaWatch()
    connection = provider(responses)
    agent = Agent(instructions="Reply.", provider=connection)
    async with (
        timeout(15),
        connection,
        agent.session() as session,
        hold_final_text(responses) as (entered, _),
    ):
        turn = await session.submit("Reply.")
        task = asyncio.create_task(
            worker.supervise(worker.Worker(tmp_path, False), turn, trace(), timeout=12, quota=watch)
        )
        await entered.wait()
        if cancel_owner:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            stopped = await turn.wait_for_attention()
            assert isinstance(stopped, TurnNeedsAttention) and stopped.reason == "interrupted"
        else:
            await turn.cancel()
            result = await task
            assert isinstance(result, TurnNeedsAttention) and result.reason == "interrupted"


@pytest.mark.parametrize("credits", [USABLE_CREDITS, NO_CREDITS], ids=["usable", "exhausted"])
async def test_native_provider_quota_wins_with_optimistic_or_adverse_account_evidence(
    provider_peer: ProviderErrorPeer, tmp_path: Path, feed: Feed, credits: quota.Credits
) -> None:
    provider_peer.failure = (429, "insufficient_quota", "insufficient_quota")
    watch = quota.QuotaWatch()
    binding = watch.attach("account-a")
    feed.read(watch, binding, credits)
    connection = provider(provider_peer)
    agent = Agent(instructions="Reply.", provider=connection)
    active_worker = worker.Worker(tmp_path, False)
    async with timeout(15), connection, agent.session() as session:
        turn = await session.submit("Reply.")
        if credits == NO_CREDITS:
            async for _ in turn.events():
                pass
        result = await worker.supervise(active_worker, turn, trace(), timeout=12, quota=watch)
        assert isinstance(result, TurnNeedsAttention) and result.reason == "quota_blocked"
        assert isinstance(result.error, NativeError)
        assert result.error.category == "usage_limit_exceeded"

    assert provider_peer.failed_requests == 1


async def test_unhandled_provider_error_survives_observer_eof(
    provider_peer: ProviderErrorPeer, tmp_path: Path
) -> None:
    provider_peer.failure = (401, "invalid_api_key", "invalid_request_error")
    connection = provider(provider_peer)
    agent = Agent(instructions="Reply.", provider=connection)
    async with timeout(15), connection, agent.session() as session:
        turn = await session.submit("Reply.")
        with pytest.raises(NativeError) as caught:
            await worker.supervise(
                worker.Worker(tmp_path, False), turn, trace(), timeout=12, quota=quota.QuotaWatch()
            )
        assert caught.value.category == "authentication_error"


async def test_provider_disconnect_then_explicit_same_id_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_engine = _native.create_engine

    def create_without_retries(config_json: str, *args: Any, **kwargs: Any) -> Any:
        # Change only the local fixture's retry budget. Native code still owns
        # the request, the socket error, and the turn's terminal classification.
        config = json.loads(config_json)
        config["features.unbounded_connection_retries"] = False
        config["model_providers.native_agent"].update(request_max_retries=0, stream_max_retries=0)
        return create_engine(json.dumps(config), *args, **kwargs)

    monkeypatch.setattr(_native, "create_engine", create_without_retries)
    peer = ResponsesFixture()

    def serve() -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/responses", peer.handle)
        return app

    async with timeout(20), TestServer(serve()) as server:
        peer.url = str(server.make_url("/v1"))
        port = server.port
        connection = provider(peer)
        agent = Agent(instructions="Reply.", provider=connection)
        storage = LocalStorage(tmp_path / "history")
        active_worker = worker.Worker(tmp_path, False)
        watch = quota.QuotaWatch()
        original_binding = watch.attach("account-a")
        async with connection:
            async with agent.session(storage=storage) as session:
                original_id = session.id
                peer.respond(assistant_output("before disconnect"))
                first = await session.submit("First request.")
                assert (await first.get_result()).final_output == "before disconnect"
                await server.close()
                turn = await session.submit("Lost request.")
                result = await worker.supervise(
                    active_worker, turn, trace(), timeout=8, quota=watch
                )
                assert isinstance(result, TurnNeedsAttention) and result.reason == "connection_lost"
                assert isinstance(result.error, NativeError)
                assert result.error.category == "connection_failed"

            async with (
                TestServer(serve(), port=port),
                agent.session(storage=storage, resume=original_id) as reopened,
            ):
                assert reopened.id == original_id
                current_binding = watch.attach(original_binding.account_id)
                assert current_binding.generation > original_binding.generation
                assert len(peer.requests) == 1
                peer.respond(assistant_output("after reopen"))
                turn = await reopened.submit("New request after explicit reopen.")
                recovered = await worker.supervise(
                    active_worker, turn, trace(), timeout=8, quota=watch
                )
                assert isinstance(recovered, Completed) and recovered.final_output == "after reopen"
            assert len(peer.requests) == 2
            user_messages = [
                item for item in peer.requests[-1]["input"] if item.get("role") == "user"
            ]
            assert user_messages[-1]["content"] == [
                {"type": "input_text", "text": "New request after explicit reopen."}
            ]
