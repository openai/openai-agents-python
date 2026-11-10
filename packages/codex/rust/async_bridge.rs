//! Deliver owned Rust results only after their completion owners retire.
//!
//! No result publisher enters Python from a runtime worker. The awaiting
//! Python task converts the result only after all receipt owners have exited.

use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
#[cfg(not(windows))]
use std::task::Poll;

use futures_util::FutureExt;
use pyo3::IntoPyObjectExt;
use pyo3::prelude::*;
use pyo3_async_runtimes::{TaskLocals, err::RustPanic, tokio as py_tokio};
use tokio_util::sync::CancellationToken;

use crate::py_error;

#[cfg(target_os = "linux")]
mod completion_channel;

struct Receipt {
    publishers: AtomicUsize,
    joined: AtomicBool,
    notification: Mutex<Option<Notification>>,
}

enum Notification {
    Socket(NativeWriter),
    #[cfg(target_os = "linux")]
    Channel(Arc<completion_channel::State>, u64),
}

#[cfg(unix)]
type NativeWriter = std::os::unix::net::UnixStream;
#[cfg(windows)]
type NativeWriter = std::net::TcpStream;

#[cfg(unix)]
fn take_writer(writer: &Bound<'_, PyAny>) -> PyResult<NativeWriter> {
    use std::os::fd::FromRawFd;
    let descriptor = writer.call_method0("detach")?.extract()?;
    // socket.detach transfers the one owned descriptor to this wrapper.
    Ok(unsafe { NativeWriter::from_raw_fd(descriptor) })
}

#[cfg(windows)]
fn take_writer(writer: &Bound<'_, PyAny>) -> PyResult<NativeWriter> {
    use std::os::windows::io::FromRawSocket;
    let socket = writer.call_method0("detach")?.extract()?;
    // Python's Windows socketpair supplies connected Winsock sockets. The
    // detached socket is now owned solely by this standard I/O wrapper.
    Ok(unsafe { NativeWriter::from_raw_socket(socket) })
}

struct Ticket(Arc<Receipt>);

impl Ticket {
    fn new(receipt: Arc<Receipt>) -> Self {
        receipt.publishers.fetch_add(1, Ordering::Relaxed);
        Self(receipt)
    }
}

impl Drop for Ticket {
    fn drop(&mut self) {
        if self.0.publishers.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.0.joined.store(true, Ordering::Release);
            let notification = self
                .0
                .notification
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .take();
            // Neither notification enters Python from a runtime worker.
            match notification {
                Some(Notification::Socket(writer)) => drop(writer),
                #[cfg(target_os = "linux")]
                Some(Notification::Channel(channel, id)) => channel.publish(id),
                None => {}
            }
        }
    }
}

tokio::task_local! {
    static CURRENT: Arc<Receipt>;
}

type ResultConverter = Box<dyn for<'py> FnOnce(Python<'py>) -> PyResult<Py<PyAny>> + Send>;
type Starter = Box<dyn FnOnce() + Send>;

#[pyclass(module = "agents._native")]
struct NativeCall {
    receipt: Arc<Receipt>,
    starter: Mutex<Option<Starter>>,
    result: Arc<Mutex<Option<ResultConverter>>>,
    cancelled: CancellationToken,
}

#[pymethods]
impl NativeCall {
    #[cfg(target_os = "linux")]
    #[staticmethod]
    fn completion_channel(writer: &Bound<'_, PyAny>) -> PyResult<completion_channel::Channel> {
        completion_channel::Channel::new(writer)
    }

    #[cfg(target_os = "linux")]
    fn start_shared(&self, channel: &completion_channel::Channel, id: u64) -> PyResult<bool> {
        if !channel.state.is_open() {
            return Ok(false);
        }
        let starter = self
            .starter
            .lock()
            .map_err(py_error)?
            .take()
            .ok_or_else(|| py_error("native call already started"))?;
        *self.receipt.notification.lock().map_err(py_error)? =
            Some(Notification::Channel(Arc::clone(&channel.state), id));
        starter();
        Ok(true)
    }

    fn start(&self, writer: &Bound<'_, PyAny>) -> PyResult<()> {
        // Take the starter before transferring the socket. A duplicate start
        // must never replace the physical receipt of the original operation.
        let starter = self
            .starter
            .lock()
            .map_err(py_error)?
            .take()
            .ok_or_else(|| py_error("native call already started"))?;
        let native_writer = take_writer(writer)?;
        *self.receipt.notification.lock().map_err(py_error)? =
            Some(Notification::Socket(native_writer));
        starter();
        Ok(())
    }

    fn cancel(&self) {
        self.cancelled.cancel();
    }

    fn is_joined(&self) -> bool {
        self.receipt.joined.load(Ordering::Acquire)
    }

    fn discard_unstarted(&self, py: Python<'_>) -> PyResult<()> {
        let starter = self.starter.lock().map_err(py_error)?.take();
        // A saved setup-error traceback must not own a partially polled read.
        // Dropping the starter can release native event/discovery locks.
        py.detach(move || drop(starter));
        Ok(())
    }

    fn discard_result(&self) -> PyResult<()> {
        if !self.receipt.joined.load(Ordering::Acquire) {
            return Err(py_error("native completion has not been joined"));
        }
        // Cancellation traceback frames retain NativeCall. Release a ready
        // value now, without converting it or waiting for traceback collection.
        let result = self.result.lock().map_err(py_error)?.take();
        drop(result);
        Ok(())
    }

    fn take_result(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        // Python calls this only after the joined notification. Retained writer
        // operations may outlive the primary result, so readiness is insufficient.
        if !self.receipt.joined.load(Ordering::Acquire) {
            return Err(py_error("native completion has not been joined"));
        }
        let result = self
            .result
            .lock()
            .map_err(py_error)?
            .take()
            .ok_or_else(|| py_error("native completion has no result"))?;
        result(py)
    }
}

pub(crate) fn future_into_py<'py, F, T>(py: Python<'py>, fut: F) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'a> IntoPyObject<'a> + Send + 'static,
{
    eager_future_into_py(py, fut, false)
}

// A passive request should not send work when cancellation is already ready.
pub(crate) fn cancel_first_future_into_py<'py, F, T>(
    py: Python<'py>,
    fut: F,
) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'a> IntoPyObject<'a> + Send + 'static,
{
    eager_future_into_py(py, fut, true)
}

fn eager_future_into_py<'py, F, T>(
    py: Python<'py>,
    fut: F,
    cancel_first: bool,
) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'a> IntoPyObject<'a> + Send + 'static,
{
    // Eager callers already own F. Box it before capturing it in the starter,
    // so neither Make nor the spawned wrappers retain F-sized inline storage.
    let locals = py_tokio::get_current_locals(py)?;
    let fut = Box::pin(fut);
    let call = new_call_made(py, locals, cancel_first, move || fut)?;
    await_call(py, call)
}

// Only defer construction of an async block that moves existing captures.
// Keep input parsing and validation at the caller's original stage.
pub(crate) fn future_factory_into_py<'py, Make, F, T>(
    py: Python<'py>,
    make: Make,
) -> PyResult<Bound<'py, PyAny>>
where
    Make: FnOnce() -> F + Send + 'static,
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'a> IntoPyObject<'a> + Send + 'static,
{
    let call = new_call(py, make)?;
    await_call(py, call)
}

fn await_call<'py>(py: Python<'py>, call: Py<NativeCall>) -> PyResult<Bound<'py, PyAny>> {
    // Construct lazily: cancellation before first poll starts no native work.
    py.import("agents._native_async")?
        .getattr("_await_native_completion")?
        .call1((call,))
}

fn new_call<Make, F, T>(py: Python<'_>, make: Make) -> PyResult<Py<NativeCall>>
where
    Make: FnOnce() -> F + Send + 'static,
    F: Future<Output = PyResult<T>> + Send + 'static,
    T: for<'a> IntoPyObject<'a> + Send + 'static,
{
    let locals = py_tokio::get_current_locals(py)?;
    // Invoke and box the factory only inside the existing spawned
    // CURRENT/TaskLocals/panic scopes, at the original make() point.
    new_call_made(py, locals, false, move || Box::pin(make()))
}

// All callers supply one pinned allocation already, including managed reads.
// Keep the stored awaitable pointer-sized without adding a second read box.
fn new_call_made<Make, F, T>(
    py: Python<'_>,
    locals: TaskLocals,
    cancel_first: bool,
    make: Make,
) -> PyResult<Py<NativeCall>>
where
    Make: FnOnce() -> Pin<Box<F>> + Send + 'static,
    F: Future<Output = PyResult<T>> + Send + 'static + ?Sized,
    T: for<'a> IntoPyObject<'a> + Send + 'static,
{
    let receipt = Arc::new(Receipt {
        publishers: AtomicUsize::new(0),
        joined: AtomicBool::new(false),
        notification: Mutex::new(None),
    });
    let result: Arc<Mutex<Option<ResultConverter>>> = Arc::new(Mutex::new(None));
    let cancelled = CancellationToken::new();
    let task_receipt = Arc::clone(&receipt);
    let task_result = Arc::clone(&result);
    let task_cancelled = cancelled.clone();
    let starter = Box::new(move || {
        // Register synchronously before spawn; there is no zero-owner gap.
        let ticket = Ticket::new(Arc::clone(&task_receipt));
        py_tokio::get_runtime().spawn(async move {
            let _ticket = ticket;
            CURRENT
                .scope(task_receipt, async move {
                    let outcome = AssertUnwindSafe(py_tokio::scope(locals, async move {
                        tokio::select! {
                            biased;
                            _ = task_cancelled.cancelled(), if cancel_first => None,
                            value = make() => Some(value),
                            _ = task_cancelled.cancelled(), if !cancel_first => None,
                        }
                    }))
                    .catch_unwind()
                    .await;
                    let converted: Option<ResultConverter> = match outcome {
                        Ok(Some(value)) => Some(Box::new(move |py| {
                            value.and_then(|value| value.into_py_any(py))
                        })),
                        Ok(None) => None,
                        Err(panic) => Some(Box::new(move |_| Err(panic_error(panic)))),
                    };
                    *task_result
                        .lock()
                        .unwrap_or_else(|error| error.into_inner()) = converted;
                })
                .await;
        });
    });
    Py::new(
        py,
        NativeCall {
            receipt,
            starter: Mutex::new(Some(starter)),
            result,
            cancelled,
        },
    )
}

fn panic_error(panic: Box<dyn std::any::Any + Send>) -> PyErr {
    let message = panic
        .downcast_ref::<&str>()
        .copied()
        .or_else(|| panic.downcast_ref::<String>().map(String::as_str))
        .unwrap_or("unknown error")
        .to_owned();
    RustPanic::new_err(format!("rust future panicked: {message}"))
}

#[cfg(not(windows))]
type EventFuture = Pin<Box<dyn Future<Output = PyResult<String>> + Send>>;

#[cfg(not(windows))]
enum EventPoll {
    Ready(PyResult<String>),
    Pending(EventFuture),
}

#[cfg(not(windows))]
fn poll_event_once(mut future: EventFuture) -> EventPoll {
    // Embedders may call Python while already entered into a Tokio runtime.
    // Keep the original bridge in that case, without consuming any event.
    if tokio::runtime::Handle::try_current().is_ok() {
        return EventPoll::Pending(future);
    }
    let polled = std::panic::catch_unwind(AssertUnwindSafe(|| {
        // The outer probe always completes on its first poll, even if the read
        // is Pending. Never block_on the event itself. Tokio supplies its
        // normal cooperative budget to bound ready channel operations.
        py_tokio::get_runtime().block_on(std::future::poll_fn(|cx| {
            Poll::Ready(future.as_mut().poll(cx))
        }))
    }));
    match polled {
        Ok(Poll::Ready(value)) => EventPoll::Ready(value),
        // Retain the same pin, including work done before Pending.
        // The first spawned poll installs the real task's waker.
        Ok(Poll::Pending) => EventPoll::Pending(future),
        Err(panic) => EventPoll::Ready(Err(panic_error(panic))),
    }
    // On Ready or panic the completed future drops here, without GIL.
}

#[cfg(not(windows))]
#[pyclass(module = "agents._native")]
struct ReadyRead {
    future: Mutex<Option<EventFuture>>,
}

#[cfg(not(windows))]
#[pymethods]
impl ReadyRead {
    fn poll(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let future = self
            .future
            .lock()
            .map_err(py_error)?
            .take()
            .ok_or_else(|| py_error("native event read already polled"))?;
        let outcome = py.detach(move || poll_event_once(future));
        match outcome {
            EventPoll::Ready(value) => value?.into_py_any(py),
            EventPoll::Pending(future) => {
                let locals = py_tokio::get_current_locals(py)?;
                Ok(new_call_made(py, locals, false, move || future)?.into_any())
            }
        }
    }
}

pub(crate) fn session_event_into_py<'py, F>(py: Python<'py>, fut: F) -> PyResult<Bound<'py, PyAny>>
where
    F: Future<Output = PyResult<String>> + Send + 'static,
{
    #[cfg(windows)]
    {
        future_into_py(py, fut)
    }
    #[cfg(not(windows))]
    {
        let read = Py::new(
            py,
            ReadyRead {
                future: Mutex::new(Some(Box::pin(fut))),
            },
        )?;
        py.import("agents._native_async")?
            .getattr("_await_native_event")?
            .call1((read,))
    }
}

// Once a managed writer operation starts, retain it even if its Python awaiter
// is cancelled. The existing publisher ticket makes _await_native_completion
// join it: neither a completed tool effect nor a suspension lease may outlive
// its owning native call.
pub(crate) async fn retain_writer_operation<T: Send + 'static>(
    commit: impl Future<Output = PyResult<T>> + Send + 'static,
) -> PyResult<T> {
    let ticket = Ticket::new(CURRENT.with(Arc::clone));
    py_tokio::get_runtime()
        .spawn(async move {
            let _ticket = ticket;
            commit.await
        })
        .await
        .map_err(py_error)?
}

#[cfg(all(test, target_os = "linux"))]
mod shared_completion_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use pyo3::types::PyCFunction;
    use std::sync::atomic::AtomicBool;

    #[cfg(not(windows))]
    #[test]
    fn managed_event_probe_yields_on_ready_broadcast_work_and_preserves_the_read() {
        const ITEMS: usize = 256;
        let (sender, mut receiver) = tokio::sync::broadcast::channel(ITEMS);
        for sequence in 0..ITEMS {
            sender.send(sequence).unwrap();
        }
        let completed = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&completed);
        let read: EventFuture = Box::pin(async move {
            for sequence in 0..ITEMS {
                assert_eq!(receiver.recv().await.map_err(py_error)?, sequence);
                observed.fetch_add(1, Ordering::Release);
            }
            Ok("finished".to_owned())
        });

        let EventPoll::Pending(read) = poll_event_once(read) else {
            panic!("one probe must yield before draining all ready channel work");
        };
        let progress = completed.load(Ordering::Acquire);
        assert!(
            progress > 0 && progress < ITEMS,
            "unexpected progress: {progress}"
        );
        // Continue the retained read under the normal runtime. All original
        // channel rows must still appear exactly once and in order.
        let joined = py_tokio::get_runtime().spawn(read);
        let value = py_tokio::get_runtime().block_on(joined).unwrap().unwrap();
        assert_eq!(value, "finished");
        assert_eq!(completed.load(Ordering::Acquire), ITEMS);
    }

    #[cfg(not(windows))]
    #[test]
    fn managed_event_probe_in_entered_runtime_defers_before_any_poll() {
        let polled = Arc::new(AtomicUsize::new(0));
        let observed = Arc::clone(&polled);
        let read: EventFuture = Box::pin(async move {
            observed.fetch_add(1, Ordering::Release);
            Ok("original bridge".to_owned())
        });
        let read = {
            let _entered = py_tokio::get_runtime().enter();
            let EventPoll::Pending(read) = poll_event_once(read) else {
                panic!("an entered runtime must use the original bridge");
            };
            assert_eq!(polled.load(Ordering::Acquire), 0);
            read
        };
        let joined = py_tokio::get_runtime().spawn(read);
        let value = py_tokio::get_runtime().block_on(joined).unwrap().unwrap();
        assert_eq!(value, "original bridge");
        assert_eq!(polled.load(Ordering::Acquire), 1);
    }

    struct ObservedResult {
        dropped: Arc<AtomicBool>,
        converted: Arc<AtomicBool>,
    }

    impl Drop for ObservedResult {
        fn drop(&mut self) {
            self.dropped.store(true, Ordering::Release);
        }
    }

    impl<'py> IntoPyObject<'py> for ObservedResult {
        type Target = PyAny;
        type Output = Bound<'py, PyAny>;
        type Error = PyErr;

        fn into_pyobject(self, py: Python<'py>) -> PyResult<Self::Output> {
            self.converted.store(true, Ordering::Release);
            Ok(py.None().into_bound(py))
        }
    }

    #[test]
    fn cancelled_socket_ready_result_is_dropped_without_conversion_or_traceback_gc() {
        Python::initialize();
        Python::attach(|py| -> PyResult<()> {
            let dropped = Arc::new(AtomicBool::new(false));
            let converted = Arc::new(AtomicBool::new(false));
            let create_dropped = Arc::clone(&dropped);
            let create_converted = Arc::clone(&converted);
            let make = PyCFunction::new_closure(py, None, None, move |args, _| {
                let value = ObservedResult {
                    dropped: Arc::clone(&create_dropped),
                    converted: Arc::clone(&create_converted),
                };
                new_call(args.py(), move || async move { Ok(value) }).map(|call| call.into_any())
            })?;
            let observe = PyCFunction::new_closure(py, None, None, move |args, _| {
                (
                    dropped.load(Ordering::Acquire),
                    converted.load(Ordering::Acquire),
                )
                    .into_py_any(args.py())
            })?;
            let script = std::ffi::CString::new(
                r#"
import asyncio
from _agents_codex._native_async import _await_socket_completion

def check(make, observe):
    async def main():
        loop = asyncio.get_running_loop()
        original = loop.sock_recv
        ready = asyncio.Event()
        release = asyncio.Event()
        async def held_receive(reader, count):
            value = await original(reader, count)
            assert value == b''
            ready.set()
            await release.wait()
            return value
        loop.sock_recv = held_receive
        task = asyncio.create_task(_await_socket_completion(make()))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            assert observe() == (False, False)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            try:
                await asyncio.wait_for(task, 5)
            except asyncio.CancelledError as caught:
                retained = caught
            else:
                raise AssertionError('cancellation lost')
            assert retained.__traceback__ is not None
            assert observe() == (True, False)
        finally:
            loop.sock_recv = original
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(main())
"#,
            )
            .unwrap();
            let module = PyModule::from_code(py, &script, c"bridge_test.py", c"bridge_test")?;
            module.getattr("check")?.call1((make, observe))?;
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn original_error_and_future_panic_reach_the_awaiting_task() {
        Python::initialize();
        Python::attach(|py| -> PyResult<()> {
            let error = pyo3::exceptions::PyValueError::new_err("synthetic owned failure");
            let expected = error.value(py).clone();
            let make = PyCFunction::new_closure(py, None, None, move |args, _| {
                let panic: bool = args.get_item(0)?.extract()?;
                let original = error.clone_ref(args.py());
                future_into_py(args.py(), async move {
                    if panic {
                        panic!("synthetic native future panic");
                    }
                    Err::<(), _>(original)
                })
                .map(Bound::unbind)
            })?;
            let script = std::ffi::CString::new(
                r#"
import asyncio

def check(make, expected):
    async def main():
        try:
            await make(False)
        except ValueError as caught:
            assert caught is expected
        else:
            raise AssertionError('original failure lost')
        try:
            await make(True)
        except BaseException as caught:
            assert type(caught).__name__ == 'RustPanic', type(caught)
            assert str(caught) == 'rust future panicked: synthetic native future panic'
        else:
            raise AssertionError('native panic lost')
    asyncio.run(main())
"#,
            )
            .unwrap();
            let module = PyModule::from_code(py, &script, c"bridge_test.py", c"bridge_test")?;
            module.getattr("check")?.call1((make, expected))?;
            Ok(())
        })
        .unwrap();
    }

    struct FactoryOwner {
        dropped: Arc<AtomicUsize>,
        all_drops_in_runtime: Arc<AtomicBool>,
    }

    impl Drop for FactoryOwner {
        fn drop(&mut self) {
            self.all_drops_in_runtime.fetch_and(
                tokio::runtime::Handle::try_current().is_ok(),
                Ordering::AcqRel,
            );
            self.dropped.fetch_add(1, Ordering::Release);
        }
    }

    #[test]
    fn factory_preserves_lazy_ownership_panic_and_ready_precedence() {
        Python::initialize();
        Python::attach(|py| -> PyResult<()> {
            let constructed = Arc::new(AtomicUsize::new(0));
            let polled = Arc::new(AtomicUsize::new(0));
            let dropped = Arc::new(AtomicUsize::new(0));
            let all_drops_in_runtime = Arc::new(AtomicBool::new(true));
            let make_constructed = Arc::clone(&constructed);
            let make_polled = Arc::clone(&polled);
            let make_dropped = Arc::clone(&dropped);
            let make_runtime = Arc::clone(&all_drops_in_runtime);
            let make = PyCFunction::new_closure(py, None, None, move |args, _| {
                let mode: u8 = args.get_item(0)?.extract()?;
                let constructed = Arc::clone(&make_constructed);
                let polled = Arc::clone(&make_polled);
                // Match the startup methods' current() owner, including its
                // runtime-entering Drop on discard outside a Tokio worker.
                let owner = crate::RuntimeOwned(Some(Arc::new(FactoryOwner {
                    dropped: Arc::clone(&make_dropped),
                    all_drops_in_runtime: Arc::clone(&make_runtime),
                })));
                let factory = move || {
                    constructed.fetch_add(1, Ordering::Release);
                    if mode == 1 {
                        panic!("synthetic native factory panic");
                    }
                    async move {
                        let _owner = owner;
                        polled.fetch_add(1, Ordering::Release);
                        if mode == 3 {
                            std::future::pending::<()>().await;
                        }
                        Ok::<_, PyErr>("factory ready".to_owned())
                    }
                };
                if mode == 1 {
                    future_factory_into_py(args.py(), factory).map(Bound::unbind)
                } else if mode == 4 {
                    let locals = py_tokio::get_current_locals(args.py())?;
                    new_call_made(args.py(), locals, true, move || Box::pin(factory()))
                        .map(|call| call.into_any())
                } else {
                    new_call(args.py(), factory).map(|call| call.into_any())
                }
            })?;
            let observe = PyCFunction::new_closure(py, None, None, move |args, _| {
                (
                    constructed.load(Ordering::Acquire),
                    polled.load(Ordering::Acquire),
                    dropped.load(Ordering::Acquire),
                    all_drops_in_runtime.load(Ordering::Acquire),
                )
                    .into_py_any(args.py())
            })?;
            let script = std::ffi::CString::new(
                r#"
import asyncio
from agents._native_async import _await_native_completion

def check(make, observe):
    async def main():
        call = make(0)
        assert observe() == (0, 0, 0, True)
        call.cancel()
        call.discard_unstarted()
        assert observe() == (0, 0, 1, True)
        call.discard_unstarted()
        try:
            call.start(None)
        except RuntimeError as caught:
            assert str(caught) == 'native call already started'
        else:
            raise AssertionError('discarded factory started')
        assert observe() == (0, 0, 1, True)
        del call
        assert observe() == (0, 0, 1, True)

        call = make(0)
        assert observe() == (0, 0, 1, True)
        del call
        assert observe() == (0, 0, 2, True)

        operation = make(1)
        assert observe() == (0, 0, 2, True)
        try:
            await asyncio.wait_for(operation, 5)
        except BaseException as caught:
            assert type(caught).__name__ == 'RustPanic', type(caught)
            assert str(caught) == 'rust future panicked: synthetic native factory panic'
        else:
            raise AssertionError('constructor panic lost')
        assert observe() == (1, 0, 3, True)

        call = make(2)
        call.cancel()
        assert observe() == (1, 0, 3, True)
        assert await asyncio.wait_for(_await_native_completion(call), 5) == 'factory ready'
        assert observe() == (2, 1, 4, True)

        call = make(3)
        call.cancel()
        try:
            await asyncio.wait_for(_await_native_completion(call), 5)
        except RuntimeError as caught:
            # Calling cancel directly does not cancel this Python Task. After
            # the physical EOF join, there is deliberately no native result.
            assert str(caught) == 'native completion has no result'
        else:
            raise AssertionError('pending future ignored cancellation')
        assert observe() == (3, 2, 5, True)

        call = make(4)
        call.cancel()
        try:
            await asyncio.wait_for(_await_native_completion(call), 5)
        except RuntimeError as caught:
            assert str(caught) == 'native completion has no result'
        else:
            raise AssertionError('cancellation-first call published a result')
        # The joined future dropped without being polled.
        assert observe() == (4, 2, 6, True)
    asyncio.run(main())
"#,
            )
            .unwrap();
            let module = PyModule::from_code(
                py,
                &script,
                c"factory_bridge_test.py",
                c"factory_bridge_test",
            )?;
            module.getattr("check")?.call1((make, observe))?;
            Ok(())
        })
        .unwrap();
    }
}
