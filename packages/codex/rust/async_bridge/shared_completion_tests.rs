use super::*;
use pyo3::types::PyCFunction;

struct HeldResult {
    dropped: Arc<AtomicBool>,
    converted: Arc<AtomicBool>,
}

impl Drop for HeldResult {
    fn drop(&mut self) {
        self.dropped.store(true, Ordering::Release);
    }
}

impl<'py> IntoPyObject<'py> for HeldResult {
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> PyResult<Self::Output> {
        self.converted.store(true, Ordering::Release);
        Ok(py.None().into_bound(py))
    }
}

#[test]
fn real_retained_work_survives_cancellation_and_ready_result_discard() {
    for transport in [
        "healthy",
        "eof",
        "read_error",
        "eof_remove_error",
        "read_error_remove_error",
    ] {
        for executor in ["normal", "occupied", "shutdown"] {
            check_retained_work(transport, executor);
        }
    }
    for transport in ["registration_error", "removal_error", "cache_error"] {
        check_retained_work(transport, "normal");
    }
}

fn check_retained_work(transport: &str, executor: &str) {
    Python::initialize();
    Python::attach(|py| -> PyResult<()> {
        let dropped = Arc::new(AtomicBool::new(false));
        let converted = Arc::new(AtomicBool::new(false));
        let started = Arc::new(AtomicBool::new(false));
        let effect = Arc::new(AtomicBool::new(false));
        let release = Arc::new(tokio::sync::Notify::new());
        let make = {
            let dropped = Arc::clone(&dropped);
            let converted = Arc::clone(&converted);
            let started = Arc::clone(&started);
            let effect = Arc::clone(&effect);
            let release = Arc::clone(&release);
            PyCFunction::new_closure(py, None, None, move |args, _| {
                let value = HeldResult {
                    dropped: Arc::clone(&dropped),
                    converted: Arc::clone(&converted),
                };
                let started = Arc::clone(&started);
                let effect = Arc::clone(&effect);
                let release = Arc::clone(&release);
                new_call(args.py(), move || async move {
                    let ticket = Ticket::new(CURRENT.with(Arc::clone));
                    py_tokio::get_runtime().spawn(async move {
                        let _ticket = ticket;
                        started.store(true, Ordering::Release);
                        release.notified().await;
                        effect.store(true, Ordering::Release);
                    });
                    Ok(value)
                })
                .map(|call| call.into_any())
            })?
        };
        let observe = PyCFunction::new_closure(py, None, None, move |args, _| {
            let call: PyRef<'_, NativeCall> = args.get_item(0)?.extract()?;
            let ready = call.result.lock().unwrap().is_some();
            (
                started.load(Ordering::Acquire),
                ready,
                effect.load(Ordering::Acquire),
                dropped.load(Ordering::Acquire),
                converted.load(Ordering::Acquire),
                call.is_joined(),
            )
                .into_py_any(args.py())
        })?;
        let release = PyCFunction::new_closure(py, None, None, move |_, _| {
            release.notify_one();
            Ok::<(), PyErr>(())
        })?;
        let independent = PyCFunction::new_closure(py, None, None, move |args, _| {
            future_into_py(args.py(), async { Ok("independent") }).map(Bound::unbind)
        })?;
        let script = std::ffi::CString::new(
            r#"
import asyncio
import concurrent.futures
import errno
import os
import threading
from _agents_codex import _native_async
from agents._native_async import _await_native_completion

class BrokenReader:
    def __init__(self, reader):
        self.reader = reader
    def recv(self, size):
        raise OSError(errno.EIO, 'injected notification read failure')
    def fileno(self):
        return self.reader.fileno()
    def close(self):
        self.reader.close()

def check(make, observe, release, independent, transport, executor):
    async def main():
        loop = asyncio.get_running_loop()
        unblock_executor = threading.Event()
        pool = None
        if executor == 'occupied':
            entered_executor = threading.Event()
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            loop.set_default_executor(pool)
            def occupy():
                entered_executor.set()
                unblock_executor.wait()
            pool.submit(occupy)
            assert entered_executor.wait(5)
        elif executor == 'shutdown':
            await loop.shutdown_default_executor()
        before = asyncio.all_tasks()
        call = make()
        if transport in ('registration_error', 'removal_error', 'cache_error'):
            original_add = loop.add_reader
            original_remove = loop.remove_reader
            original_cache = _native_async._dispatchers
            error = RuntimeError('injected registration failure')
            captured = []
            def add(descriptor, callback, *args):
                original_add(descriptor, callback, *args)
                captured.append(callback.__self__)
                if transport != 'cache_error':
                    raise error
            def remove(descriptor):
                raise RuntimeError('injected removal failure')
            class FailedCache:
                def get(self, key):
                    return original_cache.get(key)
                def __setitem__(self, key, value):
                    if key is loop:
                        raise error
                    original_cache[key] = value
                def __delitem__(self, key):
                    del original_cache[key]
            loop.add_reader = add
            if transport == 'removal_error':
                loop.remove_reader = remove
            if transport == 'cache_error':
                _native_async._dispatchers = FailedCache()
            try:
                try:
                    await _await_native_completion(call)
                except RuntimeError as caught:
                    retained = caught
                else:
                    raise AssertionError('registration failure was lost')
                assert retained is error
                assert retained.__traceback__ is not None
                assert observe(call) == (False, False, False, True, False, False)
                assert len(captured) == 1
                dispatcher = captured[0]
                if transport == 'removal_error':
                    os.fstat(dispatcher._descriptor)
                    dispatcher._ready()  # Closed callbacks cannot touch a reused FD.
                    loop.remove_reader = original_remove
                    dispatcher.close()
                assert dispatcher._reader.fileno() == -1
            finally:
                loop.add_reader = original_add
                loop.remove_reader = original_remove
                _native_async._dispatchers = original_cache
                for dispatcher in captured:
                    dispatcher.close()
            return
        original_remove = loop.remove_reader
        task = asyncio.create_task(_await_native_completion(call))
        async def ready():
            while observe(call)[:2] != (True, True):
                await asyncio.sleep(0)
        try:
            await asyncio.wait_for(ready(), 5)
            assert observe(call) == (True, True, False, False, False, False)
            dispatcher = _native_async._dispatchers[loop]()
            assert dispatcher is not None
            if transport.endswith('_remove_error'):
                def fail_remove(descriptor):
                    raise RuntimeError('injected active removal failure')
                loop.remove_reader = fail_remove
            if transport.startswith('eof'):
                dispatcher._channel.close()
                dispatcher._ready()
            elif transport.startswith('read_error'):
                dispatcher._reader = BrokenReader(dispatcher._reader)
                dispatcher._ready()
            if transport.endswith('_remove_error'):
                os.fstat(dispatcher._descriptor)
                loop.remove_reader = original_remove
                async def removed():
                    while dispatcher._reader.fileno() >= 0:
                        await asyncio.sleep(0)
                await asyncio.wait_for(removed(), 5)
                assert not task.done()
                assert observe(call) == (True, True, False, False, False, False)
            if transport == 'healthy':
                assert dispatcher._poll_handle is None
            else:
                assert dispatcher._retired
                assert dispatcher._poll_handle is not None
            owned = asyncio.all_tasks() - before
            assert task in owned
            for owner in owned:
                owner.cancel()
            assert await asyncio.wait_for(independent(), 5) == 'independent'
            if transport != 'healthy':
                assert _native_async._dispatchers[loop]() is not dispatcher
            assert len(dispatcher._pending) == 1
            waiter = next(iter(dispatcher._pending.values()))[1]
            waiter.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert observe(call) == (True, True, False, False, False, False)
            release()
            try:
                await asyncio.wait_for(task, 5)
            except asyncio.CancelledError as caught:
                retained = caught
            else:
                raise AssertionError('cancellation lost')
            assert retained.__traceback__ is not None
            assert observe(call) == (True, False, True, True, False, True)
            assert not dispatcher._pending
            assert dispatcher._poll_handle is None
        finally:
            loop.remove_reader = original_remove
            release()
            await asyncio.gather(task, return_exceptions=True)
            unblock_executor.set()
            if pool is not None:
                pool.shutdown(wait=True)
    asyncio.run(main())
"#,
        )
        .unwrap();
        let module =
            PyModule::from_code(py, &script, c"shared_bridge_test.py", c"shared_bridge_test")?;
        module.getattr("check")?.call1((
            make,
            observe,
            release,
            independent,
            transport,
            executor,
        ))?;
        Ok(())
    })
    .unwrap();
}
