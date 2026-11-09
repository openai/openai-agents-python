"""Native computer adapters execute effects under the Session callback owner."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from _agents_codex.computer import (
    ComputerProvider,
    ComputerSafetyError,
    _ComputerCall,
    _ComputerResources,
    computer_tool,
)


class Driver:
    def __init__(self):
        self.calls = []

    def screenshot(self):
        self.calls.append(("screenshot",))
        return "aA=="

    def __getattr__(self, name):
        def action(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return action


async def invoke(tool, resources, call, admit=lambda: True):
    captured = tool._capture(None)
    return json.loads(
        await captured.invoke(
            resources,
            admit,
            SimpleNamespace(context=None),
            _ComputerCall.model_validate({"type": "computer_call", "call_id": "c1", **call}),
        )
    )


@pytest.mark.asyncio
async def test_native_declaration_all_actions_and_terminal_screenshot_reuse():
    driver = Driver()
    tool = computer_tool(driver)
    assert json.loads(tool._spec_json()) == {"type": "computer", "toolType": "computer"}
    actions = [
        {"type": "click", "x": 1, "y": 2, "button": "left", "keys": ["SHIFT"]},
        {"type": "double_click", "x": 1, "y": 2},
        {"type": "scroll", "x": 1, "y": 2, "scroll_x": 3, "scroll_y": 4},
        {"type": "type", "text": "hello"},
        {"type": "wait"},
        {"type": "move", "x": 4, "y": 5},
        {"type": "keypress", "keys": ["CTRL", "C"]},
        {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]},
        {"type": "screenshot"},
    ]
    output = await invoke(tool, _ComputerResources(), {"actions": actions})
    assert [call[0] for call in driver.calls] == [action["type"] for action in actions]
    assert driver.calls[0][2]["keys"] == ["SHIFT"]
    assert driver.calls[-2][1] == ([(1, 2), (3, 4)],)
    assert output["output"] == {
        "type": "computer_screenshot",
        "image_url": "data:image/png;base64,aA==",
    }


@pytest.mark.asyncio
async def test_single_action_precedence_and_screenshot_only():
    driver = Driver()
    tool = computer_tool(driver)
    resources = _ComputerResources()
    await invoke(tool, resources, {"action": {"type": "type", "text": "legacy"}})
    await invoke(tool, resources, {"action": {"type": "wait"}, "actions": [{"type": "screenshot"}]})
    await invoke(tool, resources, {"actions": []})
    assert [call[0] for call in driver.calls] == ["type", "screenshot", "screenshot", "screenshot"]
    await resources.close()
    assert all(call[0] != "close" for call in driver.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, lambda *_: False, lambda *_: 1])
async def test_pending_safety_fails_closed_before_acquisition(policy):
    calls = []
    tool = computer_tool(ComputerProvider(lambda _: calls.append("create")), on_safety_check=policy)
    with pytest.raises(ComputerSafetyError):
        await invoke(
            tool,
            _ComputerResources(),
            {"pending_safety_checks": [{"id": "s1", "message": "confirm"}]},
        )
    assert calls == []


@pytest.mark.asyncio
async def test_policy_cannot_rewrite_original_acknowledgments():
    check = {"id": "s1", "code": "domain", "message": "confirm", "extra": {"keep": True}}

    def policy(_, checks):
        checks[0]["id"] = "rewritten"
        checks[0]["extra"]["keep"] = False
        return True

    result = await invoke(
        computer_tool(Driver(), on_safety_check=policy),
        _ComputerResources(),
        {"pending_safety_checks": [check]},
    )
    assert result["acknowledged_safety_checks"] == [check]


@pytest.mark.asyncio
async def test_acquired_driver_reused_per_session_and_disposed_once():
    acquired, disposed = [], []

    def create(_):
        driver = Driver()
        acquired.append(driver)
        return driver

    tool = computer_tool(ComputerProvider(create, disposed.append))
    first, second = _ComputerResources(), _ComputerResources()
    await asyncio.gather(invoke(tool, first, {}), invoke(tool, first, {}))
    await invoke(tool, second, {})
    assert len(acquired) == 2
    await first.close()
    await first.close()
    await second.close()
    assert disposed == acquired
    assert [call[0] for call in acquired[0].calls] == ["screenshot", "screenshot"]


@pytest.mark.asyncio
async def test_invalid_later_action_has_no_partial_device_effects():
    driver = Driver()
    with pytest.raises(ValueError):
        await invoke(
            computer_tool(driver),
            _ComputerResources(),
            {"actions": [{"type": "wait"}, {"type": "unknown"}]},
        )
    assert driver.calls == []


@pytest.mark.asyncio
async def test_cancelled_sync_acquisition_retains_driver_until_disposal():
    entered, release = threading.Event(), threading.Event()
    driver, disposed = Driver(), []

    def create(_):
        entered.set()
        release.wait(5)
        return driver

    resources = _ComputerResources()
    pending = asyncio.create_task(
        invoke(computer_tool(ComputerProvider(create, disposed.append)), resources, {})
    )
    await asyncio.to_thread(entered.wait, 5)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await resources.close()
    assert disposed == [driver]
    assert driver.calls == []


@pytest.mark.asyncio
async def test_approval_observes_detached_actions_and_denies_before_acquisition():
    created = []

    def approve(_context, actions):
        assert actions == [{"type": "wait"}]
        actions.clear()
        return False

    tool = computer_tool(ComputerProvider(lambda _: created.append("create")), approve=approve)
    with pytest.raises(ComputerSafetyError):
        await invoke(tool, _ComputerResources(), {"action": {"type": "wait"}})
    assert created == []


@pytest.mark.asyncio
async def test_invalid_explicit_screenshot_is_not_retried():
    driver = Driver()
    calls = []
    driver.screenshot = lambda: calls.append("screenshot")
    with pytest.raises(TypeError):
        await invoke(
            computer_tool(driver), _ComputerResources(), {"action": {"type": "screenshot"}}
        )
    assert calls == ["screenshot"]


@pytest.mark.asyncio
async def test_async_driver_failure_disposes_acquired_driver_once():
    class AsyncDriver(Driver):
        async def wait(self):
            self.calls.append(("wait",))
            raise RuntimeError("device failed")

    driver, disposed = AsyncDriver(), []

    async def create(_):
        return driver

    async def dispose(value):
        disposed.append(value)

    resources = _ComputerResources()
    with pytest.raises(RuntimeError, match="device failed"):
        await invoke(
            computer_tool(ComputerProvider(create, dispose)),
            resources,
            {"action": {"type": "wait"}},
        )
    assert driver.calls == [("wait",)]
    await resources.close()
    await resources.close()
    assert disposed == [driver]


@pytest.mark.asyncio
@pytest.mark.parametrize("keys", [None, [], ["SHIFT"]])
async def test_mouse_modifiers_allow_positional_only_and_older_drivers(keys):
    class PositionalDriver(Driver):
        def click(self, x, y, button, /):
            self.calls.append(("click", x, y, button))

    driver = PositionalDriver()
    await invoke(
        computer_tool(driver),
        _ComputerResources(),
        {"action": {"type": "click", "x": 1, "y": 2, "button": "left", "keys": keys}},
    )
    assert driver.calls == [("click", 1, 2, "left"), ("screenshot",)]


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_cancelled_sync_factory_joins_issued_future_before_disposal(completed, wrapped):
    driver, disposed = Driver(), []
    entered, release = threading.Event(), threading.Event()
    future = asyncio.get_running_loop().create_future()
    if completed:
        future.set_result(driver)

    class IssuedAcquisition:
        def __await__(self):
            return future.__await__()

    def create(_):
        entered.set()
        assert release.wait(5)
        return IssuedAcquisition() if wrapped else future

    resources = _ComputerResources()
    task = asyncio.create_task(
        invoke(
            computer_tool(ComputerProvider(create, disposed.append)),
            resources,
            {"actions": [{"type": "wait"}]},
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    if not completed:
        await asyncio.sleep(0)
        assert not task.done()
        future.set_result(driver)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert disposed == []
    assert driver.calls == []
    await resources.close()
    await resources.close()
    assert disposed == [driver]


async def test_cancelled_entered_custom_awaitable_is_joined_once_before_disposal():
    driver, disposed, awaits = Driver(), [], []
    entered = asyncio.Event()
    future = asyncio.get_running_loop().create_future()

    class IssuedAcquisition:
        def __await__(self):
            awaits.append(True)
            entered.set()
            return future.__await__()

    resources = _ComputerResources()
    task = asyncio.create_task(
        invoke(
            computer_tool(ComputerProvider(lambda _: IssuedAcquisition(), disposed.append)),
            resources,
            {"actions": [{"type": "wait"}]},
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    future.set_result(driver)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert awaits == [True]
    assert disposed == driver.calls == []
    await resources.close()
    await resources.close()
    assert disposed == [driver]


async def test_cancelled_sync_factory_does_not_start_unentered_coroutine():
    entered, release = threading.Event(), threading.Event()
    started, disposed = [], []

    async def acquire():
        started.append(True)
        return Driver()

    def create(_):
        entered.set()
        assert release.wait(5)
        return acquire()

    resources = _ComputerResources()
    task = asyncio.create_task(
        invoke(
            computer_tool(ComputerProvider(create, disposed.append)),
            resources,
            {"actions": [{"type": "wait"}]},
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    await resources.close()
    assert started == disposed == []


@pytest.mark.parametrize("method", ["wait", "screenshot"])
async def test_cancelled_sync_driver_joins_issued_task_before_disposal(method):
    entered, returning, release = threading.Event(), threading.Event(), threading.Event()
    operation_release = asyncio.Event()
    loop = asyncio.get_running_loop()
    order = []

    async def operation():
        await operation_release.wait()
        order.append("operation finished")
        return "aA==" if method == "screenshot" else None

    async def issue():
        return asyncio.create_task(operation())

    def callback():
        pending = asyncio.run_coroutine_threadsafe(issue(), loop).result(5)
        entered.set()
        assert release.wait(5)
        returning.set()
        return pending

    driver = Driver()
    setattr(driver, method, callback)
    resources = _ComputerResources()
    tool = computer_tool(ComputerProvider(lambda _: driver, lambda _: order.append("disposed")))
    task = asyncio.create_task(invoke(tool, resources, {"actions": [{"type": method}]}))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.to_thread(returning.wait, 5)

    async def cleanup():
        with pytest.raises(asyncio.CancelledError):
            await task
        await resources.close()

    closing = asyncio.create_task(cleanup())
    await asyncio.sleep(0.05)
    assert order == []
    task.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    operation_release.set()
    await asyncio.wait_for(closing, 5)
    assert order == ["operation finished", "disposed"]


async def test_cancelled_sync_driver_does_not_start_unentered_coroutine():
    entered, release = threading.Event(), threading.Event()
    effects = []

    async def deferred():
        effects.append("must not run")

    def wait():
        entered.set()
        assert release.wait(5)
        return deferred()

    driver = Driver()
    driver.wait = wait
    resources = _ComputerResources()
    task = asyncio.create_task(
        invoke(computer_tool(driver), resources, {"actions": [{"type": "wait"}]})
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    await resources.close()
    assert effects == driver.calls == []


async def test_borrowed_device_gate_does_not_retain_a_released_device():
    import gc
    import weakref

    driver = Driver()
    reference = weakref.ref(driver)
    tool = computer_tool(driver)
    resources = _ComputerResources()
    await invoke(tool, resources, {"action": {"type": "wait"}})
    await resources.close()
    del driver, tool, resources
    gc.collect()
    assert reference() is None
