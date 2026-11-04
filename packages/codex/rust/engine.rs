//! Constructs native services and owns their orderly shutdown.

use std::collections::{HashMap, HashSet, VecDeque};
use std::future::{Future, poll_fn};
use std::pin::Pin;
use std::sync::{
    Arc, RwLock, Weak,
    atomic::{AtomicBool, Ordering},
};
use std::task::Poll;
use std::time::{Duration, SystemTime};

use anyhow::{Context, ensure};
use codex_core_api::*;
use codex_history::{ResumedHistory, RolloutItem};
use codex_http_client::HttpClientFactory;
use codex_login::AuthManager;
use codex_models_manager::ModelsManagerConfig;
use codex_models_manager::manager::{ModelsManager, ModelsManagerFuture, RefreshStrategy};
use codex_protocol::config_types::CollaborationModeMask;
use codex_protocol::openai_models::{ModelInfo, ModelPreset, ModelsResponse};
use codex_protocol::protocol::{Event, ThreadHistoryMode};
use codex_protocol::turn_input::SuspendTurnOutcome;
use codex_thread_store::{
    DeleteThreadParams, ForkBoundary, LoadThreadHistoryParams, LocalThreadStore,
    LocalThreadStoreConfig, PrepareForkParams, ReadThreadByRolloutPathParams, ReadThreadParams,
    StoredThread, ThreadStore, ThreadStoreError,
};
use tokio::sync::{Mutex, OwnedRwLockReadGuard, RwLock as AsyncRwLock, broadcast, mpsc, watch};

use crate::{NativeThread, config, extensions, goals, options, telemetry};

pub(crate) const CLOSE_TIMEOUT: Duration = Duration::from_secs(30);

pub struct EngineState {
    pub manager: Arc<ThreadManager>,
    pub context_gate: Arc<AsyncRwLock<()>>,
    pub context_retired: AtomicBool,
    pub store: Arc<dyn ThreadStore>,
    pub local_checklist_store: Option<Arc<crate::storage::LocalChecklistStore>>,
    pub agent_graph: Arc<crate::storage::MemoryAgentGraph>,
    pub goals: Option<goals::GoalBackend>,
    pub deferred_environment: bool,
    local_environment_error: Option<String>,
    pub native_turn_control: bool,
    sdk_user_input_enabled: Option<bool>,
    pub attached_environment: RwLock<Option<codex_protocol::protocol::TurnEnvironmentSelection>>,
    pub environment_connection: Arc<Mutex<()>>,
    config_json: String,
    runtime_json: String,
    pub(crate) closed: AtomicBool,
    pub(crate) lifecycle: Mutex<()>,
    event_owners: Mutex<HashMap<ThreadId, ThreadId>>,
    // MemoryThreadStore cannot persist extension mode. Retain only provenance
    // for this engine/store lifetime, including after the live owner closes.
    managed_thread_ids: Mutex<HashSet<ThreadId>>,
    // Only the PyO3 slot is tracked, weakly: this must not keep a ThreadState
    // (which itself owns its EngineState) alive.
    native_threads: std::sync::Mutex<Vec<Weak<std::sync::Mutex<crate::NativeThreadOwner>>>>,
    pub startup_warnings: Vec<String>,
    extension_events: Arc<extensions::Events>,
    cloud_config: codex_config::CloudConfigBundleLoader,
    pub(crate) service_config: Config,
    initial_config_available: AtomicBool,
    pub(crate) interception: Arc<crate::native_interception::Boundary>,
    pub restored_creation_times: Mutex<HashMap<ThreadId, SystemTime>>,
}

impl EngineState {
    pub(crate) fn validate_hosted_search(config: &Config, info: &ModelInfo) -> anyhow::Result<()> {
        // Lite selects Core's standalone executor even with its feature flag
        // off. A hosted grant must not silently acquire that other route.
        ensure!(
            config.web_search_mode.value() == codex_protocol::config_types::WebSearchMode::Disabled
                || config.features.enabled(Feature::StandaloneWebSearch)
                || !info.use_responses_lite,
            "WebSearch hosted route is unavailable with the selected native model's Responses Lite protocol"
        );
        Ok(())
    }

    pub(crate) async fn validate_hosted_search_model(&self, model: &str) -> anyhow::Result<()> {
        if self.service_config.web_search_mode.value()
            != codex_protocol::config_types::WebSearchMode::Disabled
            && !self
                .service_config
                .features
                .enabled(Feature::StandaloneWebSearch)
        {
            let info = self
                .manager
                .get_models_manager()
                .get_model_info(model, &self.service_config.to_models_manager_config())
                .await;
            Self::validate_hosted_search(&self.service_config, &info)?;
        }
        Ok(())
    }

    pub fn operation(&self, closing: bool) -> anyhow::Result<OwnedRwLockReadGuard<()>> {
        let permit = self
            .context_gate
            .clone()
            .try_read_owned()
            .context("application compaction is in progress")?;
        ensure!(
            closing
                || (!self.closed.load(Ordering::Acquire)
                    && !self.context_retired.load(Ordering::Acquire)),
            "Codex engine is closed"
        );
        Ok(permit)
    }

    #[cfg(test)]
    pub async fn new(
        config_json: String,
        runtime_json: String,
        checkpoint: bool,
        api_key: Option<String>,
    ) -> anyhow::Result<Self> {
        Self::new_with_model(config_json, runtime_json, checkpoint, api_key, None).await
    }

    pub async fn new_with_model(
        config_json: String,
        runtime_json: String,
        checkpoint: bool,
        api_key: Option<String>,
        initial_model: Option<String>,
    ) -> anyhow::Result<Self> {
        telemetry::engine_starting().await?;
        let runtime_json = config::prepare_runtime(&runtime_json)?;
        let (mut config, cloud_config) = config::load(&config_json, &runtime_json, None).await?;
        let native_turn_control = config::native_turn_control(&runtime_json)?;
        let sdk_user_input_enabled = config::sdk_native_config(&runtime_json)?
            .then_some(config.experimental_request_user_input_enabled);
        if native_turn_control {
            Self::check_controlled_config(&config)?;
        }
        let native_agents = config::native_agents(&runtime_json)?;
        if native_agents {
            if let Some(model) = &initial_model {
                config.model = Some(model.clone());
            }
        }
        ensure!(
            !checkpoint || !config.ephemeral,
            "checkpoints require persistent threads"
        );
        let added_telemetry_warning =
            telemetry::export_requested(&config) && !telemetry::is_initialized();
        if added_telemetry_warning {
            config.startup_warnings.push(
                "Telemetry export is configured but not initialized; call configure_telemetry \
                 before starting any Codex clients to enable process-wide export"
                    .into(),
            );
        }
        // Portable imports always use isolated memory, even when their historical
        // options mention a local path. Only this current engine can opt in.
        let local_home = if checkpoint {
            None
        } else {
            config::native_thread_store(&runtime_json)?
        };
        if local_home.is_some() {
            ensure!(
                !config.ephemeral,
                "native local SDK storage requires non-ephemeral threads"
            );
        }
        let goals_enabled = config::native_goal_store_enabled(&runtime_json)?;
        let state_db = if goals_enabled || local_home.is_some() {
            // StateRuntime supplies the exact goal store used by the extension.
            // Avoid rollout::try_init's unrelated host rollout backfill.
            Some(
                codex_state::StateRuntime::init(
                    config.sqlite_config().clone(),
                    config.model_provider_id.clone(),
                )
                .await?,
            )
        } else {
            None
        };
        let agent_graph = Arc::new(match &local_home {
            Some(_) => crate::storage::MemoryAgentGraph::local(
                state_db
                    .as_ref()
                    .expect("local store state DB was initialized")
                    .clone(),
            ),
            None => crate::storage::MemoryAgentGraph::default(),
        });
        let local_checklist_store = local_home.map(|home| {
            let mut storage_config = LocalThreadStoreConfig::from_config(&config);
            storage_config.codex_home = home.to_path_buf();
            let inner = LocalThreadStore::new(storage_config.clone(), state_db.clone());
            Arc::new(crate::storage::LocalChecklistStore::new(
                storage_config,
                inner,
                agent_graph.clone(),
            ))
        });
        let store: Arc<dyn ThreadStore> = match &local_checklist_store {
            Some(store) => store.clone(),
            None => Arc::new(crate::storage::MemoryThreadStore::default()),
        };
        let auth = crate::auth::manager(&config, api_key).await?;
        let provider = codex_model_provider::create_model_provider(
            config.model_provider.clone(),
            Some(auth.clone()),
        );
        let models = provider.models_manager_without_cache(config.model_catalog.clone());
        models.set_api_key_model_discovery_enabled(
            config.features.enabled(Feature::ApiKeyModelDiscovery),
        );
        let models: SharedModelsManager = if sdk_user_input_enabled == Some(false) {
            Arc::new(ConfiguredModels { inner: models })
        } else {
            models
        };
        let models: SharedModelsManager = if native_agents {
            let catalog = crate::native_agents::validated_catalog(&config, &models).await?;
            Arc::new(codex_models_manager::manager::StaticModelsManager::new(
                Some(auth.clone()),
                ModelsResponse { models: catalog },
            ))
        } else {
            if config.web_search_mode.value()
                != codex_protocol::config_types::WebSearchMode::Disabled
                && !config.features.enabled(Feature::StandaloneWebSearch)
            {
                let model = if checkpoint {
                    initial_model.clone()
                } else {
                    // An explicit destination model wins on SDK local resume;
                    // otherwise use the current authorized store's persisted model.
                    // Portable imports still use their separately validated model.
                    let requested = if config.model.is_some() {
                        config.model.clone()
                    } else if let Some(id) = config::sdk_resume_thread_id(&runtime_json)? {
                        ensure!(
                            local_checklist_store.is_some(),
                            "SDK resume requires current native local storage"
                        );
                        let model = store
                            .read_thread(ReadThreadParams {
                                thread_id: id,
                                include_archived: true,
                                include_history: false,
                            })
                            .await?
                            .model;
                        local_checklist_store
                            .as_ref()
                            .expect("local store checked above")
                            .expect_resume_model(id, model.clone())?;
                        model
                    } else {
                        None
                    };
                    // The same admitted model must reach the matching local
                    // resume's thread config; Core does not restore it from history.
                    config.model = requested;
                    Some(
                        models
                            .get_default_model(
                                &config.model,
                                false,
                                RefreshStrategy::OnlineIfUncached,
                                config.http_client_factory(),
                            )
                            .await,
                    )
                };
                if let Some(model) = model {
                    let info = models
                        .get_model_info(&model, &config.to_models_manager_config())
                        .await;
                    Self::validate_hosted_search(&config, &info)?;
                }
            }
            models
        };
        let deferred_environment = config::deferred_environment(&runtime_json)?.is_some();
        let (environment, local_environment_error) = match config::environment(&runtime_json)? {
            Some(environment) => (environment.connect(&config).await?, None),
            None if deferred_environment => (
                EnvironmentManager::without_environments(config.http_client_factory()),
                None,
            ),
            None => crate::environment::for_unattached_threads(&config, /*sdk_local*/ false).await?,
        };
        let environment = Arc::new(environment);
        let installation_id = ThreadId::new().to_string();
        let user_instructions = Arc::new(options::EmptyUserInstructions);
        let extension_events = Arc::new(extensions::Events::new());
        let code_mode = config::code_mode_provider(&runtime_json, &config)?.or_else(|| {
            // Mirror ThreadManager's pinned default if no package helper was
            // selected, so every enabled native Code Mode session uses the same
            // owning delegate. Disabled Code Mode still uses Core's default.
            config::code_mode_host_enabled(&config).then(|| {
                Arc::new(codex_code_mode::ProcessOwnedCodeModeSessionProvider::default())
                    as Arc<dyn codex_code_mode::CodeModeSessionProvider>
            })
        });
        let interception = Arc::new(crate::native_interception::Boundary::new(
            agent_graph.clone(),
        ));
        let goals = if goals_enabled {
            Some(goals::GoalBackend {
                state_db: state_db.expect("enabled goal store was initialized"),
                service: Arc::new(codex_goal_extension::GoalService::new()),
            })
        } else {
            None
        };
        let manager = Arc::new_cyclic(|weak| {
            let extensions = extensions::build(
                &config,
                auth.clone(),
                goals.as_ref(),
                local_checklist_store.is_some(),
                weak.clone(),
                environment.clone(),
                extension_events.clone(),
                interception.clone(),
            );
            let manager = ThreadManager::new(
                &config,
                auth.clone(),
                models,
                CodexAppsToolsCache::new_without_cache(),
                SessionSource::Exec,
                environment,
                extensions,
                user_instructions,
                None,
                passthrough_image_store(),
                store.clone(),
                Some(agent_graph.clone()),
                installation_id,
                None,
                None,
            );
            match code_mode {
                Some(provider) => manager.with_code_mode_session_provider(Arc::new(
                    crate::native_interception::Provider {
                        inner: provider,
                        boundary: interception.clone(),
                    },
                )),
                None => manager,
            }
        });
        Ok(Self {
            manager,
            context_gate: Arc::new(AsyncRwLock::new(())),
            context_retired: AtomicBool::new(false),
            store,
            local_checklist_store,
            agent_graph,
            goals,
            deferred_environment,
            local_environment_error,
            native_turn_control,
            sdk_user_input_enabled,
            attached_environment: RwLock::new(None),
            environment_connection: Arc::new(Mutex::new(())),
            config_json,
            runtime_json,
            closed: AtomicBool::new(false),
            native_threads: std::sync::Mutex::new(Vec::new()),
            lifecycle: Mutex::new(()),
            event_owners: Mutex::new(HashMap::new()),
            managed_thread_ids: Mutex::new(HashSet::new()),
            startup_warnings: config.startup_warnings.clone(),
            extension_events,
            cloud_config,
            service_config: config,
            initial_config_available: AtomicBool::new(
                sdk_user_input_enabled.is_some() && !added_telemetry_warning,
            ),
            interception,
            restored_creation_times: Mutex::new(HashMap::new()),
        })
    }

    pub async fn start(
        self: &Arc<Self>,
        options: options::ThreadOptions,
        history: Option<InitialHistory>,
    ) -> anyhow::Result<NativeThread> {
        self.start_with_tools(options, history, None, false).await
    }

    pub async fn start_with_tools(
        self: &Arc<Self>,
        mut options: options::ThreadOptions,
        history: Option<InitialHistory>,
        python_specs: Option<Vec<codex_protocol::dynamic_tools::DynamicToolSpec>>,
        use_initial_config: bool,
    ) -> anyhow::Result<NativeThread> {
        let _guard = self.lifecycle.lock().await;
        // Only the private, immediate first open may share the Engine's snapshot.
        // Any start attempt retires it before validation or asynchronous work.
        let first_start = self.initial_config_available.swap(false, Ordering::AcqRel);
        let initial_config = (use_initial_config
            && first_start
            && history.is_none()
            && python_specs.is_some()
            && options.permits_initial_config())
        .then_some(&self.service_config);
        let manual_slots = std::mem::take(&mut options.manual_tool_slots);
        let resume = options.sdk_resume_thread_id;
        let sdk_history = options.take_sdk_history(history.is_some(), python_specs.is_some())?;
        let (mut startup, instructions) = self.build_startup(options, initial_config).await?;
        if let Some(id) = resume {
            ensure!(
                self.store.as_any().is::<LocalThreadStore>()
                    && python_specs.is_some()
                    && history.is_none()
                    && matches!(startup.initial_history, InitialHistory::New)
                    && startup.reserved_thread_id.is_none(),
                "SDK resume requires local storage, current replaceable tools, and no other history or identity"
            );
            startup.initial_history = self.history(&id.to_string()).await?;
            ensure!(
                startup
                    .initial_history
                    .get_dynamic_tools()
                    .unwrap_or_default()
                    .is_empty(),
                "SDK resume cannot authorize saved startup tools; use an SDK-created thread"
            );
            if startup.config.model.is_none()
                && config::sdk_resume_thread_id(&self.runtime_json)? == Some(id)
            {
                startup.config.model = self.service_config.model.clone();
            }
        }
        if let Some(history) = history {
            startup.initial_history = history;
        }
        if let Some(specs) = python_specs {
            ensure!(
                (resume.is_some() || matches!(startup.initial_history, InitialHistory::New))
                    && startup.reserved_thread_id.is_none(),
                "start replaceable tools on a new thread; use a portable checkpoint to restore"
            );
            crate::python_tools::PythonTools::install_manual(&mut startup, specs, &manual_slots)?;
        }
        if let Some(items) = sdk_history {
            startup.initial_history = InitialHistory::Forked(
                items
                    .into_iter()
                    .map(|item| RolloutItem::ResponseItem(item.into()))
                    .collect(),
            );
        }
        self.start_prepared(startup, instructions).await
    }

    // The caller holds lifecycle through validation, native import and start.
    pub(crate) async fn start_prepared(
        self: &Arc<Self>,
        startup: StartThreadOptions,
        instructions: Arc<options::ThreadInstructions>,
    ) -> anyhow::Result<NativeThread> {
        let id = match &startup.initial_history {
            InitialHistory::Resumed(history) => {
                crate::checkpoint::validate_imported_history(&history.history)?;
                Some(history.conversation_id)
            }
            InitialHistory::Forked(history) => {
                crate::checkpoint::validate_imported_history(history)?;
                startup.reserved_thread_id
            }
            _ => startup.reserved_thread_id,
        };
        if let Some(id) = id {
            self.ensure_no_live_owner(id).await?;
            ensure!(
                !self.managed_thread_ids.lock().await.contains(&id),
                "Bare resume of a managed thread is unsupported; restore a portable checkpoint in a new engine"
            );
        }
        let extension_events = self.extension_events.subscribe();
        let new_thread = self.manager.start_thread(startup).await?;
        let native = self
            .wrap(new_thread, instructions, extension_events)
            .await?;
        Ok(native)
    }

    // The caller holds lifecycle. Ephemeral threads have a manager entry without a store record.
    pub(crate) async fn ensure_no_live_owner(&self, id: ThreadId) -> anyhow::Result<()> {
        let owned = self.event_owners.lock().await.contains_key(&id);
        ensure!(
            !owned && self.manager.get_thread(id).await.is_err(),
            "This thread already has a live owner; reuse its Session"
        );
        Ok(())
    }

    // The caller holds lifecycle. Imported IDs prove store ownership only;
    // the optional native owner is the exact root this attempt started.
    pub(crate) async fn rollback_checkpoint_import(
        &self,
        imported: &[ThreadId],
        owner: Option<&ThreadState>,
    ) -> anyhow::Result<()> {
        if let Some(owner) = owner {
            owner.close().await?;
        }
        if imported.is_empty() {
            return Ok(());
        }
        for id in imported {
            self.interception.clear_code_state(*id);
            if let Some(goals) = &self.goals {
                goals.state_db.delete_threads_strict(&[*id]).await?;
            }
            self.store
                .delete_thread(DeleteThreadParams { thread_id: *id })
                .await?;
        }
        self.agent_graph.remove_checkpoint_edges(imported).await;
        {
            let mut creation_times = self.restored_creation_times.lock().await;
            for id in imported {
                creation_times.remove(id);
            }
        }
        {
            let mut managed = self.managed_thread_ids.lock().await;
            for id in imported {
                managed.remove(id);
            }
        }
        Ok(())
    }

    async fn wrap(
        self: &Arc<Self>,
        new: NewThread,
        instructions: Arc<options::ThreadInstructions>,
        extension_events: broadcast::Receiver<(String, Event)>,
    ) -> anyhow::Result<NativeThread> {
        // Claiming an existing root in claim_subtree is idempotent only for
        // that owner's reader; it must never admit a second wrapper.
        ensure!(
            !self.event_owners.lock().await.contains_key(&new.thread_id),
            "This thread already has a live owner; reuse its Session"
        );
        if new
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>()
            .is_some()
        {
            self.managed_thread_ids.lock().await.insert(new.thread_id);
        }
        // Subscribe before inspecting descendants so a concurrent spawn cannot be missed.
        let created = self.manager.subscribe_thread_created();
        let state = Arc::new(ThreadState {
            id: new.thread_id,
            thread: new.thread.clone(),
            engine: self.clone(),
            instructions,
            events: Mutex::new(ThreadEvents {
                initial: Some(Event {
                    id: String::new(),
                    msg: EventMsg::SessionConfigured(new.session_configured),
                }),
                extensions: extension_events,
                created,
                graph: self.agent_graph.subscribe(new.thread_id).await,
                native: Some(next_native_event(new.thread)),
                session_terminal: None,
                children: VecDeque::new(),
                pending: VecDeque::new(),
                next_instance: 0,
            }),
            child_instances: RwLock::new(HashMap::new()),
            child_questions: Mutex::new(HashMap::new()),
            child_question_gate: Mutex::new(()),
            closed: AtomicBool::new(false),
            closing: watch::channel(false).0,
            close_lock: Mutex::new(()),
            mutations: Arc::new(Mutex::new(())),
        });
        if let Err(error) = state
            .discover_children(&mut *state.events.lock().await)
            .await
        {
            let _ = state.thread.shutdown_and_wait().await;
            self.manager
                .remove_thread_if_matches(&state.id, &state.thread)
                .await;
            return Err(error);
        }
        Ok(NativeThread::new(state))
    }

    pub(crate) async fn rebuild_local(
        self: &Arc<Self>,
        state: &Arc<ThreadState>,
        options: options::ThreadOptions,
        specs: Vec<codex_protocol::dynamic_tools::DynamicToolSpec>,
        expected: Vec<codex_history::RolloutItem>,
        prepared: Vec<codex_history::RolloutItem>,
        receipt: impl Into<crate::storage::Rebuild>,
    ) -> anyhow::Result<NativeThread> {
        let _lifecycle = self.lifecycle.lock().await;
        let store = self
            .local_checklist_store
            .as_ref()
            .context("local recovery requires native local storage")?;
        let (mut startup, instructions) = self.build_startup(options, None).await?;
        let receipt = receipt.into();
        // Compaction holds the source context lease through this handoff.
        // Core owns the restorable data, including environment selections and
        // profile roots absent from the public snapshot. Cold recovery retains
        // its original current-startup-settings behavior.
        let settings = if matches!(&receipt, crate::storage::Rebuild::Compaction) {
            let restorable = state.thread.restorable_thread_settings().await;
            let environments = restorable
                .environments
                .as_ref()
                .context("local compaction did not capture current environments")?;
            // Required native MCP servers start before final settings restore.
            // Keep the current ordered selections, including an empty selection.
            startup.environments = Some(environments.environments.clone());
            Some((state.thread.thread_settings_snapshot().await, restorable))
        } else {
            None
        };
        startup.initial_history = self.history(&state.id.to_string()).await?;
        crate::python_tools::PythonTools::install(&mut startup, specs)?;
        // Compare and publish under the same retained writer, including the
        // ordinary missing-result append. Preparation alone is not publication.
        store
            .begin_rebuild(state.id, &expected, &prepared, receipt)
            .await?;
        let result = async {
            state.close().await?;
            self.managed_thread_ids.lock().await.remove(&state.id);
            // LocalChecklistStore supplies the freshly flushed native history
            // to Core's ordinary resume while retaining the OS writer lock.
            startup.initial_history = self.history(&state.id.to_string()).await?;
            if let Some((expected_settings, _)) = &settings {
                // Seed Core's fallback cwd from the captured current settings.
                startup.config.cwd = expected_settings.cwd.clone();
            }
            let native = self.start_prepared(startup, instructions).await?;
            if let Some((expected_settings, mut restorable)) = settings {
                let candidate = native.current()?;
                let restored = async {
                    // A native tier clear can normalize None to "default".
                    // As in portable compaction, don't clear an identical tier.
                    if candidate
                        .thread
                        .thread_settings_snapshot()
                        .await
                        .service_tier
                        == expected_settings.service_tier
                    {
                        restorable.service_tier = None;
                    }
                    // The replacement owns a new one-use turn initializer.
                    restorable.turn_extension_init = None;
                    candidate.thread.restore_thread_settings(restorable).await?;
                    ensure!(
                        candidate.thread.thread_settings_snapshot().await == expected_settings,
                        "local compaction cannot carry current thread settings"
                    );
                    candidate.thread.checkpoint_thread_settings().await?;
                    anyhow::Ok(())
                }
                .await;
                if let Err(error) = restored {
                    // The append is already durable. Do not return a partly
                    // restored owner or leave its writer held after rejection.
                    // Python's existing local commit failure path fences until
                    // close/reopen. The old thread cannot save over this history.
                    candidate.close().await?;
                    return Err(error);
                }
            }
            Ok(native)
        }
        .await;
        if result.is_err() {
            // A hook/config failure can precede close, and a failed close can
            // leave native shutdown running. Never release its writer early.
            store
                .abort_rebuild(state.id, state.closed.load(Ordering::Acquire))
                .await?;
        }
        result
    }

    pub fn capability_roots(
        &self,
    ) -> anyhow::Result<Vec<codex_protocol::capabilities::SelectedCapabilityRoot>> {
        let environment = config::environment(&self.runtime_json)?;
        let deferred = config::deferred_environment(&self.runtime_json)?;
        Ok(environment
            .and_then(|environment| environment.capability_roots())
            .or_else(|| deferred.and_then(|environment| environment.capability_roots()))
            .unwrap_or_default())
    }

    pub async fn read_stored_thread(&self, id: ThreadId) -> Result<StoredThread, ThreadStoreError> {
        let params = ReadThreadParams {
            thread_id: id,
            include_archived: true,
            include_history: false,
        };
        let mut stored = self.store.read_thread(params).await?;
        // Core regenerates imported rollout headers and reindexes their timestamps
        // on cold resume. Keep the checkpoint's original birth time in SDK reads.
        if let Some(created_at) = self.restored_creation_times.lock().await.get(&id) {
            stored.created_at = (*created_at).into();
        }
        Ok(stored)
    }

    pub(crate) async fn load_checkpoint_history(
        &self,
        id: ThreadId,
    ) -> Result<Vec<codex_history::RolloutItem>, ThreadStoreError> {
        if let Some(store) = &self.local_checklist_store {
            store.load_owned_history(id).await
        } else {
            Ok(self
                .store
                .load_history(LoadThreadHistoryParams {
                    thread_id: id,
                    include_archived: true,
                })
                .await?
                .items)
        }
    }

    async fn stored(&self, id_or_path: &str) -> anyhow::Result<StoredThread> {
        Ok(if let Ok(id) = ThreadId::from_string(id_or_path) {
            self.store
                .read_thread(ReadThreadParams {
                    thread_id: id,
                    include_archived: true,
                    include_history: false,
                })
                .await?
        } else {
            self.store
                .read_thread_by_rollout_path(ReadThreadByRolloutPathParams {
                    rollout_path: id_or_path.into(),
                    include_archived: true,
                    include_history: false,
                })
                .await?
        })
    }

    pub async fn history(&self, id_or_path: &str) -> anyhow::Result<InitialHistory> {
        let stored = self.stored(id_or_path).await?;
        let history = self
            .store
            .load_latest_model_context(LoadThreadHistoryParams {
                thread_id: stored.thread_id,
                include_archived: true,
            })
            .await?;
        Ok(InitialHistory::Resumed(ResumedHistory {
            conversation_id: stored.thread_id,
            history: Arc::new(history.items),
            history_revision: history.revision,
            rollout_path: stored.rollout_path,
        }))
    }

    pub async fn fork(
        self: &Arc<Self>,
        id_or_path: &str,
        nth_user_message: Option<usize>,
        boundary: ForkSelection,
        options: options::ThreadOptions,
        python_specs: Option<Vec<codex_protocol::dynamic_tools::DynamicToolSpec>>,
    ) -> anyhow::Result<NativeThread> {
        let _guard = self.lifecycle.lock().await;
        let (mut startup, instructions) = self.build_startup(options, None).await?;
        let mut managed_parent = false;
        let mut source_code = None;
        if let Ok(id) = ThreadId::from_string(id_or_path)
            && let Ok(thread) = self.manager.get_thread(id).await
        {
            if let Some(tools) = thread
                .thread_extension_data()
                .get::<crate::python_tools::PythonTools>()
            {
                ensure!(
                    python_specs.is_some(),
                    "fork requires current managed tools"
                );
                tools.check_idle(&thread).await?;
                source_code = crate::checkpoint::code_state_for_thread(self, id).await?;
                ensure!(
                    source_code.is_none()
                        || (nth_user_message.is_none()
                            && matches!(
                                &boundary,
                                ForkSelection::Latest | ForkSelection::Interrupted
                            )),
                    "code-mode state can only fork at the latest settled boundary"
                );
                // A managed fork checkpoints its own new thread ID. Seed the
                // same current model fields as managed checkpoint restore so
                // the new settings record cannot revert to engine defaults.
                // Do not copy credentials, MCP grants or the source runtime.
                let settings = thread.thread_settings_snapshot().await;
                startup.config.model = Some(settings.model.clone());
                startup.config.model_reasoning_effort = settings.reasoning_effort.clone();
                startup.config.model_reasoning_summary = settings.reasoning_summary;
                startup.config.service_tier = settings.service_tier.clone();
                // Session still holds its checkpoint fence and this engine's
                // lifecycle lock. Inspect the existing source subtree before
                // allocating a new owner, without consuming or retiring cells.
                for child_id in self.manager.list_agent_subtree_thread_ids(id).await? {
                    let child = match self.manager.get_thread(child_id).await {
                        Ok(child) => child,
                        Err(error)
                            if matches!(
                                error.details(),
                                codex_protocol::error::CodexErrorDetails::ThreadNotFound(_)
                            ) =>
                        {
                            // Unloaded stored descendants have no live native VM.
                            continue;
                        }
                        Err(error) => return Err(error.into()),
                    };
                    ensure!(
                        child.list_background_terminals().await.is_empty(),
                        "fork requires settled background terminals throughout the source subtree"
                    );
                    child.export_code_mode_state().await.map_err(|error| anyhow::anyhow!(
                        "fork requires settled native code cells throughout the source subtree: {error}"
                    ))?;
                }
                managed_parent = true;
            }
            thread.ensure_rollout_materialized().await;
            thread.flush_rollout().await?;
        }
        ensure!(
            python_specs.is_none() || managed_parent,
            "managed tools can only fork from a live replaceable session"
        );
        let stored = self.stored(id_or_path).await?;
        let extension_events = self.extension_events.subscribe();
        let (snapshot, history) = if stored.history_mode == ThreadHistoryMode::Paginated {
            ensure!(
                nth_user_message.is_none(),
                "paginated history uses before_turn or through_turn boundaries instead of user-message indexes"
            );
            let boundary = match boundary {
                ForkSelection::Latest | ForkSelection::Interrupted => ForkBoundary::Latest,
                ForkSelection::BeforeTurn(id) => ForkBoundary::BeforeTurn(id),
                ForkSelection::ThroughTurn(id) => ForkBoundary::ThroughTurn(id),
            };
            let prepared = self
                .store
                .prepare_fork(PrepareForkParams {
                    thread_id: stored.thread_id,
                    boundary,
                })
                .await?;
            (
                ForkSnapshot::Interrupted,
                InitialHistory::Resumed(ResumedHistory {
                    conversation_id: prepared.source_thread_id,
                    history: prepared.model_context,
                    history_revision: None,
                    rollout_path: None,
                }),
            )
        } else {
            ensure!(
                matches!(boundary, ForkSelection::Latest | ForkSelection::Interrupted),
                "turn-id fork boundaries require paginated history"
            );
            let snapshot = nth_user_message
                .map(ForkSnapshot::TruncateBeforeNthUserMessage)
                .unwrap_or(ForkSnapshot::Interrupted);
            let history = self.history(id_or_path).await?;
            (snapshot, history)
        };
        if let Some(specs) = python_specs {
            // The native fork will select its own boundary. Install against
            // that source's fixed header, not a fresh startup's empty history.
            startup.initial_history = history.clone();
            crate::python_tools::PythonTools::install(&mut startup, specs)?;
        }
        let new = self
            .manager
            .fork_thread_from_history(snapshot, startup, history)
            .await?;
        let fork_id = new.thread_id;
        if let Some(snapshot) = source_code {
            self.interception.stage_code_state(fork_id, snapshot);
        }
        let mut native = self.wrap(new, instructions, extension_events).await?;
        if managed_parent {
            // This owned ID is imported from fork history, never the source
            // ID. The SDK can discard its temporary writer after export.
            native.checkpoint_import = Some(vec![fork_id]);
        }
        Ok(native)
    }

    pub async fn close(&self) -> anyhow::Result<()> {
        let _guard = self.lifecycle.lock().await;
        self.close_locked().await
    }

    // Only the package worker's lost control channel calls this, after joining
    // all admitted IPC jobs. Explicit stop and raw/nonlocal sessions still close.
    pub async fn close_after_worker_loss(&self) -> anyhow::Result<()> {
        let _guard = self.lifecycle.lock().await;
        let mut failure = None;
        if !self.closed.load(Ordering::Acquire)
            && let Some(store) = &self.local_checklist_store
        {
            let roots: Vec<_> = self
                .event_owners
                .lock()
                .await
                .iter()
                .filter_map(|(id, owner)| (id == owner).then_some(*id))
                .collect();
            for id in roots {
                let Ok(thread) = self.manager.get_thread(id).await else {
                    continue;
                };
                let Some(tools) = thread
                    .thread_extension_data()
                    .get::<crate::python_tools::PythonTools>()
                else {
                    continue;
                };
                match store
                    .begin_loss_suspension(id, tools.pending_on_loss(&id.to_string()))
                    .await
                {
                    Ok(false) => continue,
                    Err(error) => {
                        failure.get_or_insert_with(|| anyhow::Error::from(error));
                        continue;
                    }
                    Ok(true) => {}
                }
                let outcome = thread.suspend_turn_and_shutdown().await;
                let suspended_turn = match &outcome {
                    Ok(SuspendTurnOutcome::Suspended { turn_id }) => Some(turn_id.as_str()),
                    _ => None,
                };
                let suspended = suspended_turn.is_some();
                // Attribution controls only the suffix, not native liveness.
                // A successfully suspended and finalized writer is closed even
                // when it had to retain an ambiguous suffix in full.
                let finalized = store.finish_loss_suspension(id, suspended_turn).await;
                if let Err(error) = outcome {
                    failure.get_or_insert_with(|| anyhow::Error::from(error));
                }
                match finalized {
                    Ok(_) if suspended => {
                        self.manager.remove_thread_if_matches(&id, &thread).await;
                    }
                    Ok(_) => {}
                    Err(error) => {
                        failure.get_or_insert_with(|| anyhow::Error::from(error));
                    }
                }
            }
        }
        // Never let a suspend or writer error skip the remaining native roots,
        // state DB or Engine teardown (including on a later Engine::drop).
        self.close_locked().await?;
        if let Some(error) = failure {
            return Err(error);
        }
        Ok(())
    }

    async fn close_locked(&self) -> anyhow::Result<()> {
        if self.closed.load(Ordering::Acquire) {
            return Ok(());
        }
        let report = self
            .manager
            .shutdown_all_threads_bounded(CLOSE_TIMEOUT)
            .await;
        ensure!(
            report.timed_out.is_empty() && report.submit_failed.is_empty(),
            "Codex shutdown incomplete: {} timed out, {} failed",
            report.timed_out.len(),
            report.submit_failed.len()
        );
        self.interception.check_code_import_cleanup()?;
        if let Some(goals) = &self.goals {
            goals.state_db.close().await;
        } else if let Some(store) = self.store.as_any().downcast_ref::<LocalThreadStore>()
            && let Some(state_db) = store.state_db().await
        {
            state_db.close().await;
        }
        self.closed.store(true, Ordering::Release);
        Ok(())
    }

    pub(crate) fn register_native_thread(
        &self,
        owner: &Arc<std::sync::Mutex<crate::NativeThreadOwner>>,
    ) {
        let mut threads = self.native_threads.lock().expect("native threads poisoned");
        threads.retain(|thread| thread.strong_count() != 0);
        threads.push(Arc::downgrade(owner));
    }

    pub(crate) fn release_native_threads(&self) {
        for thread in self
            .native_threads
            .lock()
            .expect("native threads poisoned")
            .drain(..)
            .filter_map(|thread| thread.upgrade())
        {
            thread
                .lock()
                .expect("native thread owner poisoned")
                .release();
        }
    }

    pub(crate) async fn build_startup(
        &self,
        options: options::ThreadOptions,
        initial_config: Option<&Config>,
    ) -> anyhow::Result<(StartThreadOptions, Arc<options::ThreadInstructions>)> {
        self.initial_config_available
            .store(false, Ordering::Release);
        ensure!(
            !self.closed.load(Ordering::Acquire),
            "Codex engine is closed"
        );
        let (mut startup, instructions) = options
            .build(
                &self.config_json,
                &self.runtime_json,
                self.cloud_config.clone(),
                initial_config,
                &self.manager.environment_manager(),
            )
            .await?;
        self.apply_deferred_environment(&mut startup)?;
        if startup.environments.is_none()
            && startup
                .thread_extension_init
                .get::<crate::canonical_rules::Rules>()
                .is_some()
        {
            ensure!(
                self.manager
                    .environment_manager()
                    .try_local_environment()
                    .is_some(),
                "Canonical rules require a local executor: {}",
                self.local_environment_error
                    .as_deref()
                    .unwrap_or("no local executor is registered"),
            );
            startup.environments = Some(vec![codex_protocol::protocol::TurnEnvironmentSelection {
                environment_id: codex_exec_server::LOCAL_ENVIRONMENT_ID.to_owned(),
                cwd: codex_utils_path_uri::PathUri::from_abs_path(&startup.config.cwd),
                workspace_roots: startup
                    .config
                    .workspace_roots
                    .iter()
                    .map(codex_utils_path_uri::PathUri::from_abs_path)
                    .collect(),
                config: codex_protocol::protocol::EnvironmentConfigState::FromThread,
            }]);
        }
        let config = &startup.config;
        let original = &self.service_config;
        if let Some(enabled) = self.sdk_user_input_enabled {
            ensure!(
                config.experimental_request_user_input_enabled == enabled,
                "SDK user-input policy belongs to the Codex client; create a separate client to change it"
            );
        }
        if self.store.as_any().is::<LocalThreadStore>() {
            ensure!(
                !config.ephemeral,
                "native local SDK storage does not support ephemeral threads"
            );
        }
        // Enforce at each root startup, including raw starts/forks/restores in
        // this manager. A V1 peer could otherwise send_input to the owner by ID.
        if self.native_turn_control {
            Self::check_controlled_config(config)?;
        }
        ensure!(
            config.experimental_thread_store == original.experimental_thread_store
                && config.sqlite_config() == original.sqlite_config()
                && config.model_catalog == original.model_catalog
                && config.model_provider_id == original.model_provider_id
                && config.model_provider == original.model_provider
                && [
                    Feature::ApiKeyModelDiscovery,
                    Feature::LocalThreadStoreCompression,
                    Feature::BackgroundPaginatedRolloutMigration,
                ]
                .into_iter()
                .all(|feature| config.features.enabled(feature)
                    == original.features.enabled(feature)),
            "Storage and model-discovery services belong to the Codex client; create a separate client to change them"
        );
        ensure!(
            config::code_mode_host_enabled(config) == config::code_mode_host_enabled(original),
            "Code Mode host selection belongs to the Codex client; create a separate client to change it"
        );
        Ok((startup, instructions))
    }

    fn check_controlled_config(config: &Config) -> anyhow::Result<()> {
        ensure!(
            config.features.enabled(Feature::MultiAgentV2) || !config.agents_enabled,
            "native turn control requires effective features.multi_agent_v2=true or agents.enabled=false throughout its runtime; native V1 send_input can interrupt the root"
        );
        Ok(())
    }

    fn apply_deferred_environment(&self, startup: &mut StartThreadOptions) -> anyhow::Result<()> {
        if !self.deferred_environment {
            return Ok(());
        }
        ensure!(
            !startup
                .config
                .features
                .enabled(Feature::MultiAgentV2DynamicTools),
            "deferred environments do not support multi_agent_v2_dynamic_tools; child environment provisioning is unavailable"
        );
        if startup.environments.is_none() {
            startup.environments = Some(
                self.attached_environment
                    .read()
                    .map_err(|_| anyhow::anyhow!("environment state unavailable"))?
                    .clone()
                    .into_iter()
                    .collect(),
            );
        }
        Ok(())
    }
}

// SDK unattended mode removes question capabilities before Core registers tools.
#[derive(Debug)]
struct ConfiguredModels {
    inner: SharedModelsManager,
}

impl ModelsManager for ConfiguredModels {
    fn set_api_key_model_discovery_enabled(&self, enabled: bool) {
        self.inner.set_api_key_model_discovery_enabled(enabled);
    }

    fn list_models(
        &self,
        strategy: RefreshStrategy,
        factory: HttpClientFactory,
    ) -> ModelsManagerFuture<'_, Vec<ModelPreset>> {
        self.inner.list_models(strategy, factory)
    }

    fn raw_model_catalog(
        &self,
        strategy: RefreshStrategy,
        factory: HttpClientFactory,
    ) -> ModelsManagerFuture<'_, ModelsResponse> {
        self.inner.raw_model_catalog(strategy, factory)
    }

    fn refresh_after_auth_change(&self, factory: HttpClientFactory) -> ModelsManagerFuture<'_, ()> {
        self.inner.refresh_after_auth_change(factory)
    }

    fn get_remote_models(&self) -> ModelsManagerFuture<'_, Vec<ModelInfo>> {
        self.inner.get_remote_models()
    }

    fn try_get_remote_models(&self) -> Result<Vec<ModelInfo>, tokio::sync::TryLockError> {
        self.inner.try_get_remote_models()
    }

    fn auth_manager(&self) -> Option<&AuthManager> {
        self.inner.auth_manager()
    }

    fn build_available_models(&self, models: Vec<ModelInfo>) -> Vec<ModelPreset> {
        self.inner.build_available_models(models)
    }

    fn list_collaboration_modes(&self) -> Vec<CollaborationModeMask> {
        self.inner.list_collaboration_modes()
    }

    fn try_list_models(&self) -> Result<Vec<ModelPreset>, tokio::sync::TryLockError> {
        self.inner.try_list_models()
    }

    fn get_default_model<'a>(
        &'a self,
        model: &'a Option<String>,
        allow_provider_model_fallback: bool,
        strategy: RefreshStrategy,
        factory: HttpClientFactory,
    ) -> ModelsManagerFuture<'a, String> {
        self.inner
            .get_default_model(model, allow_provider_model_fallback, strategy, factory)
    }

    fn get_model_info<'a>(
        &'a self,
        model: &'a str,
        config: &'a ModelsManagerConfig,
    ) -> ModelsManagerFuture<'a, ModelInfo> {
        Box::pin(async move {
            let mut info = self.inner.get_model_info(model, config).await;
            info.experimental_supported_tools.retain(|tool| {
                !matches!(
                    tool.as_str(),
                    "request_user_input_async" | "send_user_message_async"
                )
            });
            info
        })
    }

    fn refresh_if_new_etag(
        &self,
        etag: String,
        factory: HttpClientFactory,
    ) -> ModelsManagerFuture<'_, ()> {
        self.inner.refresh_if_new_etag(etag, factory)
    }
}

#[derive(Default, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ForkSelection {
    #[default]
    Latest,
    Interrupted,
    BeforeTurn(String),
    ThroughTurn(String),
}

pub struct ThreadState {
    pub id: ThreadId,
    pub thread: Arc<CodexThread>,
    pub engine: Arc<EngineState>,
    pub instructions: Arc<options::ThreadInstructions>,
    events: Mutex<ThreadEvents>,
    pub mutations: Arc<Mutex<()>>,
    closed: AtomicBool,
    close_lock: Mutex<()>,
    closing: watch::Sender<bool>,
    pub(crate) child_instances: RwLock<HashMap<ThreadId, crate::native_agents::Instance>>,
    child_questions: Mutex<HashMap<String, OwnedChildQuestion>>,
    child_question_gate: Mutex<()>,
}

#[derive(Clone)]
struct OwnedChildQuestion {
    child: ThreadId,
    instance: u64,
    grant: codex_core::OwnedQuestion,
}

struct ThreadEvents {
    initial: Option<Event>,
    extensions: broadcast::Receiver<(String, Event)>,
    created: broadcast::Receiver<ThreadId>,
    graph: mpsc::UnboundedReceiver<crate::storage::GraphEvent>,
    // Like a child read, keep an already received native Error across other
    // select branches while its typed reporting receipt is being finalized.
    native: Option<NextEvent>,
    // Retain a dequeued terminal while its admission-ready receipt is pending,
    // including when a Python reader is cancelled before that receipt arrives.
    session_terminal: Option<NextSessionTerminal>,
    children: VecDeque<(ThreadId, ChildReader)>,
    pending: VecDeque<(ChildEventSource, Event)>,
    next_instance: u64,
}

type NextEvent = Pin<Box<dyn Future<Output = codex_protocol::error::Result<Event>> + Send>>;
type NextSessionTerminal = Pin<
    Box<
        dyn Future<Output = anyhow::Result<(Event, bool, Arc<crate::python_tools::TurnAdmission>)>>
            + Send,
    >,
>;
type ChildEventSource = (ThreadId, u64, Option<String>);

struct ChildReader {
    thread: Weak<CodexThread>,
    next: Option<NextEvent>,
    instance: u64,
    incarnation: Option<String>,
}

fn next_native_event(thread: Arc<CodexThread>) -> NextEvent {
    Box::pin(async move {
        let event = thread.next_event().await?;
        Ok(event)
    })
}

async fn read_child(children: &mut VecDeque<(ThreadId, ChildReader)>) -> (ChildEventSource, Event) {
    let (id, result) = poll_fn(|cx| {
        // Put each reader at the back before yielding so a busy child cannot starve siblings.
        for _ in 0..children.len() {
            let (id, mut reader) = children.pop_front().expect("child reader exists");
            let event = reader.next.as_mut().map(|next| next.as_mut().poll(cx));
            children.push_back((id, reader));
            if let Some(Poll::Ready(event)) = event {
                return Poll::Ready((id, event));
            }
        }
        Poll::Pending
    })
    .await;
    let event = result.unwrap_or_else(|_| Event {
        id: String::new(),
        msg: EventMsg::ShutdownComplete,
    });
    let reader = &mut children.back_mut().expect("child reader exists").1;
    reader.next = if matches!(event.msg, EventMsg::ShutdownComplete) {
        None
    } else {
        reader.thread.upgrade().map(next_native_event)
    };
    ((id, reader.instance, reader.incarnation.clone()), event)
}

fn native_event(
    source: Option<(ThreadId, u64)>,
    mut event: Event,
    session_reader: bool,
) -> anyhow::Result<serde_json::Value> {
    // Session observations and stored items use Core's same retention limit.
    // Keep the full native event for callers of the low-level raw reader.
    if session_reader
        && matches!(event.msg, EventMsg::ItemCompleted(_))
        && let Some(std::borrow::Cow::Owned(retained)) =
            codex_rollout::persisted_event_msg(&event.msg, ThreadHistoryMode::Paginated)
    {
        event.msg = retained;
    }
    let mut value = serde_json::to_value(&event)?;
    if let Some((id, instance)) = source {
        value["subagent_id"] = serde_json::json!(id);
        value["subagent_instance_id"] = serde_json::json!(instance);
    }
    if let EventMsg::Error(error) = &event.msg {
        value["error_affects_turn_status"] = serde_json::json!(error.affects_turn_status());
    }
    Ok(value)
}

fn graph_event(event: crate::storage::GraphEvent) -> anyhow::Result<serde_json::Value> {
    let mut message = serde_json::to_value(event)?;
    message["type"] = serde_json::json!("sdk_subagent_lifecycle");
    Ok(serde_json::json!({"id": "", "msg": message}))
}

impl ThreadState {
    async fn claim_subtree(&self) -> anyhow::Result<Vec<ThreadId>> {
        let ids = self
            .engine
            .manager
            .list_agent_subtree_thread_ids(self.id)
            .await?;
        let mut owners = self.engine.event_owners.lock().await;
        ensure!(
            ids.iter()
                .all(|id| owners.get(id).is_none_or(|owner| *owner == self.id)),
            "Overlapping SDK sessions cannot consume the same subagent events"
        );
        for id in &ids {
            owners.insert(*id, self.id);
        }
        Ok(ids)
    }

    async fn discover_children(&self, events: &mut ThreadEvents) -> anyhow::Result<()> {
        for id in self.claim_subtree().await? {
            if id == self.id {
                continue;
            }
            let Ok(thread) = self.engine.manager.get_thread(id).await else {
                continue;
            };
            // An evicted child may be loaded again under the same durable thread ID.
            let previous = events
                .children
                .iter_mut()
                .find(|(child_id, _)| *child_id == id);
            if previous
                .as_ref()
                .is_some_and(|(_, reader)| Weak::ptr_eq(&reader.thread, &Arc::downgrade(&thread)))
            {
                continue;
            }
            events.next_instance += 1;
            let reader = ChildReader {
                next: Some(next_native_event(thread.clone())),
                thread: Arc::downgrade(&thread),
                instance: events.next_instance,
                // Identity belongs to this read owner, including after its
                // durable ID is reloaded or the weak thread can no longer upgrade.
                incarnation: thread
                    .thread_extension_data()
                    .get::<crate::native_interception::Control>()
                    .map(|control| control.incarnation.clone()),
            };
            let _question_gate = self.child_question_gate.lock().await;
            self.child_instances
                .write()
                .map_err(|_| anyhow::anyhow!("Subagent state unavailable"))?
                .insert(
                    id,
                    crate::native_agents::Instance {
                        id: reader.instance,
                        incarnation: reader.incarnation.clone(),
                        thread: Arc::downgrade(&thread),
                        turn: None,
                        usage: None,
                        submitted: HashMap::new(),
                        during_submission: None,
                    },
                )
                .inspect(|previous| previous.retire());
            if let Some((_, previous)) = previous {
                events.pending.push_back((
                    (id, previous.instance, previous.incarnation.clone()),
                    Event {
                        id: String::new(),
                        msg: EventMsg::ShutdownComplete,
                    },
                ));
                *previous = reader;
            } else {
                events.children.push_back((id, reader));
            }
        }
        Ok(())
    }

    async fn child_event(
        self: &Arc<Self>,
        source: ChildEventSource,
        mut event: Event,
        session_reader: bool,
    ) -> anyhow::Result<serde_json::Value> {
        let mut current = false;
        let mut turn_failed = None;
        let mut thread = None;
        {
            let mut instances = self
                .child_instances
                .write()
                .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?;
            if let Some(instance) = instances.get_mut(&source.0)
                && instance.id == source.1
                && !*self.closing.borrow()
            {
                current = true;
                if source.2.is_some() && instance.incarnation == source.2 {
                    thread = instance.thread.upgrade();
                }
                let observed_failure = instance.observe(self, source.0, &event);
                if source.2.is_some() && instance.incarnation == source.2 {
                    turn_failed = observed_failure;
                }
            }
        }
        if !current {
            // An obsolete read source can retire its own callbacks only.
            // Never publish a stale tool event as a newly callable MCP hook.
            event = Event {
                id: String::new(),
                msg: EventMsg::ShutdownComplete,
            };
        }
        let question = if let Some(thread) = thread.as_ref() {
            thread.capture_owned_question(&event).await
        } else {
            None
        };
        let mut event = native_event(Some((source.0, source.1)), event, session_reader)?;
        if let Some(grant) = question {
            let id = ThreadId::new().to_string();
            self.child_questions.lock().await.insert(
                id.clone(),
                OwnedChildQuestion {
                    child: source.0,
                    instance: source.1,
                    grant,
                },
            );
            event["msg"]["_sdk_question_id"] = serde_json::json!(id);
        }
        if let Some(tools) = thread.and_then(|thread| {
            thread
                .thread_extension_data()
                .get::<crate::python_tools::PythonTools>()
        }) && let Some(turn_id) = event["id"].as_str()
            && let Ok(receipt) = tools.admission_ready(turn_id)
        {
            // A newer child turn can replace admission before an older event
            // is read. Missing metadata must never borrow the newer receipt.
            receipt.attach_provider_error(&mut event);
            if turn_failed == Some(true) {
                receipt.attach_policy_failure(&mut event);
            }
        }
        if let Some(incarnation) = source.2 {
            event["subagent_incarnation"] = serde_json::json!(incarnation);
        }
        if let Some(failed) = turn_failed {
            event["subagent_turn_failed"] = serde_json::json!(failed);
        }
        Ok(event)
    }

    pub async fn next_event(self: &Arc<Self>) -> anyhow::Result<serde_json::Value> {
        self.read_event(false).await
    }

    pub async fn next_session_event(self: &Arc<Self>) -> anyhow::Result<serde_json::Value> {
        self.read_event(true).await
    }

    async fn read_event(
        self: &Arc<Self>,
        admission_ready: bool,
    ) -> anyhow::Result<serde_json::Value> {
        let python_tools = self
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>();
        let mut closing = self.closing.subscribe();
        ensure!(!*closing.borrow(), "Codex thread is closed");
        let mut events = self.events.lock().await;
        ensure!(!*closing.borrow(), "Codex thread is closed");
        if let Some(event) = events.initial.take() {
            return native_event(None, event, admission_ready);
        }
        loop {
            if let Some(terminal) = events.session_terminal.as_mut() {
                let result = tokio::select! {
                    _ = closing.changed() => anyhow::bail!("Codex thread is closed"),
                    result = terminal => result,
                };
                events.session_terminal = None;
                let (event, steering_tail, receipt) = result?;
                if let Some(settlement) = self
                    .thread
                    .thread_extension_data()
                    .get::<crate::turn_settlement::Settlement>()
                {
                    settlement.delivered(&event.msg);
                }
                let mut value = native_event(None, event, admission_ready)?;
                receipt.attach_provider_error(&mut value);
                if !value["msg"]["error"].is_null() {
                    receipt.attach_policy_failure(&mut value);
                }
                value["sdk_unsampled_steering"] = serde_json::json!(steering_tail);
                return Ok(value);
            }
            // Seed restored descendants and deliver queued graph changes before child completions.
            if let Ok(event) = events.graph.try_recv() {
                self.discover_children(&mut events).await?;
                return graph_event(event);
            }
            if let Some((source, event)) = events.pending.pop_front() {
                return self.child_event(source, event, admission_ready).await;
            }
            let ThreadEvents {
                extensions,
                created,
                graph,
                children,
                native,
                ..
            } = &mut *events;
            tokio::select! {
                _ = closing.changed() => anyhow::bail!("Codex thread is closed"),
                request = async {
                    python_tools.as_ref().expect("guarded Python channel").next_request().await
                }, if python_tools.is_some() => return Ok(request),
                event = async { native.as_mut().expect("guarded native reader").await }, if native.is_some() => {
                    events.native = Some(next_native_event(self.thread.clone()));
                    let event = event?;
                    if admission_ready && let EventMsg::TurnComplete(done) = &event.msg {
                        ensure!(done.turn_id == event.id, "Native completion turn identity differs");
                        let receipt = python_tools.as_ref()
                            .context("Session event reads require replaceable tools")?
                            .admission_ready(&done.turn_id)?;
                        events.session_terminal = Some(Box::pin(async move {
                            receipt.wait().await?;
                            let steering_tail = receipt.has_steering_tail();
                            Ok((event, steering_tail, receipt))
                        }));
                        continue;
                    }
                    if let Some(settlement) = self.thread.thread_extension_data()
                        .get::<crate::turn_settlement::Settlement>() {
                        settlement.delivered(&event.msg);
                    }
                    return native_event(None, event, admission_ready);
                },
                (source, event) = read_child(children) => return self.child_event(source, event, admission_ready).await,
                Some(event) = graph.recv() => {
                    self.discover_children(&mut events).await?;
                    return graph_event(event);
                },
                notification = created.recv() => {
                    match notification {
                        Ok(_) | Err(broadcast::error::RecvError::Lagged(_)) => {
                            self.discover_children(&mut events).await?;
                        }
                        Err(broadcast::error::RecvError::Closed) => anyhow::bail!("Thread discovery closed"),
                    }
                }
                event = extensions.recv() => {
                    let (target, event) = event?;
                    if target.is_empty() || target == self.id.to_string() {
                        return native_event(None, event, admission_ready);
                    }
                    if let Ok(id) = ThreadId::from_string(&target) {
                        if !events.children.iter().any(|(child_id, _)| *child_id == id) {
                            self.discover_children(&mut events).await?;
                        }
                        if let Some((_, reader)) = events.children.iter().find(|(child_id, _)| *child_id == id) {
                            let source = (id, reader.instance, reader.incarnation.clone());
                            events.pending.push_back((source, event));
                        }
                    }
                }
            }
        }
    }

    pub async fn child_question_pending(&self, id: &str) -> bool {
        let question = self.child_questions.lock().await.get(id).cloned();
        let Some(question) = question else {
            return false;
        };
        let current = self.child_instances.read().ok().is_some_and(|instances| {
            instances
                .get(&question.child)
                .is_some_and(|instance| instance.id == question.instance)
        });
        let pending = !*self.closing.borrow() && current && question.grant.is_pending().await;
        if !pending && question.grant.url_completion_status().await == Some(false) {
            self.child_questions.lock().await.remove(id);
        }
        pending
    }

    pub async fn child_url_completion_status(&self, id: &str) -> Option<bool> {
        let question = self.child_questions.lock().await.get(id).cloned();
        let Some(question) = question else {
            return Some(false);
        };
        let current = self.child_instances.read().ok().is_some_and(|instances| {
            instances
                .get(&question.child)
                .is_some_and(|instance| instance.id == question.instance)
        });
        let status = question.grant.url_completion_status().await;
        let status = if status != Some(true) && (*self.closing.borrow() || !current) {
            Some(false)
        } else {
            status
        };
        if status.is_some() {
            self.child_questions.lock().await.remove(id);
        }
        status
    }

    pub async fn child_url_pending(&self, id: &str) -> bool {
        let question = self.child_questions.lock().await.get(id).cloned();
        let Some(question) = question else {
            return false;
        };
        let current = self.child_instances.read().ok().is_some_and(|instances| {
            instances
                .get(&question.child)
                .is_some_and(|instance| instance.id == question.instance)
        });
        // A pending probe must not consume a terminal receipt queued for Python.
        !*self.closing.borrow() && current && question.grant.is_url_pending().await
    }

    pub async fn cancel_child_url_completion(&self, id: &str) -> bool {
        let _question_gate = self.child_question_gate.lock().await;
        let question = self.child_questions.lock().await.remove(id);
        let Some(question) = question else {
            return false;
        };
        // This retained grant can only revoke its original URL lifecycle.
        question.grant.cancel_url_completion().await
    }

    pub async fn respond_child_question(
        &self,
        id: &str,
        operation: &str,
        params: serde_json::Value,
    ) -> anyhow::Result<bool> {
        let response = crate::ops::question_response(operation, params)?;
        let _question_gate = self.child_question_gate.lock().await;
        if !self.child_question_pending(id).await {
            return Ok(false);
        }
        let Some(question) = self.child_questions.lock().await.get(id).cloned() else {
            return Ok(false);
        };
        let url = question.grant.url_completion_status().await != Some(false);
        // The Core grant checks the captured turn-state identity again at consumption.
        let accepted = question.grant.respond(response).await;
        if !url {
            self.child_questions.lock().await.remove(id);
        }
        Ok(accepted)
    }

    pub async fn respond_to_subagent(
        &self,
        id: ThreadId,
        instance: u64,
        operation: &str,
        params: serde_json::Value,
    ) -> anyhow::Result<serde_json::Value> {
        ensure!(
            matches!(
                operation,
                "exec_approval"
                    | "patch_approval"
                    | "resolve_elicitation"
                    | "user_input_answer"
                    | "request_permissions_response"
                    | "dynamic_tool_response"
            ),
            "Only interaction responses can be sent to a subagent"
        );
        ensure!(!*self.closing.borrow(), "Codex thread is closed");
        ensure!(
            id != self.id && self.engine.event_owners.lock().await.get(&id) == Some(&self.id),
            "Thread is not a subagent of this session"
        );
        let Ok(thread) = self.engine.manager.get_thread(id).await else {
            return Ok(serde_json::Value::Bool(false));
        };
        {
            let instances = self
                .child_instances
                .read()
                .map_err(|_| anyhow::anyhow!("Subagent state unavailable"))?;
            if !instances.get(&id).is_some_and(|current| {
                current.id == instance && Weak::ptr_eq(&current.thread, &Arc::downgrade(&thread))
            }) {
                return Ok(serde_json::Value::Bool(false));
            }
        }
        match crate::ops::call(&thread, operation, params).await {
            Err(error)
                if error
                    .downcast_ref::<codex_protocol::error::CodexErr>()
                    .is_some_and(|error| {
                        matches!(
                            error.details(),
                            codex_protocol::error::CodexErrorDetails::InternalAgentDied
                        )
                    }) =>
            {
                // The child can close between the identity check and queueing its response.
                Ok(serde_json::Value::Bool(false))
            }
            result => result,
        }
    }

    fn retire_child_turns(&self) -> anyhow::Result<()> {
        for instance in self
            .child_instances
            .read()
            .map_err(|_| anyhow::anyhow!("Native child state unavailable"))?
            .values()
        {
            instance.retire();
        }
        Ok(())
    }

    pub(crate) fn finish_child_event_reader(&self) -> anyhow::Result<()> {
        self.closing.send_replace(true);
        if let Some(control) = self
            .thread
            .thread_extension_data()
            .get::<crate::native_interception::Control>()
        {
            control.revoke();
        }
        // child_event checks closing while holding the same instance lock.
        // A late native read therefore cannot publish a new unresolved turn.
        self.retire_child_turns()
    }

    // Called with mutations held. Set only after native shutdown or sealed
    // suspension was joined; revocation/closing alone is not a cleanup proof.
    pub(crate) fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    pub async fn close(&self) -> anyhow::Result<()> {
        let _guard = self.close_lock.lock().await;
        if let Some(settlement) = self
            .thread
            .thread_extension_data()
            .get::<crate::turn_settlement::Settlement>()
        {
            settlement.retire();
        }
        // Retire admission before waiting for an in-flight submit's mutation lock.
        if let Some(control) = self
            .thread
            .thread_extension_data()
            .get::<crate::native_interception::Control>()
        {
            control.revoke();
        }
        if let Some(control) = self
            .thread
            .thread_extension_data()
            .get::<crate::mcp_result_control::McpResultControl>()
        {
            control.revoke();
        }
        if let Some(tools) = self
            .thread
            .thread_extension_data()
            .get::<crate::python_tools::PythonTools>()
        {
            tools.retire_steps();
        }
        let _mutation = self.mutations.lock().await;
        if self.closed.load(Ordering::Acquire) {
            return Ok(());
        }
        self.closing.send_replace(true);
        {
            let mut events = self.events.lock().await;
            self.retire_child_turns()?;
            events.native = None;
            events.session_terminal = None;
            events.children.clear();
            events.graph.close();
        }
        tokio::time::timeout(CLOSE_TIMEOUT, async {
            self.thread.shutdown_and_wait().await?;
            // Shut down parents before rediscovering children created during shutdown.
            let mut stopped = HashMap::new();
            loop {
                let mut found = false;
                for id in self.claim_subtree().await? {
                    if id == self.id {
                        continue;
                    }
                    if let Ok(thread) = self.engine.manager.get_thread(id).await {
                        if stopped
                            .get(&id)
                            .is_some_and(|previous| Arc::ptr_eq(previous, &thread))
                        {
                            continue;
                        }
                        found = true;
                        thread.shutdown_and_wait().await?;
                        stopped.insert(id, thread);
                    }
                }
                if !found {
                    break;
                }
            }
            // Retain ancestors until every descendant has stopped, even without a graph store.
            for (id, thread) in stopped {
                self.engine
                    .manager
                    .remove_thread_if_matches(&id, &thread)
                    .await;
            }
            anyhow::Ok(())
        })
        .await
        .context("Codex thread shutdown timed out")??;
        self.engine
            .manager
            .remove_thread_if_matches(&self.id, &self.thread)
            .await;
        self.engine
            .event_owners
            .lock()
            .await
            .retain(|_, owner| *owner != self.id);
        self.child_questions.lock().await.clear();
        self.closed.store(true, Ordering::Release);
        Ok(())
    }

    // Called only after Core has joined and the retained native writer has
    // finalized its suspension. Ordinary close must not send a second shutdown.
    pub(crate) async fn retire_suspended(&self) {
        self.engine
            .manager
            .remove_thread_if_matches(&self.id, &self.thread)
            .await;
        self.engine
            .event_owners
            .lock()
            .await
            .retain(|_, owner| *owner != self.id);
        self.closing.send_replace(true);
        self.child_questions.lock().await.clear();
        self.closed.store(true, Ordering::Release);
    }
}

#[cfg(test)]
#[path = "turn_settlement_tests.rs"]
pub(crate) mod settlement_tests;

#[cfg(test)]
mod rebuild_consumer_tests {
    use super::*;
    use codex_history::RolloutItem;
    use codex_protocol::dynamic_tools::{DynamicToolFunctionSpec, DynamicToolSpec};
    use codex_protocol::models::{
        ContentItem, FunctionCallOutputPayload, InternalChatMessageMetadataPassthrough,
        ResponseItem,
    };
    use serde_json::json;

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn verified_status_survives_engine_rebuild_and_core_reconstruction() {
        let directory = tempfile::TempDir::new().expect("isolated native home");
        let home = directory.path().join("home");
        let work = directory.path().join("work");
        let storage = directory.path().join("storage");
        for path in [&home, &work, &storage] {
            std::fs::create_dir(path).expect("create test-owned path");
        }
        // No inference, MCP, executor or catalog requests. This is the
        // supported model-only local-store constructor with no ambient auth.
        let config = json!({
            "model": "receipt-fixture",
            "model_provider": "fixture",
            "model_providers.fixture": {
                "name": "Receipt fixture",
                "base_url": "http://127.0.0.1:9/v1",
                "wire_api": "responses",
                "requires_openai_auth": false,
                "supports_websockets": false,
                "request_max_retries": 0,
                "stream_max_retries": 0
            },
            "approval_policy": "never",
            "sandbox_mode": "read-only",
            "web_search": "disabled",
            "agents.enabled": false,
            "features.code_mode": false,
            "features.instant_interrupt": false
        });
        let runtime = json!({
            "codex_home": home,
            "cwd": work,
            "native_thread_store": storage,
            "codex_self_exe": null,
            "ephemeral": false
        });
        let engine = Arc::new(
            EngineState::new(
                config.to_string(),
                runtime.to_string(),
                false,
                Some("fixture-unused-key".into()),
            )
            .await
            .expect("construct actual native engine"),
        );
        // Joining the probe contains assertions/panics. The engine shutdown
        // below is awaited before rethrow, so all actual owners release even
        // if setup, rebuild or snapshot inspection fails.
        let probe = tokio::spawn({
            let engine = engine.clone();
            async move {
                for (call_id, verified) in [("failure-call", Some(false)), ("legacy-call", None)] {
                    let specs = vec![DynamicToolSpec::Function(DynamicToolFunctionSpec {
                        name: "reserve".into(),
                        description: "Record the known reservation outcome".into(),
                        input_schema: json!({
                            "type": "object",
                            "properties": {"quantity": {"type": "integer"}},
                            "required": ["quantity"],
                            "additionalProperties": false
                        }),
                        defer_loading: false,
                    })];
                    let original = engine
                        .start_with_tools(
                            options::ThreadOptions::default(),
                            None,
                            Some(specs.clone()),
                            false,
                        )
                        .await
                        .expect("start managed local root");
                    let original_state = original.current().expect("live original thread");
                    let id = original_state.id;
                    let turn_id = format!("original-{call_id}");
                    let unrelated = ResponseItem::Message {
                        id: None,
                        role: "user".into(),
                        content: vec![ContentItem::InputText {
                            text: "retain this unrelated account note verbatim".into(),
                        }],
                        phase: None,
                        internal_chat_message_metadata_passthrough: None,
                    };
                    let earlier_call = ResponseItem::FunctionCall {
                        id: None,
                        name: "reserve".into(),
                        namespace: None,
                        arguments: r#"{"quantity":3}"#.into(),
                        encrypted_function_args: None,
                        call_id: "earlier-call".into(),
                        internal_chat_message_metadata_passthrough: Some(
                            InternalChatMessageMetadataPassthrough {
                                turn_id: Some("earlier-turn".into()),
                                ..Default::default()
                            },
                        ),
                    };
                    let earlier_output = ResponseItem::FunctionCallOutput {
                        id: None,
                        call_id: Some("earlier-call".into()),
                        name: None,
                        namespace: None,
                        output: FunctionCallOutputPayload::from_text(
                            "earlier receipt: reservation filled".into(),
                        ),
                        internal_chat_message_metadata_passthrough: Some(
                            InternalChatMessageMetadataPassthrough {
                                turn_id: Some("earlier-turn".into()),
                                ..Default::default()
                            },
                        ),
                    };
                    let call = ResponseItem::FunctionCall {
                        id: None,
                        name: "reserve".into(),
                        namespace: None,
                        arguments: r#"{"quantity":7}"#.into(),
                        encrypted_function_args: None,
                        call_id: call_id.into(),
                        internal_chat_message_metadata_passthrough: Some(
                            InternalChatMessageMetadataPassthrough {
                                turn_id: Some(turn_id.clone()),
                                ..Default::default()
                            },
                        ),
                    };
                    // Use Core's public history entry point, which records,
                    // checkpoints and flushes through the real local writer.
                    original_state
                        .thread
                        .inject_response_items(vec![unrelated, earlier_call, earlier_output, call])
                        .await
                        .expect("record pre-rebuild native context");
                    let before = original_state.thread.conversation_history_snapshot().await;
                    // Core assigns IDs and missing metadata at injection. Select
                    // by the fixture's literal semantics, then retain real IDs
                    // and metadata for the post-rebuild identity comparisons.
                    let notes: Vec<_> = before
                        .items()
                        .filter(|item| {
                            matches!(
                                item,
                                ResponseItem::Message { role, content, .. }
                                if role == "user" && matches!(
                                    content.as_slice(),
                                    [ContentItem::InputText { text }]
                                    if text == "retain this unrelated account note verbatim"
                                )
                            )
                        })
                        .collect();
                    assert_eq!(notes.len(), 1, "unique recorded fixture note");
                    let recorded_note = (*notes[0]).clone();
                    assert!(recorded_note.id().is_some_and(|id| !id.as_str().is_empty()));
                    let recorded_calls: Vec<ResponseItem> = [
                        ("earlier-call", "earlier-turn", r#"{"quantity":3}"#),
                        (call_id, turn_id.as_str(), r#"{"quantity":7}"#),
                    ]
                    .into_iter()
                    .map(|(expected_call, expected_turn, expected_arguments)| {
                        let calls: Vec<_> = before
                            .items()
                            .filter(|item| {
                                matches!(
                                    item,
                                    ResponseItem::FunctionCall {
                                        name, namespace, arguments, call_id: actual,
                                        internal_chat_message_metadata_passthrough: Some(meta), ..
                                    }
                                    if name == "reserve" && namespace.is_none()
                                        && arguments == expected_arguments
                                        && actual == expected_call
                                        && meta.turn_id.as_deref() == Some(expected_turn)
                                )
                            })
                            .collect();
                        assert_eq!(calls.len(), 1, "unique recorded call {expected_call}");
                        let recorded = (*calls[0]).clone();
                        assert!(recorded.id().is_some_and(|id| !id.as_str().is_empty()));
                        recorded
                    })
                    .collect();
                    let earlier_results: Vec<_> = before
                        .items()
                        .filter(|item| {
                            matches!(
                                item,
                                ResponseItem::FunctionCallOutput {
                                    call_id: Some(actual), ..
                                } if actual == "earlier-call"
                            )
                        })
                        .collect();
                    assert_eq!(earlier_results.len(), 1, "one preceding completed result");
                    let recorded_earlier_result = (*earlier_results[0]).clone();
                    assert!(
                        recorded_earlier_result
                            .id()
                            .is_some_and(|id| !id.as_str().is_empty())
                    );
                    let ResponseItem::FunctionCallOutput {
                        output,
                        internal_chat_message_metadata_passthrough: Some(meta),
                        ..
                    } = &recorded_earlier_result
                    else {
                        panic!("preceding result lost its native turn");
                    };
                    assert_eq!(meta.turn_id.as_deref(), Some("earlier-turn"));
                    assert_eq!(
                        output.text_content(),
                        Some("earlier receipt: reservation filled")
                    );
                    assert_eq!(output.success, None, "preceding omitted status");
                    let expected = engine
                        .load_checkpoint_history(id)
                        .await
                        .expect("read current writer-owned history");
                    let mut prepared = expected.clone();
                    prepared.push(RolloutItem::ResponseItem(
                        ResponseItem::FunctionCallOutput {
                            id: None,
                            call_id: Some(call_id.into()),
                            name: None,
                            namespace: None,
                            output: FunctionCallOutputPayload::from_text(
                                "external receipt: reservation declined".into(),
                            ),
                            internal_chat_message_metadata_passthrough: Some(
                                InternalChatMessageMetadataPassthrough {
                                    turn_id: Some(turn_id.clone()),
                                    ..Default::default()
                                },
                            ),
                        }
                        .into(),
                    ));
                    let rebuilt = engine
                        .rebuild_local(
                            &original_state,
                            options::ThreadOptions::default(),
                            specs,
                            expected,
                            prepared,
                            crate::storage::RebuildReceipt {
                                turn_id: turn_id.clone(),
                                call_id: call_id.into(),
                                success: verified,
                            },
                        )
                        .await
                        .expect("production SDK local rebuild");
                    let rebuilt_state = rebuilt.current().expect("live rebuilt thread");
                    assert_eq!(
                        rebuilt_state.id, id,
                        "the original root owns reconstruction"
                    );
                    assert!(original_state.closed.load(Ordering::Acquire));
                    let snapshot = rebuilt_state.thread.conversation_history_snapshot().await;
                    for recorded in std::iter::once(&recorded_note).chain(recorded_calls.iter()) {
                        let retained: Vec<_> = snapshot
                            .items()
                            .filter(|item| item.id() == recorded.id())
                            .collect();
                        assert_eq!(retained.len(), 1, "one item at its native recorded ID");
                        assert_eq!(retained[0], recorded, "native identity and body retained");
                    }
                    let earlier_results: Vec<_> = snapshot
                        .items()
                        .filter(|item| {
                            matches!(
                                item,
                                ResponseItem::FunctionCallOutput {
                                    call_id: Some(actual), ..
                                } if actual == "earlier-call"
                            )
                        })
                        .collect();
                    assert_eq!(earlier_results.len(), 1, "preceding output retained once");
                    assert_eq!(
                        earlier_results[0], &recorded_earlier_result,
                        "original native ID, turn, body and unknown status unchanged"
                    );
                    let ResponseItem::FunctionCallOutput { output, .. } = earlier_results[0] else {
                        panic!("preceding result changed type");
                    };
                    assert_eq!(output.success, None, "no blanket receipt status");
                    let outputs: Vec<_> = snapshot
                        .items()
                        .filter_map(|item| match item {
                            ResponseItem::FunctionCallOutput {
                                call_id: Some(actual),
                                output,
                                internal_chat_message_metadata_passthrough: Some(meta),
                                ..
                            } if actual == call_id => Some((output, meta)),
                            _ => None,
                        })
                        .collect();
                    assert_eq!(
                        outputs.len(),
                        1,
                        "one confirmed result, no replay or duplication"
                    );
                    assert_eq!(outputs[0].1.turn_id.as_deref(), Some(turn_id.as_str()));
                    assert_eq!(
                        outputs[0].0.text_content(),
                        Some("external receipt: reservation declined")
                    );
                    assert_eq!(
                        outputs[0].0.success, verified,
                        "inspect real typed Core history"
                    );
                    rebuilt_state.close().await.expect("retire rebuilt owner");
                }
            }
        })
        .await;
        let cleanup = engine.close().await;
        if let Err(error) = probe {
            if error.is_panic() {
                std::panic::resume_unwind(error.into_panic());
            }
            panic!("probe task did not complete: {error}");
        }
        cleanup.expect("all native threads and state DB shut down");
    }
}
