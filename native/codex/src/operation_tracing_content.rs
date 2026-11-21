//! Bounded, opt-in projections; no transport headers or binary/reasoning payloads.
use serde::Serialize;
use serde_json::{Value, json};
use std::io::{self, Write};

pub(super) const LIMIT: usize = 32 * 1024;

struct Bounded(Vec<u8>);
impl Write for Bounded {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > LIMIT {
            return Err(io::Error::other("capture budget"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub(super) fn omitted() -> Value {
    json!({"omitted": true, "reason": "unsupported_or_oversized_content"})
}

pub(super) fn capture<T: Serialize + ?Sized>(value: &T) -> Value {
    let mut bytes = Bounded(Vec::new());
    if serde_json::to_writer(&mut bytes, value).is_err() {
        return omitted();
    }
    let projected = serde_json::from_slice(&bytes.0)
        .map(|value| project(value, 0))
        .unwrap_or_else(|_| omitted());
    let mut check = Bounded(Vec::new());
    if serde_json::to_writer(&mut check, &projected).is_err() {
        omitted()
    } else {
        projected
    }
}

/// Prioritize actual messages; optional tool schemas share the retained budget.
pub(super) fn request<T: Serialize + ?Sized>(
    input: &[codex_protocol::models::ResponseItem],
    tools: Option<&T>,
    previous: Option<&str>,
    tool_choice: &str,
    parallel_tool_calls: bool,
) -> Value {
    // Newest messages and accepted results take priority over old instructions.
    let mut items = Vec::new();
    let mut remaining = LIMIT - 2048;
    let mut omitted_input = input.len() > 128;
    for item in input.iter().rev().take(128) {
        let item = capture(item);
        let size = serde_json::to_vec(&item)
            .map(|v| v.len() + 1)
            .unwrap_or(LIMIT);
        if item.get("omitted") == Some(&Value::Bool(true)) || size > remaining {
            omitted_input = true;
            continue;
        }
        remaining -= size;
        items.push(item);
    }
    if omitted_input {
        items.push(omitted());
    }
    items.reverse();
    let mut value = json!({"input":items, "tool_choice":capture(tool_choice), "parallel_tool_calls":parallel_tool_calls});
    if let Some(previous) = previous.filter(|id| id.len() <= 256) {
        value["previous_response_id"] = json!(previous);
    }
    if let Some(tools) = tools {
        value["tools"] = capture(tools);
        let mut check = Bounded(Vec::new());
        if serde_json::to_writer(&mut check, &value).is_err() {
            value["tools"] = json!([omitted()]);
        }
    }
    capture(&value)
}

fn project(value: Value, depth: usize) -> Value {
    if depth > 24 {
        return omitted();
    }
    match value {
        Value::Object(mut fields) => {
            if fields
                .get("type")
                .and_then(Value::as_str)
                .is_some_and(|kind| {
                    matches!(
                        kind,
                        "reasoning"
                            | "reasoning_text"
                            | "thinking"
                            | "compaction"
                            | "input_image"
                            | "output_image"
                            | "image"
                            | "input_audio"
                            | "output_audio"
                            | "input_file"
                            | "file"
                            | "audio"
                            | "video"
                            | "image_generation_call"
                    )
                })
            {
                return omitted();
            }
            fields.retain(|key, _| {
                !matches!(
                    key.to_ascii_lowercase().replace('_', "").as_str(),
                    "apikey"
                        | "authorization"
                        | "accesstoken"
                        | "refreshtoken"
                        | "password"
                        | "headers"
                        | "clientmetadata"
                        | "encryptedcontent"
                        | "reasoning"
                        | "reasoningcontent"
                        | "reasoningdetails"
                        | "thinking"
                        | "imageurl"
                        | "filedata"
                        | "imagedata"
                        | "audiodata"
                        | "b64json"
                        | "xapikey"
                )
            });
            Value::Object(
                fields
                    .into_iter()
                    .map(|(k, v)| (k, project(v, depth + 1)))
                    .collect(),
            )
        }
        Value::Array(items) => {
            Value::Array(items.into_iter().map(|v| project(v, depth + 1)).collect())
        }
        Value::String(text) => {
            if text.to_ascii_lowercase().contains("data:") {
                return omitted();
            }
            if matches!(text.trim_start().as_bytes().first(), Some(b'{' | b'[')) {
                return match serde_json::from_str::<Value>(&text) {
                    Ok(value) => Value::String(project(value, depth + 1).to_string()),
                    Err(_) => omitted(),
                };
            }
            Value::String(text)
        }
        value => value,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn content_projection_omits_nested_credentials_media_and_oversized_values() {
        let value = capture(&json!({
            "input": [{"type":"message", "content":[{"type":"input_text", "text":"keep"},
                {"type":"input_image", "image_url":"hidden-image"}]}],
            "arguments": "{\"password\":\"hidden-password\",\"query\":\"keep\"}",
            "client_metadata": {"secret":"hidden-metadata"},
            "encrypted_content":"hidden-reasoning"
        }));
        let encoded = value.to_string();
        assert!(encoded.contains("keep"));
        assert!(!encoded.contains("hidden"));
        assert_eq!(capture(&"x".repeat(LIMIT + 1)), omitted());
    }
    #[test]
    fn request_prioritizes_recent_prompt_and_accepted_result_over_large_instructions() {
        let input: Vec<codex_protocol::models::ResponseItem> = serde_json::from_value(json!([
            {"type":"message", "role":"developer", "content":[{"type":"input_text", "text":"x".repeat(LIMIT + 1)}]},
            {"type":"message", "role":"user", "content":[{"type":"input_text", "text":"recent user prompt"}]},
            {"type":"function_call_output", "call_id":"call", "output":"recent accepted result"}
        ])).unwrap();
        let tools = serde_json::value::RawValue::from_string(
            json!([{"name":"large", "description":"x".repeat(LIMIT)}]).to_string(),
        )
        .unwrap();
        let value = request(&input, Some(&tools), Some("previous"), "auto", true);
        let encoded = value.to_string();
        assert!(encoded.len() <= LIMIT);
        assert!(
            encoded.contains("recent user prompt") && encoded.contains("recent accepted result")
        );
        assert_eq!(value["input"][0]["omitted"], true);
        assert!(value["tools"].to_string().contains("omitted"));
        assert_eq!(value["previous_response_id"], "previous");
    }
}
