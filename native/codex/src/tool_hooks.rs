use std::path::Path;

use codex_config::{
    ConfigLayerEntry, ConfigLayerSource, ConfigLayerStack, build_cli_overrides_layer,
};
use codex_core_api::CodexThread;
use codex_hooks::{HookListEntry, HookListEntryHandler, HooksConfig, list_hooks};
use codex_protocol::protocol::{HookEventName, HookSource, HookTrustStatus};
use serde_json::{Value, json};
use toml::value::Table;

const CONFIG_ERROR: &str = "Unable to configure SDK tool hooks for Codex.";

// Multi-agent v2 and agent message board tools use the same configurable namespace.
const COLLABORATION_TOOL_MEMBERS: &[&str] = &[
    "create_channel",
    "followup_task",
    "get_channels",
    "interrupt_agent",
    "list_agents",
    "list_threads",
    "post",
    "read_post",
    "read_thread",
    "search_posts",
    "send_message",
    "spawn_agent",
    "subscribe",
    "unsubscribe",
    "wait_agent",
];

pub(crate) fn validate_collaboration_names(
    registration: &Value,
    namespace: Option<&str>,
) -> Result<(), String> {
    let sdk_names = registration["sdk_names"].as_array().ok_or(CONFIG_ERROR)?;
    let namespace = namespace.filter(|name| *name != "functions").unwrap_or_default();
    for &member in COLLABORATION_TOOL_MEMBERS {
        // Core keeps the legacy spawn_agent hook name for this namespace.
        let name = if namespace == "multi_agent_v1" && member == "spawn_agent" {
            member.to_owned()
        } else {
            format!("{namespace}{member}")
        };
        if sdk_names.iter().any(|sdk_name| sdk_name.as_str() == Some(name.as_str())) {
            return Err(format!(
                "Codex native hooks reserve {name}. Rename or namespace the SDK tool, \
                 or enable sdk_only_tool_hooks."
            ));
        }
    }
    Ok(())
}

fn is_sdk_hook(hook: &HookListEntry, server_name: &str) -> bool {
    hook.source == HookSource::SessionFlags
        && matches!(
            &hook.handler,
            HookListEntryHandler::McpTool { server, tool }
                if server == server_name && tool == "dispatch"
        )
}

fn toml_value(value: &Value) -> Result<toml::Value, String> {
    // Deserialize JSON numbers as TOML integers. Serializing a serde_json::Value directly
    // can turn numbers into private maps when another crate enables arbitrary precision.
    serde_json::from_str(&value.to_string()).map_err(|_| CONFIG_ERROR.into())
}

pub(crate) fn add_overrides(
    overrides: &mut Vec<(String, toml::Value)>,
    registration: &Value,
    executable: &Path,
) -> Result<(), String> {
    let server = registration["server"].as_str().ok_or(CONFIG_ERROR)?;
    let handlers = registration["handlers"].as_object().ok_or(CONFIG_ERROR)?;
    let port = registration["port"].as_u64().ok_or(CONFIG_ERROR)?;
    let key = registration["key"].as_str().ok_or(CONFIG_ERROR)?;
    let timeout = registration["timeout"].as_u64().ok_or(CONFIG_ERROR)?;
    // The managed sandbox already protects this executable and its replaceable ancestors.
    let mcp = toml_value(&json!({
        "command": executable.to_str().ok_or(CONFIG_ERROR)?,
        "args": ["--sdk-hook-stdio", &port.to_string()],
        "env": {"AGENTS_CODEX_HOOK_KEY": key},
        "required": true,
        "tool_timeout_sec": timeout,
    }))?;

    let flags = build_cli_overrides_layer(overrides);
    let mut hooks = match flags.get("hooks") {
        Some(hooks) => hooks.as_table().cloned().ok_or(CONFIG_ERROR)?,
        None => Table::new(),
    };
    for (event, group) in handlers {
        let group = toml_value(group)?;
        hooks
            .entry(event.clone())
            .or_insert_with(|| toml::Value::Array(Vec::new()))
            .as_array_mut()
            .ok_or(CONFIG_ERROR)?
            .push(group);
    }

    // Trust only the handlers the SDK appended. Core computes the keys and hashes it will verify.
    let layer = toml::Value::Table(Table::from_iter([(
        "hooks".into(),
        toml::Value::Table(hooks.clone()),
    )]));
    let stack = ConfigLayerStack::new(
        vec![ConfigLayerEntry::new(
            ConfigLayerSource::SessionFlags,
            layer,
        )],
        Default::default(),
        Default::default(),
    )
    .map_err(|_| CONFIG_ERROR)?;
    let discovered = list_hooks(HooksConfig {
        feature_enabled: true,
        config_layer_stack: Some(stack),
        ..Default::default()
    });
    let state = hooks
        .entry("state".to_owned())
        .or_insert_with(|| toml::Value::Table(Table::new()))
        .as_table_mut()
        .ok_or(CONFIG_ERROR)?;
    let mut sdk_count = 0;
    for hook in discovered
        .hooks
        .into_iter()
        .filter(|hook| is_sdk_hook(hook, server))
    {
        state.insert(
            hook.key,
            toml::Value::Table(Table::from_iter([(
                "trusted_hash".into(),
                toml::Value::String(hook.current_hash),
            )])),
        );
        sdk_count += 1;
    }
    if sdk_count != handlers.len() {
        return Err(CONFIG_ERROR.into());
    }
    overrides.push(("hooks".into(), toml::Value::Table(hooks)));
    overrides.push((format!("mcp_servers.{server}"), mcp));
    Ok(())
}

pub(crate) fn trusted(thread: &CodexThread, server: &str) -> Vec<HookListEntry> {
    thread
        .list_hooks()
        .hooks
        .into_iter()
        .filter(|hook| {
            is_sdk_hook(hook, server)
                && hook.enabled
                && hook.trust_status == HookTrustStatus::Trusted
                && matches!(
                    hook.event_name,
                    HookEventName::PreToolUse | HookEventName::PostToolUse
                )
        })
        .collect()
}

pub(crate) fn registered(thread: &CodexThread, server: &str) -> Vec<(&'static str, i64)> {
    trusted(thread, server)
        .into_iter()
        .filter_map(|hook| {
            let event = match hook.event_name {
                HookEventName::PreToolUse => "pre_tool_use",
                HookEventName::PostToolUse => "post_tool_use",
                _ => return None,
            };
            Some((event, hook.display_order))
        })
        .collect()
}
