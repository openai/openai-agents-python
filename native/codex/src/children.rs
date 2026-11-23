//! Restrict an ephemeral tree before Core registers its collaboration tools.
use codex_core::config::{AgentRoleConfig, Config};
use codex_extension_api::{
    ModelRequestContributor, ModelRequestInput, ModelResponseError, ModelResponseInterceptor,
    ModelResponseStream, ResponseEvent,
};
use codex_features::Feature;
use codex_protocol::config_types::WebSearchMode;
use codex_protocol::models::ResponseItem;
use codex_protocol::openai_models::{ConfigShellToolType, ModelsResponse};
use codex_protocol::protocol::MultiAgentVersion;
use futures::{StreamExt, stream};
use pyo3::FromPyObject;
use serde_json::json;
use std::sync::{
    Arc, OnceLock,
    atomic::{AtomicUsize, Ordering},
};
use tempfile::TempDir;

use crate::NativeResult;

#[derive(FromPyObject)]
pub struct Children {
    roles: Vec<Role>,
    max_delegation_attempts: usize,
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
    pub fn budget(&self) -> NativeResult<Arc<Budget>> {
        let wait_close = self
            .max_delegation_attempts
            .checked_mul(2)
            .ok_or("Codex native delegation limit is too large.")?;
        Ok(Arc::new(Budget {
            wait_close: Arc::new(AtomicUsize::new(wait_close)),
            remaining: Arc::new(AtomicUsize::new(self.max_delegation_attempts)),
            denial: Arc::default(),
        }))
    }

    pub fn defaults(&self) -> Vec<String> {
        let mut overrides = vec![
            "agents.enabled=true".into(),
            "features.multi_agent=true".into(),
            "features.multi_agent_v2={enabled=false,disable_native_tools=true}".into(),
            format!(
                "agents.max_concurrent_threads_per_session={}",
                self.max_delegation_attempts
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
            || config.agent_max_threads != Some(self.max_delegation_attempts)
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

pub(crate) const MODEL_REQUIRED: &str = "Native children require an exact V1 model. \
    Run `codex debug models` and pass a slug with multi_agent_version=v1 to provider.get_model(slug).";

// Core selects collaboration and Code Mode from model metadata independently of
// feature flags. Freeze one approved entry so neither refresh nor a child override
// can select a different model. Only remove tool capabilities.
pub fn catalog(mut catalog: ModelsResponse, model: &str) -> NativeResult<ModelsResponse> {
    catalog.models.retain(|entry| entry.slug == model);
    let [entry] = catalog.models.as_mut_slice() else {
        return Err(MODEL_REQUIRED.into());
    };
    if entry.multi_agent_version != Some(MultiAgentVersion::V1) {
        return Err(MODEL_REQUIRED.into());
    }
    entry.tool_mode = Some(codex_protocol::openai_models::ToolMode::Direct);
    entry.supports_search_tool = false;
    entry.shell_type = ConfigShellToolType::Disabled;
    entry.apply_patch_tool_type = None;
    entry.experimental_supported_tools.clear();
    Ok(catalog)
}

// Only the interceptor supplies these limit tags and messages.
pub type Denial = (Option<&'static str>, &'static str);

#[derive(Clone, Debug)]
pub struct Budget {
    remaining: Arc<AtomicUsize>,
    wait_close: Arc<AtomicUsize>,
    denial: Arc<OnceLock<Denial>>,
}

impl Budget {
    pub fn denial(&self) -> Option<Denial> {
        self.denial.get().copied()
    }

    fn deny(&self, denial: Denial) -> ModelResponseError {
        let _ = self.denial.set(denial);
        ModelResponseError::InvalidRequest {
            message: denial.1.to_owned(),
        }
    }

    fn admit(&self, item: &ResponseItem) -> Result<(), ModelResponseError> {
        use ResponseItem::*;
        let (remaining, error) = match item {
            // Core routes V1 functions by namespace and name, never by arguments or ID.
            FunctionCall {
                namespace: Some(ns),
                name,
                ..
            } if ns == "multi_agent_v1"
                && matches!(name.as_str(), "wait_agent" | "close_agent") =>
            {
                (
                    &self.wait_close,
                    (
                        Some("wait_close_attempts"),
                        "Codex native wait/close limit exceeded.",
                    ),
                )
            }
            Message { .. }
            | AgentMessage { .. }
            | Reasoning { .. }
            | AdditionalTools { .. }
            | FunctionCallOutput { .. }
            | CustomToolCallOutput { .. }
            | ToolSearchOutput { .. }
            | Compaction { .. }
            | ConfigurationUpdate { .. }
            | CompactionTrigger { .. }
            | ContextCompaction { .. } => return Ok(()),
            _ => (
                &self.remaining,
                (
                    Some("delegation_attempts"),
                    "Codex native delegation limit exceeded.",
                ),
            ),
        };
        remaining
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |left| {
                left.checked_sub(1)
            })
            .map_err(|_| self.deny(error))?;
        // V1 handlers cannot accept custom payloads. Fail before routing alternate forms.
        if !matches!(item, FunctionCall { .. }) {
            return Err(self.deny((None, "Unsupported native delegation call.")));
        }
        Ok(())
    }
}

impl ModelRequestContributor for Budget {
    fn request(&self, _: ModelRequestInput<'_>) -> Option<Box<dyn ModelResponseInterceptor>> {
        Some(Box::new(self.clone()))
    }
}

impl ModelResponseInterceptor for Budget {
    fn intercept(self: Box<Self>, stream: ModelResponseStream) -> ModelResponseStream {
        stream::try_unfold((self, stream), |(budget, mut stream)| async move {
            let Some(event) = stream.next().await else {
                return Ok(None);
            };
            let event = event?;
            if let ResponseEvent::OutputItemDone(item) = &event {
                budget.admit(item)?;
            }
            Ok(Some((event, (budget, stream))))
        })
        .boxed()
    }
}
