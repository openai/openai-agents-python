//! Restrict an ephemeral tree before Core registers its collaboration tools.
use codex_core::config::{AgentRoleConfig, Config};
use codex_features::Feature;
use codex_protocol::config_types::WebSearchMode;
use codex_protocol::openai_models::{ConfigShellToolType, ModelsResponse};
use codex_protocol::protocol::MultiAgentVersion;
use pyo3::FromPyObject;
use serde_json::json;
use tempfile::TempDir;

use crate::NativeResult;

#[derive(FromPyObject)]
pub struct Children {
    roles: Vec<Role>,
    max_children: usize,
    max_depth: i32,
}

#[derive(FromPyObject)]
struct Role {
    name: String,
    description: String,
    instructions: String,
}

const DISABLED: &[Feature] = &[
    Feature::Apps,
    Feature::Plugins,
    Feature::CodexHooks,
    Feature::CodeMode,
    Feature::CodeModeOnly,
    Feature::DeferredExecutor,
    Feature::ShellTool,
    Feature::ViewImage,
    Feature::ImageGeneration,
    Feature::RequestPermissionsTool,
];

impl Children {
    pub fn defaults(&self) -> Vec<String> {
        let mut overrides = vec![
            "agents.enabled=true".into(),
            "features.multi_agent=true".into(),
            "features.multi_agent_v2={enabled=false,disable_native_tools=true}".into(),
            format!(
                "agents.max_concurrent_threads_per_session={}",
                self.max_children
            ),
            format!("agents.max_depth={}", self.max_depth),
            "tools.update_plan.enabled=false".into(),
            "tools.experimental_request_user_input.enabled=false".into(),
            "notify=[]".into(),
        ];
        overrides.extend(
            DISABLED
                .iter()
                .map(|feature| format!("features.{}=false", feature.key())),
        );
        overrides
    }

    pub fn validate(&self, config: &Config) -> NativeResult<()> {
        if !config.ephemeral
            || !config.agents_enabled
            || !config.features.enabled(Feature::Collab)
            || config.features.enabled(Feature::MultiAgentV2)
            || DISABLED
                .iter()
                .any(|feature| config.features.enabled(*feature))
            || config
                .mcp_servers
                .get()
                .values()
                .any(|server| server.enabled)
            || !config.multi_agent_v2.disable_native_tools
            || config.agent_default_subagent_model.is_some()
            || config.agent_max_threads != Some(self.max_children)
            || config.agent_max_depth != self.max_depth
            || config.experimental_request_user_input_enabled
            || config.update_plan_enabled
            || config.web_search_mode.value() != WebSearchMode::Disabled
            || config.notify.as_ref().is_some_and(|args| !args.is_empty())
        {
            return Err("Codex configuration conflicts with bounded native children.".into());
        }
        Ok(())
    }

    pub fn install(&self, config: &mut Config) -> NativeResult<TempDir> {
        let mut builder = tempfile::Builder::new();
        builder.prefix("codex-roles-");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            builder.permissions(std::fs::Permissions::from_mode(0o700));
        }
        let dir = builder
            .tempdir()
            .map_err(|_| "Cannot create Codex role directory.")?;
        config.agent_roles.clear();
        for (index, role) in self.roles.iter().enumerate() {
            let file = dir.path().join(format!("{index}.toml"));
            let body = toml::to_string(&json!({"developer_instructions": role.instructions}))
                .map_err(|_| "Cannot encode Codex role.")?;
            std::fs::write(&file, body).map_err(|_| "Cannot write Codex role.")?;
            config.agent_roles.insert(
                role.name.clone(),
                AgentRoleConfig {
                    description: Some(role.description.clone()),
                    config_file: Some(file),
                    nickname_candidates: None,
                },
            );
        }
        Ok(dir)
    }
}

// Core selects collaboration and Code Mode from model metadata independently of
// feature flags. Freeze one approved entry so neither refresh nor a child override
// can select a different model. Only remove tool capabilities.
pub fn catalog(mut catalog: ModelsResponse, model: &str) -> NativeResult<ModelsResponse> {
    catalog.models.retain(|entry| entry.slug == model);
    let [entry] = catalog.models.as_mut_slice() else {
        return Err("Native children require an exact V1 catalog model.".into());
    };
    if entry.multi_agent_version != Some(MultiAgentVersion::V1) {
        return Err("Native children require a V1 Responses catalog model.".into());
    }
    entry.tool_mode = Some(codex_protocol::openai_models::ToolMode::Direct);
    entry.supports_search_tool = false;
    entry.shell_type = ConfigShellToolType::Disabled;
    entry.apply_patch_tool_type = None;
    entry.experimental_supported_tools.clear();
    Ok(catalog)
}
