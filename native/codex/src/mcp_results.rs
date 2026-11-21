//! Each selected call owns its reply, even when attempts reuse call IDs.
use codex_extension_api::{
    McpToolResultInput, McpToolSource, ToolLifecycleContributor, ToolLifecycleFuture,
};
use codex_protocol::mcp::CallToolResult;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};

pub struct Selection {
    pub tools: Vec<(String, String)>,
    pub requests: mpsc::UnboundedSender<PendingResult>,
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
