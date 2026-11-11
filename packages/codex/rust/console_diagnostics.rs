//! Optional local diagnostics on the existing process telemetry subscriber.

use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};

use anyhow::{Result, anyhow, ensure};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use tracing::{Event, Level, Metadata, Subscriber, field::Visit};
use tracing_subscriber::{Layer, Registry, filter::filter_fn, fmt::MakeWriter, layer::Context};

const KEY: &str = "sdk.console_diagnostics";
const INVALID: &str = "Invalid console diagnostic options";

#[derive(Clone, Copy, Default, Deserialize)]
#[serde(rename_all = "lowercase")]
enum MinimumLevel {
    Error,
    #[default]
    Warn,
    Info,
    Debug,
    Trace,
}

impl MinimumLevel {
    fn permits(self, level: &Level) -> bool {
        let maximum = match self {
            Self::Error => Level::ERROR,
            Self::Warn => Level::WARN,
            Self::Info => Level::INFO,
            Self::Debug => Level::DEBUG,
            Self::Trace => Level::TRACE,
        };
        *level <= maximum
    }
}

#[derive(Clone, Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct ConsoleDiagnostics {
    level: MinimumLevel,
    targets: Vec<String>,
    include_fields: bool,
}

/// Remove SDK-only options before Core interprets its configuration.
pub(crate) fn extract(config_json: &str) -> Result<(String, Option<ConsoleDiagnostics>)> {
    let mut config: Map<String, Value> = serde_json::from_str(config_json)
        .map_err(|_| anyhow!("Invalid telemetry configuration"))?;
    let diagnostics = config
        .remove(KEY)
        .map(|value| {
            let options: ConsoleDiagnostics =
                serde_json::from_value(value).map_err(|_| anyhow!(INVALID))?;
            ensure!(
                options.targets.iter().all(|target| !target.is_empty()),
                INVALID
            );
            Ok(options)
        })
        .transpose()?;
    Ok((serde_json::to_string(&config)?, diagnostics))
}

impl ConsoleDiagnostics {
    fn permits(&self, metadata: &Metadata<'_>) -> bool {
        self.level.permits(metadata.level())
            && (self.targets.is_empty()
                || self.targets.iter().any(|target| {
                    metadata.target() == target
                        || metadata
                            .target()
                            .strip_prefix(target)
                            .is_some_and(|suffix| suffix.starts_with("::"))
                }))
    }

    pub(crate) fn layer(self, active: &'static AtomicBool) -> impl Layer<Registry> {
        let filter = self.clone();
        ConsoleLayer {
            include_fields: self.include_fields,
            active,
            writer: std::io::stdout,
        }
        .with_filter(filter_fn(move |metadata| filter.permits(metadata)))
    }
}

struct ConsoleLayer<W> {
    include_fields: bool,
    active: &'static AtomicBool,
    writer: W,
}

impl<S, W> Layer<S> for ConsoleLayer<W>
where
    S: Subscriber,
    W: for<'writer> MakeWriter<'writer> + Send + Sync + 'static,
{
    fn on_event(&self, event: &Event<'_>, _context: Context<'_, S>) {
        // Do not cache this mutable admission check in callsite interest.
        // A write admitted before shutdown may finish after its transition.
        if !self.active.load(Ordering::Acquire) {
            return;
        }
        let metadata = event.metadata();
        let mut record = json!({
            "kind": "native_diagnostic",
            "level": metadata.level().as_str(),
            "target": metadata.target(),
            "name": metadata.name(),
            "file": metadata.file(),
            "line": metadata.line(),
        });
        if self.include_fields {
            let mut fields = Fields(Map::new());
            event.record(&mut fields);
            record["fields"] = Value::Object(fields.0);
        }
        // Default records never visit Debug/Display implementations, messages
        // or error fields: interpolated messages may contain sensitive data.
        if let Ok(mut encoded) = serde_json::to_vec(&record) {
            encoded.push(b'\n');
            let _ = self.writer.make_writer().write_all(&encoded);
        }
    }
}

struct Fields(Map<String, Value>);

impl Visit for Fields {
    fn record_debug(&mut self, field: &tracing::field::Field, value: &dyn std::fmt::Debug) {
        self.0
            .insert(field.name().into(), format!("{value:?}").into());
    }

    fn record_str(&mut self, field: &tracing::field::Field, value: &str) {
        self.0.insert(field.name().into(), value.into());
    }

    fn record_bool(&mut self, field: &tracing::field::Field, value: bool) {
        self.0.insert(field.name().into(), value.into());
    }

    fn record_i64(&mut self, field: &tracing::field::Field, value: i64) {
        self.0.insert(field.name().into(), value.into());
    }

    fn record_u64(&mut self, field: &tracing::field::Field, value: u64) {
        self.0.insert(field.name().into(), value.into());
    }

    fn record_f64(&mut self, field: &tracing::field::Field, value: f64) {
        self.0.insert(field.name().into(), value.into());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};
    use tracing_subscriber::layer::SubscriberExt;

    #[derive(Clone, Default)]
    struct Buffer(Arc<Mutex<Vec<u8>>>);

    impl Write for Buffer {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(bytes);
            Ok(bytes.len())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    impl<'writer> MakeWriter<'writer> for Buffer {
        type Writer = Self;

        fn make_writer(&'writer self) -> Self::Writer {
            self.clone()
        }
    }

    struct MustNotFormat;

    impl std::fmt::Debug for MustNotFormat {
        fn fmt(&self, _formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            panic!("default diagnostics must not format sensitive fields");
        }
    }

    #[test]
    fn filters_native_events_without_visiting_fields_or_muting_other_layers() {
        static ACTIVE: AtomicBool = AtomicBool::new(false);
        let buffer = Buffer::default();
        let other = Buffer::default();
        let options = ConsoleDiagnostics {
            targets: vec!["diagnostic_test".into()],
            ..Default::default()
        };
        let layer = ConsoleLayer {
            include_fields: false,
            active: &ACTIVE,
            writer: buffer.clone(),
        }
        .with_filter(filter_fn(move |metadata| options.permits(metadata)));
        static ALWAYS_ACTIVE: AtomicBool = AtomicBool::new(true);
        let other_layer = ConsoleLayer {
            include_fields: false,
            active: &ALWAYS_ACTIVE,
            writer: other.clone(),
        };
        tracing::subscriber::with_default(
            tracing_subscriber::registry().with(layer).with(other_layer),
            || {
                tracing::warn!(target: "diagnostic_test", hidden = ?MustNotFormat, "inactive");
                ACTIVE.store(true, Ordering::Release);
                tracing::warn!(target: "diagnostic_test", hidden = ?MustNotFormat, "private input");
                tracing::error!(target: "diagnostic_test::child", "private error");
                tracing::info!(target: "diagnostic_test", "below minimum");
                tracing::warn!(target: "diagnostic_test_other", "not a target child");
                ACTIVE.store(false, Ordering::Release);
                tracing::error!(target: "diagnostic_test", "after shutdown");
            },
        );
        let bytes = buffer.0.lock().unwrap();
        let lines: Vec<Value> = String::from_utf8(bytes.clone())
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(lines.len(), 2);
        assert_eq!(lines[0]["level"], "WARN");
        assert_eq!(lines[1]["target"], "diagnostic_test::child");
        assert!(lines.iter().all(|line| line.get("fields").is_none()));
        assert!(
            !String::from_utf8(bytes.clone())
                .unwrap()
                .contains("private")
        );
        assert_eq!(
            other
                .0
                .lock()
                .unwrap()
                .iter()
                .filter(|byte| **byte == b'\n')
                .count(),
            6
        );
    }

    #[test]
    fn includes_diagnostic_fields_only_with_explicit_opt_in() {
        static ACTIVE: AtomicBool = AtomicBool::new(true);
        let buffer = Buffer::default();
        tracing::subscriber::with_default(
            tracing_subscriber::registry().with(ConsoleLayer {
                include_fields: true,
                active: &ACTIVE,
                writer: buffer.clone(),
            }),
            || tracing::warn!(target: "diagnostic_test", count = 2u64, user_text = "synthetic-private", "details"),
        );
        let record: Value = serde_json::from_slice(&buffer.0.lock().unwrap()).unwrap();
        assert_eq!(record["fields"]["count"], 2);
        assert_eq!(record["fields"]["user_text"], "synthetic-private");
        assert_eq!(record["fields"]["message"], "details");
    }

    #[test]
    fn extracts_sdk_options_without_changing_core_settings() {
        let (remaining, options) = extract(
            r#"{"otel.environment":"test","sdk.console_diagnostics":{"level":"debug","targets":["codex_core"]}}"#,
        )
        .unwrap();
        assert!(options.is_some());
        assert_eq!(
            serde_json::from_str::<Value>(&remaining).unwrap(),
            json!({"otel.environment":"test"})
        );
        for invalid in [
            r#"{"level":"verbose"}"#,
            r#"{"targets":[""]}"#,
            r#"{"include_fields":"yes"}"#,
            r#"{"unknown":"synthetic-private"}"#,
            "null",
        ] {
            let input = format!(r#"{{"{KEY}":{invalid}}}"#);
            assert_eq!(extract(&input).err().unwrap().to_string(), INVALID);
        }
        assert!(extract("{}").unwrap().1.is_none());
    }
}
