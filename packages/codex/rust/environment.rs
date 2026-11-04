//! Bind the selected executor and its application-owned policy.

use std::collections::HashMap;
use std::io::ErrorKind;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, ensure};
use codex_config::{NetworkConstraints, NetworkDomainPermissionToml, NetworkDomainPermissionsToml};
use codex_core::{
    config::validate_environment_network_policy, windows_sandbox::WindowsSandboxLevelExt,
};
use codex_core_api::{
    AbsolutePathBuf, CodexThread, CodexThreadSettingsOverrides, Config, EnvironmentManager,
    EnvironmentNetworkPolicy, NetworkDomainPermission, NetworkDomainPermissionEntry,
    NetworkDomainPermissions, NetworkProxySpec,
};
use codex_exec_server::{
    Environment, EnvironmentInfo, ExecParams, ExecProcessEvent, ExecServerError,
    ExecServerRuntimeOptions, FileSystemSandboxContext, GetMetadataOptions, LOCAL_ENVIRONMENT_ID,
    ProcessId, REMOTE_ENVIRONMENT_ID, RemoteEnvironmentOptions,
};
use codex_protocol::capabilities::{CapabilityRootLocation, SelectedCapabilityRoot};
use codex_protocol::config_types::{
    ShellEnvironmentPolicy, ShellEnvironmentPolicyInherit, WindowsSandboxLevel,
};
use codex_protocol::models::{PermissionProfile, PermissionProfileSnapshot};
use codex_protocol::permissions::{
    FileSystemAccessMode, FileSystemPath, FileSystemSpecialPath, NetworkSandboxPolicy,
};
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
use tokio_tungstenite::tungstenite::{Error as WebSocketError, error::TlsError};

use crate::local_sandbox::SYSTEM_EXECUTABLE_PATH;

pub async fn for_unattached_threads(
    config: &Config,
    sdk_local: bool,
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
    let paths = if sdk_local {
        let paths = paths.with_join_unreturned_local_exec_on_interrupt();
        #[cfg(target_os = "macos")]
        let paths = paths.with_restricted_macos_process_sandbox();
        paths
    } else {
        paths
    };
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
    url: Option<String>,
    #[serde(default)]
    local: bool,
    cwd: PathUri,
    #[serde(default)]
    headers: HashMap<String, String>,
    connect_timeout: Option<f64>,
    capability_directories: Option<Vec<PathUri>>,
    writable_roots: Option<Vec<PathUri>>,
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
        capability_roots(
            REMOTE_ENVIRONMENT_ID,
            self.capability_directories.as_deref(),
        )
    }
}

fn capability_roots(
    environment_id: &str,
    directories: Option<&[PathUri]>,
) -> Option<Vec<SelectedCapabilityRoot>> {
    directories.map(|directories| {
        directories
            .iter()
            .map(|path| SelectedCapabilityRoot {
                id: format!(
                    "codex-agents:{environment_id}:capability-directory:{:x}",
                    Sha256::digest(path.to_string().as_bytes())
                ),
                location: CapabilityRootLocation::Environment {
                    environment_id: environment_id.to_owned(),
                    path: path.clone(),
                },
            })
            .collect()
    })
}

/// Installed on a thread after selecting an executor with strict write controls.
#[derive(Clone, Default)]
pub(crate) struct StrictExecutorWrites {
    roots: HashMap<PathUri, PathUri>,
}

impl EnvironmentOptions {
    fn id(&self) -> &'static str {
        if self.local {
            LOCAL_ENVIRONMENT_ID
        } else {
            REMOTE_ENVIRONMENT_ID
        }
    }

    pub(crate) fn strict_writes(&self) -> bool {
        self.local || self.writable_roots.is_some()
    }

    pub fn capability_roots(&self) -> Option<Vec<SelectedCapabilityRoot>> {
        capability_roots(self.id(), self.capability_directories.as_deref())
    }

    pub async fn connect(self, config: &Config) -> Result<EnvironmentManager> {
        self.check_config(config)?;
        if self.local {
            ensure!(
                self.url.is_none() && self.headers.is_empty() && self.connect_timeout.is_none(),
                "a local executor does not accept connection settings"
            );
            check_local_helpers(config)?;
            let (manager, _) = for_unattached_threads(config, /*sdk_local*/ true).await?;
            let environment = manager
                .try_local_environment()
                .context("local executor requires the native process helper")?;
            environment.wait_until_ready().await.map_err(|error| {
                anyhow::anyhow!(
                    "local executor failed to become ready: {}",
                    startup_cause(&error)
                )
            })?;
            let info = executor_info(&environment).await?;
            let (profile, ..) = self.profile(&environment, &info).await?;
            self.check_network_target(&info, &profile)?;
            probe_local_sandbox(&environment, &self.cwd, profile).await?;
            return Ok(manager);
        }
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
            let (manager, _) = for_unattached_threads(config, /*sdk_local*/ false).await?;
            ensure!(
                manager.try_local_environment().is_some(),
                "local stdio MCP requires the native process helper"
            );
            manager
        } else {
            EnvironmentManager::without_environments(config.http_client_factory())
        };
        let environment = self.register(&manager)?;
        environment
            .wait_until_ready()
            .await
            .map_err(remote_startup_error)?;
        if self.strict_writes() || self.network.is_some() {
            let info = executor_info(&environment).await?;
            let (profile, ..) = self.profile(&environment, &info).await?;
            self.check_network_target(&info, &profile)?;
        }
        Ok(manager)
    }

    pub fn restrict_network_approval(&self, config: &mut Config) -> Result<()> {
        let strict_writes = self.strict_writes();
        if strict_writes {
            check_rule_directories(config)?;
            config
                .features
                .enable(codex_core_api::Feature::WriteStdinApproval)
                .context("Environment grants require stdin approval")?;
            config
                .features
                .disable(codex_core_api::Feature::RequestPermissionsTool)
                .context("Environment grants forbid permission requests")?;
        } else if self.network.is_none() {
            return Ok(());
        }
        // Escalation can bypass the write and network policy.
        let policy = &mut config.permissions.approval_policy;
        let granular = match policy.value() {
            AskForApproval::Never => return Ok(()),
            AskForApproval::Granular(existing)
                if !existing.sandbox_approval
                    && (!strict_writes || !existing.request_permissions) =>
            {
                return Ok(());
            }
            AskForApproval::Granular(existing) => GranularApprovalConfig {
                sandbox_approval: false,
                request_permissions: !strict_writes && existing.request_permissions,
                ..existing
            },
            AskForApproval::OnRequest => GranularApprovalConfig {
                sandbox_approval: false,
                rules: true,
                skill_approval: true,
                request_permissions: !strict_writes,
                mcp_elicitations: true,
            },
            AskForApproval::UnlessTrusted => {
                anyhow::bail!("executor policy cannot preserve untrusted command approvals")
            }
        };
        policy
            .set(AskForApproval::Granular(granular))
            .context("executor policy conflicts with required approvals")
    }

    fn check_config(&self, config: &Config) -> Result<()> {
        if self.strict_writes() {
            check_writable_config(config, /*local_session_proxy*/ false)?;
        }
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

    fn check_network_target(
        &self,
        info: &EnvironmentInfo,
        profile: &PermissionProfile,
    ) -> Result<()> {
        if let Some(network) = &self.network {
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
            validate_environment_network_policy(network, profile, platform)?;
        }
        Ok(())
    }

    fn register(&self, manager: &EnvironmentManager) -> Result<Arc<Environment>> {
        ensure!(
            !self.local,
            "a deferred executor requires a remote connection"
        );
        manager
            .upsert_environment_with_options(
                REMOTE_ENVIRONMENT_ID.to_owned(),
                RemoteEnvironmentOptions {
                    exec_server_url: self.url.clone().context("remote executor requires a URL")?,
                    http_headers: self.headers.clone(),
                    connect_timeout: self
                        .connect_timeout
                        .map(Duration::try_from_secs_f64)
                        .transpose()
                        .map_err(|_| anyhow::anyhow!("invalid remote executor connect timeout"))?,
                },
                Default::default(),
            )
            .map_err(|error| {
                anyhow::anyhow!(
                    "invalid remote executor connection settings: {}",
                    startup_cause(&error)
                )
            })?;
        Ok(manager
            .get_environment(REMOTE_ENVIRONMENT_ID)
            .context("remote environment was not registered")?)
    }

    async fn profile(
        &self,
        environment: &Environment,
        info: &EnvironmentInfo,
    ) -> Result<(PermissionProfile, PathUri, StrictExecutorWrites)> {
        if self.strict_writes() {
            strict_write_profile(
                environment,
                info,
                &self.cwd,
                self.writable_roots.as_deref().unwrap_or_default(),
                self.local,
            )
            .await
        } else {
            Ok((
                workspace_profile(std::slice::from_ref(&self.cwd)),
                self.cwd.clone(),
                StrictExecutorWrites::default(),
            ))
        }
    }

    pub(crate) async fn selection(
        &self,
        config: &Config,
        manager: &EnvironmentManager,
    ) -> Result<(TurnEnvironmentSelection, StrictExecutorWrites)> {
        self.check_config(config)?;
        let environment = manager
            .get_environment(self.id())
            .context("selected executor was not registered")?;
        let needs_info = self.strict_writes()
            || self.network.is_some()
            || self.cwd.infer_path_convention() == Some(PathConvention::Windows);
        let (profile, workspace_root, writes, windows_executor) = if needs_info {
            let info = executor_info(&environment).await?;
            let (profile, workspace_root, writes) = self.profile(&environment, &info).await?;
            self.check_network_target(&info, &profile)?;
            (
                profile,
                workspace_root,
                writes,
                info.platform_os.as_deref() == Some("windows"),
            )
        } else {
            (
                workspace_profile(std::slice::from_ref(&self.cwd)),
                self.cwd.clone(),
                StrictExecutorWrites::default(),
                false,
            )
        };
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
        let workspace_roots = vec![workspace_root];
        let shell_environment_policy = if self.local {
            ShellEnvironmentPolicy {
                inherit: ShellEnvironmentPolicyInherit::None,
                r#set: local_shell_env(),
                ..Default::default()
            }
        } else {
            permissions.shell_environment_policy.clone()
        };
        let selection = TurnEnvironmentSelection {
            environment_id: self.id().to_owned(),
            workspace_roots: workspace_roots.clone(),
            cwd: self.cwd.clone(),
            // Owner configuration prevents executor-advertised roots from being added.
            config: EnvironmentConfigState::Ready(EnvironmentConfig {
                allow_login_shell: !self.local && permissions.allow_login_shell,
                workspace_roots,
                permission_profile: PermissionProfileSnapshot::legacy(profile),
                shell_environment_policy,
                windows_sandbox_level,
                windows_sandbox_type,
                use_legacy_landlock: config.features.use_legacy_landlock(),
                exec_policy: None,
                mcp_policy: None,
                network_policy: if self.local {
                    None
                } else {
                    self.network.clone()
                },
                selected_capability_roots,
            }),
        };
        Ok((selection, writes))
    }

    pub(crate) fn configure_local_proxy(
        &self,
        config: &mut Config,
        selection: &TurnEnvironmentSelection,
    ) -> Result<()> {
        let Some(network) = self.network.as_ref().filter(|_| self.local) else {
            return Ok(());
        };
        let EnvironmentConfigState::Ready(settings) = &selection.config else {
            anyhow::bail!("local network policy requires a ready Session Environment");
        };
        ensure!(
            selection.environment_id == LOCAL_ENVIRONMENT_ID
                && config.permissions.network.is_none(),
            "local network policy cannot replace an existing controller proxy"
        );
        config.permissions.network = Some(local_proxy_spec(
            network,
            settings.permission_profile.permission_profile(),
        )?);
        Ok(())
    }
}

fn local_proxy_spec(
    network: &EnvironmentNetworkPolicy,
    profile: &PermissionProfile,
) -> Result<NetworkProxySpec> {
    let domains = network
        .domains
        .as_ref()
        .map(|domains| NetworkDomainPermissionsToml {
            entries: domains
                .entries
                .iter()
                .filter_map(|entry| {
                    let permission = match entry.permission {
                        NetworkDomainPermission::Allow => NetworkDomainPermissionToml::Allow,
                        NetworkDomainPermission::Deny => NetworkDomainPermissionToml::Deny,
                        NetworkDomainPermission::None => return None,
                    };
                    Some((entry.pattern.clone(), permission))
                })
                .collect(),
        });
    // Core keeps these constraints when it recomputes the session proxy for a new profile.
    NetworkProxySpec::from_config_and_constraints(
        Default::default(),
        Some(NetworkConstraints {
            enabled: Some(true),
            http_port: Some(0),
            socks_port: Some(0),
            allow_upstream_proxy: Some(false),
            dangerously_allow_non_loopback_proxy: Some(false),
            dangerously_allow_all_unix_sockets: Some(network.dangerously_allow_all_unix_sockets),
            domains,
            managed_allowed_domains_only: Some(true),
            allow_local_binding: network.allow_local_binding,
            ..Default::default()
        }),
        profile,
    )
    .context("could not configure the local executor network proxy")
}

pub(crate) fn local_session_network_policy(config: &Config) -> Option<EnvironmentNetworkPolicy> {
    config
        .permissions
        .network
        .as_ref()
        .map(NetworkProxySpec::environment_policy)
        .filter(|policy| policy.requires_proxy)
}

pub(crate) fn workspace_profile(roots: &[PathUri]) -> PermissionProfile {
    PermissionProfile::workspace_write_with_path_uris(
        &[],
        NetworkSandboxPolicy::Restricted,
        true,
        true,
    )
    .materialize_project_roots_with_path_uris(roots)
}

fn check_rule_directories(config: &Config) -> Result<()> {
    let stack = &config.config_layer_stack;
    ensure!(
        stack.requirements().exec_policy.is_none(),
        "strict executor write policy does not support managed exec-policy rules"
    );
    for layer in stack.layers_low_to_high() {
        if stack.ignore_user_and_project_exec_policy_rules()
            && matches!(
                layer.name,
                codex_config::ConfigLayerSource::User { .. }
                    | codex_config::ConfigLayerSource::Project { .. }
            )
        {
            continue;
        }
        let Some(folder) = layer.config_folder() else {
            continue;
        };
        match std::fs::symlink_metadata(folder.join("rules")) {
            Ok(_) => anyhow::bail!(
                "strict executor write policy does not support native exec-policy rule directories"
            ),
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(_) => anyhow::bail!("could not verify native exec-policy rule directories"),
        }
    }
    Ok(())
}

fn check_writable_config(config: &Config, local_session_proxy: bool) -> Result<()> {
    check_rule_directories(config)?;
    let approval = config.permissions.approval_policy.value();
    let no_escalation = approval == AskForApproval::Never
        || matches!(approval, AskForApproval::Granular(policy) if !policy.sandbox_approval && !policy.request_permissions);
    ensure!(
        no_escalation
            && (local_session_proxy || config.permissions.network.is_none())
            && !config.features.use_legacy_landlock()
            && config
                .features
                .enabled(codex_core_api::Feature::WriteStdinApproval)
            && !config
                .features
                .enabled(codex_core_api::Feature::RequestPermissionsTool),
        "strict executor write policy requires the standard sandbox with escalation and permission requests disabled"
    );
    Ok(())
}

async fn executor_info(environment: &Environment) -> Result<EnvironmentInfo> {
    environment.info().await.map_err(|error| {
        anyhow::anyhow!(
            "could not verify executor capabilities: {}",
            startup_cause(&error)
        )
    })
}

async fn strict_write_profile(
    environment: &Environment,
    info: &EnvironmentInfo,
    cwd: &PathUri,
    additional: &[PathUri],
    local: bool,
) -> Result<(PermissionProfile, PathUri, StrictExecutorWrites)> {
    ensure!(
        matches!(
            Platform::from_platform_os(info.platform_os.as_deref()),
            Platform::Linux | Platform::Macos
        ) && info.capabilities.sandboxed_file_streaming,
        "strict executor write policy requires a Linux or macOS executor with sandboxed file access"
    );
    // Resolve on the executor, so controller path aliases cannot change the grant.
    let filesystem = environment.get_filesystem();
    let mut roots = Vec::with_capacity(additional.len() + 1);
    let mut writes = StrictExecutorWrites::default();
    for root in std::iter::once(cwd).chain(additional) {
        ensure!(
            root.infer_path_convention() == Some(PathConvention::Posix) && root.parent().is_some(),
            "executor write roots must be absolute directories below the filesystem root"
        );
        let canonical = filesystem
            .canonicalize(root, None)
            .await
            .map_err(|_| anyhow::anyhow!("could not resolve an executor write root"))?;
        ensure!(
            canonical.infer_path_convention() == Some(PathConvention::Posix)
                && canonical.parent().is_some()
                && filesystem
                    .get_metadata(
                        &canonical,
                        GetMetadataOptions {
                            follow_symlinks: false
                        },
                        None
                    )
                    .await
                    .map_err(|_| anyhow::anyhow!("could not verify an executor write root"))?
                    .is_directory,
            "executor write roots must be existing directories below the filesystem root"
        );
        writes.roots.insert(root.clone(), canonical.clone());
        if !roots.contains(&canonical) {
            roots.push(canonical);
        }
    }
    let mut filesystem_policy = workspace_profile(&roots).file_system_sandbox_policy();
    if local {
        check_local_helper_write_roots(environment, &roots)?;
        for entry in &mut filesystem_policy.entries {
            if entry.access == FileSystemAccessMode::Read
                && matches!(
                    entry.path,
                    FileSystemPath::Special {
                        value: FileSystemSpecialPath::Root
                    }
                )
            {
                entry.path = FileSystemPath::Special {
                    value: FileSystemSpecialPath::Minimal,
                };
            }
        }
    }
    // A caller may explicitly grant a linked worktree's gitdir or common dir.
    // Remove only default carveouts at those exact paths. Core still adds
    // read-only carveouts for protected metadata under every granted root.
    filesystem_policy.entries.retain(|entry| {
        entry.access != FileSystemAccessMode::Read
            || !matches!(&entry.path, FileSystemPath::Path { path } if roots.contains(path))
    });
    // The first resolved root is always the implicit working directory grant.
    Ok((
        PermissionProfile::from_runtime_permissions(
            &filesystem_policy,
            NetworkSandboxPolicy::Restricted,
        ),
        roots[0].clone(),
        writes,
    ))
}

fn check_local_helper_write_roots(environment: &Environment, roots: &[PathUri]) -> Result<()> {
    let paths = environment
        .local_runtime_paths()
        .context("local executor requires native helper paths")?;
    let roots = roots
        .iter()
        .map(PathUri::to_abs_path)
        .collect::<std::io::Result<Vec<_>>>()?;
    let packaged_helpers = std::iter::once(&paths.codex_self_exe)
        .chain(paths.codex_linux_sandbox_exe.as_ref())
        .map(|helper| helper.as_path());
    for alias in packaged_helpers.clone() {
        let resolved = std::fs::canonicalize(alias)
            .map_err(|_| anyhow::anyhow!("could not verify the local executor helper path"))?;
        // Protect the helper, its package directory (including bundled resources),
        // and each ancestor of a configured symlink path.
        for path in [alias, resolved.as_path()] {
            let install = path
                .parent()
                .and_then(Path::parent)
                .context("local executor helpers must have an installation directory")?;
            let install = std::fs::canonicalize(install)
                .map_err(|_| anyhow::anyhow!("could not verify the local executor helper path"))?;
            ensure!(
                roots
                    .iter()
                    .all(|root| !root.as_path().starts_with(&install)),
                "local executor writable roots must stay outside the native helper installation"
            );
        }
    }
    // Bubblewrap may run before isolation to report its supported options.
    // Protect its fixed search directories even when they lack a bwrap file.
    let system_search = SYSTEM_EXECUTABLE_PATH
        .split(':')
        .filter(|_| cfg!(target_os = "linux"))
        .map(Path::new);
    let system_bwraps = system_search
        .clone()
        .map(|directory| directory.join("bwrap"))
        .filter(|path| path.symlink_metadata().is_ok())
        .collect::<Vec<_>>();
    for path in packaged_helpers
        .chain(system_search)
        .chain(system_bwraps.iter().map(|path| path.as_path()))
    {
        ensure!(
            path.is_absolute(),
            "local executor helpers must have absolute paths"
        );
        let mut links_left = 40;
        check_local_helper_path(&roots, path, &mut PathBuf::new(), &mut links_left)?;
    }
    Ok(())
}

fn check_local_helper_path(
    roots: &[AbsolutePathBuf],
    path: &Path,
    resolved: &mut PathBuf,
    links_left: &mut usize,
) -> Result<()> {
    for part in path.components() {
        match part {
            Component::CurDir => continue,
            Component::ParentDir => {
                resolved.pop();
                continue;
            }
            _ => resolved.push(part),
        }
        let is_symlink = matches!(part, Component::Normal(_))
            && resolved
                .symlink_metadata()
                .context("could not resolve a local sandbox helper path")?
                .is_symlink();
        if matches!(part, Component::Normal(_)) && !is_symlink {
            *resolved = resolved
                .canonicalize()
                .context("could not resolve a local sandbox helper path")?;
        }
        // Protect each directory entry before following it. A writable link
        // can redirect the launch even when its current target is trusted.
        ensure!(
            !roots
                .iter()
                .any(|root| resolved.starts_with(root.as_path())),
            "local executor writable roots must stay outside the native helper installation"
        );
        if is_symlink {
            *links_left = links_left
                .checked_sub(1)
                .context("could not resolve a local sandbox helper path")?;
            let target = resolved
                .read_link()
                .context("could not resolve a local sandbox helper path")?;
            resolved.pop();
            check_local_helper_path(roots, &target, resolved, links_left)?;
        }
    }
    Ok(())
}

/// Replace just the write profile of the currently selected executor.
pub(crate) async fn replace_writable_roots(
    selection: &TurnEnvironmentSelection,
    config: &Config,
    manager: &EnvironmentManager,
    current: &StrictExecutorWrites,
    writable_roots: Vec<PathUri>,
) -> Result<(TurnEnvironmentSelection, StrictExecutorWrites)> {
    // The SDK installs its local proxy after rejecting an ambient controller at selection.
    let local = selection.environment_id == LOCAL_ENVIRONMENT_ID;
    check_writable_config(config, local)?;
    let mut selection = selection.clone();
    let EnvironmentConfigState::Ready(settings) = &mut selection.config else {
        anyhow::bail!("writable_roots requires a ready Session Environment")
    };
    let environment = manager
        .get_environment(&selection.environment_id)
        .context("selected executor was not registered")?;
    let info = executor_info(&environment).await?;
    let (profile, workspace_root, writes) =
        strict_write_profile(&environment, &info, &selection.cwd, &writable_roots, local).await?;
    ensure!(
        selection.workspace_roots.as_slice() == std::slice::from_ref(&workspace_root),
        "executor working directory changed since Session startup"
    );
    for (requested, canonical) in &writes.roots {
        let previous = current
            .roots
            .get(requested)
            .or_else(|| current.roots.values().find(|root| *root == requested));
        ensure!(
            previous.is_none_or(|root| root == canonical),
            "executor write root changed since it was selected"
        );
    }
    let local_network = if local {
        local_session_network_policy(config)
    } else {
        None
    };
    if let Some(network) = settings.network_policy.as_ref().or(local_network.as_ref()) {
        ensure!(
            info.capabilities.network_proxy_launch,
            "network policy requires an executor with managed proxy launch"
        );
        validate_environment_network_policy(
            network,
            &profile,
            Platform::from_platform_os(info.platform_os.as_deref()),
        )?;
    }
    settings.permission_profile = PermissionProfileSnapshot::legacy(profile);
    Ok((selection, writes))
}

fn check_local_helpers(config: &Config) -> Result<()> {
    ensure!(
        cfg!(any(target_os = "linux", target_os = "macos")),
        "the local executor requires Linux or macOS"
    );
    ensure!(
        config
            .codex_self_exe
            .as_ref()
            .is_some_and(|path| path.is_file()),
        "local executor requires the native process helper"
    );
    #[cfg(target_os = "linux")]
    ensure!(
        config
            .codex_linux_sandbox_exe
            .as_ref()
            .is_some_and(|path| path.is_file()),
        "local executor requires the native Linux sandbox helper"
    );
    Ok(())
}

fn local_shell_env() -> HashMap<String, String> {
    HashMap::from([("PATH".to_owned(), SYSTEM_EXECUTABLE_PATH.to_owned())])
}

async fn probe_local_sandbox(
    environment: &Environment,
    cwd: &PathUri,
    profile: PermissionProfile,
) -> Result<()> {
    // The direct local backend has no transport handshake. A fixed command
    // verifies that this process can launch the requested sandbox.
    let params = ExecParams {
        process_id: ProcessId::new("sdk-local-sandbox-readiness"),
        metadata: None,
        argv: vec!["/bin/sh".to_owned(), "-c".to_owned(), "exit 0".to_owned()],
        cwd: cwd.clone(),
        env_policy: None,
        shell_snapshot: None,
        env: local_shell_env(),
        tty: false,
        pipe_stdin: false,
        arg0: None,
        sandbox: Some(FileSystemSandboxContext::from_permission_profile(
            profile,
            cwd.clone(),
        )),
        enforce_managed_network: false,
        managed_network: None,
        network_proxy: None,
    };
    let backend = environment.get_exec_backend();
    let process = backend.start(params).await.map_err(|error| {
        anyhow::anyhow!(
            "local executor sandbox launch failed: {}",
            startup_cause(&error)
        )
    })?;
    let expected_sandbox = if cfg!(target_os = "macos") {
        SandboxType::MacosSeatbelt
    } else {
        SandboxType::LinuxSeccomp
    };
    let mut events = process.process.subscribe_events();
    let verified = tokio::time::timeout(Duration::from_secs(5), async {
        while let Ok(event) = events.recv().await {
            match event {
                ExecProcessEvent::Output(_) => {}
                ExecProcessEvent::Exited { exit_code, .. } => return exit_code == 0,
                _ => return false,
            }
        }
        false
    })
    .await;
    if process.sandbox_type != Some(expected_sandbox) || !matches!(verified, Ok(true)) {
        let _ = process.process.terminate().await;
        anyhow::bail!("local executor could not establish the platform sandbox");
    }
    Ok(())
}

fn remote_startup_error(error: ExecServerError) -> anyhow::Error {
    anyhow::anyhow!(
        "remote executor failed to become ready: {}",
        startup_cause(&error)
    )
}

pub fn selected_executor(manager: &EnvironmentManager) -> Option<Arc<Environment>> {
    // WSS sessions can also reserve a local backend for host MCP servers.
    manager
        .get_environment(REMOTE_ENVIRONMENT_ID)
        .or_else(|| manager.get_environment(LOCAL_ENVIRONMENT_ID))
}

fn startup_cause(mut error: &ExecServerError) -> String {
    while let ExecServerError::ConnectionAttempt(source) = error {
        error = source;
    }
    match error {
        ExecServerError::Spawn(error) => format!("process launch failed ({:?})", error.kind()),
        ExecServerError::WebSocketConnectTimeout { .. } => "WebSocket connection timed out".into(),
        ExecServerError::WebSocketConnect { source, .. } => websocket_cause(source),
        ExecServerError::WebSocketConfiguration(_) => {
            "invalid WebSocket connection configuration".into()
        }
        ExecServerError::InitializeTimedOut { .. } => "executor initialization timed out".into(),
        ExecServerError::ApplicationNetworkPolicy(_) => {
            "application network policy denied the connection".into()
        }
        ExecServerError::AuthenticationRequired(_) => "executor authentication denied".into(),
        ExecServerError::Closed | ExecServerError::Disconnected(_) => {
            "connection closed during startup".into()
        }
        ExecServerError::Protocol(_) | ExecServerError::Json(_) => {
            "executor protocol error during startup".into()
        }
        ExecServerError::Server { code, .. } => {
            format!("executor rejected initialization (RPC code {code})")
        }
        _ => "executor connection or initialization failed".into(),
    }
}

fn websocket_cause(error: &WebSocketError) -> String {
    match error {
        WebSocketError::Http(response) => {
            let status = response.status();
            let reason = if matches!(
                status,
                http::StatusCode::UNAUTHORIZED | http::StatusCode::FORBIDDEN
            ) {
                "executor authentication denied"
            } else {
                "WebSocket handshake rejected"
            };
            format!("{reason} (HTTP {})", status.as_u16())
        }
        WebSocketError::Io(error) => {
            if let Some(error) = error
                .get_ref()
                .and_then(|source| source.downcast_ref::<rustls::Error>())
            {
                tls_cause(error).into()
            } else if error.kind() == ErrorKind::ConnectionRefused {
                "connection refused".into()
            } else {
                format!("WebSocket connection I/O failed ({:?})", error.kind())
            }
        }
        WebSocketError::Tls(TlsError::Rustls(error)) => tls_cause(error).into(),
        WebSocketError::Tls(_) => "TLS handshake failed".into(),
        WebSocketError::Protocol(_) => "WebSocket handshake protocol error".into(),
        _ => "WebSocket connection failed".into(),
    }
}

fn tls_cause(error: &rustls::Error) -> &'static str {
    use rustls::{CertificateError, Error};
    match error {
        Error::InvalidCertificate(CertificateError::UnknownIssuer) => {
            "TLS certificate issuer is not trusted"
        }
        Error::InvalidCertificate(
            CertificateError::NotValidForName | CertificateError::NotValidForNameContext { .. },
        ) => "TLS certificate does not match the executor host",
        Error::InvalidCertificate(_) => "TLS certificate verification failed",
        _ => "TLS handshake failed",
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
        environment
            .wait_until_ready()
            .await
            .map_err(remote_startup_error)?;
        let config = state.thread.config().await;
        let (selection, _) = request.environment.selection(&config, &manager).await?;

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
        request.environment.check_config(&config)?;
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
    use super::{ExecServerError, WebSocketError, remote_startup_error};
    use serde_json::json;
    use std::sync::Arc;
    use std::time::Duration;

    #[cfg(unix)]
    #[tokio::test]
    async fn local_helper_refuses_a_writable_intermediate_symlink() -> anyhow::Result<()> {
        use std::fs;
        use std::os::unix::fs::symlink;

        use super::{EnvironmentManager, ExecServerRuntimeOptions, PathUri};

        let fixture = tempfile::tempdir()?;
        let workspace = fixture.path().join("workspace");
        let launcher = fixture.path().join("packaged/bin/codex-sdk-runtime");
        let trusted = fixture.path().join("trusted/bin/codex-sdk-runtime");
        fs::create_dir(&workspace)?;
        fs::create_dir_all(launcher.parent().unwrap())?;
        fs::create_dir_all(trusted.parent().unwrap())?;
        fs::write(&trusted, b"trusted helper")?;
        symlink(
            "../trusted/bin/codex-sdk-runtime",
            workspace.join("launcher"),
        )?;
        symlink("../../workspace/launcher", &launcher)?;
        let paths = ExecServerRuntimeOptions::new(launcher, None)?;
        let manager = EnvironmentManager::create_for_tests(None, Some(paths)).await;
        let environment = manager.try_local_environment().unwrap();
        let write_root = PathUri::from_host_native_path(workspace.canonicalize()?)?;

        assert_eq!(
            super::check_local_helper_write_roots(&environment, &[write_root])
                .unwrap_err()
                .to_string(),
            "local executor writable roots must stay outside the native helper installation"
        );
        Ok(())
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    #[tokio::test]
    async fn local_proxy_listens_on_loopback_and_enforces_a_fixed_allowlist() -> anyhow::Result<()>
    {
        use codex_protocol::models::PermissionProfile;
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let environment = crate::config::environment(
            &json!({
                "environment": {
                    "local": true,
                    "cwd": "file:///work",
                    "network": {
                        "allowed_domains": ["allowed.example.invalid"],
                        "denied_domains": ["blocked.example.invalid"],
                        "allow_all_unix_sockets": true,
                    },
                },
            })
            .to_string(),
        )?
        .unwrap();
        let policy = environment.network.unwrap();
        let profile = PermissionProfile::workspace_write();
        let spec = super::local_proxy_spec(&policy, &profile)?;
        assert_eq!(
            serde_json::to_value(spec.environment_policy())?,
            serde_json::to_value(policy)?
        );
        let running = spec
            .start_proxy(
                &profile,
                Default::default(),
                Default::default(),
                /*policy_decider*/ None,
                /*blocked_request_observer*/ None,
                /*enable_network_approval_flow*/ false,
                Default::default(),
            )
            .await?;
        let address = running.proxy().http_addr();
        assert!(address.ip().is_loopback());
        assert_ne!(address.port(), 0);

        tokio::time::timeout(Duration::from_secs(5), async {
            for (host, reason) in [
                ("blocked.example.invalid", "blocked-by-denylist"),
                ("unlisted.example.invalid", "blocked-by-allowlist"),
            ] {
                let mut stream = tokio::net::TcpStream::connect(address).await?;
                stream.write_all(format!(
                    "GET http://{host}/ HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
                ).as_bytes()).await?;
                let mut response = String::new();
                stream.read_to_string(&mut response).await?;
                assert!(response.starts_with("HTTP/1.1 403 "), "{response}");
                assert!(
                    response
                        .lines()
                        .any(|line| line == format!("x-proxy-error: {reason}"))
                );
            }
            anyhow::Ok(())
        })
        .await??;
        Ok(())
    }

    #[test]
    fn startup_error_preserves_only_safe_handshake_details() {
        let response = http::Response::builder()
            .status(http::StatusCode::FORBIDDEN)
            .header("Authorization", "Bearer private-header")
            .body(Some(b"private-body".to_vec()))
            .unwrap();
        let failed = remote_startup_error(ExecServerError::ConnectionAttempt(Arc::new(
            ExecServerError::WebSocketConnect {
                url: "wss://executor.invalid/private-path".to_owned(),
                source: WebSocketError::Http(Box::new(response)),
            },
        )));
        assert_eq!(
            failed.to_string(),
            "remote executor failed to become ready: executor authentication denied (HTTP 403)"
        );
        assert_eq!(failed.chain().count(), 1);
        assert_eq!(
            remote_startup_error(ExecServerError::InitializeTimedOut {
                timeout: Duration::from_secs(2),
            })
            .to_string(),
            "remote executor failed to become ready: executor initialization timed out"
        );
    }

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
