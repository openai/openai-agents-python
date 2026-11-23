//! Each selected call owns its reply, even when attempts reuse call IDs.
use codex_extension_api::{
    McpToolResultInput, McpToolSource, ModelRequestContributor, ModelRequestInput,
    ModelResponseError, ModelResponseInterceptor, ModelResponseStream, ResponseEvent,
    ToolLifecycleContributor, ToolLifecycleFuture,
};
use codex_mcp::{McpServerSource, McpSnapshotDetail};
use codex_protocol::mcp::CallToolResult;
use codex_protocol::models::ResponseItem;
use futures::{StreamExt, stream};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use tokio::sync::{mpsc, oneshot};

pub struct Selection {
    pub tools: Vec<(String, String)>,
    pub requests: mpsc::UnboundedSender<PendingResult>,
}

// Core captures this process setting at root-thread startup, including an empty path.
pub fn check_recording() -> Result<(), String> {
    if std::env::var_os("CODEX_ROLLOUT_TRACE_ROOT").is_some() {
        return Err(
            "Unset CODEX_ROLLOUT_TRACE_ROOT to use native_mcp_result_transforms. \
                    Rollout recording captures results before editing."
                .to_owned(),
        );
    }
    Ok(())
}

// Check the current thread's filtered catalog without fetching resources or starting
// a second connection. The result hook still checks provenance on every call.
pub async fn verify(thread: &codex_core::CodexThread, tools: &[(String, String)]) -> PyResult<()> {
    check_recording().map_err(super::py_error)?;
    let mut catalogs = std::collections::HashMap::new();
    for (index, (server, name)) in tools.iter().enumerate() {
        let unavailable = || {
            PyValueError::new_err(format!(
                "native_mcp_result_transforms[{index}] requires an enabled configured server and an available raw tool name."
            ))
        };
        if !catalogs.contains_key(server) {
            let (config, mut snapshot) = thread
                .mcp_server_status_snapshot(server, McpSnapshotDetail::ToolsAndAuthOnly, None)
                .await
                .map_err(|_| unavailable())?;
            let peer = config
                .mcp_server_catalog
                .server(server)
                .ok_or_else(unavailable)?;
            if peer.source() != &McpServerSource::Config || !peer.config().enabled {
                return Err(unavailable());
            }
            catalogs.insert(
                server,
                snapshot
                    .tools_by_server
                    .remove(server)
                    .ok_or_else(unavailable)?,
            );
        }
        if !catalogs[server].contains_key(name) {
            return Err(unavailable());
        }
    }
    Ok(())
}

#[pyclass]
pub struct PendingResult {
    #[pyo3(get)]
    request: String,
    reply: Option<oneshot::Sender<CallToolResult>>,
    hook_name: Option<String>,
}

#[pymethods]
impl PendingResult {
    fn respond(&mut self, encoded: &str) -> PyResult<Option<String>> {
        let invalid = || PyValueError::new_err("Invalid native MCP result replacement.");
        if encoded.len() > 1024 * 1024 {
            return Err(PyValueError::new_err(
                "Native MCP replacement exceeds 1 MiB.",
            ));
        }
        let value: Value = serde_json::from_str(encoded).map_err(|_| invalid())?;
        let fields = value.as_object().ok_or_else(invalid)?;
        if fields.keys().any(|key| {
            !matches!(
                key.as_str(),
                "content" | "structuredContent" | "isError" | "_meta"
            )
        }) {
            return Err(invalid());
        }
        // Validate MCP blocks, but retain their extension fields in Core's JSON representation.
        let _: rmcp::model::CallToolResult =
            serde_json::from_str(encoded).map_err(|_| invalid())?;
        let result: CallToolResult = serde_json::from_value(value).map_err(|_| invalid())?;
        let success = result.is_error != Some(true);
        let expired = || super::py_error("Native MCP result request expired.");
        self.reply
            .take()
            .ok_or_else(expired)?
            .send(result)
            .map_err(|_| expired())?;
        Ok(self.hook_name.take().filter(|_| success))
    }

    fn discard(&mut self) {
        self.reply.take();
    }
}

// PostToolUse can collapse distinct raw tool names. When both editors run, reject
// repeated model call IDs before dispatch, even if the Python reader is behind.
#[derive(Debug, Default, Clone)]
struct CallIds(Arc<Mutex<HashSet<String>>>);

impl ModelResponseInterceptor for CallIds {
    fn intercept(self: Box<Self>, stream: ModelResponseStream) -> ModelResponseStream {
        stream::try_unfold((self, stream), |(ids, mut stream)| async move {
            let Some(event) = stream.next().await else {
                return Ok(None);
            };
            let event = event?;
            if let ResponseEvent::OutputItemDone(
                ResponseItem::FunctionCall { call_id, .. }
                | ResponseItem::CustomToolCall { call_id, .. },
            ) = &event
                && (call_id.is_empty() || !ids.0.lock().unwrap().insert(call_id.clone()))
            {
                return Err(ModelResponseError::InvalidRequest {
                    message: "Codex returned an invalid native tool call.".to_owned(),
                });
            }
            Ok(Some((event, (ids, stream))))
        })
        .boxed()
    }
}

#[derive(Debug, Default)]
pub struct Transform {
    guarded: Mutex<HashMap<String, CallIds>>,
}

impl Transform {
    pub fn guard_calls(&self, thread_id: String) {
        self.guarded.lock().unwrap().entry(thread_id).or_default();
    }

    pub fn close(&self, thread_id: &str) {
        self.guarded.lock().unwrap().remove(thread_id);
    }
}

impl ModelRequestContributor for Transform {
    fn request(&self, input: ModelRequestInput<'_>) -> Option<Box<dyn ModelResponseInterceptor>> {
        let ids = self.guarded.lock().unwrap().get(input.thread_id)?.clone();
        Some(Box::new(ids))
    }
}

impl ToolLifecycleContributor for Transform {
    fn on_mcp_tool_result<'a>(&'a self, input: McpToolResultInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            let Some(selection) = input.thread_store.get::<Selection>() else {
                return;
            };
            let tool = input.mcp_tool.tool_info();
            if input.mcp_tool.source() != &McpToolSource::Config
                || !selection.tools.iter().any(|(server, name)| {
                    server == &tool.server_name && name == tool.tool.name.as_ref()
                })
            {
                return;
            }

            // Cancellation or a lost reply must never publish the peer payload.
            let original = std::mem::replace(
                input.result,
                CallToolResult {
                    content: vec![json!({"type":"text","text":"MCP result withheld"})],
                    structured_content: None,
                    is_error: Some(true),
                    meta: None,
                },
            );
            let failed = original.is_error == Some(true);
            // Match McpHandler's PostToolUse name using Core's resolved callable parts.
            // Peer and replacement errors skip PostToolUse.
            let hook_name = (!failed).then(|| {
                let name = format!(
                    "{}__{}",
                    tool.callable_namespace.trim_end_matches('_'),
                    tool.callable_name.trim_start_matches('_')
                );
                if name.starts_with("mcp__") {
                    name
                } else {
                    format!("mcp__{name}")
                }
            });
            let request = json!({
                "turn_id": input.turn_id, "call_id": input.call_id,
                "server": tool.server_name, "name": tool.tool.name,
                "arguments": input.arguments, "result": original,
            })
            .to_string();
            let (reply, result) = oneshot::channel();
            if selection
                .requests
                .send(PendingResult {
                    request,
                    reply: Some(reply),
                    hook_name,
                })
                .is_ok()
                && let Ok(mut replacement) = result.await
            {
                if failed {
                    replacement.is_error = Some(true);
                }
                *input.result = replacement;
            }
        })
    }
}
