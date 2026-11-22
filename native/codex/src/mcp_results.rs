//! Each selected call owns its reply, even when attempts reuse call IDs.
use codex_extension_api::{
    McpToolResultInput, McpToolSource, ToolLifecycleContributor, ToolLifecycleFuture,
};
use codex_mcp::{McpServerSource, McpSnapshotDetail};
use codex_protocol::mcp::CallToolResult;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::{Value, json};
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
}

#[pymethods]
impl PendingResult {
    fn respond(&mut self, encoded: &str) -> PyResult<()> {
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
        let result = serde_json::from_value(value).map_err(|_| invalid())?;
        let expired = || super::py_error("Native MCP result request expired.");
        self.reply
            .take()
            .ok_or_else(expired)?
            .send(result)
            .map_err(|_| expired())
    }

    fn discard(&mut self) {
        self.reply.take();
    }
}

pub struct Transform;

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
