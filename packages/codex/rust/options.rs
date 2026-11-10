//! Thread startup options and host-provided instruction snapshots.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, RwLock};

use codex_core_api::{Config, EnvironmentManager, InitialHistory, StartThreadOptions};
use codex_extension_api::{
    Instructions, LoadInstructionsFuture, LoadedUserInstructions, ThreadInstructionsProvider,
    ToolPolicy, UserInstructionsProvider,
};
use codex_protocol::dynamic_tools::DynamicToolSpec;
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::{SessionSource, ThreadHistoryMode, ThreadSource, W3cTraceContext};
use codex_protocol::{ThreadId, ToolName};
use serde::{Deserialize, Deserializer};
use serde_json::{Map, Value};

use crate::environment::LocalPrivatePaths;
use crate::{config, ops};

pub struct ThreadInstructions(pub RwLock<Option<String>>, pub bool);

pub struct EmptyUserInstructions;

impl UserInstructionsProvider for EmptyUserInstructions {
    fn load_user_instructions(&self) -> LoadInstructionsFuture<'_> {
        Box::pin(async { LoadedUserInstructions::default() })
    }
}

impl ThreadInstructionsProvider for ThreadInstructions {
    fn load_thread_instructions(&self) -> LoadInstructionsFuture<'_> {
        Box::pin(async {
            match self.0.read() {
                Ok(text) => LoadedUserInstructions {
                    instructions: text.clone().map(|text| Instructions { text, source: None }),
                    warnings: Vec::new(),
                },
                Err(_) => LoadedUserInstructions {
                    instructions: None,
                    warnings: vec!["thread instructions unavailable".into()],
                },
            }
        })
    }

    fn share_with_subagents(&self) -> bool {
        self.1
    }
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ThreadOptions {
    // Private Agents SDK entry path; never used for portable checkpoint import.
    #[serde(rename = "_sdk_resume_thread_id")]
    pub sdk_resume_thread_id: Option<ThreadId>,
    #[serde(rename = "_sdk_history", deserialize_with = "deserialize_sdk_history")]
    sdk_history: Option<Vec<ResponseItem>>,
    #[serde(rename = "_application_instructions")]
    pub(crate) application_instructions: Option<String>,
    #[serde(rename = "_agent_profiles")]
    pub(crate) agent_profiles: Option<crate::agent_profiles::OpeningProfiles>,
    #[serde(rename = "_sdk_instruction_files")]
    pub(crate) sdk_instruction_files: Option<Vec<crate::application_instructions::InstructionFile>>,
    #[serde(rename = "_manual_tool_slots")]
    pub(crate) manual_tool_slots: Vec<usize>,
    #[serde(rename = "_terminal_tool_slots")]
    pub(crate) terminal_tool_slots: Vec<usize>,
    #[serde(rename = "_transform_context")]
    pub(crate) transform_context: bool,
    #[serde(rename = "_before_compact")]
    pub(crate) before_compact: bool,
    #[serde(rename = "_before_complete")]
    pub(crate) before_complete: bool,
    #[serde(rename = "_buffered_responses")]
    buffered_responses: bool,
    // The public native-agent declaration; the same owner guards root and children.
    #[serde(rename = "_native_agent_roles")]
    native_agent_roles: Option<Vec<String>>,
    #[serde(rename = "_native_agent_max_depth")]
    native_agent_max_depth: Option<usize>,
    #[serde(rename = "_canonical_rules")]
    canonical_rules: bool,
    config: Map<String, Value>,
    runtime: Map<String, Value>,
    instructions: Option<String>,
    share_instructions_with_subagents: bool,
    allow_provider_model_fallback: bool,
    initial_history: Option<InitialHistory>,
    history_mode: Option<ThreadHistoryMode>,
    session_source: Option<SessionSource>,
    thread_source: Option<ThreadSource>,
    dynamic_tools: Vec<DynamicToolSpec>,
    #[serde(rename = "_hosted_tools")]
    hosted_tools: Vec<codex_tools::HostedToolSpec>,
    metrics_service_name: Option<String>,
    parent_trace: Option<W3cTraceContext>,
    environments: Option<Value>,
    reserved_thread_id: Option<ThreadId>,
    disabled_plugin_ids: Option<Vec<String>>,
    client_mcp_extensions: Map<String, Value>,
    #[serde(rename = "_sdk_hosted_verification")]
    hosted_verification: Option<Value>,
    #[serde(rename = "_sdk_chatgpt_apps")]
    chatgpt_apps: Option<Value>,
    user_instructions: Option<UserInstructions>,
    tool_policy: Option<Policy>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct Policy {
    allowed_tools: Option<Vec<ToolName>>,
    allowed_mcp_tools: Option<Vec<(String, String, ToolName)>>,
    require_managed_sandbox: bool,
    require_unified_exec: bool,
    expose_additional_permissions: Option<bool>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UserInstructions {
    instructions: Option<InstructionText>,
    #[serde(default)]
    warnings: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InstructionText {
    text: String,
    source: Option<codex_core_api::AbsolutePathBuf>,
}

impl ThreadOptions {
    pub fn permits_initial_config(&self) -> bool {
        self.sdk_resume_thread_id.is_none()
            && self.sdk_history.is_none()
            && self.initial_history.is_none()
            && self.reserved_thread_id.is_none()
    }

    pub fn take_sdk_history(
        &mut self,
        has_external_history: bool,
        has_python_tools: bool,
    ) -> anyhow::Result<Option<Vec<ResponseItem>>> {
        if self.sdk_history.is_some() {
            anyhow::ensure!(
                self.sdk_resume_thread_id.is_none()
                    && self.initial_history.is_none()
                    && self.reserved_thread_id.is_none()
                    && !has_external_history
                    && has_python_tools,
                "SDK input history requires current Python tools and no other history or identity"
            );
        }
        Ok(self.sdk_history.take())
    }

    pub async fn build(
        self,
        base_config: &str,
        base_runtime: &str,
        cloud: codex_config::CloudConfigBundleLoader,
        initial_config: Option<&Config>,
        environment_manager: &EnvironmentManager,
        private_paths: LocalPrivatePaths<'_>,
    ) -> anyhow::Result<(StartThreadOptions, Arc<ThreadInstructions>)> {
        anyhow::ensure!(
            self.sdk_history.is_none(),
            "SDK input history can only be used to start an SDK thread"
        );
        let base: Map<String, Value> = serde_json::from_str(base_runtime)?;
        for key in [
            "_native_interception_http",
            "environment",
            "deferred_environment",
            "codex_home",
            "native_thread_store",
            "codex_self_exe",
            "codex_linux_sandbox_exe",
            "code_mode_host_exe",
            "linux_sandbox_pid_namespace",
            "proxy_private_ips_via_upstream",
            "allowed_symlinked_codex_home",
        ] {
            if let Some(value) = self.runtime.get(key) {
                anyhow::ensure!(
                    base.get(key) == Some(value),
                    "{key} belongs to the Codex client runtime; create a separate client to change it"
                );
            }
        }
        let runtime = merged(base_runtime, self.runtime)?;
        let source = merged(base_config, self.config)?;
        let configuration = match initial_config {
            Some(initial)
                if serde_json::from_str::<Value>(&source)?
                    == serde_json::from_str::<Value>(base_config)?
                    && serde_json::from_str::<Map<String, Value>>(&runtime)? == base =>
            {
                initial.clone()
            }
            _ => config::load(&source, &runtime, Some(cloud)).await?.0,
        };
        let instructions = Arc::new(ThreadInstructions(
            RwLock::new(self.instructions),
            self.share_instructions_with_subagents,
        ));
        let mut options = StartThreadOptions::new(configuration);
        options
            .thread_extension_init
            .insert(codex_core::BufferedResponsesPreference(
                self.buffered_responses,
            ));
        options
            .thread_extension_init
            .insert(codex_extension_api::HostedModelTools {
                tools: self.hosted_tools,
            });
        anyhow::ensure!(
            self.hosted_verification.is_none() || self.chatgpt_apps.is_none(),
            "Select one explicit Apps declaration"
        );
        if let Some(value) = self.chatgpt_apps {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Apps {
                access_token: String,
                account_id: String,
            }
            let apps: Apps = crate::config::decode(value)
                .map_err(|_| anyhow::anyhow!("Invalid ChatGPT Apps declaration"))?;
            let manager =
                crate::auth::apps_manager(&options.config, &apps.access_token, &apps.account_id)
                    .await?;
            let registration = codex_mcp::codex_apps_mcp_server_config(
                &options.config.chatgpt_base_url,
                options.config.apps_mcp_product_sku.as_deref(),
                None,
            );
            options
                .thread_extension_init
                .insert(codex_mcp::ApplicationApps::chatgpt(registration, manager));
        }
        if let Some(value) = self.hosted_verification {
            let mut registration =
                crate::mcp_roster::decode(serde_json::json!({ "codex_apps": value }))?;
            let registration = registration
                .remove("codex_apps")
                .ok_or_else(|| anyhow::anyhow!("Missing hosted verification registration"))?;
            anyhow::ensure!(
                registration.is_local_environment(),
                "Hosted verification requires the application host"
            );
            options
                .thread_extension_init
                .insert(codex_mcp::ApplicationApps::verification(registration));
        }
        if let Some(roles) = self.native_agent_roles {
            anyhow::ensure!(
                config::native_agents(&runtime)?,
                "Native agents require their initial runtime declaration"
            );
            let max_depth = self.native_agent_max_depth.unwrap_or(1);
            anyhow::ensure!(max_depth > 0, "NativeAgents max_depth must be positive");
            options
                .thread_extension_init
                .insert(crate::native_agents::Policy {
                    roles,
                    max_depth,
                    instruction_files: self.sdk_instruction_files.unwrap_or_default(),
                    allowed: Default::default(),
                });
        }
        if self.application_instructions.is_some() {
            options
                .thread_extension_init
                .insert(crate::application_instructions::Instructions::default());
        }
        if self.canonical_rules {
            options
                .thread_extension_init
                .insert(crate::canonical_rules::Rules::default());
        }
        options.thread_instructions_provider = Some(instructions.clone());
        options.allow_provider_model_fallback = self.allow_provider_model_fallback;
        options.initial_history = self.initial_history.unwrap_or(InitialHistory::New);
        options.history_mode = self.history_mode;
        options.session_source = self.session_source;
        options.thread_source = self.thread_source;
        options.dynamic_tools = self.dynamic_tools;
        options.metrics_service_name = self.metrics_service_name;
        options.parent_trace = self.parent_trace;
        let environment = config::environment(base_runtime)?;
        let deferred = config::deferred_environment(base_runtime)?;
        let roots = environment
            .as_ref()
            .and_then(|env| env.capability_roots())
            .or_else(|| deferred.as_ref().and_then(|env| env.capability_roots()));
        // An explicit empty selection also clears roots restored from history.
        options
            .thread_extension_init
            .insert(roots.unwrap_or_default());
        options.environments = match (self.environments, environment) {
            (Some(environments), _) => Some(ops::environments(environments)?),
            (None, Some(env)) => {
                let strict_writes = env.strict_writes();
                let (selection, writes) = env
                    .selection(&options.config, environment_manager, private_paths)
                    .await?;
                env.configure_local_proxy(&mut options.config, &selection)?;
                if self.canonical_rules && env.is_local() {
                    if let [root] = selection.workspace_roots.as_slice() {
                        if root != &selection.cwd {
                            options.thread_extension_init.insert(
                                codex_core::ProjectReadPathMapping {
                                    environment_id: selection.environment_id.clone(),
                                    declared_cwd: selection.cwd.clone(),
                                    canonical_cwd: root.clone(),
                                },
                            );
                        }
                    }
                }
                if strict_writes {
                    options.thread_extension_init.insert(writes);
                }
                Some(vec![selection])
            }
            (None, None) => None,
        };
        options.reserved_thread_id = self.reserved_thread_id;
        options.disabled_plugin_ids = self.disabled_plugin_ids;
        options.client_mcp_extensions =
            codex_protocol::mcp::ClientMcpExtensions::new(self.client_mcp_extensions);
        options.user_instructions = self.user_instructions.map(|user| LoadedUserInstructions {
            instructions: user.instructions.map(|i| Instructions {
                text: i.text,
                source: i.source,
            }),
            warnings: user.warnings,
        });
        if let Some(policy) = self.tool_policy {
            options.thread_extension_init.insert(ToolPolicy {
                allowed_tools: policy.allowed_tools,
                allowed_mcp_tools: policy.allowed_mcp_tools,
                require_managed_sandbox: policy.require_managed_sandbox,
                require_unified_exec: policy.require_unified_exec,
                expose_additional_permissions: policy.expose_additional_permissions.unwrap_or(true),
            });
        }
        Ok((options, instructions))
    }
}

fn deserialize_sdk_history<'de, D>(deserializer: D) -> Result<Option<Vec<ResponseItem>>, D::Error>
where
    D: Deserializer<'de>,
{
    use serde::de::Error;

    let Some(raw) = Option::<Vec<Value>>::deserialize(deserializer)? else {
        return Ok(None);
    };
    let mut items = Vec::with_capacity(raw.len());
    let mut seen = HashSet::new();
    let mut pending = HashMap::new();
    for value in raw {
        let obj = value
            .as_object()
            .ok_or_else(|| D::Error::custom("SDK history item must be an object"))?;
        let kind = obj.get("type").and_then(Value::as_str);
        let fields: &[&str] = match kind {
            Some("message") => &["type", "id", "role", "content", "phase"],
            Some("function_call") => &["type", "id", "name", "namespace", "arguments", "call_id"],
            Some("custom_tool_call") => &["type", "id", "name", "namespace", "input", "call_id"],
            Some("function_call_output" | "custom_tool_call_output") => {
                &["type", "id", "call_id", "output"]
            }
            Some("reasoning") => &["type", "id", "summary", "content", "encrypted_content"],
            _ => return Err(D::Error::custom("Unsupported SDK history item type")),
        };
        if obj.keys().any(|key| !fields.contains(&key.as_str())) {
            return Err(D::Error::custom("Unsupported fields in SDK history"));
        }
        match kind {
            Some("message") => {
                let role = obj.get("role").and_then(Value::as_str);
                if role == Some("system") {
                    return Err(D::Error::custom(
                        "SDK history cannot preserve system messages; use Agent.instructions or a developer message",
                    ));
                }
                if !matches!(role, Some("user" | "assistant" | "developer"))
                    || !pending.is_empty()
                    || (role != Some("assistant") && obj.contains_key("phase"))
                {
                    return Err(D::Error::custom(
                        "SDK history requires valid message roles and completed calls",
                    ));
                }
                validate_sdk_content::<D>(obj.get("content"), role == Some("assistant"))?;
            }
            Some("function_call" | "custom_tool_call") => {
                let id = obj
                    .get("call_id")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .ok_or_else(|| D::Error::custom("SDK history requires a tool call_id"))?;
                if !seen.insert(id.to_string()) {
                    return Err(D::Error::custom("Duplicate SDK history tool call_id"));
                }
                pending.insert(id.to_string(), kind == Some("custom_tool_call"));
            }
            Some("function_call_output" | "custom_tool_call_output") => {
                let id = obj
                    .get("call_id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| D::Error::custom("SDK history requires a tool call_id"))?;
                if pending.remove(id) != Some(kind == Some("custom_tool_call_output")) {
                    return Err(D::Error::custom(
                        "SDK history result has no matching pending call",
                    ));
                }
                let output = obj.get("output");
                if !output.is_some_and(Value::is_string) {
                    validate_sdk_content::<D>(output, false)?;
                }
            }
            Some("reasoning") => {
                for (field, kind) in [("summary", "summary_text"), ("content", "reasoning_text")] {
                    let value = obj.get(field);
                    if field == "content" && value.is_none_or(Value::is_null) {
                        continue;
                    }
                    let parts = value
                        .and_then(Value::as_array)
                        .ok_or_else(|| D::Error::custom("SDK reasoning content must be a list"))?;
                    for part in parts {
                        let part = part.as_object().ok_or_else(|| {
                            D::Error::custom("SDK reasoning content must be an object")
                        })?;
                        if part.keys().any(|key| key != "type" && key != "text")
                            || part.get("type").and_then(Value::as_str) != Some(kind)
                            || !part.get("text").is_some_and(Value::is_string)
                        {
                            return Err(D::Error::custom("Unsupported SDK reasoning content"));
                        }
                    }
                }
            }
            _ => unreachable!(),
        }
        items.push(
            serde_json::from_value(value)
                .map_err(|_| D::Error::custom("Invalid SDK history response item"))?,
        );
    }
    if !pending.is_empty() {
        return Err(D::Error::custom(
            "SDK history has tool calls without completed results",
        ));
    }
    Ok(Some(items))
}

fn validate_sdk_content<'de, D>(content: Option<&Value>, assistant: bool) -> Result<(), D::Error>
where
    D: Deserializer<'de>,
{
    use serde::de::Error;

    let parts = content
        .and_then(Value::as_array)
        .filter(|parts| !parts.is_empty())
        .ok_or_else(|| D::Error::custom("SDK history content requires a nonempty list"))?;
    for part in parts {
        let part = part
            .as_object()
            .ok_or_else(|| D::Error::custom("SDK history content must be an object"))?;
        let fields: &[&str] = match (part.get("type").and_then(Value::as_str), assistant) {
            (Some("output_text"), true) | (Some("input_text"), false) => &["type", "text"],
            (Some("input_image"), false) => {
                if part.contains_key("image_url") == part.contains_key("file_id") {
                    return Err(D::Error::custom(
                        "SDK history image requires exactly one image_url or file_id",
                    ));
                }
                &["type", "image_url", "file_id", "detail"]
            }
            (Some("input_file"), false) => {
                if ["file_id", "file_data", "file_url"]
                    .iter()
                    .filter(|key| part.contains_key(**key))
                    .count()
                    != 1
                {
                    return Err(D::Error::custom(
                        "SDK history file requires exactly one source",
                    ));
                }
                &["type", "file_id", "file_data", "file_url", "filename"]
            }
            _ => return Err(D::Error::custom("Unsupported SDK history content type")),
        };
        if part.keys().any(|key| !fields.contains(&key.as_str())) {
            return Err(D::Error::custom(
                "Unsupported fields in SDK history content",
            ));
        }
    }
    Ok(())
}

fn merged(base: &str, overrides: Map<String, Value>) -> anyhow::Result<String> {
    let mut result: Value = serde_json::from_str(base)?;
    merge_objects(&mut result, Value::Object(overrides));
    Ok(serde_json::to_string(&result)?)
}

fn merge_objects(base: &mut Value, overrides: Value) {
    match (base, overrides) {
        (Value::Object(base), Value::Object(overrides)) => {
            for (key, value) in overrides {
                merge_objects(base.entry(key).or_insert(Value::Null), value);
            }
        }
        (base, value) => *base = value,
    }
}
