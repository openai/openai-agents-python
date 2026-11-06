//! The standard agent extensions installed by Codex's application host.
use std::sync::{Arc, Weak};

use codex_core_api::{
    AnalyticsEventsClient, AuthManager, Config, EnvironmentManager, Feature, SessionSource,
    ThreadManager,
};
use codex_extension_api::{
    ExtensionEventSink, ExtensionFuture, ExtensionRegistry, ExtensionRegistryBuilder,
    ExtensionWarning, ThreadIdleInput, ThreadLifecycleContributor, ThreadReadyInput,
    ThreadResumeInput, ThreadStartInput, ThreadStopInput,
};
use codex_protocol::protocol::{Event, EventMsg, WarningEvent};
use tokio::sync::broadcast;

/// Extension events use a separate native sink. Subscribe before starting a thread.
/// An empty target denotes a global event; subscribers should retain it.
pub struct Events {
    sender: broadcast::Sender<(String, Event)>,
}

impl Events {
    pub fn new() -> Self {
        Self {
            sender: broadcast::channel(1024).0,
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<(String, Event)> {
        self.sender.subscribe()
    }
}

impl ExtensionEventSink for Events {
    fn emit(&self, event: Event) {
        let target = match &event.msg {
            EventMsg::ThreadGoalUpdated(update) => update.thread_id.to_string(),
            EventMsg::ThreadQueueChanged(update) => update.thread_id.to_string(),
            _ => String::new(),
        };
        // No receivers means no live SDK session. Lag is reported to receivers.
        let _ = self.sender.send((target, event));
    }

    fn emit_warning(&self, warning: ExtensionWarning) {
        let event = Event {
            id: warning.turn_id.unwrap_or_default(),
            msg: EventMsg::Warning(WarningEvent {
                message: warning.message,
            }),
        };
        let _ = self.sender.send((warning.thread_id, event));
    }
}

// Managed Sessions admit each root through their current Python owner. Core's
// idle goal callback queues a new root without that owner. On interruption it
// can also wait for the same session loop that is joining the aborted task.
// Keep all pinned goal startup, restore and teardown state, but do not start
// autonomous goal turns on SDK-managed threads. Unmanaged Core keeps its path.
struct LocalGoalLifecycle(Arc<dyn ThreadLifecycleContributor<Config>>);

impl ThreadLifecycleContributor<Config> for LocalGoalLifecycle {
    fn on_thread_start<'a>(
        &'a self,
        input: ThreadStartInput<'a, Config>,
    ) -> ExtensionFuture<'a, ()> {
        self.0.on_thread_start(input)
    }

    fn on_thread_ready<'a>(
        &'a self,
        input: ThreadReadyInput<'a, Config>,
    ) -> ExtensionFuture<'a, ()> {
        self.0.on_thread_ready(input)
    }

    fn on_thread_resume<'a>(&'a self, input: ThreadResumeInput<'a>) -> ExtensionFuture<'a, ()> {
        self.0.on_thread_resume(input)
    }

    fn on_thread_idle<'a>(&'a self, input: ThreadIdleInput<'a>) -> ExtensionFuture<'a, ()> {
        if input
            .thread_store
            .get::<crate::python_tools::PythonTools>()
            .is_some()
        {
            Box::pin(async {})
        } else {
            self.0.on_thread_idle(input)
        }
    }

    fn on_thread_stop<'a>(&'a self, input: ThreadStopInput<'a>) -> ExtensionFuture<'a, ()> {
        self.0.on_thread_stop(input)
    }
}

pub fn build(
    config: &Config,
    auth: Arc<AuthManager>,
    goals: Option<&crate::goals::GoalBackend>,
    local_store: bool,
    manager: Weak<ThreadManager>,
    environment: Arc<EnvironmentManager>,
    events: Arc<Events>,
    interception: Arc<crate::native_interception::Boundary>,
    managed_calls: Option<Arc<crate::storage::ManagedCalls>>,
) -> Arc<ExtensionRegistry<Config>> {
    let mut builder = ExtensionRegistryBuilder::<Config>::with_event_sink(events);
    let settlement = Arc::new(crate::turn_settlement::Contributor);
    builder.thread_lifecycle_contributor(settlement.clone());
    builder.turn_lifecycle_contributor(settlement);
    builder.prompt_contributor(Arc::new(crate::application_instructions::Contributor));
    let rules = Arc::new(crate::canonical_rules::CanonicalRules::new(
        manager.clone(),
        environment.clone(),
    ));
    builder.prompt_contributor(rules.clone());
    builder.turn_lifecycle_contributor(rules.clone());
    builder.tool_lifecycle_contributor(rules.clone());
    builder.tool_contributor(Arc::new(crate::native_interception::Contributor(rules)));
    builder.thread_lifecycle_contributor(interception.clone());
    builder.turn_lifecycle_contributor(interception.clone());
    builder.tool_lifecycle_contributor(interception.clone());
    let python_tools = Arc::new(crate::python_tools::PythonContributor {
        managed_calls,
        manager: manager.clone(),
        models_config: config.to_models_manager_config(),
        code_mode_enabled: config.features.enabled(Feature::CodeMode),
    });
    builder.prompt_contributor(python_tools.clone());
    builder.tool_contributor(python_tools.clone());
    builder.thread_lifecycle_contributor(python_tools.clone());
    builder.turn_lifecycle_contributor(python_tools.clone());
    builder.turn_lifecycle_contributor(Arc::new(crate::python_tools::GraphFenceContributor {
        manager: manager.clone(),
    }));
    builder.tool_lifecycle_contributor(python_tools);
    let recovery = Arc::new(crate::recovery_guard::RecoveryContributor::default());
    builder.thread_lifecycle_contributor(recovery.clone());
    builder.turn_lifecycle_contributor(recovery.clone());
    builder.tool_lifecycle_contributor(recovery.clone());
    let mcp_results = Arc::new(crate::mcp_result_control::McpResultContributor {
        manager: manager.clone(),
    });
    builder.thread_lifecycle_contributor(mcp_results.clone());
    builder.turn_lifecycle_contributor(mcp_results.clone());
    builder.tool_lifecycle_contributor(mcp_results);
    codex_history_notes_extension::install(&mut builder, auth.clone());
    let mut board_registry = ExtensionRegistryBuilder::with_event_sink(builder.event_sink());
    codex_core::install_agent_message_board(&mut board_registry, manager.clone());
    install_audited_extensions(
        &mut builder,
        board_registry.build(),
        crate::native_interception::DirectOutput::Json,
    );
    if let Some(goals) = goals {
        // Core goal commands require an actual LocalThreadStore. Memory
        // checkpoint engines retain the existing idle bridge without tools.
        // Reuse the same native contributor Arcs for lifecycle and tool state.
        let mut goal_registry = ExtensionRegistryBuilder::with_event_sink(builder.event_sink());
        codex_goal_extension::install_with_backend(
            &mut goal_registry,
            goals.state_db.clone(),
            AnalyticsEventsClient::disabled(),
            codex_otel::global(),
            manager.clone(),
            goals.service.clone(),
            |config: &Config| codex_goal_extension::GoalExtensionConfig {
                enabled: config.features.enabled(Feature::Goals),
                max_goal_token_budget: config.max_goal_token_budget,
            },
        );
        let goal_registry = goal_registry.build();
        for contributor in goal_registry.thread_lifecycle_contributors() {
            if local_store {
                builder.thread_lifecycle_contributor(Arc::new(LocalGoalLifecycle(
                    contributor.clone(),
                )));
            } else {
                builder.thread_lifecycle_contributor(contributor.clone());
            }
        }
        for contributor in goal_registry.config_contributors() {
            builder.config_contributor(contributor.clone());
        }
        for contributor in goal_registry.turn_lifecycle_contributors() {
            builder.turn_lifecycle_contributor(contributor.clone());
        }
        for contributor in goal_registry.token_usage_contributors() {
            builder.token_usage_contributor(contributor.clone());
        }
        for contributor in goal_registry.tool_lifecycle_contributors() {
            builder.tool_lifecycle_contributor(contributor.clone());
        }
        if local_store {
            for contributor in goal_registry.tool_contributors() {
                builder.tool_contributor(contributor.clone());
            }
        }
    }
    codex_git_attribution::install(
        &mut builder,
        auth.clone(),
        config.chatgpt_base_url.clone(),
        config.http_client_factory(),
    );
    codex_guardian_v2::install(&mut builder, auth.clone(), manager);
    codex_mcp_extension::install(&mut builder);
    codex_mcp_extension::install_plugins(&mut builder, environment.clone());
    // Explicit application registrations must follow generic Apps defaults so
    // their validated source wins when both contribute the reserved server.
    builder.mcp_server_contributor(Arc::new(crate::verification::Contributor));
    let mut media_registry = ExtensionRegistryBuilder::with_event_sink(builder.event_sink());
    codex_web_search_extension::install(&mut media_registry, auth.clone());
    install_audited_extensions(
        &mut builder,
        media_registry.build(),
        crate::native_interception::DirectOutput::ResponseBody,
    );
    // Image generation owns both real filesystem access and a typed result body.
    let mut image_registry = ExtensionRegistryBuilder::with_event_sink(builder.event_sink());
    codex_image_generation_extension::install(&mut image_registry, auth, |_: &Config| None);
    install_audited_extensions(
        &mut builder,
        image_registry.build(),
        crate::native_interception::DirectOutput::ResponseBody,
    );
    let skills = codex_skills_extension::SkillProviders::new().with_executor_provider(Arc::new(
        codex_skills_extension::ExecutorSkillProvider::new_with_restriction_product(
            environment,
            SessionSource::Exec.restriction_product(),
        ),
    ));
    let mut skills_registry = ExtensionRegistryBuilder::with_event_sink(builder.event_sink());
    let skills_metrics = codex_otel::global();
    // Skip shadow ranking only when SDK telemetry has no active consumer.
    let shadow_selection_enabled = skills_metrics.is_some()
        || !crate::telemetry::is_initialized()
        || !crate::telemetry::collection_allowed();
    codex_skills_extension::install_with_providers_and_metrics(
        &mut skills_registry,
        skills,
        skills_metrics,
        move |config: &Config| codex_skills_extension::SkillsExtensionConfig {
            include_instructions: config.include_skill_instructions,
            max_context_tokens: config.skill_max_context_tokens,
            bundled_skills_enabled: config.bundled_skills_enabled(),
            cloud_skill_enabled: config.cloud_skill_enabled,
            shadow_selection_enabled: config.features.enabled(Feature::SkillSearch)
                && (shadow_selection_enabled || crate::telemetry::export_requested(config)),
        },
    );
    install_audited_extensions(
        &mut builder,
        skills_registry.build(),
        crate::native_interception::DirectOutput::Json,
    );
    builder.model_request_contributor(Arc::new(crate::python_tools::PreparedRequestContributor));
    builder.model_request_contributor(recovery);
    builder.model_request_contributor(interception.clone());
    builder.model_request_contributor(Arc::new(crate::response_integrity::Contributor(
        interception,
    )));
    // Core wraps streams in registration order. Recovery must see the original
    // item before any application callback, then response/step control records
    // precisely the item admitted by interception. Do not collect a second copy
    // before edits (or publish Completed from a rejected interception stream).
    Arc::new(builder.build())
}

/// Only the audited pinned installers above: board registers thread/tool
/// contributors and notifications register turn lifecycle; skills registers
/// thread/turn/config/context/turn-input/skill/tool contributors; web and image
/// register thread/config/tool contributors. Preserve all
/// their original Arcs and relative registration positions. In particular do
/// not rebuild the whole registry: its public API has no approval-contributor
/// or turn-admission getter, and doing so would silently lose policy.
fn install_audited_extensions(
    builder: &mut ExtensionRegistryBuilder<Config>,
    installed: ExtensionRegistry<Config>,
    output: crate::native_interception::DirectOutput,
) {
    for contributor in installed.thread_lifecycle_contributors() {
        builder.thread_lifecycle_contributor(contributor.clone());
    }
    for contributor in installed.turn_lifecycle_contributors() {
        builder.turn_lifecycle_contributor(contributor.clone());
    }
    for contributor in installed.config_contributors() {
        builder.config_contributor(contributor.clone());
    }
    for contributor in installed.context_contributors() {
        builder.prompt_contributor(contributor.clone());
    }
    for contributor in installed.turn_input_contributors() {
        builder.turn_input_contributor(contributor.clone());
    }
    for contributor in installed.skill_invocation_contributors() {
        builder.skill_invocation_contributor(contributor.clone());
    }
    for contributor in installed.tool_contributors() {
        builder.tool_contributor(Arc::new(crate::native_interception::DirectContributor(
            Arc::new(crate::canonical_rules::AccessContributor(
                contributor.clone(),
            )),
            output,
        )));
    }
}
