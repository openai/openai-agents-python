//! Native configuration loading; defaults and precedence stay owned by Codex.
use anyhow::{Context, Result, bail};
use codex_config::{CloudConfigBundleLoader, LoaderOverrides};
use codex_core::config::{ConfigBuilder, ConfigOverrides};
use codex_core_api::{AbsolutePathBuf, Config, SqliteConfig};
use codex_protocol::config_types::{ApprovalsReviewer, Personality, SandboxMode};
use codex_protocol::models::PermissionProfile;
use codex_protocol::protocol::AskForApproval;
use serde::{Deserialize, Deserializer, de::DeserializeOwned};
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Arc;

/// Unlike ordinary nested Option decoding, JSON null explicitly clears a value.
pub fn optional<'de, D, T>(d: D) -> std::result::Result<Option<Option<T>>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(d).map(Some)
}

/// Native serde types often ignore unknown fields; a bridge must not lose options.
pub fn decode<T: DeserializeOwned>(value: Value) -> Result<T> {
    let mut ignored = Vec::new();
    let result = serde_ignored::deserialize(value, |path| ignored.push(path.to_string()))?;
    if !ignored.is_empty() {
        bail!("unknown options: {}", ignored.join(", "));
    }
    Ok(result)
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct RuntimeOptions {
    // Set by Agent.session before creating the runtime, never by a turn.
    #[serde(rename = "_native_turn_control")]
    native_turn_control: bool,
    #[serde(rename = "_native_agents")]
    native_agents: bool,
    // Interceptor-enabled Agent.session selects transport before constructing
    // the model provider. Keep this on the live runtime, never in saved TOML.
    #[serde(rename = "_native_interception_http")]
    native_interception_http: Option<crate::native_interception::Scope>,
    // The facade owns this signal on its live runtime; checkpoint runtime is ignored.
    #[serde(rename = "_sdk_native_config")]
    sdk_native_config: bool,
    #[serde(rename = "_sdk_codex_login")]
    sdk_codex_login: Option<crate::auth::SavedCodexLogin>,
    #[serde(rename = "_sdk_project_search")]
    project_search: Option<crate::project_search::Options>,
    environment: Option<crate::environment::EnvironmentOptions>,
    deferred_environment: Option<crate::environment::DeferredEnvironmentOptions>,
    codex_home: Option<PathBuf>,
    native_goal_store: Option<AbsolutePathBuf>,
    native_thread_store: Option<AbsolutePathBuf>,
    #[serde(rename = "_sdk_resume_thread_id")]
    sdk_resume_thread_id: Option<codex_protocol::ThreadId>,
    #[serde(default = "strict_by_default")]
    strict_config: bool,
    fallback_cwd: Option<PathBuf>,
    loader: LoaderOptions,
    linux_sandbox_pid_namespace: Option<String>,
    proxy_private_ips_via_upstream: Option<bool>,
    model_provider_include_internal_metadata: Option<bool>,
    code_mode_host_exe: Option<PathBuf>,
    #[cfg(target_os = "macos")]
    allowed_symlinked_codex_home: Option<AbsolutePathBuf>,
    model: Option<String>,
    review_model: Option<String>,
    cwd: Option<PathBuf>,
    approval_policy: Option<AskForApproval>,
    approvals_reviewer: Option<ApprovalsReviewer>,
    sandbox_mode: Option<SandboxMode>,
    permission_profile: Option<PermissionProfile>,
    default_permissions: Option<String>,
    persisted_permission_profile_id: Option<String>,
    model_provider: Option<String>,
    #[serde(deserialize_with = "optional")]
    service_tier: Option<Option<String>>,
    codex_self_exe: Option<PathBuf>,
    codex_linux_sandbox_exe: Option<PathBuf>,
    main_execve_wrapper_exe: Option<PathBuf>,
    default_zsh_path: Option<AbsolutePathBuf>,
    base_instructions: Option<String>,
    developer_instructions: Option<String>,
    personality: Option<Personality>,
    compact_prompt: Option<String>,
    show_raw_agent_reasoning: Option<bool>,
    tools_web_search_request: Option<bool>,
    ephemeral: Option<bool>,
    bypass_hook_trust: Option<bool>,
    additional_writable_roots: Vec<PathBuf>,
    workspace_roots: Option<Vec<AbsolutePathBuf>>,
}

/// Supply native path metadata without creating a home or loading ambient config.
pub fn prepare_runtime(runtime_json: &str) -> Result<String> {
    let mut runtime: serde_json::Map<String, Value> = serde_json::from_str(runtime_json)?;
    let home =
        std::env::temp_dir().join(format!("codex-agents-{}", codex_protocol::ThreadId::new()));
    if runtime.get("codex_home").is_none_or(Value::is_null) {
        runtime.insert("codex_home".into(), serde_json::to_value(home.as_path())?);
    }
    if runtime.get("cwd").is_none_or(Value::is_null)
        && runtime.get("fallback_cwd").is_none_or(Value::is_null)
    {
        runtime.insert("cwd".into(), serde_json::to_value(home.join("workspace"))?);
    }
    let loader = runtime.entry("loader").or_insert(serde_json::json!({}));
    let loader = loader
        .as_object_mut()
        .context("runtime.loader must be an object")?;
    for key in [
        "ignore_user_config",
        "ignore_project_config",
        "ignore_user_and_project_exec_policy_rules",
        "ignore_managed_requirements",
        "ignore_login_requirements",
    ] {
        loader.entry(key).or_insert(Value::Bool(true));
    }
    for key in [
        "managed_config_path",
        "system_config_path",
        "system_requirements_path",
    ] {
        loader
            .entry(key)
            .or_insert(serde_json::to_value(home.as_path().join(key))?);
    }
    loader
        .entry("macos_managed_config_requirements_base64")
        .or_insert(Value::String(String::new()));
    #[cfg(target_os = "macos")]
    loader
        .entry("managed_preferences_base64")
        .or_insert(Value::String(String::new()));
    Ok(serde_json::to_string(&runtime)?)
}

fn strict_by_default() -> bool {
    true
}

pub fn environment(runtime_json: &str) -> Result<Option<crate::environment::EnvironmentOptions>> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    anyhow::ensure!(
        options.deferred_environment.is_none() || options.environment.is_none(),
        "environment and deferred_environment cannot be combined"
    );
    Ok(options.environment)
}

pub fn deferred_environment(
    runtime_json: &str,
) -> Result<Option<crate::environment::DeferredEnvironmentOptions>> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    Ok(options.deferred_environment)
}

pub fn native_turn_control(runtime_json: &str) -> Result<bool> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    Ok(options.native_turn_control)
}

pub fn native_agents(runtime_json: &str) -> Result<bool> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    Ok(options.native_agents)
}

pub fn sdk_native_config(runtime_json: &str) -> Result<bool> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    Ok(options.sdk_native_config)
}

pub fn sdk_codex_login(runtime_json: &str) -> Result<Option<crate::auth::SavedCodexLogin>> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    Ok(options.sdk_codex_login)
}

pub fn native_goal_store_enabled(runtime_json: &str) -> Result<bool> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    Ok(options.native_goal_store.is_some())
}

pub fn native_thread_store(runtime_json: &str) -> Result<Option<AbsolutePathBuf>> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    Ok(options.native_thread_store)
}

pub fn sdk_resume_thread_id(runtime_json: &str) -> Result<Option<codex_protocol::ThreadId>> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    Ok(options.sdk_resume_thread_id)
}

pub fn code_mode_provider(
    runtime_json: &str,
    config: &Config,
) -> Result<Option<Arc<dyn codex_code_mode::CodeModeSessionProvider>>> {
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    if !code_mode_host_enabled(config) {
        return Ok(None);
    }
    Ok(options.code_mode_host_exe.map(|program| {
        Arc::new(codex_code_mode::ProcessOwnedCodeModeSessionProvider::with_host_program(program))
            as Arc<dyn codex_code_mode::CodeModeSessionProvider>
    }))
}

pub fn code_mode_host_enabled(config: &Config) -> bool {
    config
        .features
        .enabled(codex_core_api::Feature::CodeModeHost)
        || config.code_mode.disable_in_process_fallback
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct LoaderOptions {
    packaged_defaults_path: Option<AbsolutePathBuf>,
    user_config_path: Option<AbsolutePathBuf>,
    user_config_profile: Option<String>,
    managed_config_path: Option<PathBuf>,
    system_config_path: Option<PathBuf>,
    system_requirements_path: Option<PathBuf>,
    ignore_managed_requirements: bool,
    ignore_login_requirements: bool,
    ignore_user_config: bool,
    ignore_project_config: bool,
    ignore_user_and_project_exec_policy_rules: bool,
    #[cfg(target_os = "macos")]
    managed_preferences_base64: Option<String>,
    macos_managed_config_requirements_base64: Option<String>,
}

fn toml_value(value: Value) -> Result<toml::Value> {
    Ok(match value {
        Value::Null => bail!("TOML cannot represent null"),
        Value::Bool(value) => toml::Value::Boolean(value),
        Value::String(value) => toml::Value::String(value),
        Value::Number(value) => {
            let number = value.to_string();
            if number.contains(['.', 'e', 'E']) {
                let value: f64 = number.parse().context("invalid TOML float")?;
                anyhow::ensure!(value.is_finite(), "TOML float must be finite");
                toml::Value::Float(value)
            } else {
                toml::Value::Integer(number.parse().context("TOML integer exceeds i64 range")?)
            }
        }
        Value::Array(values) => {
            toml::Value::Array(values.into_iter().map(toml_value).collect::<Result<_>>()?)
        }
        Value::Object(values) => toml::Value::Table(
            values
                .into_iter()
                .map(|(key, value)| Ok((key, toml_value(value)?)))
                .collect::<Result<_>>()?,
        ),
    })
}

pub async fn load(
    config_json: &str,
    runtime_json: &str,
    cloud: Option<CloudConfigBundleLoader>,
) -> Result<(Config, CloudConfigBundleLoader)> {
    let values: serde_json::Map<String, Value> = serde_json::from_str(config_json)
        .context("config must be an object of native TOML options")?;
    let mut overrides = vec![
        (
            "openai_base_url".to_owned(),
            toml::Value::String("https://api.openai.com/v1".into()),
        ),
        ("project_doc_max_bytes".to_owned(), toml::Value::Integer(0)),
        (
            "features.skip_host_skill_discovery".to_owned(),
            toml::Value::Boolean(true),
        ),
        (
            "skills.bundled.enabled".to_owned(),
            toml::Value::Boolean(false),
        ),
        (
            "features.shell_snapshot".to_owned(),
            toml::Value::Boolean(false),
        ),
        (
            "features.api_key_model_discovery".to_owned(),
            toml::Value::Boolean(false),
        ),
        ("analytics.enabled".to_owned(), toml::Value::Boolean(false)),
    ];
    let options: RuntimeOptions = decode(serde_json::from_str(runtime_json)?)?;
    anyhow::ensure!(
        options.linux_sandbox_pid_namespace.is_none()
            && options.proxy_private_ips_via_upstream.is_none(),
        "Configure executor startup options on the remote exec-server"
    );
    #[cfg(target_os = "macos")]
    anyhow::ensure!(
        options.allowed_symlinked_codex_home.is_none(),
        "Configure executor startup options on the remote exec-server"
    );
    let loader = options.loader;
    let loader = LoaderOverrides {
        packaged_defaults_path: loader.packaged_defaults_path,
        user_config_path: loader.user_config_path,
        user_config_profile: loader
            .user_config_profile
            .map(|value| value.parse())
            .transpose()?,
        managed_config_path: loader.managed_config_path,
        system_config_path: loader.system_config_path,
        system_requirements_path: loader.system_requirements_path,
        ignore_managed_requirements: loader.ignore_managed_requirements,
        ignore_login_requirements: loader.ignore_login_requirements,
        ignore_user_config: loader.ignore_user_config,
        ignore_project_config: loader.ignore_project_config,
        ignore_user_and_project_exec_policy_rules: loader.ignore_user_and_project_exec_policy_rules,
        #[cfg(target_os = "macos")]
        managed_preferences_base64: loader.managed_preferences_base64,
        macos_managed_config_requirements_base64: loader.macos_managed_config_requirements_base64,
    };
    let cloud = cloud.unwrap_or_default();
    // A live SDK/HTTP route selects its own provider before this load, at both
    // engine and thread startup. Never merge ambient provider definitions into it.
    let ambient_provider = options.sdk_native_config
        && options.model_provider.is_none()
        && !values.contains_key("model_provider");
    if options.sdk_native_config {
        let source_home = match codex_core::config::find_codex_home() {
            Ok(home) => home,
            // A fresh SDK persistent store may not have created its home yet.
            // In that case there is no user config to project. Keep the existing
            // isolated defaults, never fall back to a different user's home.
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                AbsolutePathBuf::from_absolute_path(
                    options
                        .codex_home
                        .as_ref()
                        .context("Missing SDK working home")?,
                )?
            }
            Err(_) => bail!("Unable to locate native user configuration"),
        };
        let source = codex_config::loader::load_config_layers_state(
            codex_exec_server::LOCAL_FS.as_ref(),
            &source_home,
            None,
            &[],
            codex_config::ConfigLoadOptions {
                loader_overrides: LoaderOverrides {
                    user_config_path: None,
                    ignore_user_config: false,
                    ignore_project_config: true,
                    ignore_user_and_project_exec_policy_rules: true,
                    ..loader.clone()
                },
                strict_config: false,
                cloud_config_bundle: cloud.clone(),
            },
            &codex_config::NoopThreadConfigLoader,
        )
        .await
        // Config errors may include source text. Do not expose credentials.
        .map_err(|_| anyhow::anyhow!("Unable to load native user configuration"))?;
        let effective = source.effective_config();
        // Keep the facade's historical default when no user selection is configured.
        overrides.push(("model".into(), toml::Value::String("gpt-5.1-codex".into())));
        for key in ["projects", "project_root_markers"] {
            if let Some(value) = effective.get(key) {
                overrides.push((key.into(), value.clone()));
            }
        }
        if ambient_provider {
            for key in [
                "model",
                "model_provider",
                "model_providers",
                "openai_base_url",
            ] {
                if let Some(value) = effective.get(key) {
                    overrides.push((key.into(), value.clone()));
                }
            }
        }
    }
    // Live application configuration and permissions always win over facade defaults.
    overrides.extend(
        values
            .into_iter()
            .map(|(key, value)| {
                let value =
                    toml_value(value).with_context(|| format!("invalid TOML value for {key}"))?;
                Ok((key, value))
            })
            .collect::<Result<Vec<_>>>()?,
    );
    let harness = ConfigOverrides {
        model: options.model,
        review_model: options.review_model,
        cwd: options.cwd,
        approval_policy: options.approval_policy,
        approvals_reviewer: options.approvals_reviewer,
        sandbox_mode: options.sandbox_mode,
        permission_profile: options.permission_profile,
        default_permissions: options.default_permissions,
        persisted_permission_profile_id: options.persisted_permission_profile_id,
        model_provider: options.model_provider,
        service_tier: options.service_tier,
        codex_self_exe: options.codex_self_exe,
        codex_linux_sandbox_exe: options.codex_linux_sandbox_exe,
        main_execve_wrapper_exe: options.main_execve_wrapper_exe,
        default_zsh_path: options.default_zsh_path,
        base_instructions: options.base_instructions,
        developer_instructions: options.developer_instructions,
        personality: options.personality,
        compact_prompt: options.compact_prompt,
        show_raw_agent_reasoning: options.show_raw_agent_reasoning,
        tools_web_search_request: options.tools_web_search_request,
        ephemeral: options.ephemeral,
        bypass_hook_trust: options.bypass_hook_trust,
        additional_writable_roots: options.additional_writable_roots,
        workspace_roots: options.workspace_roots,
    };
    let mut builder = ConfigBuilder::default()
        .cli_overrides(overrides)
        .harness_overrides(harness)
        .loader_overrides(loader)
        .cloud_config_bundle(cloud.clone())
        .strict_config(options.strict_config)
        .fallback_cwd(options.fallback_cwd);
    if let Some(home) = options.codex_home {
        builder = builder.codex_home(home);
    }
    let built = builder.build().await;
    let mut config = if options.sdk_native_config {
        built.map_err(|_| anyhow::anyhow!("Unable to load isolated SDK configuration"))?
    } else {
        built?
    };
    if let Some(environment) = &options.environment {
        environment.restrict_network_approval(&mut config)?;
    }
    // Only current application runtime configuration authorizes native storage.
    // Config and checkpoint sqlite_home values cannot choose its location.
    if let (Some(threads), Some(goals)) = (&options.native_thread_store, &options.native_goal_store)
    {
        anyhow::ensure!(
            threads == goals,
            "native thread and goal storage must use the same application directory"
        );
    }
    if let Some(home) = options.native_thread_store.or(options.native_goal_store) {
        config.sqlite = SqliteConfig::from_sqlite_home(home);
    }
    anyhow::ensure!(
        !config.features.enabled(codex_core_api::Feature::MemoryTool),
        "Native memories require host files; this SDK runtime keeps state in memory"
    );
    anyhow::ensure!(
        !config.bundled_skills_enabled(),
        "Bundled skills require host files; supply skills through Environment.capability_directories"
    );
    anyhow::ensure!(
        config
            .features
            .enabled(codex_core_api::Feature::SkipHostSkillDiscovery),
        "Host skill discovery is unavailable; supply Environment.capability_directories"
    );
    if options.strict_config {
        // Validate after merging: a partial provider override may not deserialize
        // on its own, which prevents the native loader's early ignored-key check.
        let effective = config.config_layer_stack.effective_config();
        let contents = toml::to_string(&effective)?;
        if let Some(error) = codex_config::config_error_from_ignored_toml_fields::<
            codex_config::config_toml::ConfigToml,
        >("Python configuration", &contents)
        {
            if options.sdk_native_config {
                bail!("Unsupported isolated SDK configuration");
            }
            bail!("{}", error.message);
        }
    }
    if let Some(enabled) = options.model_provider_include_internal_metadata {
        config.model_provider.include_internal_metadata = enabled;
        if let Some(provider) = config.model_providers.get_mut(&config.model_provider_id) {
            provider.include_internal_metadata = enabled;
        }
    }
    if let Some(scope) = options.native_interception_http {
        // The pinned WS LastResponse is made from the already-edited stream;
        // its previous_response_id still names the server's unedited prefix.
        // Resolve first so this works with built-ins and config-layer providers
        // without changing base_url, auth, retry policy, or model capabilities.
        config.model_provider.supports_websockets = false;
        for (id, provider) in &mut config.model_providers {
            // A native resumed ThreadSpawn selects its saved provider from
            // this inherited map, not necessarily the root's current one.
            if scope == crate::native_interception::Scope::ThreadTree
                || *id == config.model_provider_id
            {
                provider.supports_websockets = false;
            }
        }
    }
    Ok((config, cloud))
}

/// Current application root and opt-in tools, including checkpoint restoration.
pub fn project_search(runtime_json: &str) -> Result<Option<crate::project_search::Options>> {
    Ok(decode::<RuntimeOptions>(serde_json::from_str(runtime_json)?)?.project_search)
}
