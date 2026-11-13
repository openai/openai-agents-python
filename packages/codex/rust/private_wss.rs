//! Fresh default trust for private WSS, without changing its connection owner.
use std::sync::Arc;

use codex_core_api::{InitialHistory, StartThreadOptions};
use rustls::ClientConfig;

fn default_environment() -> bool {
    ![
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "REQUEST_METHOD",
        "CODEX_SANDBOX",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    ]
    .iter()
    .any(|name| std::env::var_os(name).is_some())
}

pub(crate) fn install(startup: &mut StartThreadOptions, requested: bool) {
    let config = &startup.config;
    if requested
        && cfg!(target_os = "linux")
        && matches!(startup.initial_history, InitialHistory::New)
        && config.model_provider_id == "native_agent"
        && config.model_provider.requires_openai_auth
        && config.model_provider.supports_websockets
        && config.model_provider.base_url.as_deref() == Some("https://api.openai.com/v1")
        && !config.model_provider.has_request_routing_overrides()
    {
        startup
            .thread_extension_init
            .insert(codex_api::SdkWebsocketTlsSelector(client_config));
    }
}

// Called by the existing Core connector on every physical acquisition/reconnect.
pub(crate) fn client_config(url: &url::Url) -> Option<Arc<ClientConfig>> {
    if !cfg!(target_os = "linux")
        || !default_environment()
        || url.as_str() != "wss://api.openai.com/v1/responses"
    {
        return None;
    }
    codex_utils_rustls_provider::ensure_rustls_crypto_provider();
    Some(Arc::new(crate::private_http::websocket_config()))
}
