//! Read-only account usage from the Codex backend.

use std::time::Duration;

use anyhow::{Context, Result, ensure};
use codex_backend_client::Client;
use codex_login::{AuthManager, CodexAuth, default_client::get_codex_user_agent};
use codex_model_provider::auth_provider_from_auth;
use codex_protocol::protocol::RateLimitSnapshot;
use serde::Serialize;

#[derive(Serialize)]
struct PlanUsage {
    account_id: Option<String>,
    identity_verified: bool,
    observed_at: String,
    limits: Vec<RateLimitSnapshot>,
    ordinary_usage_allowed: Option<bool>,
    reset_credits_available: Option<i64>,
}

pub(crate) fn client(manager: &AuthManager, base_url: String) -> Client {
    // Reuse the route pool across reads, including its native client-build slot.
    // The pooled client has no saved-login bearer attached to it.
    Client::new_without_redirects(base_url, manager.http_client_factory())
        .with_user_agent(get_codex_user_agent())
}

pub(crate) async fn read(
    manager: &AuthManager,
    client: Client,
    expected_account_id: Option<String>,
) -> Result<String> {
    // The saved-login manager fixes the owner for the engine's lifetime.
    // Use its current credentials so a passive, cancellable read cannot
    // interrupt a token refresh that would write to the login file.
    let auth = manager
        .auth_cached()
        .context("Saved Codex OAuth login is unavailable")?;
    let account_id = auth.get_account_id();
    ensure!(
        matches!(auth, CodexAuth::Chatgpt(_)) && account_id == expected_account_id,
        "Codex plan usage requires the Session's saved login"
    );
    let user_id = auth
        .get_chatgpt_user_id()
        .context("Saved Codex user ID is unavailable")?;
    let client = client.with_auth_provider(auth_provider_from_auth(&auth));
    let response = tokio::time::timeout(
        Duration::from_secs(15),
        client.get_rate_limits_with_reset_credits(),
    )
    .await
    .context("Codex plan usage read timed out")?
    .map_err(|_| anyhow::anyhow!("Unable to read Codex plan usage"))?;
    ensure!(
        response
            .account_id
            .as_ref()
            .zip(account_id.as_ref())
            .is_none_or(|(reported, expected)| reported == expected)
            && response.user_id.as_ref().is_none_or(|id| id == &user_id),
        "Codex plan usage did not match the Session's saved login"
    );
    let identity_verified =
        account_id.is_some() && response.account_id.is_some() && response.user_id.is_some();
    let snapshot = PlanUsage {
        account_id,
        identity_verified,
        observed_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false),
        limits: response.rate_limits,
        ordinary_usage_allowed: response
            .ordinary_usage_allowed
            .filter(|_| identity_verified),
        reset_credits_available: response
            .rate_limit_reset_credits
            .map(|summary| summary.available_count),
    };
    Ok(serde_json::to_string(&snapshot)?)
}
