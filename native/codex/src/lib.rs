use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result, bail};
use codex_app_server_client::{
    DEFAULT_IN_PROCESS_CHANNEL_CAPACITY, EmbeddedNetworkPolicy, EnvironmentManager,
    ExecServerRuntimeOptions, InProcessAppServerClient, InProcessClientStartArgs,
    InProcessServerEvent,
    legacy_core::config::{ConfigBuilder, ConfigOverrides, find_codex_home},
};
use codex_app_server_protocol::{
    ClientRequest, JSONRPCErrorError, McpServerElicitationAction,
    McpServerElicitationRequestResponse, RequestId, ServerNotification, ServerRequest,
    ThreadBackgroundTerminalsCleanParams, ThreadBackgroundTerminalsCleanResponse,
    ThreadInjectItemsParams, ThreadInjectItemsResponse, ThreadItem, ThreadStartParams,
    ThreadStartResponse, TokenUsageBreakdown, TurnInterruptParams, TurnInterruptResponse,
    TurnStartParams, TurnStartResponse, TurnStatus, UserInput,
};
use codex_arg0::Arg0DispatchPaths;
use codex_config::{CloudConfigBundleLoader, LoaderOverrides};
use codex_feedback::CodexFeedback;
use codex_protocol::{
    models::MessagePhase, permissions::FileSystemSandboxPolicy, protocol::SessionSource,
};
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

struct TurnInput {
    prompt: String,
    instructions: Option<String>,
    model: Option<String>,
    cwd: String,
    history: Vec<Value>,
}

struct ExecutionHelpers<'a> {
    executable: &'a Path,
    aliases: Option<tempfile::TempDir>,
}

impl ExecutionHelpers<'_> {
    fn require_protected_paths(&self, policy: &FileSystemSandboxPolicy, cwd: &Path) -> Result<()> {
        if !policy.has_full_disk_write_access()
            && std::iter::once(self.executable)
                .chain(self.aliases.as_ref().map(|aliases| aliases.path()))
                .any(|path| policy.can_write_local_path_with_cwd(path, cwd))
        {
            bail!("Place the Codex executable and CODEX_HOME outside sandbox-writable paths");
        }
        Ok(())
    }
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
#[pyo3(signature = (prompt, instructions, model, cwd, history, codex_executable))]
fn run_turn(
    py: Python<'_>,
    prompt: String,
    instructions: Option<String>,
    model: Option<String>,
    cwd: String,
    history: Vec<(String, String)>,
    codex_executable: String,
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
                let (mut client, helpers) =
                    start_client(&cwd, Path::new(&codex_executable)).await?;
                let input = TurnInput {
                    prompt,
                    instructions,
                    model,
                    cwd,
                    history,
                };
                let result = execute_turn(&mut client, input, &helpers, &mut result_tx).await;
                let shutdown = client.shutdown().await.context("Could not shut down Codex");
                let output = result?;
                shutdown?;
                Ok(output)
            }
            .await;
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

fn prepare_helper_paths(
    executable: &Path,
    codex_home: &Path,
) -> Result<(Arg0DispatchPaths, Option<tempfile::TempDir>)> {
    if !executable.is_absolute() || !executable.is_file() {
        bail!("Codex execution requires the absolute path to a native Codex executable");
    }
    let paths = Arg0DispatchPaths {
        codex_self_exe: Some(executable.to_path_buf()),
        ..Default::default()
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::{DirBuilderExt, PermissionsExt, symlink};

        let alias_root = codex_home.join("tmp/arg0");
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&alias_root)
            .context("Could not create Codex helper root")?;
        let alias_root = alias_root
            .canonicalize()
            .context("Could not resolve Codex helper root")?;
        #[cfg(not(debug_assertions))]
        {
            let codex_home = codex_home
                .canonicalize()
                .context("Could not resolve CODEX_HOME")?;
            for temp_root in [std::env::temp_dir(), PathBuf::from("/tmp")] {
                let temp_root = temp_root.canonicalize().unwrap_or(temp_root);
                if codex_home.starts_with(&temp_root) || alias_root.starts_with(&temp_root) {
                    bail!(
                        "Use a CODEX_HOME and helper root outside system temporary directories for Codex execution"
                    );
                }
            }
        }
        if std::fs::metadata(&alias_root)?.permissions().mode() & 0o022 != 0 {
            bail!("The Codex helper root must not be writable by other users");
        }
        // The aliases retain Codex's argv[0] dispatch without changing Python's PATH.
        let aliases = tempfile::Builder::new()
            .prefix("codex-provider-arg0-")
            .tempdir_in(alias_root)
            .context("Could not create Codex helper directory")?;
        let execve = aliases.path().join("codex-execve-wrapper");
        symlink(executable, &execve).context("Could not create Codex shell helper alias")?;
        let linux_sandbox = if cfg!(target_os = "linux") {
            let alias = aliases.path().join("codex-linux-sandbox");
            symlink(executable, &alias)
                .context("Could not create Codex Linux sandbox helper alias")?;
            Some(alias)
        } else {
            None
        };
        Ok((
            Arg0DispatchPaths {
                codex_linux_sandbox_exe: linux_sandbox,
                main_execve_wrapper_exe: Some(execve),
                ..paths
            },
            Some(aliases),
        ))
    }
    #[cfg(not(unix))]
    {
        let _ = codex_home;
        Ok((paths, None))
    }
}

async fn start_client<'a>(
    cwd: &str,
    executable: &'a Path,
) -> Result<(InProcessAppServerClient, ExecutionHelpers<'a>)> {
    let cwd = PathBuf::from(cwd);
    if !cwd.is_absolute() || !cwd.is_dir() {
        bail!("Codex execution requires an existing absolute working directory");
    }
    ensure_rustls_crypto_provider();
    let codex_home = find_codex_home().context("Could not resolve CODEX_HOME")?;
    let (arg0_paths, aliases) = prepare_helper_paths(executable, codex_home.as_path())?;
    let helpers = ExecutionHelpers {
        executable,
        aliases,
    };
    let loader_overrides = LoaderOverrides::default();
    let cloud_config_bundle = CloudConfigBundleLoader::default();
    let embedded_network_policy = EmbeddedNetworkPolicy::load(&loader_overrides).await;
    let mut config = ConfigBuilder::default()
        .codex_home(codex_home.to_path_buf())
        .harness_overrides(ConfigOverrides {
            cwd: Some(cwd),
            codex_self_exe: arg0_paths.codex_self_exe.clone(),
            codex_linux_sandbox_exe: arg0_paths.codex_linux_sandbox_exe.clone(),
            main_execve_wrapper_exe: arg0_paths.main_execve_wrapper_exe.clone(),
            ..Default::default()
        })
        .loader_overrides(loader_overrides.clone())
        .cloud_config_bundle(cloud_config_bundle.clone())
        .build()
        .await
        .context("Could not load Codex configuration")?;
    helpers.require_protected_paths(
        &config.permissions.file_system_sandbox_policy(),
        config.cwd.as_path(),
    )?;
    embedded_network_policy.activate(&mut config);
    let runtime_paths = ExecServerRuntimeOptions::from_optional_paths(
        arg0_paths.codex_self_exe.clone(),
        arg0_paths.codex_linux_sandbox_exe.clone(),
    )?;
    #[cfg(target_os = "macos")]
    let runtime_paths = runtime_paths.with_allowed_symlinked_codex_home(
        codex_config::allowed_symlinked_codex_home(&config.config_layer_stack, &config.codex_home),
    );
    let environment_manager = EnvironmentManager::from_codex_home(
        config.codex_home.clone(),
        Some(runtime_paths),
        embedded_network_policy.bind(config.http_client_factory()),
    )
    .await
    .context("Could not load Codex execution environments")?;
    let client = InProcessAppServerClient::start(InProcessClientStartArgs {
        arg0_paths,
        config: Arc::new(config),
        cli_overrides: Vec::new(),
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
    .context("Could not start Codex")?;
    Ok((client, helpers))
}

async fn reject_interactive_request(
    client: &InProcessAppServerClient,
    request: ServerRequest,
) -> Result<()> {
    if let ServerRequest::McpServerElicitationRequest { request_id, .. } = request {
        client
            .resolve_server_request(
                request_id,
                serde_json::to_value(McpServerElicitationRequestResponse {
                    action: McpServerElicitationAction::Cancel,
                    content: None,
                    meta: None,
                })?,
            )
            .await?;
    } else {
        client
            .reject_server_request(
                request.id().clone(),
                JSONRPCErrorError {
                    code: -32000,
                    message: "CodexProvider cannot answer interactive requests".into(),
                    data: None,
                },
            )
            .await?;
    }
    Ok(())
}

async fn execute_turn(
    client: &mut InProcessAppServerClient,
    input: TurnInput,
    helpers: &ExecutionHelpers<'_>,
    result_tx: &mut oneshot::Sender<Result<TurnOutput>>,
) -> Result<TurnOutput> {
    if result_tx.is_closed() {
        bail!("Codex turn cancelled");
    }
    let response: ThreadStartResponse = client
        .request_typed(ClientRequest::ThreadStart {
            request_id: RequestId::Integer(1),
            params: ThreadStartParams {
                model: input.model,
                cwd: Some(input.cwd),
                developer_instructions: input.instructions,
                ephemeral: Some(true),
                ..Default::default()
            },
        })
        .await?;
    let thread_id = response.thread.id;
    let result = async {
        // Codex may reload project configuration after establishing project trust.
        let file_policy = FileSystemSandboxPolicy::from_legacy_sandbox_policy_for_cwd(
            &response.sandbox.to_core(),
            response.cwd.as_path(),
        );
        helpers.require_protected_paths(&file_policy, response.cwd.as_path())?;

        if result_tx.is_closed() {
            bail!("Codex turn cancelled");
        }
        if !input.history.is_empty() {
            client
                .request_typed::<ThreadInjectItemsResponse>(ClientRequest::ThreadInjectItems {
                    request_id: RequestId::Integer(2),
                    params: ThreadInjectItemsParams {
                        thread_id: thread_id.clone(),
                        items: input.history,
                    },
                })
                .await?;
            if result_tx.is_closed() {
                bail!("Codex turn cancelled");
            }
        }

        // Once turn/start is sent, retain its response even if Python cancels.
        // The request can start tools before returning the owned turn ID.
        let started = client
            .request_typed::<TurnStartResponse>(ClientRequest::TurnStart {
                request_id: RequestId::Integer(3),
                params: TurnStartParams {
                    thread_id: thread_id.clone(),
                    input: vec![UserInput::Text {
                        text: input.prompt,
                        text_elements: Vec::new(),
                    }],
                    ..Default::default()
                },
            })
            .await;
        let turn_id = match started {
            Ok(started) => started.turn.id,
            Err(error) => {
                interrupt_turn(client, &thread_id, "").await;
                return Err(error.into());
            }
        };
        let mut terminal = false;
        let result = tokio::select! {
            biased;
            () = result_tx.closed() => Err(anyhow::anyhow!("Codex turn cancelled")),
            result = read_turn(client, &thread_id, &turn_id, response.model, &mut terminal) => result,
        };
        if !terminal {
            interrupt_turn(client, &thread_id, &turn_id).await;
        }
        result
    }
    .await;

    // Stock turn interruption leaves background terminals running for reuse.
    // This provider owns an ephemeral thread, so it must also clean its terminals.
    let cleanup = client
        .request_typed::<ThreadBackgroundTerminalsCleanResponse>(
            ClientRequest::ThreadBackgroundTerminalsClean {
                request_id: RequestId::Integer(5),
                params: ThreadBackgroundTerminalsCleanParams { thread_id },
            },
        )
        .await;
    let output = result?;
    cleanup.context("Could not clean Codex background terminals")?;
    Ok(output)
}

async fn interrupt_turn(client: &InProcessAppServerClient, thread_id: &str, turn_id: &str) {
    // A turn can finish before the interrupt arrives. Keep the original error
    // primary and clean the owned thread even if Codex rejects this interrupt.
    let _ = client
        .request_typed::<TurnInterruptResponse>(ClientRequest::TurnInterrupt {
            request_id: RequestId::Integer(4),
            params: TurnInterruptParams {
                thread_id: thread_id.to_owned(),
                turn_id: turn_id.to_owned(),
            },
        })
        .await;
}

async fn read_turn(
    client: &mut InProcessAppServerClient,
    thread_id: &str,
    turn_id: &str,
    mut model: String,
    terminal: &mut bool,
) -> Result<TurnOutput> {
    let mut usage = None;
    let mut answer = None;
    let mut unphased_answer = None;

    while let Some(event) = client.next_event().await {
        let notification = match event {
            InProcessServerEvent::ServerNotification(notification) => notification,
            InProcessServerEvent::Lagged { skipped } => {
                bail!("Codex event stream lost {skipped} events")
            }
            InProcessServerEvent::ServerRequest(request) => {
                reject_interactive_request(client, *request).await?;
                continue;
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
                *terminal = true;
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
                    turn_id: turn_id.to_owned(),
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
