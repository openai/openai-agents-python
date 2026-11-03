//! API-key authentication owned by the SDK, without Codex login discovery.

use std::num::NonZeroU64;
use std::sync::Arc;

use anyhow::{Result, ensure};
use codex_core_api::Config;
use codex_login::{
    AuthManager, CodexAuth, ExternalAuth, ExternalAuthFuture, ExternalAuthRefreshContext,
};
use codex_protocol::config_types::ModelProviderAuthInfo;

struct ApiKey {
    auth: CodexAuth,
}

impl ExternalAuth for ApiKey {
    fn resolve(&self) -> ExternalAuthFuture<'_, CodexAuth> {
        Box::pin(async { Ok(self.auth.clone()) })
    }

    fn refresh(&self, _context: ExternalAuthRefreshContext) -> ExternalAuthFuture<'_, CodexAuth> {
        self.resolve()
    }
}

pub async fn manager(config: &Config, api_key: Option<String>) -> Result<Arc<AuthManager>> {
    let api_key = match api_key {
        Some(key) => {
            ensure!(!key.trim().is_empty(), "api_key must not be empty");
            Some(key.trim().to_owned())
        }
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
