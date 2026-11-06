//! Exact-recipe eligibility for the Provider's single prepared-session entry.
use super::*;
use codex_core::config::{SdkEphemeralConfigBindings, SdkEphemeralConfigTemplate};
use sha2::{Digest, Sha256};

pub(crate) struct SdkConfigRecipe {
    pub(crate) key: String,
    pub(crate) bindings: SdkEphemeralConfigBindings,
    source_files: [PathBuf; 2],
}

impl SdkConfigRecipe {
    /// Decline rather than change the ordinary loader's validation/error order.
    pub(crate) fn capture(
        config_json: &str,
        original_runtime: &str,
        prepared_runtime: &str,
    ) -> Option<Self> {
        if !cfg!(target_os = "linux") || config_json.len() + original_runtime.len() > 64 * 1024 {
            return None;
        }
        let config: serde_json::Map<String, Value> = serde_json::from_str(config_json).ok()?;
        let mut runtime: serde_json::Map<String, Value> =
            serde_json::from_str(original_runtime).ok()?;
        let prepared: serde_json::Map<String, Value> =
            serde_json::from_str(prepared_runtime).ok()?;
        const CONFIG_KEYS: &[&str] = &[
            "include_permissions_instructions",
            "include_environment_context",
            "model_provider",
            "model_providers.native_agent",
            "agents.enabled",
            "features.goals",
            "tools.update_plan.enabled",
            "mcp_servers",
            "features.non_prefixed_mcp_tool_names",
            "features.code_mode",
            "features.code_mode_interrupt",
            "web_search",
            "features.standalone_web_search",
            "model",
            "otel.exporter",
            "otel.log_user_prompt",
            "otel.log_agent_responses",
            "otel.tool_result",
        ];
        if config
            .keys()
            .any(|key| !CONFIG_KEYS.contains(&key.as_str()))
            || runtime.keys().any(|key| {
                ![
                    "strict_config",
                    "loader",
                    "base_instructions",
                    "codex_home",
                    "cwd",
                    "_native_turn_control",
                ]
                .contains(&key.as_str())
            })
            || config.get("model_provider")?.as_str()? != "native_agent"
            || config.get("web_search")?.as_str()? != "disabled"
            || !config.get("mcp_servers")?.as_object()?.is_empty()
            || runtime.get("strict_config")?.as_bool()? != true
        {
            return None;
        }
        for key in [
            "include_permissions_instructions",
            "include_environment_context",
            "agents.enabled",
            "features.goals",
            "tools.update_plan.enabled",
            "features.non_prefixed_mcp_tool_names",
            "features.code_mode_interrupt",
            "features.standalone_web_search",
        ] {
            if config.get(key)?.as_bool()? {
                return None;
            }
        }
        if config
            .get("otel.exporter")
            .is_some_and(|v| v.as_str() != Some("none"))
        {
            return None;
        }
        let code_mode = config.get("features.code_mode")?.as_object()?;
        if code_mode
            .keys()
            .any(|key| !["enabled", "direct_only_tool_namespaces"].contains(&key.as_str()))
            || code_mode.get("enabled")?.as_bool()?
        {
            return None;
        }
        let provider = config.get("model_providers.native_agent")?.as_object()?;
        if provider.keys().any(|key| {
            ![
                "name",
                "base_url",
                "wire_api",
                "requires_openai_auth",
                "supports_websockets",
                "supports_standalone_web_search",
            ]
            .contains(&key.as_str())
        }) || provider.get("wire_api")?.as_str()? != "responses"
            || !provider.get("requires_openai_auth")?.as_bool()?
            || provider.get("supports_standalone_web_search")?.as_bool()?
        {
            return None;
        }
        let loader = runtime.get("loader")?.as_object()?;
        if loader.len() != 2
            || !loader.get("ignore_user_config")?.as_bool()?
            || !loader.get("ignore_project_config")?.as_bool()?
        {
            return None;
        }
        let home = AbsolutePathBuf::from_absolute_path(PathBuf::from(
            runtime.get("codex_home")?.as_str()?,
        ))
        .ok()?;
        let cwd = AbsolutePathBuf::from_absolute_path(PathBuf::from(runtime.get("cwd")?.as_str()?))
            .ok()?;
        if cwd != home.join("workspace") {
            return None;
        }
        let loader = prepared.get("loader")?.as_object()?;
        let system = AbsolutePathBuf::from_absolute_path(PathBuf::from(
            loader.get("system_config_path")?.as_str()?,
        ))
        .ok()?;
        let managed = PathBuf::from(loader.get("managed_config_path")?.as_str()?);
        for key in [
            "ignore_managed_requirements",
            "ignore_login_requirements",
            "ignore_user_config",
            "ignore_project_config",
            "ignore_user_and_project_exec_policy_rules",
        ] {
            if !loader.get(key)?.as_bool()? {
                return None;
            }
        }
        runtime.remove("codex_home");
        runtime.remove("cwd");
        let encoded = serde_json::to_vec(&(config, runtime)).ok()?;
        Some(Self {
            key: format!("{:x}", Sha256::digest(encoded)),
            source_files: [system.to_path_buf(), managed],
            bindings: SdkEphemeralConfigBindings {
                home,
                cwd,
                system_config: system,
            },
        })
    }

    /// Preserve newly appearing source files and read/metadata errors on warm hits.
    pub(crate) async fn sources_still_empty(&self) -> bool {
        for file in &self.source_files {
            if !matches!(tokio::fs::try_exists(file).await, Ok(false)) {
                return false;
            }
        }
        true
    }

    pub(crate) fn capture_template(&self, config: &Config) -> Option<SdkEphemeralConfigTemplate> {
        SdkEphemeralConfigTemplate::capture(config, &self.bindings)
    }

    pub(crate) async fn bind(
        &self,
        template: &SdkEphemeralConfigTemplate,
    ) -> Option<(Config, CloudConfigBundleLoader)> {
        if !self.sources_still_empty().await {
            return None;
        }
        Some((
            template.bind(&self.bindings)?,
            CloudConfigBundleLoader::default(),
        ))
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;

    fn inputs(home: &std::path::Path) -> (String, String) {
        let config = serde_json::json!({
            "include_permissions_instructions": false, "include_environment_context": false,
            "model_provider": "native_agent", "model_providers.native_agent": {
                "name": "Native agent provider", "base_url": "http://127.0.0.1:1/v1",
                "wire_api": "responses", "requires_openai_auth": true,
                "supports_websockets": false, "supports_standalone_web_search": false
            }, "agents.enabled": false, "features.goals": false, "tools.update_plan.enabled": false,
            "mcp_servers": {}, "features.non_prefixed_mcp_tool_names": false,
            "features.code_mode": { "enabled": false, "direct_only_tool_namespaces": ["functions"] },
            "features.code_mode_interrupt": false, "web_search": "disabled", "features.standalone_web_search": false
        }).to_string();
        let runtime = serde_json::json!({ "strict_config": true,
            "loader": { "ignore_user_config": true, "ignore_project_config": true },
            "base_instructions": "SDK instructions", "codex_home": home,
            "cwd": home.join("workspace") })
        .to_string();
        (config, runtime)
    }

    #[test]
    fn recipe_identity_excludes_only_owned_paths_and_tracks_definition_changes() {
        let root = tempfile::tempdir().unwrap();
        let (config, runtime) = inputs(&root.path().join("first"));
        let prepared = crate::config::prepare_runtime(&runtime).unwrap();
        let first = SdkConfigRecipe::capture(&config, &runtime, &prepared).unwrap();
        let (_, runtime) = inputs(&root.path().join("second"));
        let prepared = crate::config::prepare_runtime(&runtime).unwrap();
        let second = SdkConfigRecipe::capture(&config, &runtime, &prepared).unwrap();
        assert_eq!(first.key, second.key);
        assert_ne!(first.bindings.home, second.bindings.home);
        let mut changed: Value = serde_json::from_str(&config).unwrap();
        changed["model"] = Value::String("different-model".into());
        assert_ne!(
            first.key,
            SdkConfigRecipe::capture(&changed.to_string(), &runtime, &prepared)
                .unwrap()
                .key
        );
        changed["model_providers.native_agent"]["http_headers"] =
            serde_json::json!({"Authorization":"synthetic-private"});
        assert!(SdkConfigRecipe::capture(&changed.to_string(), &runtime, &prepared).is_none());
        let mut runtime: Value = serde_json::from_str(&runtime).unwrap();
        runtime["environment"] = serde_json::json!({});
        assert!(SdkConfigRecipe::capture(&config, &runtime.to_string(), &prepared).is_none());
        assert!(SdkConfigRecipe::capture("[]", "{}", "{}").is_none());
    }

    #[tokio::test]
    async fn current_source_file_appearance_declines_template_binding() {
        let root = tempfile::tempdir().unwrap();
        let (config, runtime) = inputs(root.path());
        let mut prepared: Value =
            serde_json::from_str(&crate::config::prepare_runtime(&runtime).unwrap()).unwrap();
        prepared["loader"]["system_config_path"] = serde_json::json!(root.path().join("system"));
        prepared["loader"]["managed_config_path"] = serde_json::json!(root.path().join("managed"));
        let recipe = SdkConfigRecipe::capture(&config, &runtime, &prepared.to_string()).unwrap();
        assert!(recipe.sources_still_empty().await);
        for file in &recipe.source_files {
            std::fs::write(file, "unknown_config = true").unwrap();
            assert!(!recipe.sources_still_empty().await);
            std::fs::remove_file(file).unwrap();
        }
        assert!(recipe.sources_still_empty().await);
        std::fs::create_dir(&recipe.source_files[0]).unwrap();
        assert!(!recipe.sources_still_empty().await);
        std::fs::remove_dir(&recipe.source_files[0]).unwrap();
        std::os::unix::fs::symlink(&recipe.source_files[0], &recipe.source_files[0]).unwrap();
        assert!(!recipe.sources_still_empty().await);
    }
}
