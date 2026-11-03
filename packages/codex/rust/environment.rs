//! Register declared MCP execution and bind the selected remote executor.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use codex_core::{
    config::validate_environment_network_policy, windows_sandbox::WindowsSandboxLevelExt,
};
use codex_core_api::{
    CodexThread, CodexThreadSettingsOverrides, Config, EnvironmentManager,
    EnvironmentNetworkPolicy, NetworkDomainPermission, NetworkDomainPermissionEntry,
    NetworkDomainPermissions,
};
use codex_exec_server::{
    ExecServerRuntimeOptions, REMOTE_ENVIRONMENT_ID, RemoteEnvironmentOptions,
};
use codex_protocol::capabilities::{CapabilityRootLocation, SelectedCapabilityRoot};
use codex_protocol::config_types::WindowsSandboxLevel;
use codex_protocol::models::{PermissionProfile, PermissionProfileSnapshot};
use codex_protocol::permissions::NetworkSandboxPolicy;
use codex_protocol::protocol::{
    AskForApproval, EnvironmentConfig, EnvironmentConfigState, GranularApprovalConfig, Op,
    ThreadSettingsOverrides, TurnEnvironmentSelection, TurnEnvironmentSelections,
    TurnSettingsUpdate, TurnSettingsUpdateOutcome,
};
use codex_protocol::sandbox::SandboxType;
use codex_utils_path_uri::{PathConvention, PathUri, Platform};
use serde::{Deserialize, Deserializer};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::sync::oneshot;

pub async fn for_unattached_threads(
    config: &Config,
) -> Result<(EnvironmentManager, Option<String>)> {
    // An explicitly absent helper supports model-only clients. Native MCP
    // admits servers against the actual manager at session selection time.
    if config.codex_self_exe.is_none() {
        return Ok((
            EnvironmentManager::without_environments(config.http_client_factory()),
            Some("no local executor is registered".to_owned()),
        ));
    }
    let paths = ExecServerRuntimeOptions::from_optional_paths(
        config.codex_self_exe.clone(),
        config.codex_linux_sandbox_exe.clone(),
    )?;
    // The pinned public constructor reads a file; never read or replace the caller's home.
    // Shared Codex sessions may admit a current local MCP roster after manager
    // construction. Reserve its lazy executor here: only configured MCP starts
    // it without a workspace selection. No default grants ordinary threads a
    // shell. SDK workspace and canonical rules explicitly select local instead.
    let home = tempfile::tempdir().context("prepare native MCP environment")?;
    std::fs::write(
        home.path().join("environments.toml"),
        "default = \"none\"\ninclude_local = true\n",
    )?;
    let prepared = EnvironmentManager::prepare_from_codex_home(home.path()).await?;
    // Noise takes precedence over the file upstream. Do not start an ambient executor.
    ensure!(
        !prepared.default_environment_is_remote(),
        "native local MCP configuration cannot select an ambient remote environment"
    );
    Ok((
        prepared.build(Some(paths), config.http_client_factory())?,
        None,
    ))
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnvironmentOptions {
    url: String,
    cwd: PathUri,
    #[serde(default)]
    headers: HashMap<String, String>,
    connect_timeout: Option<f64>,
    capability_directories: Option<Vec<PathUri>>,
    #[serde(default)]
    local_mcp: bool,
    #[serde(default, deserialize_with = "deserialize_network")]
    network: Option<EnvironmentNetworkPolicy>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct NetworkOptions {
    allowed_domains: Vec<String>,
    denied_domains: Vec<String>,
    allow_all_unix_sockets: bool,
    allow_local_binding: bool,
}

fn deserialize_network<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<EnvironmentNetworkPolicy>, D::Error> {
    let Some(options) = Option::<NetworkOptions>::deserialize(deserializer)? else {
        return Ok(None);
    };
    let mut domains = NetworkDomainPermissions::default();
    for (patterns, permission) in [
        (options.allowed_domains, NetworkDomainPermission::Allow),
        (options.denied_domains, NetworkDomainPermission::Deny),
    ] {
        domains.entries.extend(
            patterns
                .into_iter()
                .map(|pattern| NetworkDomainPermissionEntry {
                    pattern,
                    permission,
                }),
        );
    }
    Ok(Some(EnvironmentNetworkPolicy {
        requires_proxy: true,
        // Core resolves conflicting domain rules and enforces the strict allowlist.
        domains: Some(domains),
        unix_sockets: None,
        allow_upstream_proxy: false,
        dangerously_allow_all_unix_sockets: options.allow_all_unix_sockets,
        allow_local_binding: Some(options.allow_local_binding),
        managed_allowed_domains_only: true,
    }))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeferredEnvironmentOptions {
    capability_directories: Option<Vec<PathUri>>,
}

impl DeferredEnvironmentOptions {
    pub fn capability_roots(&self) -> Option<Vec<SelectedCapabilityRoot>> {
        capability_roots(self.capability_directories.as_deref())
    }
}

fn capability_roots(directories: Option<&[PathUri]>) -> Option<Vec<SelectedCapabilityRoot>> {
    directories.map(|directories| {
        directories
            .iter()
            .map(|path| SelectedCapabilityRoot {
                id: format!(
                    "codex-agents:{REMOTE_ENVIRONMENT_ID}:capability-directory:{:x}",
                    Sha256::digest(path.to_string().as_bytes())
                ),
                location: CapabilityRootLocation::Environment {
                    environment_id: REMOTE_ENVIRONMENT_ID.to_owned(),
                    path: path.clone(),
                },
            })
            .collect()
    })
}

impl EnvironmentOptions {
    pub fn capability_roots(&self) -> Option<Vec<SelectedCapabilityRoot>> {
        capability_roots(self.capability_directories.as_deref())
    }

    fn permission_profile(&self) -> PermissionProfile {
        // Resolve the application-owned root now so a command's workdir cannot expand writes.
        PermissionProfile::workspace_write_with_path_uris(
            &[],
            NetworkSandboxPolicy::Restricted,
            true,
            true,
        )
        .materialize_project_roots_with_path_uris(std::slice::from_ref(&self.cwd))
    }

    pub async fn connect(self, config: &Config) -> Result<EnvironmentManager> {
        self.check_network_config(config)?;
        let needs_local_mcp = self.local_mcp
            || config.mcp_servers.get().values().any(|server| {
                server.enabled
                    && server.is_local_environment()
                    && matches!(
                        server.transport,
                        codex_config::McpServerTransportConfig::Stdio { .. }
                    )
            });
        let manager = if needs_local_mcp {
            let (manager, _) = for_unattached_threads(config).await?;
            ensure!(
                manager.try_local_environment().is_some(),
                "local stdio MCP requires the native process helper"
            );
            manager
        } else {
            EnvironmentManager::without_environments(config.http_client_factory())
        };
        let environment = self
            .register(&manager)
            .map_err(|_| anyhow::anyhow!("invalid remote executor connection settings"))?;
        environment
            .wait_until_ready()
            .await
            .map_err(|_| anyhow::anyhow!("remote executor failed to become ready"))?;
        self.check_network_target(&environment).await?;
        Ok(manager)
    }

    pub fn restrict_network_approval(&self, config: &mut Config) -> Result<()> {
        if self.network.is_none() {
            return Ok(());
        }
        // An escalated command bypasses the proxy. Preserve all other approval categories.
        let policy = &mut config.permissions.approval_policy;
        let granular = match policy.value() {
            AskForApproval::Never => return Ok(()),
            AskForApproval::Granular(existing) if !existing.sandbox_approval => return Ok(()),
            AskForApproval::Granular(existing) => GranularApprovalConfig {
                sandbox_approval: false,
                ..existing
            },
            AskForApproval::OnRequest => GranularApprovalConfig {
                sandbox_approval: false,
                rules: true,
                skill_approval: true,
                request_permissions: true,
                mcp_elicitations: true,
            },
            AskForApproval::UnlessTrusted => {
                anyhow::bail!("network policy cannot preserve untrusted command approvals")
            }
        };
        policy
            .set(AskForApproval::Granular(granular))
            .context("network policy conflicts with required approvals")
    }

    fn check_network_config(&self, config: &Config) -> Result<()> {
        if self.network.is_some() {
            let approval = config.permissions.approval_policy.value();
            let no_escalation = approval == AskForApproval::Never
                || matches!(approval, AskForApproval::Granular(policy) if !policy.sandbox_approval);
            ensure!(
                config.permissions.network.is_none()
                    && !config.features.use_legacy_landlock()
                    && no_escalation,
                "network policy requires the standard sandbox without escalation or a controller proxy"
            );
        }
        Ok(())
    }

    async fn check_network_target(
        &self,
        environment: &codex_exec_server::Environment,
    ) -> Result<()> {
        if let Some(network) = &self.network {
            let info = environment
                .info()
                .await
                .map_err(|_| anyhow::anyhow!("could not verify remote executor capabilities"))?;
            let platform = Platform::from_platform_os(info.platform_os.as_deref());
            ensure!(
                platform != Platform::Linux || network.dangerously_allow_all_unix_sockets,
                "this executor cannot enforce Unix-socket denial for an explicit network policy. \
                 Linux requires explicit access to all Unix sockets (allow_all_unix_sockets=True)"
            );
            ensure!(
                matches!(platform, Platform::Linux | Platform::Macos)
                    && info.capabilities.network_proxy_launch,
                "network policy requires a Linux or macOS executor with managed proxy launch"
            );
            ensure!(
                self.cwd.infer_path_convention() == Some(PathConvention::Posix),
                "network policy requires a POSIX executor workspace"
            );
            validate_environment_network_policy(network, &self.permission_profile(), platform)?;
        }
        Ok(())
    }

    fn register(
        &self,
        manager: &EnvironmentManager,
    ) -> Result<Arc<codex_exec_server::Environment>> {
        manager.upsert_environment_with_options(
            REMOTE_ENVIRONMENT_ID.to_owned(),
            RemoteEnvironmentOptions {
                exec_server_url: self.url.clone(),
                http_headers: self.headers.clone(),
                connect_timeout: self
                    .connect_timeout
                    .map(Duration::try_from_secs_f64)
                    .transpose()
                    .context("connect_timeout must be finite and nonnegative")?,
            },
            Default::default(),
        )?;
        Ok(manager
            .get_environment(REMOTE_ENVIRONMENT_ID)
            .context("remote environment was not registered")?)
    }

    pub async fn is_windows_executor(&self, manager: &EnvironmentManager) -> Result<bool> {
        if self.cwd.infer_path_convention() != Some(PathConvention::Windows) {
            return Ok(false);
        }
        let environment = manager
            .get_environment(REMOTE_ENVIRONMENT_ID)
            .context("remote environment was not registered")?;
        let info = environment
            .info()
            .await
            .map_err(|_| anyhow::anyhow!("could not verify remote executor platform"))?;
        Ok(info.platform_os.as_deref() == Some("windows"))
    }

    pub fn selection(self, config: &Config, windows_executor: bool) -> TurnEnvironmentSelection {
        let permissions = &config.permissions;
        let windows_sandbox_type = config.windows_sandbox_type_from_config();
        let windows_sandbox_level = match WindowsSandboxLevel::from_config(config) {
            // Core's legacy approval check runs on the controller. For a verified Unix
            // peer, the POSIX workspace keeps Windows sandbox selection off the wire.
            WindowsSandboxLevel::Disabled
                if windows_sandbox_type == SandboxType::None
                    && (windows_executor || (cfg!(windows) && self.network.is_some())) =>
            {
                WindowsSandboxLevel::RestrictedToken
            }
            configured => configured,
        };
        let selected_capability_roots = self.capability_roots().unwrap_or_default();
        let workspace_roots = vec![self.cwd.clone()];
        let permission_profile = PermissionProfileSnapshot::legacy(self.permission_profile());
        TurnEnvironmentSelection {
            environment_id: REMOTE_ENVIRONMENT_ID.to_owned(),
            workspace_roots: workspace_roots.clone(),
            cwd: self.cwd,
            // Owner configuration prevents executor-advertised roots from being added.
            config: EnvironmentConfigState::Ready(EnvironmentConfig {
                allow_login_shell: permissions.allow_login_shell,
                workspace_roots,
                permission_profile,
                shell_environment_policy: permissions.shell_environment_policy.clone(),
                windows_sandbox_level,
                windows_sandbox_type,
                use_legacy_landlock: config.features.use_legacy_landlock(),
                exec_policy: None,
                mcp_policy: None,
                network_policy: self.network,
                selected_capability_roots,
            }),
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ConnectEnvironment {
    turn_id: String,
    environment: EnvironmentOptions,
    timeout: f64,
}

/// Attach only to the turn that requested the environment; never retarget a newer turn.
pub async fn connect_environment(
    state: Arc<crate::engine::ThreadState>,
    params: Value,
) -> Result<Value> {
    let request: ConnectEnvironment = crate::config::decode(params)?;
    ensure!(
        state.engine.deferred_environment,
        "client has no deferred environment"
    );
    ensure!(
        request.environment.capability_directories.is_none(),
        "deferred capability directories must be declared before the session starts"
    );
    ensure!(
        request.timeout > 0.0,
        "environment timeout must be positive"
    );
    let timeout = Duration::try_from_secs_f64(request.timeout)
        .context("environment timeout must be finite")?;
    let prepared = tokio::time::timeout(timeout, async {
        let connection = state
            .engine
            .environment_connection
            .clone()
            .lock_owned()
            .await;
        if state
            .thread
            .current_turn_environment_selections(&request.turn_id)
            .await
            .is_none()
        {
            return Ok(None);
        }
        ensure!(
            state
                .engine
                .attached_environment
                .read()
                .map_err(|_| anyhow::anyhow!("environment state unavailable"))?
                .is_none(),
            "deferred environment is already connected"
        );
        let manager = state.engine.manager.environment_manager();
        let environment = request.environment.register(&manager)?;
        environment.wait_until_ready().await?;
        request
            .environment
            .check_network_target(&environment)
            .await?;
        let windows_executor = request.environment.is_windows_executor(&manager).await?;

        // Connection is slow; serialize only the final update with turn admission and interrupts.
        let mutation = state.mutations.clone().lock_owned().await;
        if state
            .thread
            .current_turn_environment_selections(&request.turn_id)
            .await
            .is_none()
        {
            return Ok(None);
        }
        let config = state.thread.config().await;
        request.environment.check_network_config(&config)?;
        let selection = request
            .environment
            .clone()
            .selection(&config, windows_executor);
        let selections = TurnEnvironmentSelections::new(
            state.thread.config_snapshot().await.cwd().clone(),
            vec![selection.clone()],
        );
        state
            .thread
            .preview_thread_settings_overrides(CodexThreadSettingsOverrides {
                environments: Some(selections.clone()),
                ..Default::default()
            })
            .await?;
        Ok::<_, anyhow::Error>(Some((connection, mutation, selection, selections)))
    })
    .await;
    let prepared = match prepared {
        Err(_) => return Ok(json!({"status": "timeout"})),
        Ok(result) => result
            .map_err(|_| anyhow::anyhow!("remote environment connection or attachment failed"))?,
    };
    let Some((connection, mutation, selection, selections)) = prepared else {
        return Ok(json!({"status": "target_unavailable"}));
    };
    // Once an operation enters Core's queue it cannot be withdrawn. Finish this short
    // commit even if Python cancels, retaining the same admission lock until state agrees.
    let committed = tokio::spawn(async move {
        let (_connection, _mutation) = (connection, mutation);
        let previous = state
            .thread
            .current_turn_environment_selections(&request.turn_id)
            .await;
        let Some(previous) = previous else {
            return Ok(json!({"status": "target_unavailable"}));
        };
        match select_for_turn(&state.thread, &request.turn_id, vec![selection.clone()]).await? {
            TurnSettingsUpdateOutcome::TargetUnavailable => {
                return Ok(json!({"status": "target_unavailable"}));
            }
            TurnSettingsUpdateOutcome::Rejected { .. } => {
                anyhow::bail!("environment attachment rejected")
            }
            TurnSettingsUpdateOutcome::Applied => {}
        }
        if let Err(error) = state
            .thread
            .update_thread_settings(ThreadSettingsOverrides {
                environments: Some(selections),
                ..Default::default()
            })
            .await
        {
            let _ = select_for_turn(&state.thread, &request.turn_id, previous).await;
            return Err(error.into());
        }
        *state
            .engine
            .attached_environment
            .write()
            .map_err(|_| anyhow::anyhow!("environment state unavailable"))? = Some(selection);
        Ok::<_, anyhow::Error>(json!({"status": "applied"}))
    })
    .await;
    // URLs, authentication headers, and executor error bodies must not reach model-facing errors.
    committed
        .map_err(|_| anyhow::anyhow!("environment attachment interrupted"))?
        .map_err(|_| anyhow::anyhow!("remote environment connection or attachment failed"))
}

async fn select_for_turn(
    thread: &CodexThread,
    turn_id: &str,
    environments: Vec<TurnEnvironmentSelection>,
) -> Result<TurnSettingsUpdateOutcome> {
    let (reply, outcome) = oneshot::channel();
    thread
        .submit(Op::TurnSettings {
            turn_id: turn_id.to_owned(),
            update: TurnSettingsUpdate {
                environments: Some(environments),
                ..Default::default()
            },
            reply,
        })
        .await?;
    Ok(outcome.await?)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    #[test]
    fn runtime_decoder_accepts_direct_environment_and_rejects_retired_fields() {
        let direct = json!({
            "environment": {
                "url": "wss://executor.example",
                "cwd": "file:///work",
                "headers": {},
                "connect_timeout": null
            }
        });
        assert!(
            crate::config::environment(&direct.to_string())
                .expect("direct environment should decode")
                .is_some()
        );

        for (field, value) in [
            ("_registry", json!(true)),
            ("_sdk_exec_user", json!("sandbox-user")),
        ] {
            let mut retired = direct.clone();
            retired["environment"]
                .as_object_mut()
                .expect("environment object")
                .insert(field.to_owned(), value);
            let error = match crate::config::environment(&retired.to_string()) {
                Ok(_) => panic!("retired native environment field should be rejected"),
                Err(error) => error,
            };
            assert!(
                error.to_string().contains(field),
                "decoder error should identify {field}: {error}"
            );
        }
    }
}
