//! Explicit process ownership for Codex's process-global telemetry providers.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::{Context, Result, anyhow, ensure};
use codex_config::types::OtelExporterKind;
use codex_core_api::Config;
use codex_http_client::{DestinationPolicy, NetworkPolicy, NetworkPolicyController};
use codex_otel::OtelProvider;
use tokio::sync::Mutex;
use tracing_subscriber::{Layer, Registry, layer::SubscriberExt, reload};

use crate::config;

struct State {
    claimed: bool,
    engine_started: bool,
    provider: Option<OtelProvider>,
}

static STATE: Mutex<State> = Mutex::const_new(State {
    claimed: false,
    engine_started: false,
    provider: None,
});
static INITIALIZED: AtomicBool = AtomicBool::new(false);
const SERVICE: &str = "codex_agents_python";
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);

type ExportLayer = Box<dyn Layer<Registry> + Send + Sync>;

pub fn is_initialized() -> bool {
    INITIALIZED.load(Ordering::Acquire)
}

pub async fn engine_starting() -> Result<()> {
    let mut state = STATE.lock().await;
    ensure!(
        !state.claimed || is_initialized(),
        "Codex telemetry was shut down or failed to initialize; start a new process"
    );
    state.engine_started = true;
    Ok(())
}

pub fn export_requested(config: &Config) -> bool {
    !matches!(config.otel.exporter, OtelExporterKind::None)
        || !matches!(config.otel.trace_exporter, OtelExporterKind::None)
        || (config.analytics_enabled.unwrap_or(true)
            && !matches!(config.otel.metrics_exporter, OtelExporterKind::None))
}

fn uses_http_mtls(exporter: &OtelExporterKind) -> bool {
    matches!(
        exporter,
        OtelExporterKind::OtlpHttp { tls: Some(tls), .. }
            if tls.client_certificate.is_some() && tls.client_private_key.is_some()
    )
}

fn http_mtls_network_policy(policy: &NetworkPolicy) -> NetworkPolicy {
    if policy != &NetworkPolicy::unmanaged() {
        return policy.clone();
    }
    // The pinned OTLP blocking client does not select the TLS backend needed
    // by PEM identities. A managed policy selects its existing async TLS client.
    // Only replace the unrestricted default; retain every existing restriction.
    let controller = NetworkPolicyController::default();
    let policy = controller.policy();
    controller.publish(policy.revision(), DestinationPolicy::Unrestricted);
    policy
}

pub async fn initialize(config_json: &str, runtime_json: &str) -> Result<()> {
    let mut state = STATE.lock().await;
    ensure!(
        !state.claimed,
        "Codex telemetry can only be configured once per process"
    );
    ensure!(
        !state.engine_started,
        "Configure telemetry before creating any Codex clients"
    );
    ensure!(
        !tracing::dispatcher::has_been_set(),
        "A Rust tracing subscriber is already installed; Codex telemetry cannot replace it"
    );
    let runtime_json = config::prepare_runtime(runtime_json)?;
    let (mut config, _) = config::load(config_json, &runtime_json, None).await?;
    if uses_http_mtls(&config.otel.exporter)
        || (config.analytics_enabled.unwrap_or(true)
            && uses_http_mtls(&config.otel.metrics_exporter))
    {
        // This Config belongs only to telemetry initialization, not to Sessions.
        config.application_network_policy =
            http_mtls_network_policy(&config.application_network_policy);
    }

    // Claim the subscriber first: losing this race must not modify native OTel globals.
    let (logger, handle) = reload::Layer::new(None::<ExportLayer>);
    let mut layers: Vec<ExportLayer> = vec![
        logger
            .with_filter(tracing_subscriber::filter::filter_fn(
                OtelProvider::log_export_filter,
            ))
            .boxed(),
    ];
    if !matches!(config.otel.trace_exporter, OtelExporterKind::None) {
        layers.push(OtelProvider::reloadable_tracing_layer(SERVICE).boxed());
    }
    tracing::subscriber::set_global_default(tracing_subscriber::registry().with(layers))
        .context("Could not claim the process-wide Rust tracing subscriber")?;
    state.claimed = true;

    let provider = codex_core::otel_init::build_provider(
        &config,
        env!("CARGO_PKG_VERSION"),
        Some(SERVICE),
        true,
    )
    .map_err(|error| {
        anyhow!("Telemetry initialization failed; start a new process before retrying: {error}")
    })?;
    let logger = provider
        .as_ref()
        .and_then(OtelProvider::logger_export_layer)
        .map(Layer::boxed);
    if let Err(error) = handle.reload(logger) {
        if let Some(provider) = provider {
            provider.shutdown_with_timeout(SHUTDOWN_TIMEOUT).await?;
        }
        return Err(anyhow!(
            "Could not install telemetry export layers: {error}"
        ));
    }
    codex_core::otel_init::record_process_start(provider.as_ref(), SERVICE);
    codex_core::otel_init::install_sqlite_telemetry(provider.as_ref(), SERVICE);
    state.provider = provider;
    INITIALIZED.store(true, Ordering::Release);
    Ok(())
}

pub async fn shutdown() -> Result<()> {
    let mut state = STATE.lock().await;
    INITIALIZED.store(false, Ordering::Release);
    if let Some(provider) = state.provider.take() {
        provider.shutdown_with_timeout(SHUTDOWN_TIMEOUT).await?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    #[test]
    fn only_http_client_identities_need_the_transport_adapter() {
        let directory = tempfile::tempdir().unwrap();
        let certificate = directory.path().join("client.pem");
        let private_key = directory.path().join("client.key");
        for (kind, tls, expected) in [
            ("otlp-http", serde_json::json!({}), false),
            (
                "otlp-http",
                serde_json::json!({"ca-certificate": certificate}),
                false,
            ),
            (
                "otlp-http",
                serde_json::json!({"client-certificate": certificate}),
                false,
            ),
            (
                "otlp-http",
                serde_json::json!({"client-certificate": certificate, "client-private-key": private_key}),
                true,
            ),
            (
                "otlp-grpc",
                serde_json::json!({"client-certificate": certificate, "client-private-key": private_key}),
                false,
            ),
        ] {
            let exporter = serde_json::from_value(serde_json::json!({kind: {
                "endpoint": "https://collector.example", "protocol": "json", "tls": tls
            }}))
            .unwrap();
            assert_eq!(uses_http_mtls(&exporter), expected);
        }
    }

    #[test]
    fn http_mtls_default_policy_allows_exports_after_controller_is_dropped() {
        let policy = http_mtls_network_policy(&NetworkPolicy::unmanaged());
        assert!(policy.is_managed());
        assert!(policy.acquire_for_unsupported_sdk().is_ok());
        assert!(
            policy
                .acquire(&"https://collector.example/v1/logs".parse().unwrap())
                .is_ok()
        );
    }

    #[test]
    fn http_mtls_preserves_unmanaged_endpoint_restrictions() {
        let endpoint = "https://collector.example/v1/logs".parse().unwrap();
        let original = NetworkPolicy::unmanaged().restrict_to_endpoints(BTreeSet::from([endpoint]));
        let policy = http_mtls_network_policy(&original);
        assert_eq!(policy, original);
        assert!(policy.acquire_for_unsupported_sdk().is_err());
        assert!(
            policy
                .acquire(&"https://other.example/v1/logs".parse().unwrap())
                .is_err()
        );
    }

    #[test]
    fn http_mtls_preserves_managed_restrictions_and_revocation() {
        let controller = NetworkPolicyController::default();
        let original = controller.policy();
        let policy = http_mtls_network_policy(&original);
        assert_eq!(policy, original);
        assert!(policy.acquire_for_unsupported_sdk().is_err());
        controller.publish(policy.revision(), DestinationPolicy::Unrestricted);
        let permit = policy.acquire_for_unsupported_sdk().unwrap();
        controller.publish(
            policy.revision(),
            DestinationPolicy::Restricted {
                allowed_hosts: BTreeSet::new(),
            },
        );
        assert!(permit.check().is_err());
        assert!(policy.acquire_for_unsupported_sdk().is_err());
    }
}
