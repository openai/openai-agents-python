//! SDK authentication for Responses and Apps.

use std::num::NonZeroU64;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use codex_core_api::Config;
use codex_login::{
    AuthManager, CodexAuth, ExternalAuth, ExternalAuthFuture, ExternalAuthRefreshContext,
    RefreshTokenError,
};
use codex_protocol::auth::{RefreshTokenFailedError, RefreshTokenFailedReason};
use codex_protocol::config_types::ModelProviderAuthInfo;
use serde::Deserialize;
use sha2::{Digest, Sha256};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SavedCodexLogin {
    pub codex_home: Option<PathBuf>,
    pub account_id: Option<String>,
    pub expected_binding: Option<String>,
    pub credential_directory: Option<PathBuf>,
}

pub(crate) struct CodexAccount {
    pub account_id: Option<String>,
    pub plan_type: String,
    pub binding: String,
    pub auth_file: PathBuf,
    pub credential_directory: Option<PathBuf>,
}

impl SavedCodexLogin {
    pub fn open(self, config: &Config) -> Result<(Arc<AuthManager>, CodexAccount)> {
        let codex_home = match self.codex_home {
            Some(path) => path,
            None => codex_core::config::find_codex_home()
                .context("Cannot locate the Codex login directory")?
                .to_path_buf(),
        }
        .canonicalize()
        .context("Cannot open the Codex login directory")?;
        let auth_file = codex_home.join("auth.json");
        let credential_directory = self
            .credential_directory
            .map(|directory| {
                ensure!(
                    directory.is_absolute(),
                    "CodexLogin.credential_directory must be an absolute path"
                );
                let directory = directory
                    .canonicalize()
                    .context("Cannot open the Codex credential directory")?;
                ensure!(
                    directory.parent().is_some() && directory.is_dir(),
                    "CodexLogin.credential_directory must be a directory below the filesystem root"
                );
                let target = auth_file
                    .canonicalize()
                    .context("Cannot resolve the saved Codex login")?;
                ensure!(
                    target == directory.join("auth.json"),
                    "The saved Codex login must resolve to auth.json in CodexLogin.credential_directory"
                );
                Ok(directory)
            })
            .transpose()?;
        // Reload and refresh use the same directory as the executor's deny.
        let storage_home = credential_directory.clone().unwrap_or(codex_home);
        let manager = AuthManager::shared_saved_chatgpt(storage_home, config.auth_route_config())
            .map_err(|_| {
            anyhow::anyhow!(
                "No usable saved Codex OAuth login. Sign in with ChatGPT using codex login first."
            )
        })?;
        let auth = manager
            .auth_cached()
            .context("Saved Codex OAuth login is unavailable")?;
        let account_id = auth.get_account_id();
        ensure!(
            self.account_id
                .as_ref()
                .is_none_or(|expected| Some(expected) == account_id.as_ref()),
            "Saved Codex login does not match the expected account"
        );
        let user_id = auth
            .get_chatgpt_user_id()
            .context("Saved Codex user ID is unavailable")?;
        let binding = format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&(&account_id, user_id))?)
        );
        ensure!(
            self.expected_binding
                .as_ref()
                .is_none_or(|expected| *expected == binding),
            "Session requires the original Codex login"
        );
        let plan_type = auth
            .account_plan_type()
            .map(|plan| serde_json::to_value(plan))
            .transpose()?
            .and_then(|value| value.as_str().map(str::to_owned))
            .unwrap_or_else(|| "unknown".to_owned());
        Ok((
            manager,
            CodexAccount {
                account_id,
                plan_type,
                binding,
                auth_file,
                credential_directory,
            },
        ))
    }
}

struct ApiKey {
    auth: CodexAuth,
    app_managed_oauth: bool,
}

impl ExternalAuth for ApiKey {
    fn resolve(&self) -> ExternalAuthFuture<'_, CodexAuth> {
        Box::pin(async { Ok(self.auth.clone()) })
    }

    fn can_refresh(&self) -> bool {
        !self.app_managed_oauth
    }

    fn refresh(&self, _context: ExternalAuthRefreshContext) -> ExternalAuthFuture<'_, CodexAuth> {
        if self.app_managed_oauth {
            return Box::pin(async {
                Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    "Sign in with ChatGPT access was rejected; check the selected registration",
                ))
            });
        }
        self.resolve()
    }

    fn classify_error(&self, error: std::io::Error) -> RefreshTokenError {
        if self.app_managed_oauth {
            RefreshTokenError::Permanent(RefreshTokenFailedError::new(
                RefreshTokenFailedReason::Other,
                error.to_string(),
            ))
        } else {
            RefreshTokenError::Transient(error)
        }
    }
}

pub async fn manager(
    config: &Config,
    api_key: Option<String>,
    app_managed_oauth: bool,
) -> Result<Arc<AuthManager>> {
    let api_key = match api_key {
        Some(key) => {
            ensure!(!key.trim().is_empty(), "api_key must not be empty");
            Some(key.trim().to_owned())
        }
        None if app_managed_oauth => anyhow::bail!("Sign in with ChatGPT requires an access token"),
        None => codex_login::read_openai_api_key_from_env(),
    };
    ensure!(
        api_key.is_some() || !config.model_provider.requires_openai_auth,
        "Provide api_key or set OPENAI_API_KEY"
    );

    // This constructor alone skips all environment and stored-login discovery.
    // Its command provider is replaced before the manager can resolve any auth.
    let manager = AuthManager::external_bearer_only(ModelProviderAuthInfo {
        command: String::new(),
        args: Vec::new(),
        timeout_ms: NonZeroU64::MIN,
        refresh_interval_ms: 0,
        cwd: config.cwd.clone(),
    });
    match api_key {
        Some(key) => {
            manager
                .set_external_auth(Arc::new(ApiKey {
                    auth: CodexAuth::from_api_key(&key),
                    app_managed_oauth,
                }))
                .await?;
        }
        None => manager.clear_external_auth(),
    }
    Ok(manager)
}

// One explicit application-owned Apps principal; model authentication is untouched.
struct AppsBearer(CodexAuth);
impl ExternalAuth for AppsBearer {
    fn resolve(&self) -> ExternalAuthFuture<'_, CodexAuth> {
        Box::pin(async { Ok(self.0.clone()) })
    }
    fn refresh(&self, _context: ExternalAuthRefreshContext) -> ExternalAuthFuture<'_, CodexAuth> {
        Box::pin(async {
            Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "ChatGPT Apps credentials expired; open a fresh Session",
            ))
        })
    }
}
pub async fn apps_manager(
    config: &Config,
    access_token: &str,
    account_id: &str,
) -> Result<Arc<AuthManager>> {
    let auth = CodexAuth::from_external_chatgpt_tokens(access_token, account_id, None)
        .map_err(|_| anyhow::anyhow!("Invalid ChatGPT Apps credentials"))?;
    ensure!(
        auth.get_account_id().as_deref() == Some(account_id)
            && auth.get_chatgpt_user_id().is_some(),
        "ChatGPT Apps identity is unavailable"
    );
    let manager = AuthManager::external_bearer_only(ModelProviderAuthInfo {
        command: String::new(),
        args: Vec::new(),
        timeout_ms: NonZeroU64::MIN,
        refresh_interval_ms: 0,
        cwd: config.cwd.clone(),
    });
    manager
        .set_external_auth(Arc::new(AppsBearer(auth)))
        .await
        .map_err(|_| anyhow::anyhow!("Invalid ChatGPT Apps credentials"))?;
    Ok(manager)
}
