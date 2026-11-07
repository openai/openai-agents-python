//! A loop-owned wakeup channel for already joined native calls.

use std::io::{ErrorKind, Write};
use std::sync::{Arc, Mutex};

use pyo3::prelude::*;

use super::{NativeWriter, take_writer};

struct Inner {
    writer: Option<NativeWriter>,
    ready: Vec<u64>,
}

pub(super) struct State(Mutex<Inner>);

impl State {
    pub(super) fn is_open(&self) -> bool {
        self.0
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .writer
            .is_some()
    }

    pub(super) fn publish(&self, id: u64) {
        let mut inner = self.0.lock().unwrap_or_else(|error| error.into_inner());
        if inner.writer.is_none() {
            // A retired channel uses Python's bounded timer recovery instead.
            return;
        }
        let notify = inner.ready.is_empty();
        inner.ready.push(id);
        if notify {
            let writer = inner.writer.as_mut().expect("open completion writer");
            match writer.write_all(&[0]) {
                Ok(()) => {}
                // A full socket is already readable; the queue is authoritative.
                Err(error) if error.kind() == ErrorKind::WouldBlock => {}
                Err(_) => {
                    // EOF wakes the loop even when writing a notification fails.
                    // Python then checks durable per-call joins until they finish.
                    inner.writer.take();
                }
            }
        }
    }

    fn close(&self) {
        let mut inner = self.0.lock().unwrap_or_else(|error| error.into_inner());
        inner.writer.take();
        inner.ready.clear();
    }
}

#[pyclass(module = "agents._native")]
pub(super) struct Channel {
    pub(super) state: Arc<State>,
}

impl Channel {
    pub(super) fn new(writer: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self {
            state: Arc::new(State(Mutex::new(Inner {
                writer: Some(take_writer(writer)?),
                ready: Vec::new(),
            }))),
        })
    }
}

impl Drop for Channel {
    fn drop(&mut self) {
        self.state.close();
    }
}

#[pymethods]
impl Channel {
    fn take_ready(&self) -> Vec<u64> {
        let mut inner = self
            .state
            .0
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        std::mem::take(&mut inner.ready)
    }

    fn close(&self) {
        self.state.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;
    use std::net::Shutdown;

    fn pair() -> (NativeWriter, Channel) {
        let (reader, writer) = NativeWriter::pair().unwrap();
        reader.set_nonblocking(true).unwrap();
        writer.set_nonblocking(true).unwrap();
        let channel = Channel {
            state: Arc::new(State(Mutex::new(Inner {
                writer: Some(writer),
                ready: Vec::new(),
            }))),
        };
        (reader, channel)
    }

    #[test]
    fn write_failure_retires_the_channel_without_panicking() {
        let (mut reader, channel) = pair();
        reader.shutdown(Shutdown::Read).unwrap();
        channel.state.publish(17);
        assert!(!channel.state.is_open());
        assert_eq!(channel.take_ready(), vec![17]);
        assert_eq!(reader.read(&mut [0; 1]).unwrap(), 0);
    }

    #[test]
    fn a_publication_between_read_and_drain_stays_in_the_same_batch() {
        let (mut reader, channel) = pair();
        channel.state.publish(1);
        assert_eq!(reader.read(&mut [0; 2]).unwrap(), 1);
        channel.state.publish(2);
        assert_eq!(channel.take_ready(), vec![1, 2]);
        assert_eq!(
            reader.read(&mut [0; 1]).unwrap_err().kind(),
            ErrorKind::WouldBlock
        );
        channel.state.publish(3);
        assert_eq!(reader.read(&mut [0; 1]).unwrap(), 1);
        assert_eq!(channel.take_ready(), vec![3]);
    }
}
