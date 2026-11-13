//! Child transport policy preserves the shared provider and its existing HTTP owner.
use super::*;
use serde_json::json;

async fn parent_config() -> (tempfile::TempDir, Config) {
    let directory = tempfile::TempDir::new().expect("isolated config home");
    let home = directory.path().join("home");
    let cwd = directory.path().join("work");
    std::fs::create_dir(&home).unwrap();
    std::fs::create_dir(&cwd).unwrap();
    let source = json!({
        "model": "transport-fixture",
        "model_provider": "native_agent",
        "model_providers.native_agent": {
            "name": "Synthetic transport provider",
            "base_url": "https://api.openai.com/v1",
            "wire_api": "responses",
            "requires_openai_auth": true,
            "supports_websockets": true,
            "request_max_retries": 0
        },
        "agents.enabled": false,
        "web_search": "disabled"
    });
    let runtime = json!({"codex_home": home, "cwd": cwd, "ephemeral": false});
    let (config, _) = crate::config::load(&source.to_string(), &runtime.to_string(), None)
        .await
        .expect("load configuration without constructing a model session");
    (directory, config)
}

#[tokio::test]
async fn private_http_changes_only_owned_provider_and_attaches_its_retained_pool() {
    let (_directory, parent) = parent_config().await;
    let original = parent.model_provider.clone();
    let original_catalog = parent.model_providers.clone();
    let pool = crate::private_http::pool();
    let ordinary_pool = codex_http_client::SdkModelHttpPool::default();
    let mut child = StartThreadOptions::new(parent.clone());
    prepare_private_transport(&mut child, Some(&pool), true).unwrap();
    let mut expected = original.clone();
    expected.supports_websockets = false;
    assert_eq!(child.config.model_provider, expected);
    assert_eq!(parent.model_provider, original);
    assert_eq!(parent.model_providers, original_catalog);
    let mut expected_catalog = original_catalog;
    expected_catalog
        .get_mut("native_agent")
        .unwrap()
        .supports_websockets = false;
    assert_eq!(child.config.model_providers, expected_catalog);
    assert!(
        child
            .thread_extension_init
            .get::<codex_http_client::SdkModelHttpPool>()
            .is_some()
    );

    // A later ordinary child of the same parent keeps both its permission and pool.
    let mut ordinary = StartThreadOptions::new(parent.clone());
    prepare_private_transport(&mut ordinary, Some(&ordinary_pool), false).unwrap();
    assert_eq!(ordinary.config.model_provider, original);
    assert!(
        ordinary
            .thread_extension_init
            .get::<codex_http_client::SdkModelHttpPool>()
            .is_some()
    );
    let mut independent = StartThreadOptions::new(parent.clone());
    prepare_private_transport(&mut independent, Some(&pool), true).unwrap();
    assert!(matches!(&child.initial_history, InitialHistory::New));
    assert!(matches!(&independent.initial_history, InitialHistory::New));
    // Independent scopes share only this uncredentialed socket owner.
    pool.close();
    let factory = codex_http_client::HttpClientFactory::new(
        codex_http_client::OutboundProxyPolicy::ReqwestDefault,
    );
    for options in [&child, &independent] {
        assert!(
            options
                .thread_extension_init
                .get::<codex_http_client::SdkModelHttpPool>()
                .unwrap()
                .client(&factory, "invalid-url", false, http::HeaderMap::new())
                .is_err()
        );
    }
    assert!(
        ordinary
            .thread_extension_init
            .get::<codex_http_client::SdkModelHttpPool>()
            .unwrap()
            .client(&factory, "invalid-url", false, http::HeaderMap::new())
            .unwrap()
            .is_none()
    );
}

#[tokio::test]
async fn private_http_rejects_explicit_http_without_mutating_provider_or_pool() {
    let (_directory, mut config) = parent_config().await;
    config.model_provider.supports_websockets = false;
    let original = config.model_provider.clone();
    let mut startup = StartThreadOptions::new(config);
    let pool = codex_http_client::SdkModelHttpPool::default();
    assert!(prepare_private_transport(&mut startup, Some(&pool), true).is_err());
    assert_eq!(startup.config.model_provider, original);
    assert!(
        startup
            .thread_extension_init
            .get::<codex_http_client::SdkModelHttpPool>()
            .is_none()
    );
}

#[test]
fn private_http_policy_cannot_be_set_by_json_options() {
    let ordinary: options::ThreadOptions = serde_json::from_str("{}").unwrap();
    assert!(!ordinary.private_http);
    assert!(!ordinary.process_private_http);
    for key in ["process_private_http", "_process_private_http"] {
        let value = json!({(key): true});
        assert!(serde_json::from_value::<options::ThreadOptions>(value).is_err());
    }
    assert!(serde_json::from_str::<options::ThreadOptions>(r#"{"private_http":true}"#).is_err());
    assert!(serde_json::from_str::<options::ThreadOptions>(r#"{"_private_http":true}"#).is_err());
}

#[tokio::test]
async fn private_http_rejects_both_sign_in_with_chatgpt_provider_names() {
    let (_directory, parent) = parent_config().await;
    let pool = codex_http_client::SdkModelHttpPool::default();
    for prefix in ["native_agent_siwc_", "native_agent_siwc_verified_"] {
        let mut config = parent.clone();
        config.model_provider_id = format!("{prefix}{}", "a".repeat(64));
        let original = config.model_provider.clone();
        let original_catalog = config.model_providers.clone();
        let mut startup = StartThreadOptions::new(config);
        assert!(prepare_private_transport(&mut startup, Some(&pool), true).is_err());
        assert_eq!(startup.config.model_provider, original);
        assert_eq!(startup.config.model_providers, original_catalog);
        assert!(
            startup
                .thread_extension_init
                .get::<codex_http_client::SdkModelHttpPool>()
                .is_none()
        );
    }
}

#[tokio::test]
async fn process_http_intent_declines_hooks_and_preserves_ordinary_startup() {
    for hook in ["_before_complete", "_tool_batch_completion"] {
        let directory = tempfile::TempDir::new().unwrap();
        let config = json!({
            "model": "gpt-5.2", "model_provider": "native_agent",
            "model_providers.native_agent": {
                "name": "Idle fallback fixture", "base_url": "http://127.0.0.1:9/v1",
                "wire_api": "responses", "requires_openai_auth": false,
                "supports_websockets": false, "request_max_retries": 0, "stream_max_retries": 0
            },
            "approval_policy": "never", "sandbox_mode": "read-only",
            "web_search": "disabled", "agents.enabled": false, "features.code_mode": false,
            "features.shell_snapshot": false
        });
        let runtime = json!({"codex_home": directory.path(), "cwd": directory.path()});
        let engine = Arc::new(
            EngineState::new(config.to_string(), runtime.to_string(), false, None)
                .await
                .unwrap(),
        );
        let mut options: options::ThreadOptions = serde_json::from_value(json!({
            "_application_instructions": "", "tool_policy": {"allowed_tools": []},
            (hook): true
        }))
        .unwrap();
        options.process_private_http = true;
        // Real Engine startup without a turn; no model request or WS prewarm.
        let outcome = engine
            .start_with_initial_messages(options, None, Some(Default::default()), true, None)
            .await;
        let observed = outcome.as_ref().ok().map(|native| {
            native.current().map(|state| {
                state
                    .thread
                    .thread_extension_data()
                    .get::<codex_http_client::SdkModelHttpPool>()
                    .is_some()
            })
        });
        let closed = engine.close().await;
        closed.expect("join actual engine before reporting an assertion failure");
        assert!(
            !observed
                .expect("ordinary startup survives an optimization miss")
                .expect("capture actual live thread before close")
        );
    }
}
