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
static TRACE_INITIALIZED: AtomicBool = AtomicBool::new(false);
static ACTIVE: AtomicBool = AtomicBool::new(false);
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
    config.otel.trace_processors
        || !matches!(config.otel.exporter, OtelExporterKind::None)
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
    let (config_json, diagnostics) = crate::console_diagnostics::extract(config_json)?;
    let runtime_json = config::prepare_runtime(runtime_json)?;
    let (mut config, _) = config::load(&config_json, &runtime_json, None).await?;
    if uses_http_mtls(&config.otel.exporter)
        || (config.analytics_enabled.unwrap_or(true)
            && uses_http_mtls(&config.otel.metrics_exporter))
    {
        // This Config belongs only to telemetry initialization, not to Sessions.
        config.application_network_policy =
            http_mtls_network_policy(&config.application_network_policy);
    }

    codex_otel::sdk_trace::configure_pipeline(false);
    // Claim the subscriber first: losing this race must not modify native OTel globals.
    let (logger, handle) = reload::Layer::new(None::<ExportLayer>);
    let mut layers: Vec<ExportLayer> = vec![
        logger
            .with_filter(tracing_subscriber::filter::filter_fn(
                OtelProvider::log_export_filter,
            ))
            .boxed(),
    ];
    layers.push(OtelProvider::reloadable_tracing_layer(SERVICE).boxed());
    if let Some(diagnostics) = diagnostics {
        layers.push(diagnostics.layer(&ACTIVE).boxed());
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
    TRACE_INITIALIZED.store(
        config.otel.trace_processors
            || !matches!(config.otel.trace_exporter, OtelExporterKind::None),
        Ordering::Release,
    );
    codex_otel::sdk_trace::configure_pipeline(TRACE_INITIALIZED.load(Ordering::Acquire));
    ACTIVE.store(true, Ordering::Release);
    state.provider = provider;
    INITIALIZED.store(true, Ordering::Release);
    Ok(())
}

pub async fn shutdown(timeout_ms: Option<u32>) -> Result<()> {
    let timeout = timeout(timeout_ms)?;
    let started = std::time::Instant::now();
    // Publish before owner admission: an older flush may hold STATE until this
    // terminal call's entire budget expires. Its worker must still stop.
    codex_otel::sdk_trace_routes::begin_shutdown(started + timeout);
    let _host_credentials = codex_otel::sdk_trace_routes::HostTraceDeliveryShutdown;
    TRACE_INITIALIZED.store(false, Ordering::Release);
    ACTIVE.store(false, Ordering::Release);
    codex_otel::sdk_trace::configure_pipeline(false);
    INITIALIZED.store(false, Ordering::Release);
    let mut state = tokio::time::timeout(timeout, STATE.lock())
        .await
        .map_err(|_| anyhow!("Telemetry shutdown timed out waiting for the telemetry owner"))?;
    if let Some(provider) = state.provider.take() {
        let remaining = timeout.saturating_sub(started.elapsed());
        let _ = provider.force_flush_with_timeout(remaining).await;
        provider
            .shutdown_with_timeout(timeout.saturating_sub(started.elapsed()))
            .await?;
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

fn timeout(timeout_ms: Option<u32>) -> Result<Duration> {
    let value = timeout_ms.unwrap_or(SHUTDOWN_TIMEOUT.as_millis() as u32);
    ensure!(
        value > 0 && value <= 60_000,
        "Telemetry timeout must be between 1 and 60000 milliseconds"
    );
    Ok(Duration::from_millis(u64::from(value)))
}
pub fn tracing_ready() -> Result<()> {
    ensure!(
        TRACE_INITIALIZED.load(Ordering::Acquire),
        "Configure a trace exporter or trace processors before starting native traces"
    );
    Ok(())
}
pub async fn force_flush(timeout_ms: Option<u32>) -> Result<String> {
    let timeout = timeout(timeout_ms)?;
    let started = std::time::Instant::now();
    scope_ready()?;
    let state = match tokio::time::timeout(timeout, STATE.lock()).await {
        Ok(state) => state,
        // No barrier was admitted; zero identifies the empty prefix covered by this attempt.
        Err(_) => return Ok(serde_json::json!({"status":"timeout", "dropped_records":0, "failed_deliveries":0, "barrier":0}).to_string()),
    };
    let remaining = timeout.saturating_sub(started.elapsed());
    if let Some(provider) = state.provider.as_ref() {
        Ok(serde_json::to_string(
            &provider.force_flush_with_timeout(remaining).await,
        )?)
    } else {
        Ok(serde_json::json!({"status":"success", "dropped_records":0, "failed_deliveries":0, "barrier":0}).to_string())
    }
}

pub fn scope_ready() -> Result<()> {
    ensure!(
        ACTIVE.load(Ordering::Acquire),
        "Configure native telemetry before starting traces; shutdown is terminal"
    );
    Ok(())
}
