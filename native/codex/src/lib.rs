use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result, bail};
use codex_app_server_client::{
    DEFAULT_IN_PROCESS_CHANNEL_CAPACITY, EmbeddedNetworkPolicy, EnvironmentManager,
    InProcessAppServerClient, InProcessClientStartArgs, InProcessServerEvent,
    legacy_core::config::{ConfigBuilder, ConfigOverrides},
};
use codex_app_server_protocol::{
    AskForApproval, ClientRequest, RequestId, SandboxMode, SandboxPolicy, ServerNotification,
    ThreadInjectItemsParams, ThreadInjectItemsResponse, ThreadItem, ThreadStartParams,
    ThreadStartResponse, TokenUsageBreakdown, TurnStartParams, TurnStartResponse, TurnStatus,
    UserInput,
};
use codex_config::{CloudConfigBundleLoader, LoaderOverrides, TomlValue};
use codex_feedback::CodexFeedback;
use codex_protocol::{config_types, models::MessagePhase, protocol::SessionSource};
use codex_utils_rustls_provider::ensure_rustls_crypto_provider;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use serde_json::{Value, json};
use tokio::sync::oneshot;

struct TurnOutput {
    turn_id: String,
    model: String,
    item_id: String,
    text: String,
    usage: Option<TokenUsageBreakdown>,
}

impl TurnOutput {
    fn into_python(self, py: Python<'_>) -> PyResult<Py<PyDict>> {
        let output = PyDict::new(py);
        output.set_item("turn_id", self.turn_id)?;
        output.set_item("model", self.model)?;
        output.set_item("item_id", self.item_id)?;
        output.set_item("text", self.text)?;
        let usage = self.usage.as_ref();
        output.set_item("input_tokens", usage.map(|value| value.input_tokens))?;
        output.set_item(
            "cached_input_tokens",
            usage.map(|value| value.cached_input_tokens),
        )?;
        output.set_item(
            "cache_write_input_tokens",
            usage.map(|value| value.cache_write_input_tokens),
        )?;
        output.set_item("output_tokens", usage.map(|value| value.output_tokens))?;
        output.set_item(
            "reasoning_output_tokens",
            usage.map(|value| value.reasoning_output_tokens),
        )?;
        output.set_item("total_tokens", usage.map(|value| value.total_tokens))?;
        Ok(output.unbind())
    }
}

#[pyfunction]
#[pyo3(signature = (prompt, instructions, model, cwd, history))]
fn run_turn(
    py: Python<'_>,
    prompt: String,
    instructions: Option<String>,
    model: Option<String>,
    cwd: String,
    history: Vec<(String, String)>,
) -> PyResult<Bound<'_, PyAny>> {
    pyo3_async_runtimes::tokio::future_into_py(py, async move {
        let history = history
            .into_iter()
            .map(history_item)
            .collect::<Result<_>>()
            .map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        let (mut result_tx, result_rx) = oneshot::channel();
        // Python cancellation closes the receiver. Keep shutdown owned by the
        // background task so cancellation cannot drop the shutdown future.
        tokio::spawn(async move {
            if result_tx.is_closed() {
                return;
            }
            let result = async {
                let mut client = start_client(&cwd).await?;
                let result = tokio::select! {
                    biased;
                    () = result_tx.closed() => Err(anyhow::anyhow!("Codex turn cancelled")),
                    result = execute_turn(&mut client, prompt, instructions, model, cwd, history) => result,
                };
                let shutdown = client.shutdown().await.context("Could not shut down Codex");
                let output = result?;
                shutdown?;
                Ok(output)
            }.await;
            let _ = result_tx.send(result);
        });

        let result: Result<TurnOutput> = result_rx
            .await
            .map_err(|_| PyRuntimeError::new_err("Codex runtime stopped unexpectedly"))?;
        let output = result.map_err(|error| PyRuntimeError::new_err(error.to_string()))?;
        Python::attach(|py| output.into_python(py))
    })
}

fn history_item((role, text): (String, String)) -> Result<Value> {
    let content_type = match role.as_str() {
        "user" => "input_text",
        "assistant" => "output_text",
        _ => bail!("Codex history supports user and assistant messages"),
    };
    Ok(json!({
        "type": "message",
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }))
}

async fn start_client(cwd: &str) -> Result<InProcessAppServerClient> {
    ensure_rustls_crypto_provider();
    let loader_overrides = LoaderOverrides {
        ignore_project_config: true,
        ..Default::default()
    };
    let cli_overrides = vec![
        ("features.hooks".to_string(), TomlValue::Boolean(false)),
        ("features.plugins".to_string(), TomlValue::Boolean(false)),
    ];
    let cloud_config_bundle = CloudConfigBundleLoader::default();
    let embedded_network_policy = EmbeddedNetworkPolicy::load(&loader_overrides).await;
    let mut config = ConfigBuilder::default()
        .harness_overrides(ConfigOverrides {
            cwd: Some(PathBuf::from(cwd)),
            sandbox_mode: Some(config_types::SandboxMode::ReadOnly),
            approval_policy: Some(codex_protocol::protocol::AskForApproval::Never),
            ..Default::default()
        })
        .cli_overrides(cli_overrides.clone())
        .loader_overrides(loader_overrides.clone())
        .cloud_config_bundle(cloud_config_bundle.clone())
        .build()
        .await
        .context("Could not load Codex configuration")?;
    embedded_network_policy.activate(&mut config);
    // The Python process has no Codex executable for sandbox dispatch.
    let environment_manager = EnvironmentManager::without_environments(
        embedded_network_policy.bind(config.http_client_factory()),
    );
    InProcessAppServerClient::start(InProcessClientStartArgs {
        arg0_paths: Default::default(),
        config: Arc::new(config),
        cli_overrides,
        loader_overrides,
        strict_config: false,
        cloud_config_bundle,
        embedded_network_policy,
        feedback: CodexFeedback::new(),
        log_db: None,
        state_db: None,
        environment_manager: Arc::new(environment_manager),
        config_warnings: Vec::new(),
        session_source: SessionSource::Exec,
        enable_codex_api_key_env: true,
        client_name: "openai-agents-python".to_string(),
        client_version: env!("CARGO_PKG_VERSION").to_string(),
        experimental_api: true,
        mcp_server_openai_form_elicitation: false,
        opt_out_notification_methods: Vec::new(),
        channel_capacity: DEFAULT_IN_PROCESS_CHANNEL_CAPACITY,
    })
    .await
    .context("Could not start Codex")
}

async fn execute_turn(
    client: &mut InProcessAppServerClient,
    prompt: String,
    instructions: Option<String>,
    model: Option<String>,
    cwd: String,
    history: Vec<Value>,
) -> Result<TurnOutput> {
    let response: ThreadStartResponse = client
        .request_typed(ClientRequest::ThreadStart {
            request_id: RequestId::Integer(1),
            params: ThreadStartParams {
                model,
                cwd: Some(cwd),
                developer_instructions: instructions,
                approval_policy: Some(AskForApproval::Never),
                sandbox: Some(SandboxMode::ReadOnly),
                ephemeral: Some(true),
                environments: Some(Vec::new()),
                ..Default::default()
            },
        })
        .await?;
    if response.approval_policy != AskForApproval::Never
        || response.sandbox
            != (SandboxPolicy::ReadOnly {
                network_access: false,
            })
    {
        bail!("Codex configuration requires permissions unavailable to the provider");
    }
    let thread_id = response.thread.id;
    let mut model = response.model;

    if !history.is_empty() {
        client
            .request_typed::<ThreadInjectItemsResponse>(ClientRequest::ThreadInjectItems {
                request_id: RequestId::Integer(2),
                params: ThreadInjectItemsParams {
                    thread_id: thread_id.clone(),
                    items: history,
                },
            })
            .await?;
    }

    let response: TurnStartResponse = client
        .request_typed(ClientRequest::TurnStart {
            request_id: RequestId::Integer(3),
            params: TurnStartParams {
                thread_id: thread_id.clone(),
                input: vec![UserInput::Text {
                    text: prompt,
                    text_elements: Vec::new(),
                }],
                ..Default::default()
            },
        })
        .await?;
    let turn_id = response.turn.id;
    let mut usage = None;
    let mut answer = None;
    let mut unphased_answer = None;

    while let Some(event) = client.next_event().await {
        let notification = match event {
            InProcessServerEvent::ServerNotification(notification) => notification,
            InProcessServerEvent::Lagged { skipped } => {
                bail!("Codex event stream lost {skipped} events")
            }
            InProcessServerEvent::ServerRequest(_) => {
                bail!("Codex turn requires interactive input")
            }
        };
        match *notification {
            ServerNotification::ItemCompleted(event)
                if event.thread_id == thread_id && event.turn_id == turn_id =>
            {
                if let ThreadItem::AgentMessage {
                    id, text, phase, ..
                } = event.item
                {
                    match phase {
                        Some(MessagePhase::FinalAnswer) => answer = Some((id, text)),
                        None => unphased_answer = Some((id, text)),
                        _ => {}
                    }
                }
            }
            ServerNotification::ThreadTokenUsageUpdated(event)
                if event.thread_id == thread_id && event.turn_id == turn_id =>
            {
                usage = Some(event.token_usage.total);
            }
            ServerNotification::ModelRerouted(event)
                if event.thread_id == thread_id && event.turn_id == turn_id =>
            {
                model = event.to_model;
            }
            ServerNotification::TurnCompleted(event)
                if event.thread_id == thread_id && event.turn.id == turn_id =>
            {
                if event.turn.status != TurnStatus::Completed {
                    match event.turn.error {
                        Some(error) => bail!("Codex turn failed: {}", error.message),
                        None => bail!("Codex turn ended with status {:?}", event.turn.status),
                    }
                }
                let (item_id, text) = answer
                    .or(unphased_answer)
                    .context("Codex turn completed without an assistant message")?;
                return Ok(TurnOutput {
                    turn_id,
                    model,
                    item_id,
                    text,
                    usage,
                });
            }
            _ => {}
        }
    }
    bail!("Codex event stream ended before the turn completed")
}

#[pymodule]
fn _openai_agents_codex(module: &Bound<'_, PyModule>) -> PyResult<()> {
    let mut runtime = tokio::runtime::Builder::new_multi_thread();
    runtime
        .enable_all()
        .thread_stack_size(codex_async_utils::THREAD_STACK_SIZE_BYTES);
    pyo3_async_runtimes::tokio::init(runtime);
    module.add_function(wrap_pyfunction!(run_turn, module)?)
}
