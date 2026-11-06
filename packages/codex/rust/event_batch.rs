//! Private, bounded collection for an already-admitted, callback-free root.
//! The existing managed reader remains the only source and projects every event.
use std::future::Future;
use std::panic::AssertUnwindSafe;

use futures_util::FutureExt;
use pyo3::PyErr;
use serde_json::Value;

use crate::async_bridge::{EventValue, panic_error};

const MAX_RECORDS: usize = 32;
const MAX_ENCODED_BYTES: usize = 128 * 1024;

fn passive(event: &Value, expected_turn: &str) -> bool {
    if event.get("subagent_id").is_some_and(|id| !id.is_null())
        || event["id"].as_str() != Some(expected_turn)
    {
        return false;
    }
    match event["msg"]["type"].as_str() {
        Some(
            "task_started"
            | "turn_started"
            | "user_message"
            | "warning"
            | "token_count"
            | "raw_response_completed"
            | "_sdk_request_metadata"
            | "agent_message"
            | "agent_message_delta"
            | "agent_message_content_delta"
            | "agent_reasoning"
            | "agent_reasoning_delta"
            | "agent_reasoning_raw_content"
            | "agent_reasoning_raw_content_delta"
            | "agent_reasoning_section_break",
        ) => true,
        Some("raw_response_item") => matches!(
            event["msg"]["item"]["type"].as_str(),
            Some("message" | "reasoning")
        ),
        Some("item_started" | "item_completed") => matches!(
            event["msg"]["item"]["type"].as_str(),
            Some("UserMessage" | "AgentMessage" | "Reasoning")
        ),
        // Questions, callbacks, errors, lifecycle records and unknown variants
        // return immediately. A terminal keeps its exact managed annotations.
        _ => false,
    }
}

pub(crate) async fn collect<Read, ReadFuture>(
    mut read: Read,
    expected_turn: &str,
    enabled: bool,
) -> EventValue
where
    Read: FnMut() -> ReadFuture,
    ReadFuture: Future<Output = anyhow::Result<Value>>,
{
    let mut json = String::from("[");
    let mut records = 0;
    let mut barrier = false;
    let mut following_error: Option<PyErr> = None;
    loop {
        // Await the whole existing read. Never discard a partially polled read
        // after dequeuing graph/child/control work, and never keep it dormant.
        let next = AssertUnwindSafe(async { read().await })
            .catch_unwind()
            .await;
        let event = match next {
            Ok(Ok(event)) => event,
            Ok(Err(error)) => {
                following_error = Some(crate::py_error(error));
                break;
            }
            Err(panic) => {
                following_error = Some(panic_error(panic));
                break;
            }
        };
        let encoded = match serde_json::to_string(&event) {
            Ok(encoded) => encoded,
            Err(error) => {
                following_error = Some(crate::py_error(error));
                break;
            }
        };
        barrier = !enabled || !passive(&event, expected_turn);
        if records != 0 {
            json.push(',');
        }
        // Encode each event exactly once, as the existing single-event binding
        // does. One Python JSON decode projects the whole bounded array.
        json.push_str(&encoded);
        records += 1;
        if barrier || records >= MAX_RECORDS || json.len() >= MAX_ENCODED_BYTES {
            break;
        }
    }
    json.push(']');
    EventValue::Batch {
        events_json: json,
        following_error,
        barrier,
    }
}

#[cfg(test)]
#[path = "event_batch_tests.rs"]
mod tests;
