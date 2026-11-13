"""Private bounded Python trace processor workers and callback ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable as _Callable, Mapping, Sequence
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event, Lock
from time import monotonic
from typing import TYPE_CHECKING, Any

from ._public import preserve_module_identity as _preserve_module_identity
from ._trace_delivery import _trace_delivery_context, _TraceDeliveryOwner

if TYPE_CHECKING:
    from .telemetry import TraceLifecycleEvent, TraceProcessor

_in_processor: ContextVar[bool] = ContextVar("agents_trace_processor", default=False)
_worker_count = 0
_worker_lock = Lock()
_MAX_PROCESSOR_WORKERS = 16


@dataclass
class _ProcessorDelivery:
    event: TraceLifecycleEvent
    result: Future[tuple[bool, float]] = field(default_factory=Future)


@dataclass
class _ProcessorBatch:
    deliveries: tuple[_ProcessorDelivery, ...]
    loss_epoch: int = 0
    callback_timeout: float | None = 1.0
    admitted_at: float = field(default_factory=monotonic)
    stopped: Event = field(default_factory=Event)
    completion: Future[None] | None = None
    credential_delivery: _TraceDeliveryOwner | None = None

    def done(self) -> bool:
        # The worker completes records in order, including fenced records.
        return self.deliveries[-1].result.done()

    async def wait(self) -> int:
        failures = 0
        previous_finished = self.admitted_at
        try:
            for index, delivery in enumerate(self.deliveries):
                if delivery.result.done():
                    success, previous_finished = delivery.result.result()
                else:
                    remaining = (
                        None
                        if self.callback_timeout is None
                        else max(0.0, previous_finished + self.callback_timeout - monotonic())
                    )
                    completion = asyncio.wrap_future(delivery.result)
                    done, _ = await asyncio.wait((completion,), timeout=remaining)
                    if not done:
                        return failures + len(self.deliveries) - index
                    success, previous_finished = completion.result()
                failures += not success
            return failures
        finally:
            # A late callback may finish, but cannot begin subsequent records
            # after the host times out, is cancelled, or retires this batch.
            self.stopped.set()
            if self.credential_delivery is not None:
                self.credential_delivery.retire()


class _ProcessorRoute:
    """One private daemon loop and one bounded batch in flight per route."""

    def __init__(self, processor: TraceProcessor):
        self.processor = processor
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pending: _ProcessorBatch | None = None
        self.loss_epoch = 0

    async def _invoke(
        self,
        callback: _Callable[..., Any],
        *args: Any,
        delivery: _TraceDeliveryOwner | None = None,
        events: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        import inspect

        failed = False
        token = _in_processor.set(True)
        try:
            with _trace_delivery_context(delivery, events):
                result = callback(*args)
                if inspect.isawaitable(result):
                    await result
        except BaseException:
            # Contain even callback cancellation and BaseException subclasses.
            # Raise outside the handler so raw callback errors are not chained.
            failed = True
        finally:
            _in_processor.reset(token)
        if failed:
            raise RuntimeError("Trace processor delivery failed")

    async def _recover_loss(self, epoch: int) -> None:
        if epoch > self.loss_epoch:
            callback = getattr(self.processor, "_on_native_trace_loss", None)
            if callable(callback):
                await self._invoke(callback)
            self.loss_epoch = epoch

    async def _invoke_batch(self, batch: _ProcessorBatch) -> None:
        previous_finished = batch.admitted_at
        timeout = batch.callback_timeout
        callback = getattr(self.processor, "on_trace_events", None)
        if callable(callback):
            success = False
            if not batch.stopped.is_set() and (
                timeout is None or monotonic() < previous_finished + timeout
            ):
                try:
                    await self._recover_loss(batch.loss_epoch)
                    await self._invoke(
                        callback,
                        tuple(item.event for item in batch.deliveries),
                        delivery=batch.credential_delivery,
                        events=tuple(item.event for item in batch.deliveries),
                    )
                    success = (
                        timeout is None or monotonic() < previous_finished + timeout
                    ) and not batch.stopped.is_set()
                except Exception:
                    pass
            if not success:
                batch.stopped.set()
            finished = monotonic()
            for delivery in batch.deliveries:
                delivery.result.set_result((success, finished))
            return
        for delivery in batch.deliveries:
            now = monotonic()
            deadline = None if timeout is None else previous_finished + timeout
            if batch.stopped.is_set() or (deadline is not None and now >= deadline):
                batch.stopped.set()
                delivery.result.set_result((False, now))
                continue
            success = True
            try:
                await self._recover_loss(batch.loss_epoch)
                await self._invoke(
                    self.processor.on_trace_event,
                    delivery.event,
                    delivery=batch.credential_delivery,
                    events=(delivery.event,),
                )
            except Exception:
                success = False
            finished = monotonic()
            if deadline is not None and finished >= deadline:
                # Check on the worker too: a delayed host timer must not let a
                # late-returning callback start the remainder of this group.
                batch.stopped.set()
                success = False
            delivery.result.set_result((success, finished))
            previous_finished = finished

    def submit(
        self,
        events: Sequence[TraceLifecycleEvent],
        loss_epoch: int = 0,
        credential_routes: Mapping[str, Any] | None = None,
        *,
        processor_id: str,
        callback_timeout: float | None,
    ) -> _ProcessorBatch:
        from threading import Thread

        global _worker_count
        if self.loop is None:
            with _worker_lock:
                if _worker_count >= _MAX_PROCESSOR_WORKERS:
                    raise RuntimeError("Trace processor worker capacity exhausted")
                _worker_count += 1
            try:
                loop = asyncio.new_event_loop()
            except BaseException:
                with _worker_lock:
                    _worker_count -= 1
                raise
            self.loop = loop

            def serve() -> None:
                global _worker_count
                asyncio.set_event_loop(loop)
                try:
                    loop.run_forever()
                finally:
                    loop.close()
                    with _worker_lock:
                        _worker_count -= 1

            try:
                Thread(target=serve, daemon=True, name="agents-trace-processor").start()
            except BaseException:
                loop.close()
                self.loop = None
                with _worker_lock:
                    _worker_count -= 1
                raise
        batch = _ProcessorBatch(
            tuple(_ProcessorDelivery(event) for event in events),
            loss_epoch=loss_epoch,
            callback_timeout=callback_timeout,
        )
        batch.credential_delivery = _TraceDeliveryOwner(
            events, credential_routes or {}, batch.stopped, processor_id
        )
        work = self._invoke_batch(batch)
        try:
            batch.completion = asyncio.run_coroutine_threadsafe(work, self.loop)
        except BaseException:
            work.close()
            raise
        self.pending = batch
        return batch

    def close(self) -> None:
        loop = self.loop
        if loop is None:
            return

        def stop(_future: Any = None) -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(loop.stop)

        if self.pending is not None:
            self.pending.stopped.set()
            if self.pending.credential_delivery is not None:
                self.pending.credential_delivery.retire()
            completion = self.pending.completion
            if completion is not None and not completion.done():
                # A blocked callback retains only its bounded batch and worker.
                completion.add_done_callback(stop)
                return
        stop()


_preserve_module_identity(globals(), "agents.telemetry")
