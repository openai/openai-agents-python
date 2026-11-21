//! Codex-owned configuration and provider-scoped serving authentication.
use std::path::{Path, PathBuf};
use std::sync::Arc;

use codex_config::{
    ApplicationRequirementsToml, CloudConfigBundleLoader, ConfigLoadOptions, LoaderOverrides,
    NetworkDomainPermissionToml,
};
use codex_core::config::{
    Config, ConfigBuilder, ConfigOverrides, bootstrap_auth_config, find_codex_home,
    load_config_toml_with_layer_stack,
};
use codex_core_api::{
    AuthManager, CodexAppsToolsCache, CodexHomeUserInstructionsProvider, EnvironmentManager,
    ExecServerRuntimeOptions, ExtensionRegistryBuilder, SessionSource, ThreadManager,
    build_models_manager, init_state_db, local_agent_graph_store_from_state_db,
    passthrough_image_store, resolve_installation_id, thread_store_from_config,
};
use codex_features::Feature;
use codex_http_client::{DestinationPolicy, NetworkPolicyController};
use codex_login::{CodexAuth, ExternalAuth, ExternalAuthFuture, ExternalAuthRefreshContext};
use codex_protocol::config_types::{ShellEnvironmentPolicyInherit, WebSearchMode};
use codex_protocol::models::{PermissionProfile, SandboxEnforcement};
use codex_protocol::openai_models::{InputModality, ModelInfo, WebSearchToolType};
use codex_protocol::permissions::{
    FileSystemAccessMode, FileSystemSandboxEntry, FileSystemSandboxPolicy,
    FileSystemSandboxPolicyContext,
};
use codex_protocol::protocol::SandboxPolicy;
use serde_json::{Value, json};

type Result<T> = std::result::Result<T, String>;

pub struct CoreServices {
    pub children: Option<Arc<crate::children::Children>>,
    pub config: Config,
    pub manager: Arc<ThreadManager>,
    home: PathBuf,
    cwd: PathBuf,
    overrides: Vec<(String, toml::Value)>,
    cloud: CloudConfigBundleLoader,
    network: NetworkPolicyController,
    local_network: NetworkPolicyController,
    policy_auth: Arc<AuthManager>,
    policy_source_generation: u64,
    helpers: HelperPaths,
}

struct HelperPaths {
    executable: PathBuf,
    linux_sandbox: Option<PathBuf>,
}

impl HelperPaths {
    fn prepare(executable: PathBuf) -> Result<Self> {
        let executable = executable
            .canonicalize()
            .map_err(|_| "The packaged Codex runtime helper is missing".to_owned())?;
        let linux_sandbox = if cfg!(target_os = "linux") {
            let packaged = executable
                .parent()
                .ok_or("Codex helper has no parent directory")?
                .join("codex-linux-sandbox");
            if !packaged.is_file() {
                return Err("The packaged Codex Linux sandbox helper is missing".into());
            }
            Some(packaged)
        } else {
            None
        };
        Ok(Self {
            executable,
            linux_sandbox,
        })
    }

    fn overrides(&self, cwd: &Path) -> ConfigOverrides {
        ConfigOverrides {
            cwd: Some(cwd.to_path_buf()),
            codex_self_exe: Some(self.executable.clone()),
            codex_linux_sandbox_exe: self.linux_sandbox.clone(),
            ephemeral: Some(true),
            ..Default::default()
        }
    }
    fn protect(&self, config: &mut Config) -> Result<()> {
        let profile = config.permissions.permission_profile().clone();
        if profile.enforcement() != SandboxEnforcement::Managed {
            return Ok(());
        }
        let directory = self
            .executable
            .parent()
            .ok_or("Codex helper has no parent directory")?;
        let effective = config.permissions.file_system_sandbox_policy();
        let roots = effective.get_writable_roots_with_cwd(config.cwd.as_path());
        if !effective.can_write_local_path_with_cwd(directory, config.cwd.as_path())
            && !roots
                .iter()
                .any(|root| root.root.as_path().starts_with(directory))
        {
            return Ok(());
        }
        let root = roots.iter().filter(|root| directory.starts_with(root.root.as_path()))
            .min_by_key(|root| root.root.as_path().components().count())
            .ok_or("Cannot protect Codex runtime helpers under the current permissions; install the optional wheel outside writable workspace paths")?;
        let relative = directory
            .strip_prefix(root.root.as_path())
            .map_err(|e| e.to_string())?;
        let first = relative.components().next().ok_or("Codex runtime helpers occupy a writable root; install the optional wheel outside that root")?;
        // Protect the first descendant of the writable mount, not just the binary's
        // directory: otherwise a tool could rename an ancestor and replace the original path.
        let boundary = root.root.as_path().join(first);
        if config.cwd.as_path().starts_with(&boundary) {
            return Err("Protecting Codex runtime helpers would make the workspace read-only; install the optional wheel in a separate virtualenv".into());
        }
        let (mut file_system, network) = profile.to_runtime_permissions();
        let cwd = config.cwd.clone().into();
        let workspace_roots: Vec<_> = config
            .permissions
            .workspace_roots()
            .iter()
            .cloned()
            .map(Into::into)
            .collect();
        let temporary_directories = vec![
            codex_core_api::AbsolutePathBuf::from_absolute_path(std::env::temp_dir())
                .map_err(|e| e.to_string())?
                .into(),
        ];
        let context = FileSystemSandboxPolicyContext {
            cwd: &cwd,
            workspace_roots: &workspace_roots,
            user_home_dir: None,
            temporary_directories: Some(&temporary_directories),
        };
        // Explicit nested write grants must not reopen the installed helper environment.
        for entry in &mut file_system.entries {
            if entry.access.can_write()
                && FileSystemSandboxPolicy::restricted(vec![entry.clone()])
                    .resolved_entries(&context)
                    .iter()
                    .any(|(path, _)| {
                        path.to_abs_path()
                            .is_ok_and(|path| path.as_path().starts_with(&boundary))
                    })
            {
                entry.access = FileSystemAccessMode::Read;
            }
        }
        let directory = codex_core_api::AbsolutePathBuf::from_absolute_path(boundary)
            .map_err(|e| e.to_string())?;
        file_system.entries.push(FileSystemSandboxEntry::new(
            directory.into(),
            FileSystemAccessMode::Read,
        ));
        // Tighten the existing profile through Core so managed denials and constraints remain intact.
        config
            .permissions
            .set_permission_profile(
                PermissionProfile::from_runtime_permissions_with_enforcement(
                    profile.enforcement(),
                    &file_system,
                    network,
                ),
            )
            .map_err(|e| e.to_string())
    }
}

// ExternalAuth replaces only this manager's serving identity, without writing saved credentials.
struct ProviderApiKey(String);
impl ExternalAuth for ProviderApiKey {
    fn resolve(&self) -> ExternalAuthFuture<'_, CodexAuth> {
        Box::pin(async { Ok(CodexAuth::from_api_key(&self.0)) })
    }
    fn refresh(&self, _: ExternalAuthRefreshContext) -> ExternalAuthFuture<'_, CodexAuth> {
        self.resolve()
    }
}

fn destination_policy(application: Option<&ApplicationRequirementsToml>) -> DestinationPolicy {
    match application.and_then(|a| a.network.as_ref()) {
        Some(network) if network.enabled => DestinationPolicy::Restricted {
            allowed_hosts: network
                .domains
                .iter()
                .filter(|(_, permission)| **permission == NetworkDomainPermissionToml::Allow)
                .map(|(host, _)| host.clone())
                .collect(),
        },
        _ => DestinationPolicy::Unrestricted,
    }
}

fn parse_overrides(overrides: Vec<String>) -> Result<Vec<(String, toml::Value)>> {
    overrides
        .into_iter()
        .map(|entry| {
            let (key, value) = entry
                .split_once('=')
                .ok_or("Codex configuration overrides must use key=value")?;
            if key.trim().is_empty() {
                return Err("Codex configuration override keys cannot be empty".into());
            }
            Ok((
                key.trim().to_owned(),
                value
                    .trim()
                    .parse::<toml::Value>()
                    .unwrap_or_else(|_| toml::Value::String(value.trim().to_owned())),
            ))
        })
        .collect()
}

fn install_residency_requirement(config: &Config) {
    // Codex routes residency through process-wide defaults. Keep an observed requirement
    // installed when another provider has no requirement, rather than weakening routing.
    if let Some(requirement) = config.enforce_residency.value() {
        codex_login::default_client::set_default_client_residency_requirement(Some(requirement));
    }
}

fn bind_policy(config: &mut Config, controller: &NetworkPolicyController) {
    install_residency_requirement(config);
    let application = config
        .config_layer_stack
        .requirements()
        .application
        .as_ref()
        .map(|a| &a.value);
    controller.publish(
        controller.policy().revision(),
        destination_policy(application),
    );
    config.application_network_policy = controller.policy();
}

fn apply_shell_environment_defaults(config: &mut Config) {
    let configured = config.config_layer_stack.layers_high_to_low().any(|layer| {
        layer
            .config
            .get("shell_environment_policy")
            .and_then(|value| value.get("inherit"))
            .is_some()
    });
    if !configured {
        config.permissions.shell_environment_policy.inherit = ShellEnvironmentPolicyInherit::Core;
    }
}

async fn read_config(builder: ConfigBuilder) -> Result<Config> {
    let mut config = builder.build().await.map_err(|error| error.to_string())?;
    config
        .config_layer_stack
        .ensure_config_server_applied()
        .map_err(|error| error.to_string())?;
    apply_shell_environment_defaults(&mut config);
    Ok(config)
}

async fn refresh_shared_config(
    builder: ConfigBuilder,
    controller: &NetworkPolicyController,
) -> Result<Config> {
    match read_config(builder).await {
        Ok(mut config) => {
            bind_policy(&mut config, controller);
            Ok(config)
        }
        Err(error) => {
            controller.unavailable(controller.policy().revision());
            Err(error)
        }
    }
}

fn shared_config_builder(services: &CoreServices) -> ConfigBuilder {
    ConfigBuilder::default()
        .codex_home(services.home.clone())
        .cli_overrides(services.overrides.clone())
        .harness_overrides(services.helpers.overrides(&services.cwd))
        .cloud_config_bundle(services.cloud.clone())
}

fn validate_manager_config(services: &CoreServices, config: &Config) -> Result<()> {
    if !same_policy_source(&services.policy_auth, services.policy_source_generation)
        || config.model_provider_id != services.config.model_provider_id
        || serde_json::to_value(&config.model_provider).ok()
            != serde_json::to_value(&services.config.model_provider).ok()
        || serde_json::to_value(&config.model_catalog).ok()
            != serde_json::to_value(&services.config.model_catalog).ok()
        || config.auth_config() != services.config.auth_config()
        || config.enforce_residency.get() != services.config.enforce_residency.get()
    {
        services
            .network
            .unavailable(services.network.policy().revision());
        return Err("Codex authentication, provider, or catalog configuration changed; create a new CodexProvider before continuing".into());
    }
    Ok(())
}

pub async fn build_services(
    cwd: String,
    helper_executable: PathBuf,
    overrides: Vec<String>,
    api_key: Option<String>,
    children: Option<Arc<crate::children::Children>>,
    model: Option<&str>,
) -> Result<CoreServices> {
    codex_utils_rustls_provider::ensure_rustls_crypto_provider();
    let cwd = PathBuf::from(cwd)
        .canonicalize()
        .map_err(|_| "Codex cwd must name an existing directory".to_owned())?;
    if !cwd.is_dir() {
        return Err("Codex cwd must name an existing directory".into());
    }
    let home = find_codex_home().map_err(|e| e.to_string())?.to_path_buf();
    let helpers = HelperPaths::prepare(helper_executable)?;
    let mut overrides = parse_overrides(overrides)?;
    if let Some(profile) = &children {
        overrides.splice(0..0, parse_overrides(profile.defaults())?);
    }
    // These are the provider's defaults, preceding caller overrides.
    overrides.splice(
        0..0,
        [
            ("features.apps".into(), toml::Value::Boolean(false)),
            // A snapshot would launch a login shell for every ephemeral thread.
            (
                "features.shell_snapshot".into(),
                toml::Value::Boolean(false),
            ),
            ("web_search".into(), toml::Value::String("disabled".into())),
        ],
    );
    let loader = LoaderOverrides::default();
    let local = codex_config::loader::load_local_application_requirements(
        codex_exec_server::LOCAL_FS.as_ref(),
        &loader,
    )
    .await
    .map_err(|e| e.to_string())?
    .compose(Default::default())
    .map_err(|e| e.to_string())?;
    let local_network = NetworkPolicyController::default();
    local_network.publish(
        local_network.policy().revision(),
        destination_policy(local.as_ref()),
    );
    let network = NetworkPolicyController::default();
    let absolute_cwd =
        codex_core_api::AbsolutePathBuf::from_absolute_path(&cwd).map_err(|e| e.to_string())?;
    let bootstrap = load_config_toml_with_layer_stack(
        &home,
        Some(&absolute_cwd),
        overrides.clone(),
        ConfigLoadOptions {
            loader_overrides: loader,
            strict_config: false,
            cloud_config_bundle: CloudConfigBundleLoader::default(),
        },
    )
    .await
    .map_err(|e| e.to_string())?;
    let mut auth_config = bootstrap_auth_config(&home, &bootstrap).map_err(|e| e.to_string())?;
    let factory = auth_config
        .auth_route_config
        .http_client_factory()
        .clone()
        .with_network_policy(local_network.policy());
    let endpoint = codex_backend_client::Client::new(
        auth_config
            .chatgpt_base_url
            .as_deref()
            .unwrap_or("https://chatgpt.com/backend-api/"),
        factory.clone(),
    )
    .config_bundle_url();
    auth_config.auth_route_config = codex_login::AuthRouteConfig::from_http_client_factory(
        factory.clone().with_network_policy(
            local_network
                .policy()
                .restrict_to_endpoints(endpoint.parse().into_iter().collect()),
        ),
    )
    .with_local_bootstrap_factory(factory)
    .with_application_network_policy(network.policy());
    // Workspace policy always uses the saved identity, never the caller's serving key.
    let policy_auth = AuthManager::shared_from_auth_config(auth_config.clone(), false)
        .await
        .map_err(|e| e.to_string())?;
    codex_login::GatewayLoginControl::for_runtime(&policy_auth.runtime_config())
        .require_explicit_login();
    let cloud = codex_cloud_config::managed_config_bundle_loader(
        policy_auth.clone(),
        auth_config
            .chatgpt_base_url
            .clone()
            .unwrap_or_else(|| "https://chatgpt.com/backend-api/".into()),
        home.clone(),
        auth_config.auth_route_config.http_client_factory().clone(),
        bootstrap.config_layer_stack.requirements_toml(),
    );
    let mut config = read_config(
        ConfigBuilder::default()
            .codex_home(home.clone())
            .cli_overrides(overrides.clone())
            .harness_overrides(helpers.overrides(&cwd))
            .cloud_config_bundle(cloud.clone()),
    )
    .await?;
    bind_policy(&mut config, &network);
    let auth = AuthManager::shared_from_config(&config, true)
        .await
        .map_err(|e| e.to_string())?;
    codex_login::GatewayLoginControl::for_runtime(&auth.runtime_config()).require_explicit_login();
    if let Some(key) = api_key {
        auth.set_external_auth(Arc::new(ProviderApiKey(key)))
            .await
            .map_err(|e| e.to_string())?;
    }
    let mut models = build_models_manager(&config, auth.clone());
    if let Some(profile) = &children {
        profile.validate(&config)?;
        let name = model.ok_or("Select an explicit V1 catalog model for native children.")?;
        let catalog = models
            .raw_model_catalog(
                codex_models_manager::manager::RefreshStrategy::OnlineIfUncached,
                config.http_client_factory(),
            )
            .await;
        models = Arc::new(codex_models_manager::manager::StaticModelsManager::new(
            Some(auth.clone()),
            crate::children::catalog(catalog, name)?,
            codex_models_manager::ModelOrigin::ExplicitConfig,
        ));
    }
    let paths = ExecServerRuntimeOptions::from_optional_paths(
        config.codex_self_exe.clone(),
        config.codex_linux_sandbox_exe.clone(),
    )
    .map_err(|e| e.to_string())?;
    #[cfg(target_os = "macos")]
    let paths = paths.with_allowed_symlinked_codex_home(
        codex_config::allowed_symlinked_codex_home(&config.config_layer_stack, &config.codex_home),
    );
    let environments = Arc::new(
        EnvironmentManager::from_codex_home(
            config.codex_home.clone(),
            Some(paths),
            config.http_client_factory(),
        )
        .await
        .map_err(|e| e.to_string())?,
    );
    let state_db = if children.is_some() {
        None
    } else {
        init_state_db(&config).await
    };
    let installation_id = resolve_installation_id(&config.codex_home)
        .await
        .map_err(|e| e.to_string())?;
    let host = helpers
        .executable
        .parent()
        .ok_or("Codex helper has no parent directory")?
        .join(if cfg!(windows) {
            "codex-code-mode-host.exe"
        } else {
            "codex-code-mode-host"
        });
    let manager = Arc::new_cyclic(|weak| {
        let mut extensions = ExtensionRegistryBuilder::<Config>::new();
        codex_guardian_v2::install(&mut extensions, auth.clone(), weak.clone());
        codex_core::install_agent_message_board(&mut extensions, weak.clone());
        codex_mcp_extension::install(&mut extensions);
        codex_mcp_extension::install_plugins(&mut extensions, environments.clone());
        codex_web_search_extension::install(&mut extensions, auth.clone());
        codex_image_generation_extension::install(
            &mut extensions,
            auth.clone(),
            |config: &Config| Some(config.codex_home.clone()),
        );
        let providers = codex_skills_extension::SkillProviders::new()
            .with_executor_provider(Arc::new(
                codex_skills_extension::ExecutorSkillProvider::new_with_restriction_product(
                    environments.clone(),
                    SessionSource::Exec.restriction_product(),
                ),
            ))
            .with_host_provider(Arc::new(codex_skills_extension::HostSkillProvider::new()));
        codex_skills_extension::install_with_providers(
            &mut extensions,
            providers,
            |config: &Config| codex_skills_extension::SkillsExtensionConfig {
                include_instructions: config.include_skill_instructions,
                catalog_deltas_enabled: config.features.enabled(Feature::SkillCatalogDeltas),
                omit_descriptions: config.features.enabled(Feature::OmitSkillDescriptions),
                max_context_tokens: config.skill_max_context_tokens,
                bundled_skills_enabled: config.bundled_skills_enabled(),
                cloud_skill_enabled: config.cloud_skill_enabled,
                shadow_selection_enabled: config.features.enabled(Feature::SkillSearch),
            },
        );
        let mut manager = ThreadManager::new(
            &config,
            auth.clone(),
            models,
            CodexAppsToolsCache::default(),
            SessionSource::Exec,
            environments,
            Arc::new(extensions.build()),
            Arc::new(CodexHomeUserInstructionsProvider::new(
                config.codex_home.clone(),
            )),
            None,
            passthrough_image_store(),
            if children.is_some() {
                Arc::new(codex_thread_store::InMemoryThreadStore::default())
            } else {
                thread_store_from_config(&config, state_db.clone())
            },
            local_agent_graph_store_from_state_db(state_db.as_ref()),
            installation_id,
            None,
            None,
        );
        if config.features.enabled(Feature::CodeModeHost)
            || config.code_mode.disable_in_process_fallback
        {
            manager = manager.with_code_mode_session_provider(Arc::new(
                codex_code_mode::ProcessOwnedCodeModeSessionProvider::with_host_program(host),
            ));
        }
        manager
    });
    let policy_source_generation = policy_auth
        .auth_change_state_receiver()
        .borrow()
        .source_generation;
    Ok(CoreServices {
        children,
        config,
        manager,
        home,
        cwd,
        overrides,
        cloud,
        network,
        local_network,
        policy_auth,
        policy_source_generation,
        helpers,
    })
}

fn override_value(
    overrides: &mut Vec<(String, toml::Value)>,
    key: &str,
    value: Value,
) -> Result<()> {
    let value =
        toml::Value::try_from(value).map_err(|_| format!("Invalid Codex setting: {key}"))?;
    overrides.push((key.into(), value));
    Ok(())
}

fn flag(attempt: &Value, name: &str) -> bool {
    attempt.get(name).and_then(Value::as_bool).unwrap_or(false)
}
fn strings(attempt: &Value, name: &str) -> Vec<String> {
    attempt
        .get(name)
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect()
}

fn configure_tools(
    config: &mut Config,
    sdk_sandbox: bool,
    deferred: &[String],
    require_sdk_call: bool,
) -> Result<()> {
    if (sdk_sandbox || require_sdk_call)
        && [Feature::ShellTool, Feature::ViewImage]
            .into_iter()
            .any(|feature| config.features.enabled(feature))
    {
        return Err("Codex host policy did not disable competing native workspace tools".into());
    }
    if sdk_sandbox
        && [
            Feature::Apps,
            Feature::Plugins,
            Feature::MultiAgentV2,
            Feature::CodexHooks,
            Feature::CodeMode,
        ]
        .into_iter()
        .any(|feature| config.features.enabled(feature))
    {
        return Err("Codex host policy did not apply SDK sandbox isolation".into());
    }
    if !deferred.is_empty() && config.features.enabled(Feature::CodeMode) {
        return Err("Codex code-mode settings conflict with deferred SDK tool discovery".into());
    }
    if sdk_sandbox
        && config
            .mcp_servers
            .get()
            .values()
            .any(|server| server.enabled)
    {
        return Err("CodexProvider with SDK-owned tools requires Codex MCP servers to be disabled. Register servers on Agent.mcp_servers instead.".into());
    }
    if sdk_sandbox || !deferred.is_empty() {
        let direct = &mut config.code_mode.direct_only_tool_namespaces;
        direct.retain(|name| !deferred.contains(name));
        for required in [
            (!deferred.is_empty()).then_some("functions"),
            (sdk_sandbox && !deferred.iter().any(|n| n == "agents_sdk")).then_some("agents_sdk"),
        ]
        .into_iter()
        .flatten()
        {
            if !direct.iter().any(|n| n == required) {
                direct.push(required.into());
            }
        }
    }
    Ok(())
}

fn validate_web_config(config: &Config, web: &Value, model: &ModelInfo) -> Result<()> {
    if model.use_responses_lite {
        return Err(
            "The selected Codex model uses Responses Lite and does not support hosted web search"
                .into(),
        );
    }
    if !codex_model_provider::create_model_provider(config.model_provider.clone(), None)
        .capabilities()
        .web_search
    {
        return Err(
            "The configured Codex model provider does not support hosted web search".into(),
        );
    }
    if config.model_catalog.is_some() {
        return Err(
            "CodexProvider cannot verify hosted web search with a custom model catalog".into(),
        );
    }
    let cache_only = web.get("external_web_access") == Some(&Value::Bool(false));
    if config.web_search_mode.value() == WebSearchMode::Disabled
        || (cache_only && config.web_search_mode.value() != WebSearchMode::Cached)
    {
        return Err("Codex host policy does not allow the requested hosted web search mode".into());
    }
    if cache_only
        && matches!(
            config.legacy_sandbox_policy(),
            SandboxPolicy::DangerFullAccess
        )
    {
        return Err("Codex upgrades cached web search in danger-full-access; use a restricted sandbox for external_web_access=False".into());
    }
    if let Some(types) = web.get("search_content_types").filter(|v| !v.is_null()) {
        let expected = match model.web_search_tool_type {
            WebSearchToolType::Text => json!(["text"]),
            WebSearchToolType::TextAndImage => json!(["text", "image"]),
        };
        let mut actual = types
            .as_array()
            .ok_or("Web search content types must be a list")?
            .clone();
        let mut expected = expected.as_array().unwrap().clone();
        actual.sort_by_key(Value::to_string);
        expected.sort_by_key(Value::to_string);
        if actual != expected {
            return Err("CodexProvider supports only the selected stock model's default web search content types".into());
        }
    }
    if config.features.enabled(Feature::StandaloneWebSearch) {
        return Err("Codex did not select hosted web search".into());
    }
    let effective = config
        .web_search_config
        .as_ref()
        .ok_or("Codex did not apply hosted web search settings")?;
    if let Some(size) = web.get("search_context_size").filter(|v| !v.is_null())
        && serde_json::to_value(effective.search_context_size).map_err(|e| e.to_string())? != *size
    {
        return Err("Codex did not apply the requested web search context size".into());
    }
    if let Some(domains) = web.get("allowed_domains").filter(|v| !v.is_null())
        && serde_json::to_value(
            effective
                .filters
                .as_ref()
                .and_then(|f| f.allowed_domains.as_ref()),
        )
        .map_err(|e| e.to_string())?
            != *domains
    {
        return Err("Codex did not apply the requested web search domain filter".into());
    }
    if let Some(location) = web.get("user_location").and_then(Value::as_object) {
        let effective_location =
            serde_json::to_value(&effective.user_location).map_err(|e| e.to_string())?;
        if location
            .iter()
            .any(|(key, value)| !value.is_null() && effective_location.get(key) != Some(value))
        {
            return Err("Codex did not apply the requested web search location".into());
        }
    }
    Ok(())
}

fn validate_audio_catalog(
    catalog: &codex_protocol::openai_models::ModelsResponse,
    model: &str,
) -> Result<()> {
    if !catalog
        .models
        .iter()
        .any(|entry| entry.slug == model && entry.input_modalities.contains(&InputModality::Audio))
    {
        return Err("The selected Codex catalog model does not support audio input".into());
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn validate_windows_native_tools(
    environments: &EnvironmentManager,
    config: &Config,
    sdk_sandbox: bool,
) -> Result<()> {
    use codex_core::windows_sandbox::WindowsSandboxLevelExt;
    use codex_protocol::config_types::WindowsSandboxLevel;
    use codex_sandboxing::SandboxType;
    if !sdk_sandbox
        && config.permissions.permission_profile().enforcement() == SandboxEnforcement::Managed
        && environments.default_environment_ids().iter().any(|id| {
            environments
                .get_environment(id)
                .is_some_and(|environment| !environment.is_remote())
        })
        && config.effective_local_windows_sandbox_type() == SandboxType::WindowsRestrictedToken
        && WindowsSandboxLevel::from_config(config) == WindowsSandboxLevel::Elevated
    {
        return Err("CodexProvider cannot run local native tools with the legacy elevated Windows sandbox. Select the unelevated or MXC backend, a remote-only executor configuration, or SDK-managed tools.".into());
    }
    Ok(())
}

fn same_policy_source(auth: &AuthManager, generation: u64) -> bool {
    auth.auth_change_state_receiver().borrow().source_generation == generation
}

pub async fn prepare_config(services: &CoreServices, attempt: &Value) -> Result<Config> {
    let mut overrides = services.overrides.clone();
    let sdk_sandbox = flag(attempt, "sdk_sandbox");
    let deferred = strings(attempt, "deferred_namespaces");
    if deferred.iter().any(|n| n == "functions") {
        return Err("Codex native tool discovery reserves the functions namespace; use another namespace for deferred SDK tools".into());
    }
    if sdk_sandbox || flag(attempt, "require_sdk_call") {
        override_value(&mut overrides, "features.shell_tool", json!(false))?;
        override_value(&mut overrides, "features.view_image", json!(false))?;
    }
    if sdk_sandbox {
        for feature in [
            "apps",
            "plugins",
            "multi_agent",
            "multi_agent_v2",
            "hooks",
            "code_mode",
        ] {
            override_value(&mut overrides, &format!("features.{feature}"), json!(false))?;
        }
        override_value(&mut overrides, "notify", json!([]))?;
        override_value(&mut overrides, "web_search", json!("disabled"))?;
    }
    if !deferred.is_empty() {
        override_value(&mut overrides, "features.code_mode", json!(false))?;
        override_value(
            &mut overrides,
            "features.code_mode_tool_search",
            json!(true),
        )?;
    }
    let web = attempt.get("web_search").filter(|w| !w.is_null());
    if let Some(web) = web {
        if sdk_sandbox {
            return Err("Expose web search through SDK sandbox tools with CodexProvider".into());
        }
        override_value(
            &mut overrides,
            "features.standalone_web_search",
            json!(false),
        )?;
        override_value(
            &mut overrides,
            "web_search",
            json!(
                if web.get("external_web_access") == Some(&Value::Bool(false)) {
                    "cached"
                } else {
                    "live"
                }
            ),
        )?;
        override_value(
            &mut overrides,
            "tools.web_search.context_size",
            web.get("search_context_size")
                .cloned()
                .unwrap_or(json!("medium")),
        )?;
        if let Some(domains) = web.get("allowed_domains").filter(|d| !d.is_null()) {
            override_value(
                &mut overrides,
                "tools.web_search.allowed_domains",
                domains.clone(),
            )?;
        }
        if let Some(location) = web.get("user_location").and_then(Value::as_object) {
            for (key, value) in location {
                if key != "type" && !value.is_null() {
                    override_value(
                        &mut overrides,
                        &format!("tools.web_search.location.{key}"),
                        value.clone(),
                    )?;
                }
            }
        }
    }
    for (input, config) in [
        ("reasoning_effort", "model_reasoning_effort"),
        ("reasoning_summary", "model_reasoning_summary"),
        ("verbosity", "model_verbosity"),
    ] {
        if let Some(value) = attempt.get(input).filter(|v| !v.is_null()) {
            override_value(&mut overrides, config, value.clone())?;
        }
    }
    let sdk_hooks = attempt.get("sdk_tool_hooks");
    if let Some(hooks) = sdk_hooks {
        crate::tool_hooks::add_overrides(&mut overrides, hooks, &services.helpers.executable)?;
    }
    let mut typed = services.helpers.overrides(&services.cwd);
    typed.model = attempt
        .get("model")
        .and_then(Value::as_str)
        .map(str::to_owned);
    // Resolve request-only settings before observing a shared policy refresh.
    let local = codex_config::loader::load_local_application_requirements(
        codex_exec_server::LOCAL_FS.as_ref(),
        &LoaderOverrides::default(),
    )
    .await
    .and_then(|sources| sources.compose(Default::default()))
    .map_err(|e| {
        services
            .network
            .unavailable(services.network.policy().revision());
        services
            .local_network
            .unavailable(services.local_network.policy().revision());
        e.to_string()
    })?;
    services.local_network.publish(
        services.local_network.policy().revision(),
        destination_policy(local.as_ref()),
    );
    services.policy_auth.reload().await;
    if !same_policy_source(&services.policy_auth, services.policy_source_generation) {
        services
            .network
            .unavailable(services.network.policy().revision());
        return Err(
            "The saved Codex login changed; create a new CodexProvider before continuing".into(),
        );
    }
    // Reload the same serving source without changing scoped external credentials.
    services.manager.auth_manager().reload().await;
    let request = ConfigBuilder::default()
        .codex_home(services.home.clone())
        .cli_overrides(overrides)
        .harness_overrides(typed)
        .cloud_config_bundle(services.cloud.clone());
    let mut config = match read_config(request).await {
        Ok(config) => config,
        Err(error) => {
            // A caller-only override must not cancel unrelated streams. Recheck the
            // shared inputs; publish their current policy, or revoke on shared failure.
            let current =
                refresh_shared_config(shared_config_builder(services), &services.network).await?;
            validate_manager_config(services, &current)?;
            return Err(error);
        }
    };
    bind_policy(&mut config, &services.network);
    validate_manager_config(services, &config)?;
    if let Some(hooks) = sdk_hooks {
        if !config.features.enabled(Feature::CodexHooks) {
            return Err("Codex native tool hooks are disabled in the active configuration.".into());
        }
        if !hooks["server"]
            .as_str()
            .and_then(|name| config.mcp_servers.get().get(name))
            .is_some_and(|server| server.enabled && server.is_local_environment())
        {
            return Err("Codex configuration did not allow the local SDK tool hook relay.".into());
        }
        crate::tool_hooks::validate_collaboration_names(
            hooks,
            config.multi_agent_v2.tool_namespace.as_deref(),
        )?;
    }
    if let Some(profile) = &services.children {
        profile.validate(&config)?;
        if config.model.as_deref() != attempt["model"].as_str() {
            return Err("Codex policy changed the native child model.".into());
        }
    }
    configure_tools(
        &mut config,
        sdk_sandbox,
        &deferred,
        flag(attempt, "require_sdk_call"),
    )?;
    #[cfg(windows)]
    validate_windows_native_tools(
        &services.manager.environment_manager(),
        &config,
        sdk_sandbox,
    )?;
    if !sdk_sandbox {
        services.helpers.protect(&mut config)?;
    }
    let models = services.manager.get_models_manager();
    let model = models
        .get_default_model(
            &config.model,
            false,
            codex_models_manager::manager::RefreshStrategy::OnlineIfUncached,
            config.http_client_factory(),
        )
        .await;
    config.model = Some(model.clone());
    if flag(attempt, "has_audio") {
        let catalog = models
            .raw_model_catalog(
                codex_models_manager::manager::RefreshStrategy::OnlineIfUncached,
                config.http_client_factory(),
            )
            .await;
        validate_audio_catalog(&catalog, &model)?;
    }
    if let Some(web) = web {
        let model_info = models
            .get_model_info(&model, &config.to_models_manager_config())
            .await;
        validate_web_config(&config, web, &model_info)?;
    }
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_model_info(model: &str) -> ModelInfo {
        codex_models_manager::bundled_models_response()
            .unwrap()
            .models
            .into_iter()
            .find(|info| info.slug == model)
            .unwrap_or_else(|| codex_models_manager::model_info::model_info_from_slug(model))
    }

    fn fixture_builder(root: &Path, overrides: &[&str]) -> ConfigBuilder {
        ConfigBuilder::default()
            .codex_home(root.to_path_buf())
            .harness_overrides(ConfigOverrides {
                cwd: Some(root.to_path_buf()),
                ..Default::default()
            })
            .loader_overrides(LoaderOverrides::with_managed_config_path_for_tests(
                root.join("managed.toml"),
            ))
            .cli_overrides(
                parse_overrides(overrides.iter().map(|s| (*s).to_owned()).collect()).unwrap(),
            )
    }

    async fn fixture_config(requirements: &str, overrides: &[&str]) -> (tempfile::TempDir, Config) {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("requirements.toml"), requirements).unwrap();
        let config = fixture_builder(root.path(), overrides)
            .build()
            .await
            .unwrap();
        (root, config)
    }

    #[tokio::test]
    async fn shell_environment_defaults_limit_inheritance_without_replacing_explicit_policy() {
        let (root, _) = fixture_config("", &[]).await;
        let environment = |config: &Config| {
            let values = codex_protocol::shell_environment::create_env_from_vars(
                [
                    ("PATH".to_owned(), "/usr/bin".to_owned()),
                    ("SDK_PARENT_SENTINEL".to_owned(), "parent".to_owned()),
                ],
                &config.permissions.shell_environment_policy,
                None,
            );
            ["PATH", "SDK_PARENT_SENTINEL", "SDK_COMMAND_ALLOWED"]
                .map(|name| values.get(name).cloned())
        };
        let config = read_config(fixture_builder(root.path(), &[]))
            .await
            .unwrap();
        assert_eq!(environment(&config), [Some("/usr/bin".into()), None, None]);

        std::fs::write(root.path().join("config.toml"), "[shell_environment_policy]\ninherit = \"none\"\n[shell_environment_policy.set]\nSDK_COMMAND_ALLOWED = \"scoped\"\n").unwrap();
        let config = read_config(fixture_builder(root.path(), &[]))
            .await
            .unwrap();
        assert_eq!(environment(&config), [None, None, Some("scoped".into())]);

        let config = read_config(fixture_builder(
            root.path(),
            &["shell_environment_policy.inherit=\"all\""],
        ))
        .await
        .unwrap();
        assert_eq!(
            environment(&config),
            [
                Some("/usr/bin".into()),
                Some("parent".into()),
                Some("scoped".into())
            ]
        );
    }

    #[tokio::test]
    async fn windows_admission_checks_local_executor_attached_with_remote_default() {
        let (root, config) = fixture_config(
            "",
            &["sandbox_mode=\"read-only\"", "windows.sandbox=\"elevated\""],
        )
        .await;
        for include_local in [true, false] {
            std::fs::write(
                root.path().join("environments.toml"),
                format!("default = \"remote\"\ninclude_local = {include_local}\n[[environments]]\nid = \"remote\"\nurl = \"ws://127.0.0.1:1\"\n"),
            )
            .unwrap();
            let environments = EnvironmentManager::from_codex_home(
                root.path(),
                Some(
                    ExecServerRuntimeOptions::new(std::env::current_exe().unwrap(), None).unwrap(),
                ),
                config.http_client_factory(),
            )
            .await
            .unwrap();
            assert!(environments.default_environment().unwrap().is_remote());
            assert_eq!(
                validate_windows_native_tools(&environments, &config, false).is_err(),
                include_local
            );
            assert!(validate_windows_native_tools(&environments, &config, true).is_ok());
            for overrides in [
                vec![
                    "sandbox_mode=\"read-only\"",
                    "windows.sandbox=\"unelevated\"",
                ],
                vec!["sandbox_mode=\"read-only\"", "windows.sandbox=\"mxc\""],
                vec![
                    "sandbox_mode=\"danger-full-access\"",
                    "windows.sandbox=\"elevated\"",
                ],
            ] {
                let (_, supported) = fixture_config("", &overrides).await;
                assert!(validate_windows_native_tools(&environments, &supported, false).is_ok());
            }
        }
    }

    #[tokio::test]
    async fn failed_shared_config_reload_revokes_retained_network_permit() {
        let (root, mut config) = fixture_config("", &[]).await;
        let controller = NetworkPolicyController::default();
        bind_policy(&mut config, &controller);
        let permit = controller
            .policy()
            .acquire(&"https://model.example/".parse().unwrap())
            .unwrap();
        std::fs::write(root.path().join("config.toml"), "invalid = [").unwrap();
        assert!(
            refresh_shared_config(fixture_builder(root.path(), &[]), &controller)
                .await
                .is_err()
        );
        assert!(permit.check().is_err());
    }

    #[tokio::test]
    async fn unapplied_config_server_revokes_retained_network_permit() {
        let (root, mut config) = fixture_config("", &[]).await;
        let controller = NetworkPolicyController::default();
        bind_policy(&mut config, &controller);
        let permit = controller
            .policy()
            .acquire(&"https://model.example/".parse().unwrap())
            .unwrap();
        std::fs::write(
            root.path().join("requirements.toml"),
            "[config_server]\nbase_url = \"https://policy.example/\"\n",
        )
        .unwrap();
        let builder = fixture_builder(root.path(), &[]);
        let loaded = builder.build().await.unwrap();
        assert!(
            loaded
                .config_layer_stack
                .ensure_config_server_applied()
                .is_err()
        );
        assert!(
            refresh_shared_config(fixture_builder(root.path(), &[]), &controller)
                .await
                .is_err()
        );
        assert!(permit.check().is_err());
    }

    #[tokio::test]
    async fn request_override_failure_preserves_valid_sibling_network_permit() {
        let (root, mut config) = fixture_config("", &[]).await;
        let controller = NetworkPolicyController::default();
        bind_policy(&mut config, &controller);
        let permit = controller
            .policy()
            .acquire(&"https://model.example/".parse().unwrap())
            .unwrap();
        assert!(
            read_config(fixture_builder(
                root.path(),
                &["model_verbosity=\"invalid\""]
            ))
            .await
            .is_err()
        );
        refresh_shared_config(fixture_builder(root.path(), &[]), &controller)
            .await
            .unwrap();
        assert!(permit.check().is_ok());
    }

    #[tokio::test]
    async fn request_failure_recovery_applies_current_policy_to_retained_permits() {
        let (root, mut config) = fixture_config("", &[]).await;
        let controller = NetworkPolicyController::default();
        bind_policy(&mut config, &controller);
        let denied = controller
            .policy()
            .acquire(&"https://model.example/".parse().unwrap())
            .unwrap();
        let allowed = controller
            .policy()
            .acquire(&"https://sibling.example/".parse().unwrap())
            .unwrap();
        std::fs::write(
            root.path().join("requirements.toml"),
            "[application.network.domains]\n\"sibling.example\" = \"allow\"\n",
        )
        .unwrap();
        assert!(
            read_config(fixture_builder(
                root.path(),
                &["model_verbosity=\"invalid\""]
            ))
            .await
            .is_err()
        );
        refresh_shared_config(fixture_builder(root.path(), &[]), &controller)
            .await
            .unwrap();
        assert!(denied.check().is_err());
        assert!(allowed.check().is_ok());
    }

    #[tokio::test]
    async fn host_can_narrow_live_search_to_cached() {
        let (_root, config) = fixture_config(
            "allowed_web_search_modes = [\"cached\"]",
            &[
                "sandbox_mode=\"read-only\"",
                "web_search=\"live\"",
                "features.standalone_web_search=false",
                "tools.web_search.context_size=\"medium\"",
            ],
        )
        .await;
        assert_eq!(config.web_search_mode.value(), WebSearchMode::Cached);
        assert!(
            validate_web_config(
                &config,
                &json!({"external_web_access":true,"search_context_size":"medium"}),
                &test_model_info("gpt-5.5")
            )
            .is_ok()
        );
    }

    #[tokio::test]
    async fn text_only_web_model_rejects_image_content_request() {
        let (_root, config) = fixture_config(
            "",
            &[
                "sandbox_mode=\"read-only\"",
                "web_search=\"cached\"",
                "features.standalone_web_search=false",
                "tools.web_search.context_size=\"medium\"",
            ],
        )
        .await;
        assert!(
            validate_web_config(
                &config,
                &json!({"search_content_types":["text"]}),
                &test_model_info("text-only-model")
            )
            .is_ok()
        );
        assert!(
            validate_web_config(
                &config,
                &json!({"search_content_types":["image"]}),
                &test_model_info("text-only-model")
            )
            .is_err()
        );
        assert!(
            validate_web_config(
                &config,
                &json!({"allowed_domains":["example.org"]}),
                &test_model_info("text-only-model")
            )
            .is_err()
        );
    }

    #[tokio::test]
    async fn deferred_namespaces_preserve_unrelated_direct_settings() {
        let (_root, mut config) = fixture_config(
            "",
            &[
                "features.code_mode=false",
                "features.code_mode.direct_only_tool_namespaces=[\"unrelated\",\"deferred\"]",
            ],
        )
        .await;
        configure_tools(&mut config, false, &["deferred".into()], false).unwrap();
        assert_eq!(
            config.code_mode.direct_only_tool_namespaces,
            ["unrelated", "functions"]
        );
    }

    #[test]
    fn audio_requires_metadata_for_the_selected_model() {
        let mut catalog = codex_models_manager::bundled_models_response().unwrap();
        catalog.models.truncate(1);
        let model = &mut catalog.models[0];
        model.slug = "fixture-audio-model".into();
        model.input_modalities = vec![InputModality::Text, InputModality::Audio];
        assert!(validate_audio_catalog(&catalog, "fixture-audio-model").is_ok());
        assert!(validate_audio_catalog(&catalog, "unknown-model").is_err());
        catalog.models[0].input_modalities = vec![InputModality::Text];
        assert!(validate_audio_catalog(&catalog, "fixture-audio-model").is_err());
    }

    #[tokio::test]
    async fn sdk_sandbox_rejects_inherited_native_mcp_servers() {
        let (_root, mut config) = fixture_config(
            "",
            &[
                "features.shell_tool=false",
                "features.view_image=false",
                "features.apps=false",
                "features.plugins=false",
                "features.multi_agent_v2=false",
                "features.hooks=false",
                "features.code_mode=false",
                "mcp_servers.inherited.command=\"fixture-server\"",
            ],
        )
        .await;
        assert!(
            configure_tools(&mut config, true, &[], false)
                .unwrap_err()
                .contains("MCP")
        );
    }
    #[tokio::test]
    async fn managed_residency_installs_the_upstream_http_header() {
        let (_root, config) = fixture_config("enforce_residency = \"us\"", &[]).await;
        install_residency_requirement(&config);
        assert_eq!(
            codex_login::default_client::default_headers()
                .get(codex_login::default_client::RESIDENCY_HEADER_NAME)
                .unwrap(),
            "us"
        );
        let (_root, unrestricted) = fixture_config("", &[]).await;
        install_residency_requirement(&unrestricted);
        assert_eq!(
            codex_login::default_client::default_headers()
                .get(codex_login::default_client::RESIDENCY_HEADER_NAME)
                .unwrap(),
            "us"
        );
    }
    #[tokio::test]
    async fn project_venv_helpers_remain_readable_without_losing_managed_policy() {
        let (_root, mut config) = fixture_config(
            "allowed_sandbox_modes = [\"read-only\", \"workspace-write\"]\nallowed_approval_policies = [\"never\"]\n[permissions.filesystem]\ndeny_read = [\"secret.txt\"]",
            &[
                "sandbox_mode=\"workspace-write\"",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "approval_policy=\"never\"",
            ],
        ).await;
        let cwd = config.cwd.to_path_buf();
        let helper_dir = cwd.join(".venv/lib/site-packages/agents/models/_codex_bin");
        std::fs::create_dir_all(&helper_dir).unwrap();
        let executable = helper_dir.join("codex-sdk-runtime");
        std::fs::write(&executable, b"fixture").unwrap();
        let helpers = HelperPaths {
            executable: executable.clone(),
            linux_sandbox: None,
        };
        let before = config.permissions.file_system_sandbox_policy();
        assert!(before.can_write_local_path_with_cwd(&executable, &cwd));
        assert!(!before.can_read_local_path_with_cwd(&cwd.join("secret.txt"), &cwd));
        let network = config.permissions.network_sandbox_policy();
        let profile = config.permissions.permission_profile().clone();
        let (mut fs, net) = profile.to_runtime_permissions();
        fs.entries.push(FileSystemSandboxEntry::new(
            codex_core_api::AbsolutePathBuf::from_absolute_path(&helper_dir)
                .unwrap()
                .into(),
            FileSystemAccessMode::Write,
        ));
        fs.entries.push(FileSystemSandboxEntry::new(
            codex_core_api::AbsolutePathBuf::from_absolute_path(&executable)
                .unwrap()
                .into(),
            FileSystemAccessMode::Write,
        ));
        config
            .permissions
            .set_permission_profile(
                PermissionProfile::from_runtime_permissions_with_enforcement(
                    profile.enforcement(),
                    &fs,
                    net,
                ),
            )
            .unwrap();
        helpers.protect(&mut config).unwrap();
        let after = config.permissions.file_system_sandbox_policy();
        assert!(after.can_read_local_path_with_cwd(&executable, &cwd));
        assert!(!after.can_write_local_path_with_cwd(&executable, &cwd));
        assert!(!after.can_write_local_path_with_cwd(&cwd.join(".venv"), &cwd));
        assert!(!after.can_write_local_path_with_cwd(
            &cwd.join(".venv/lib/site-packages/agents/models/codex.py"),
            &cwd
        ));
        assert!(after.can_write_local_path_with_cwd(&cwd.join("source.py"), &cwd));
        assert!(!after.can_read_local_path_with_cwd(&cwd.join("secret.txt"), &cwd));
        assert_eq!(config.permissions.network_sandbox_policy(), network);
        assert!(
            config
                .permissions
                .approval_policy
                .can_set(&codex_protocol::protocol::AskForApproval::OnRequest)
                .is_err()
        );
    }

    #[tokio::test]
    async fn explicit_unrestricted_execution_keeps_its_permissions() {
        let (_root, mut config) =
            fixture_config("", &["sandbox_mode=\"danger-full-access\""]).await;
        let helpers = HelperPaths {
            executable: config.cwd.join("codex-sdk-runtime").to_path_buf(),
            linux_sandbox: None,
        };
        let before = config.permissions.permission_profile().clone();
        helpers.protect(&mut config).unwrap();
        assert_eq!(config.permissions.permission_profile(), &before);
    }
    #[tokio::test]
    async fn changed_saved_login_remains_rejected_on_later_attempts() {
        let (root, config) = fixture_config("", &["cli_auth_credentials_store=\"file\""]).await;
        let path = root.path().join("auth.json");
        std::fs::write(&path, br#"{"OPENAI_API_KEY":"fixture-original"}"#).unwrap();
        let auth = AuthManager::shared_from_config(&config, false)
            .await
            .unwrap();
        let original = auth.auth_change_state_receiver().borrow().source_generation;
        assert!(same_policy_source(&auth, original));
        std::fs::write(&path, br#"{"OPENAI_API_KEY":"fixture-replacement"}"#).unwrap();
        auth.reload().await;
        assert!(!same_policy_source(&auth, original));
        auth.reload().await;
        assert!(!same_policy_source(&auth, original));
    }
    #[tokio::test]
    async fn unsafe_helper_placement_does_not_make_the_workspace_read_only() {
        let (_root, mut config) = fixture_config("", &["sandbox_mode=\"workspace-write\""]).await;
        let helpers = HelperPaths {
            executable: config.cwd.join("codex-sdk-runtime").to_path_buf(),
            linux_sandbox: None,
        };
        let before = config.permissions.permission_profile().clone();
        assert!(helpers.protect(&mut config).is_err());
        assert_eq!(config.permissions.permission_profile(), &before);
    }
    #[tokio::test]
    async fn hosted_search_uses_core_model_metadata() {
        let (_root, config) = fixture_config(
            "",
            &[
                "sandbox_mode=\"read-only\"",
                "web_search=\"cached\"",
                "features.standalone_web_search=false",
                "tools.web_search.context_size=\"medium\"",
            ],
        )
        .await;
        for (model, types) in [
            ("gpt-5.4", json!(["text"])),
            ("gpt-5.4-mini", json!(["text"])),
            ("gpt-5.5", json!(["text", "image"])),
        ] {
            assert!(
                validate_web_config(
                    &config,
                    &json!({"search_content_types":types}),
                    &test_model_info(model)
                )
                .is_ok(),
                "{model}"
            );
        }
        let lite = test_model_info("gpt-6-astra");
        assert!(lite.use_responses_lite);
        assert!(
            validate_web_config(
                &config,
                &json!({"search_content_types":["text", "image"]}),
                &lite
            )
            .unwrap_err()
            .contains("Responses Lite")
        );
    }
}
