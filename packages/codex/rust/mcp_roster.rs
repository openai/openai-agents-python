//! Idle SDK root roster publication and portable native ordinary configuration.
use std::collections::{BTreeMap, BTreeSet, HashMap};

use anyhow::{Result, ensure};
use codex_config::{McpServerAuth, McpServerConfig};
use codex_core::ConfigRefreshOutcome;
use codex_core_api::CodexThread;
use codex_mcp::McpSnapshotDetail;
use codex_protocol::mcp::McpServerConnectionStatus as Status;
use codex_protocol::protocol::{AgentStatus, SessionSource, ThreadSource};
use serde_json::{Value, json};

pub(crate) fn enterprise(server: &McpServerConfig) -> bool {
    matches!(server.auth, McpServerAuth::EmaAuth) || server.ema_registration().is_some()
}

// Native serialization has optional nulls (e.g. tool_timeout_sec). The existing
// checkpoint config loader takes TOML overrides, where absent != explicit null.
fn omit_nulls(value: &mut Value) {
    match value {
        Value::Object(fields) => {
            fields.retain(|_, value| !value.is_null());
            for value in fields.values_mut() {
                omit_nulls(value);
            }
        }
        Value::Array(values) => values.iter_mut().for_each(omit_nulls),
        _ => {}
    }
}

// Do not send Serde's error text to Python: it can include header/token values.
// Track only the server's config field, not arbitrary server or nested map keys.
pub fn decode(value: Value) -> Result<HashMap<String, McpServerConfig>> {
    let mut path = serde_path_to_error::Track::new();
    let deserializer = serde_path_to_error::Deserializer::new(&value, &mut path);
    let mut ignored = false;
    let servers = serde_ignored::deserialize(deserializer, |_| ignored = true);
    let servers = servers.map_err(|_| {
        let path = path.path();
        match (path.iter().next(), path.iter().nth(1)) {
            (
                Some(serde_path_to_error::Segment::Map { key: server }),
                Some(serde_path_to_error::Segment::Map { key: field }),
            ) => {
                let kind = match &value[server][field] {
                    Value::Null => "null",
                    Value::Bool(_) => "boolean",
                    Value::Number(_) => "number",
                    Value::String(_) => "string",
                    Value::Array(_) => "array",
                    Value::Object(_) => "object",
                };
                anyhow::anyhow!("invalid MCP field `{field}`: incorrect type or value ({kind})")
            }
            _ => anyhow::anyhow!(
                "invalid MCP server configuration: check transport and field combinations"
            ),
        }
    })?;
    ensure!(!ignored, "unknown options in MCP server configuration");
    Ok(servers)
}

pub async fn checkpoint_servers(thread: &CodexThread) -> Value {
    let config = thread.config().await;
    let ordinary: BTreeMap<_, _> = config
        .mcp_servers
        .get()
        .iter()
        .filter(|(_, server)| !enterprise(server))
        .collect();
    let mut servers = json!(ordinary);
    omit_nulls(&mut servers);
    servers
}

// The native mutation lock is held by the binding. Errors are only permitted
// before publication: rejected publication and listing failures return receipts.
pub async fn replace(
    thread: &CodexThread,
    requested: HashMap<String, McpServerConfig>,
) -> Result<Value> {
    let current = thread.config().await;
    let owner = thread.config_snapshot().await;
    ensure!(
        matches!(owner.thread_source, None | Some(ThreadSource::User))
            && !matches!(
                owner.session_source,
                SessionSource::Internal(_) | SessionSource::SubAgent(_)
            ),
        "MCP roster replacement requires an SDK-owned root"
    );
    ensure!(
        thread.active_turn_environment_selections().await.is_none()
            && !matches!(
                thread.agent_status().await,
                AgentStatus::Running | AgentStatus::Shutdown | AgentStatus::NotFound
            ),
        "Cannot replace MCP servers while the native thread is busy"
    );
    let mut incoming = requested.clone();
    // Native refresh considers a missing EMA entry a revocation. This API only
    // owns ordinary servers, so preserve admitted entries even on name collision.
    incoming.extend(
        current
            .mcp_servers
            .get()
            .iter()
            .filter(|(_, server)| enterprise(server))
            .map(|(name, server)| (name.clone(), server.clone())),
    );
    let mut names: BTreeSet<_> = current.mcp_servers.get().keys().cloned().collect();
    names.extend(requested.keys().cloned());
    let mut next = (*current).clone();
    // Keep the native Constrained value, including the current managed rules.
    next.mcp_servers.set(incoming)?;
    let outcome = thread.refresh_mcp_config(current, next).await;
    let mut listed = BTreeMap::new();
    let mut listing_errors = BTreeSet::new();
    // Reconcile even an already empty roster. A selected-name snapshot performs
    // the native dirty refresh; it does not claim that a cached catalog is live.
    if names.is_empty() {
        names.insert(String::new());
    }
    for name in &names {
        match thread
            .mcp_server_status_snapshot(name, McpSnapshotDetail::ToolsAndAuthOnly)
            .await
        {
            Ok((_, snapshot)) => {
                for (server, tools) in snapshot.tools_by_server {
                    let mut tool_names: Vec<_> = tools.into_keys().collect();
                    tool_names.sort();
                    listed.insert(server, tool_names);
                }
                listing_errors.extend(snapshot.tools_errors.into_keys());
            }
            Err(_) => {
                // Diagnostics can contain URL/credential text. Return the fact
                // of failure, never raw auth or endpoint errors in a receipt.
                listing_errors.insert(name.clone());
            }
        }
    }
    let actual = thread.config().await;
    let (desired, _) = thread.runtime_mcp_config_and_context(&actual).await;
    // Unlike querying against just the cached snapshot, this compares current
    // native registrations to the live runtime, including a same-name endpoint.
    let statuses = thread.mcp_connection_statuses(&desired).await;
    let mut reports = BTreeMap::new();
    let mut ready = outcome == ConfigRefreshOutcome::Published;
    let mut reported: BTreeSet<_> = actual.mcp_servers.get().keys().cloned().collect();
    reported.extend(requested.keys().cloned());
    for name in reported {
        let effective = actual.mcp_servers.get().get(&name);
        let asked = requested.get(&name);
        let accepted = asked.is_none_or(|server| effective == Some(server));
        let status = statuses.get(&name).copied();
        let listing_error = listing_errors.contains(&name);
        let server_ready = accepted
            && (status == Some(Status::Connected) && !listing_error
                || effective.is_some_and(|server| !server.enabled)
                    && asked.is_none_or(|s| !s.enabled));
        if asked.is_some() {
            ready &= server_ready;
        }
        // Disabled/removed/mismatched registrations may have stale cached tools.
        let tools = if effective.is_some_and(|s| s.enabled) && accepted {
            listed.remove(&name).unwrap_or_default()
        } else {
            Vec::new()
        };
        reports.insert(
            name.clone(),
            json!({"status": status, "accepted": accepted, "ready": server_ready,
                   "tools": tools, "listing_error": listing_error}),
        );
    }
    Ok(json!({
        "result": {
            "publication": match outcome {
                ConfigRefreshOutcome::Published => "published",
                ConfigRefreshOutcome::Stale => "stale",
                ConfigRefreshOutcome::Rejected => "rejected",
            },
            "ready": ready,
            "servers": reports,
        },
    }))
}
