//! Canonical project guidance discovered with each native step's read authority.
use std::collections::{HashMap, HashSet, VecDeque};
use std::io;
use std::sync::{Arc, Mutex, Weak};

use anyhow::{Context, Result, bail, ensure};
use codex_code_mode::CodeModeNestedToolCall;
use codex_config::config_toml::ConfigToml;
use codex_config::{
    ConfigLayerSource, ProjectTrustLookup, ProjectTrustPath, default_project_root_markers,
    merge_toml_values, project_root_markers_from_config,
};
use codex_core::ProjectReadPathMapping;
use codex_core_api::{EnvironmentManager, Feature, ThreadManager};
use codex_extension_api::{
    ContextContributor, ExtensionData, ExtensionFuture, PreviousWorldStateSection,
    RenderedWorldStateFragment, ToolCallOutcome, ToolContributor, ToolFinishInput,
    ToolLifecycleContributor, ToolLifecycleFuture, ToolPolicy, ToolStartInput,
    TurnLifecycleContributor, TurnStartInput, WorldStateContributionInput,
    WorldStateSectionContribution,
};
use codex_file_system::{
    CopyOptions, CreateDirectoryOptions, EnvironmentAccess, EnvironmentAccessKey,
    ExecutorFileSystem, ExecutorFileSystemFuture, FileMetadata, FileSystemEnvironmentAccessor,
    FileSystemReadStream, FileSystemSandboxContext, GetMetadataOptions, ReadDirectoryEntry,
    ReadFileOptions, RemoveOptions, WalkOptions, WalkOutcome, WriteFileOptions,
};
use codex_protocol::models::PermissionProfile;
use codex_protocol::protocol::EnvironmentConfigState;
use codex_tools::{
    FunctionCallError, JsonToolOutput, ResponsesApiTool, ToolCall, ToolCallSource, ToolExecutor,
    ToolExecutorFuture, ToolExposure, ToolName, ToolOutput, ToolPayload, ToolSpec,
};
use codex_utils_path_uri::{PathConvention, PathUri};
use futures_util::StreamExt;
use globset::GlobBuilder;
use serde::Deserialize;
use serde_json::{Value, json};

const SOURCE_LIMIT: usize = 32_768;
const CONTEXT_LIMIT: usize = 3_000;
const MARKERS: (&str, &str) = ("<canonical_rules>", "</canonical_rules>");

#[path = "canonical_access.rs"]
mod access;
pub use access::AccessContributor;

// Installed only from current host thread options, never from saved history.
#[derive(Default)]
pub struct Rules(Mutex<Scope>);

#[derive(Default)]
struct Scope {
    turn: String,
    identity: Option<Arc<AccessContext>>,
    batch: Option<Arc<Batch>>,
    // Only Scope selects a collecting batch. Tools never carry an old batch.
    active: Option<Arc<Batch>>,
    native_view_image: bool,
    images: HashMap<String, PendingImage>,
    // Only the bridge to Core's queued dispatch, not a history of cells or I/O.
    // A retained delegate owns the origin; entries live for one invocation.
    nested_images: HashMap<(String, String), Arc<AccessContext>>,
}

// Native step stores are built with the tool plan, before World State. Fill
// this once during discovery; yielded executors keep their issuing context.
#[derive(Default)]
struct StepAccess(Mutex<Option<Arc<AccessContext>>>);

#[derive(Clone, PartialEq)]
struct AccessContext {
    environment_id: String,
    cwd: PathUri,
    workspace_roots: Vec<PathUri>,
    // No sandbox key is provided for full-disk-read discovery at this pin.
    // Include effective permissions even when both snapshots lack a key.
    permission_profile: PermissionProfile,
    key: Option<EnvironmentAccessKey>,
}

#[derive(Default)]
struct Batch(Mutex<BatchPaths>);

#[derive(Default)]
struct BatchPaths {
    paths: HashSet<PathUri>,
    // A native call cannot be turned into a failed call by an observation.
    // Fail the rule snapshot closed on native overflow instead of dropping paths.
    overflow: bool,
}

/// An actual access owned by this Rules thread store, not a process-global journal.
/// Borrowed call/turn/env identity is validated synchronously; only bounded paths
/// survive in the current collecting batch. Never stores file contents or arguments.
struct AccessObservation<'a> {
    context: &'a Arc<AccessContext>,
    key: Option<EnvironmentAccessKey>,
    owner_turn: &'a str,
    call_id: &'a str,
    environment_id: &'a str,
    cwd: &'a PathUri,
    path: PathUri,
    kind: AccessKind,
}

#[derive(Clone, Copy)]
enum AccessKind {
    ValidatedTextRead,
    NativeImageRead,
    ExecutorRead,
    ExecutorWrite,
}

struct PendingImage {
    turn: String,
    context: Arc<AccessContext>,
    path: PathUri,
    source: ToolCallSource,
}

// Captured by the existing native exec handoff, ONLY if the issuing step
// proves Core owns view_image. A missing origin must never use today's proof.
pub(crate) struct CellImageOrigin {
    rules: Weak<Rules>,
    context: Arc<AccessContext>,
}

impl Rules {
    pub(crate) fn cell_image_origin(self: &Arc<Self>, turn: &str) -> Option<CellImageOrigin> {
        let scope = self.0.lock().expect("canonical rules scope poisoned");
        if scope.turn != turn || !scope.native_view_image {
            return None;
        }
        Some(CellImageOrigin {
            rules: Arc::downgrade(self),
            context: scope.identity.clone()?,
        })
    }
}

pub(crate) struct NestedImage {
    rules: Arc<Rules>,
    context: Arc<AccessContext>,
    turn: String,
    id: (String, String),
}

impl CellImageOrigin {
    pub(crate) fn track(
        &self,
        turn: &str,
        invocation: &CodeModeNestedToolCall,
    ) -> Option<NestedImage> {
        if !invocation.tool_name.is_default_namespace() || invocation.tool_name.name != "view_image"
        {
            return None;
        }
        let rules = self.rules.upgrade()?;
        let id = (
            invocation.cell_id.as_str().to_owned(),
            invocation.runtime_tool_call_id.clone(),
        );
        {
            let mut scope = rules.0.lock().expect("canonical rules scope poisoned");
            // An invalidated cell cannot occupy slots or overflow the new scope.
            if scope.turn != turn
                || !scope
                    .identity
                    .as_ref()
                    .is_some_and(|current| Arc::ptr_eq(current, &self.context))
            {
                return None;
            }
            if scope.nested_images.len() >= 128 {
                scope.native_overflow();
                return None;
            }
            scope.nested_images.insert(id.clone(), self.context.clone());
        }
        Some(NestedImage {
            rules,
            context: self.context.clone(),
            turn: turn.to_owned(),
            id,
        })
    }
}

// Drop covers the queued call, native errors, external cancellation and cell
// shutdown. An old task must not remove an entry belonging to a renewed owner.
impl Drop for NestedImage {
    fn drop(&mut self) {
        let mut scope = self.rules.0.lock().expect("canonical rules scope poisoned");
        if scope.turn == self.turn
            && scope
                .nested_images
                .get(&self.id)
                .is_some_and(|current| Arc::ptr_eq(current, &self.context))
        {
            scope.nested_images.remove(&self.id);
        }
    }
}

impl Scope {
    // Discard every producer's old authority and paths, not just image starts.
    // Only a step whose discovery resolved an accessor may collect NEW I/O.
    // Its tools were already constructed with StepAccess before World State.
    fn renew_context(&mut self, step: &ExtensionData, context: Option<AccessContext>) {
        let identity = context.map(Arc::new);
        *self = Self {
            turn: std::mem::take(&mut self.turn),
            active: identity.as_ref().map(|_| Arc::new(Batch::default())),
            identity,
            ..Self::default()
        };
        *step
            .get_or_init(StepAccess::default)
            .0
            .lock()
            .expect("canonical rules step poisoned") = self.identity.clone();
    }

    fn native_overflow(&mut self) {
        if let Some(batch) = self.active.clone() {
            batch
                .0
                .lock()
                .expect("canonical rules batch poisoned")
                .overflow = true;
            self.batch = Some(batch);
        }
    }

    fn observe(&mut self, access: AccessObservation<'_>) -> Result<()> {
        ensure!(
            self.turn == access.owner_turn,
            "read belongs to an expired turn"
        );
        // A context may have changed away and back. Matching values alone must
        // never reauthorize evidence from an invalidated accessor. Where native
        // discovery exposes its key, also require the exact actual access key.
        // Full-read discovery has no key; the frozen native owner and roots
        // above then delimit the context (not an unrestricted tool accessor).
        if !self.identity.as_ref().is_some_and(|context| {
            Arc::ptr_eq(context, access.context)
                && context.environment_id == access.environment_id
                && &context.cwd == access.cwd
                && context
                    .key
                    .as_ref()
                    .is_none_or(|key| access.key.as_ref() == Some(key))
        }) {
            return Ok(());
        }
        let Some(batch) = &self.active else {
            return Ok(());
        };
        // The call ID is carried by the native caller, never inferred from time or
        // a current thread. Pending native accesses must match the exact finish ID.
        // Do not retain IDs with guidance paths or constrain the native ID format.
        let _ = access.call_id;
        let mut state = batch.0.lock().expect("canonical rules batch poisoned");
        if !state.paths.contains(&access.path) && state.paths.len() >= 64 {
            match access.kind {
                AccessKind::ValidatedTextRead => bail!("read batch exceeds 64 distinct paths"),
                AccessKind::NativeImageRead
                | AccessKind::ExecutorRead
                | AccessKind::ExecutorWrite => state.overflow = true,
            }
        } else {
            state.paths.insert(access.path);
        }
        self.batch = Some(batch.clone());
        Ok(())
    }
}

pub struct CanonicalRules {
    threads: Weak<ThreadManager>,
    environments: Arc<EnvironmentManager>,
}

impl CanonicalRules {
    pub fn new(threads: Weak<ThreadManager>, environments: Arc<EnvironmentManager>) -> Self {
        Self {
            threads,
            environments,
        }
    }

    async fn current(&self, input: &WorldStateContributionInput<'_>) -> Result<String> {
        let Some(rules) = input.thread_store.get::<Rules>() else {
            return Ok(String::new());
        };
        // A previous step's eligibility cannot authorize NEW starts during
        // discovery. Its already-proven native calls may still finish after
        // Code Mode yields. Retain them unless the actual rule context changes.
        {
            let mut scope = rules.0.lock().expect("canonical rules scope poisoned");
            if scope.turn == input.turn_id {
                scope.native_view_image = false;
            }
        }
        let manager = self
            .threads
            .upgrade()
            .context("thread manager unavailable")?;
        let thread = manager.get_thread(input.thread_id).await?;
        let config = thread.config().await;
        // Match native pre-project discovery precedence, including session and managed
        // overrides. A project cannot choose the root from which its own rules are read.
        let mut pre_project = toml::Value::Table(toml::map::Map::new());
        for layer in config.config_layer_stack.layers_low_to_high() {
            if !matches!(layer.name, ConfigLayerSource::Project { .. }) {
                merge_toml_values(&mut pre_project, &layer.config);
            }
        }
        let markers = project_root_markers_from_config(&pre_project)?
            .unwrap_or_else(default_project_root_markers);
        // The read tool uses the first native selection as its cwd as well.
        let selection = input.environments.first().context("no selected executor")?;
        // The primary FromThread selection deliberately hides its normalized
        // config. Use Core's effective native snapshot, which resolves defaults
        // and materializes workspace roots. Never borrow thread permissions for
        // an unresolved owner attachment.
        let snapshot = thread.config_snapshot().await;
        let permission_profile = match &selection.config {
            EnvironmentConfigState::FromThread => snapshot.permission_profile.clone(),
            EnvironmentConfigState::Ready(config) => {
                config.permission_profile.permission_profile().clone()
            }
            EnvironmentConfigState::Pending | EnvironmentConfigState::Failed(_) => {
                bail!("selected executor configuration unavailable")
            }
        };
        let environment = self
            .environments
            .get_environment(&selection.environment_id)
            .context("selected executor unavailable")?;
        let contexts = input
            .executor_capability_discovery
            .map(|d| d.sandbox_contexts());
        let bound;
        let (fs, key) = if let Some(contexts) = contexts.filter(|c| !c.is_empty()) {
            let sandbox = contexts
                .get(&selection.environment_id)
                .context("discovery authority unavailable for selected executor")?;
            bound =
                FileSystemEnvironmentAccessor::new(environment.filesystem_ref(), sandbox.clone());
            let key = bound.cache_key();
            (ReadAccess::Authorized(&bound), Some(key))
        } else {
            // Pinned Core captures None/an empty map only for native full-disk reads.
            // This branch exposes only read operations, never an unrestricted tool accessor.
            (
                ReadAccess::NativeFullRead(environment.filesystem_ref().as_ref()),
                None,
            )
        };
        let identity = AccessContext {
            environment_id: selection.environment_id.clone(),
            cwd: selection.cwd.clone(),
            workspace_roots: selection.workspace_roots.clone(),
            permission_profile,
            key,
        };
        let (paths, origin) = {
            let mut scope = rules.0.lock().expect("canonical rules scope poisoned");
            if scope.identity.as_deref() != Some(&identity) {
                scope.renew_context(input.step_store, Some(identity));
            }
            *input
                .step_store
                .get_or_init(StepAccess::default)
                .0
                .lock()
                .expect("canonical rules step poisoned") = scope.identity.clone();
            // Never carry the issuing batch into a later inference. Keep the
            // last successful batch for display until this new one observes I/O.
            scope.active = Some(Arc::new(Batch::default()));
            let paths: Result<_> = scope.batch.as_ref().map_or_else(
                || Ok(HashSet::new()),
                |batch| {
                    let state = batch.0.lock().expect("canonical rules batch poisoned");
                    ensure!(!state.overflow, "read batch exceeds 64 distinct paths");
                    Ok(state.paths.clone())
                },
            );
            (paths, scope.identity.clone())
        };
        // Trust belongs to this captured cwd, not Config's original active_project.
        // The native trust root handles Git worktrees and is distinct from a rules marker.
        let native_config: ConfigToml = config.config_layer_stack.effective_config().try_into()?;
        let convention = selection
            .cwd
            .infer_path_convention()
            .context("unknown executor path convention")?;
        let mapping = input.thread_store.get::<ProjectReadPathMapping>();
        let discovery_cwd = mapping.as_ref().map_or(&selection.cwd, |mapping| {
            mapping.cwd(&selection.environment_id, &selection.cwd)
        });
        let repo =
            codex_git_utils::resolve_root_git_project_uri_for_trust(&fs, discovery_cwd).await;
        let repo = match repo {
            Some(path) => Some(trust_path(&fs, &path).await),
            None => None,
        };
        let mut cwd = trust_path(&fs, discovery_cwd).await;
        cwd.original = selection.cwd.inferred_native_path_string();
        if discovery_cwd != &selection.cwd {
            // This key came from the admitted local selection, not alias I/O.
            cwd.canonical = Some(discovery_cwd.inferred_native_path_string());
        }
        let selected_root = match selection.workspace_roots.as_slice() {
            [root] => Some(root),
            _ => None,
        };
        // Strict executors can canonicalize the workspace root while successful
        // tool accesses retain the selected cwd's spelling.
        let cwd_alias = selected_root
            .filter(|root| {
                *root != &selection.cwd
                    && cwd
                        .canonical
                        .as_deref()
                        .is_some_and(|canonical| canonical == root.inferred_native_path_string())
            })
            .map(|_| &selection.cwd);
        let lookup = ProjectTrustLookup::from_paths(convention, cwd, repo);
        let project = native_config.get_active_project_for_lookup(&lookup);
        // Rules represents the explicit SDK opt-in. With no matching Core
        // trust entry, discover rules at the selected workspace root using
        // the executor's existing read authority.
        let workspace_root = selected_root.filter(|_| project.is_none());
        if workspace_root.is_none() && !project.is_some_and(|project| project.is_trusted()) {
            let mut scope = rules.0.lock().expect("canonical rules scope poisoned");
            if scope.turn == input.turn_id {
                // Do not revive paths observed under revoked trust on a later reload.
                scope.renew_context(input.step_store, None);
            }
            return Ok(String::new());
        }
        // Core spec_plan adds utility tools before external tools. A ready
        // primary selection above proves its advertisement environment condition,
        // including when StableEnvironmentTools is disabled. ViewImage is a
        // session-static feature at this pin (refresh only updates MCP features).
        // The startup ToolPolicy gates both trusted and external registrations;
        // require_managed_sandbox additionally gates ALL core sources.
        let policy = input.thread_store.get::<ToolPolicy>().unwrap_or_default();
        let mut eligible = config.features.enabled(Feature::ViewImage)
            && policy.allows(&ToolName::new(None, "view_image"));
        if eligible && policy.require_managed_sandbox {
            eligible = matches!(
                snapshot.permission_profile,
                PermissionProfile::Managed { .. }
            ) && input
                .environments
                .iter()
                .all(|selection| match &selection.config {
                    EnvironmentConfigState::FromThread => true,
                    EnvironmentConfigState::Ready(environment) => matches!(
                        environment.permission_profile.permission_profile(),
                        PermissionProfile::Managed { .. }
                    ),
                    EnvironmentConfigState::Pending | EnvironmentConfigState::Failed(_) => false,
                });
        }
        // Unavailable rules are nonfatal to tool construction. Finish late
        // loading here so error invalidation cannot erase this step's proof.
        let body = async {
            let paths = paths?;
            let root = match workspace_root {
                Some(root) => root.clone(),
                None => project_root(&fs, discovery_cwd, &markers).await?,
            };
            let alias = if workspace_root.is_some()
                || (discovery_cwd != &selection.cwd && &root == discovery_cwd)
            {
                cwd_alias
            } else {
                None
            };
            load_rules(&fs, &root, alias, &paths).await
        }
        .await;
        {
            let mut scope = rules.0.lock().expect("canonical rules scope poisoned");
            if scope.turn == input.turn_id
                && scope
                    .identity
                    .as_ref()
                    .zip(origin.as_ref())
                    .is_some_and(|(current, origin)| Arc::ptr_eq(current, origin))
            {
                if body.is_err() {
                    // Invalidate cells and pending calls, even on the same
                    // filesystem. Only this newly proven step can collect.
                    scope.renew_context(input.step_store, origin.as_deref().cloned());
                }
                scope.native_view_image = eligible;
            }
        }
        Ok(body.unwrap_or_else(Self::unavailable))
    }

    fn unavailable(error: anyhow::Error) -> String {
        // Bound even errors containing paths or YAML. Do not copy source bodies.
        let detail: String = format!("{error:#}").chars().take(384).collect();
        format!("Canonical rules unavailable: {detail}")
    }
}

// Core's view_image is not an extension ToolCall and cannot be decorated via
// EnvironmentAccess. At the pin it returns Completed(success) only after
// get_metadata(is_file), sandboxed read_file and image::load_from_memory.
// Capture typed FINALIZED parameters here, then admit on that proven postcondition.
// This postcondition applies ONLY when native registration owns that name in
// the captured step, not when a Python/other external executor fills the slot.
// Blocked, failed/invalid images and MCP tools with a similar name never activate.
impl ToolLifecycleContributor for CanonicalRules {
    fn on_tool_start<'a>(&'a self, input: ToolStartInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            if input.mcp_tool.is_some()
                || !input.tool_name.is_default_namespace()
                || input.tool_name.name != "view_image"
            {
                return;
            }
            let Some(rules) = input.thread_store.get::<Rules>() else {
                return;
            };
            let ToolPayload::Function { arguments } = input.payload else {
                return;
            };
            let Ok(args) = serde_json::from_str::<ImageArguments>(arguments) else {
                return;
            };
            let mut scope = rules.0.lock().expect("canonical rules scope poisoned");
            if scope.turn != input.turn_id || scope.active.is_none() {
                return;
            }
            let context = match &input.source {
                ToolCallSource::Direct => {
                    if !scope.native_view_image {
                        return;
                    }
                    scope.identity.clone()
                }
                ToolCallSource::CodeMode {
                    cell_id,
                    runtime_tool_call_id,
                } => scope
                    .nested_images
                    .get(&(cell_id.clone(), runtime_tool_call_id.clone()))
                    .cloned(),
            };
            let Some(context) = context.filter(|origin| {
                scope
                    .identity
                    .as_ref()
                    .is_some_and(|current| Arc::ptr_eq(current, origin))
            }) else {
                return;
            };
            if scope.images.len() >= 128 {
                scope.native_overflow();
                return;
            }
            // Rules currently use the primary selected executor. Never interpret
            // a different executor's path in that primary filesystem's rules tree.
            if args
                .environment_id
                .as_ref()
                .is_some_and(|selected| selected != &context.environment_id)
            {
                return;
            }
            let mapping = input.thread_store.get::<ProjectReadPathMapping>();
            let resolved = ProjectReadPathMapping::resolve(
                mapping.as_deref(),
                &context.environment_id,
                &context.cwd,
                &args.path,
            );
            let Ok(path) = resolved else {
                return;
            };
            let pending = PendingImage {
                turn: input.turn_id.to_owned(),
                context,
                path,
                source: input.source,
            };
            scope.images.insert(input.call_id.to_owned(), pending);
        })
    }

    fn on_tool_finish<'a>(&'a self, input: ToolFinishInput<'a>) -> ToolLifecycleFuture<'a> {
        Box::pin(async move {
            if !input.tool_name.is_default_namespace() || input.tool_name.name != "view_image" {
                return;
            }
            let Some(rules) = input.thread_store.get::<Rules>() else {
                return;
            };
            let mut scope = rules.0.lock().expect("canonical rules scope poisoned");
            let Some(pending) = scope.images.remove(input.call_id) else {
                return;
            };
            if pending.turn == input.turn_id
                && pending.source == input.source
                && input.outcome == (ToolCallOutcome::Completed { success: true })
            {
                // Presence of this entry proves eligibility at start. Turn and
                // full identity (including discovery authority) changes clear it.
                // Collect only the completed path into this step; never restore
                // the yielded step's old batch and its unrelated stale paths.
                let _ = scope.observe(AccessObservation {
                    context: &pending.context,
                    key: pending.context.key.clone(),
                    owner_turn: input.turn_id,
                    call_id: input.call_id,
                    environment_id: &pending.context.environment_id,
                    cwd: &pending.context.cwd,
                    path: pending.path,
                    kind: AccessKind::NativeImageRead,
                });
            }
        })
    }
}

#[derive(Deserialize)]
struct ImageArguments {
    path: String,
    #[serde(default)]
    environment_id: Option<String>,
}

impl TurnLifecycleContributor for CanonicalRules {
    fn on_turn_start<'a>(&'a self, input: TurnStartInput<'a>) -> ExtensionFuture<'a, ()> {
        Box::pin(async move {
            if let Some(rules) = input.thread_store.get::<Rules>() {
                *rules.0.lock().expect("canonical rules scope poisoned") = Scope {
                    turn: input.turn_id.to_owned(),
                    ..Scope::default()
                };
            }
        })
    }
}

impl ContextContributor for CanonicalRules {
    fn contribute_world_state<'a>(
        &'a self,
        input: WorldStateContributionInput<'a>,
    ) -> ExtensionFuture<'a, Vec<WorldStateSectionContribution>> {
        Box::pin(async move {
            let body = match self.current(&input).await {
                Ok(body) => body,
                Err(error) => {
                    // An error before validated rule loading is a real
                    // invalidation, not an inference rollover.
                    if let Some(rules) = input.thread_store.get::<Rules>() {
                        let mut scope = rules.0.lock().expect("canonical rules scope poisoned");
                        if scope.turn == input.turn_id {
                            // If this step resolved its own discovery authority
                            // before the failure, new calls may still collect.
                            // Rebind them to a fresh epoch; no old accessor,
                            // pending image, or cell may survive an unavailable
                            // snapshot. Early discovery errors have no context.
                            let context = input.step_store.get::<StepAccess>().and_then(|step| {
                                step.0
                                    .lock()
                                    .expect("canonical rules step poisoned")
                                    .as_deref()
                                    .cloned()
                            });
                            scope.renew_context(input.step_store, context);
                        }
                    }
                    Self::unavailable(error)
                }
            };
            vec![WorldStateSectionContribution::new("spike_canonical_rules", move |previous| {
                let snapshot = if body.is_empty() { Value::Null } else { Value::String(body.clone()) };
                let unchanged = match previous {
                    PreviousWorldStateSection::Absent => body.is_empty(),
                    PreviousWorldStateSection::Known(value) => value == &snapshot,
                    PreviousWorldStateSection::Unknown => false,
                };
                let fragment = (!unchanged).then(|| RenderedWorldStateFragment::new(
                    "user", MARKERS,
                    if body.is_empty() {
                        "Canonical rules cleared. No earlier canonical rules apply.".to_owned()
                    } else {
                        format!("Current canonical project guidance; replaces all earlier canonical rules.\n{body}")
                    },
                ));
                (Some(snapshot), fragment)
            })
            // No legacy matcher: marker-like user input cannot create native state.
            .with_retained_fragment_matcher(|role, text| {
                role == "user" && text.trim_start().starts_with(MARKERS.0)
                    && text.trim_end().ends_with(MARKERS.1)
            })]
        })
    }
}

impl ToolContributor for CanonicalRules {
    fn tools(
        &self,
        _: &ExtensionData,
        thread: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        thread
            .get::<Rules>()
            .map(|rules| {
                vec![Arc::new(ReadText {
                    rules,
                    mapping: thread.get::<ProjectReadPathMapping>(),
                    step: Arc::new(StepAccess::default()),
                })
                    as Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>]
            })
            .unwrap_or_default()
    }

    fn tools_for_step(
        &self,
        _: &ExtensionData,
        thread: &ExtensionData,
        step: &ExtensionData,
    ) -> Vec<Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>> {
        thread
            .get::<Rules>()
            .map(|rules| {
                vec![Arc::new(ReadText {
                    rules,
                    mapping: thread.get::<ProjectReadPathMapping>(),
                    step: step.get_or_init(StepAccess::default),
                })
                    as Arc<dyn for<'call> ToolExecutor<ToolCall<'call>>>]
            })
            .unwrap_or_default()
    }
}

struct ReadText {
    rules: Arc<Rules>,
    mapping: Option<Arc<ProjectReadPathMapping>>,
    step: Arc<StepAccess>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadArguments {
    path: String,
}

impl<'call> ToolExecutor<ToolCall<'call>> for ReadText {
    fn tool_name(&self) -> ToolName {
        ToolName::new(None, "read_text_file")
    }
    fn spec(&self) -> ToolSpec {
        ToolSpec::Function(ResponsesApiTool {
            name: "read_text_file".into(),
            description: "Read a regular UTF-8 text file, at most 32768 bytes. Paths are absolute or relative to the environment cwd. Successful reads select applicable canonical project rules.".into(),
            strict: true,
            defer_loading: None,
            parameters: serde_json::from_value(json!({
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": false
            })).expect("static read_text_file schema"),
            output_schema: None,
        })
    }
    fn exposure(&self) -> ToolExposure {
        ToolExposure::DirectModelOnly
    }
    fn supports_parallel_tool_calls(&self) -> bool {
        true
    }
    fn handle<'a>(&'a self, call: ToolCall<'call>) -> ToolExecutorFuture<'a>
    where
        ToolCall<'call>: 'a,
    {
        Box::pin(async move {
            let read = async {
                ensure!(
                    call.source == ToolCallSource::Direct,
                    "read_text_file is only available directly"
                );
                let args: ReadArguments = serde_json::from_str(call.function_arguments()?)?;
                let env = call.environments.first().context("no selected executor")?;
                let path = ProjectReadPathMapping::resolve(
                    self.mapping.as_deref(),
                    &env.environment_id,
                    &env.cwd,
                    &args.path,
                )?;
                let context = self
                    .step
                    .0
                    .lock()
                    .expect("canonical rules step poisoned")
                    .clone();
                let key = env.fs().cache_key();
                let text = read_text(&ReadAccess::Authorized(env.fs()), &path).await?;
                if let Some(context) = context {
                    let mut scope = self.rules.0.lock().expect("canonical rules scope poisoned");
                    scope.observe(AccessObservation {
                        context: &context,
                        key: Some(key),
                        owner_turn: &call.turn_id,
                        call_id: &call.call_id,
                        environment_id: &env.environment_id,
                        cwd: &env.cwd,
                        path,
                        kind: AccessKind::ValidatedTextRead,
                    })?;
                }
                Ok::<_, anyhow::Error>(text)
            }
            .await
            .map_err(|e| FunctionCallError::RespondToModel(format!("read_text_file: {e}")))?;
            Ok(Box::new(JsonToolOutput::new(Value::String(read))) as Box<dyn ToolOutput>)
        })
    }
}

// Full-read discovery follows
// Core's captured sandbox decision; actual Read calls always use ToolEnvironment.fs.
enum ReadAccess<'a> {
    Authorized(&'a dyn EnvironmentAccess),
    NativeFullRead(&'a dyn ExecutorFileSystem),
}

async fn trust_path(fs: &ReadAccess<'_>, path: &PathUri) -> ProjectTrustPath {
    ProjectTrustPath {
        original: path.inferred_native_path_string(),
        canonical: fs
            .canonicalize(path, None)
            .await
            .ok()
            .map(|p| p.inferred_native_path_string()),
    }
}

// The native Git trust helper accepts ExecutorFileSystem, not EnvironmentAccess.
// Bind every read to the captured authority and reject the entire write surface.
// Ignore the helper's sandbox parameter; it cannot override native discovery.
impl ExecutorFileSystem for ReadAccess<'_> {
    fn canonicalize<'a>(
        &'a self,
        path: &'a PathUri,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, PathUri> {
        match self {
            Self::Authorized(fs) => fs.canonicalize(path),
            Self::NativeFullRead(fs) => fs.canonicalize(path, None),
        }
    }
    fn read_file<'a>(
        &'a self,
        path: &'a PathUri,
        options: ReadFileOptions,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, Vec<u8>> {
        match self {
            Self::Authorized(fs) => fs.read_file(path, options),
            Self::NativeFullRead(fs) => fs.read_file(path, options, None),
        }
    }
    fn read_file_stream<'a>(
        &'a self,
        path: &'a PathUri,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, FileSystemReadStream> {
        match self {
            Self::Authorized(fs) => fs.read_file_stream(path),
            Self::NativeFullRead(fs) => fs.read_file_stream(path, None),
        }
    }
    fn get_metadata<'a>(
        &'a self,
        path: &'a PathUri,
        options: GetMetadataOptions,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, FileMetadata> {
        match self {
            Self::Authorized(fs) => fs.get_metadata(path, options),
            Self::NativeFullRead(fs) => fs.get_metadata(path, options, None),
        }
    }
    fn read_directory<'a>(
        &'a self,
        path: &'a PathUri,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, Vec<ReadDirectoryEntry>> {
        match self {
            Self::Authorized(fs) => fs.read_directory(path),
            Self::NativeFullRead(fs) => fs.read_directory(path, None),
        }
    }
    fn walk<'a>(
        &'a self,
        path: &'a PathUri,
        options: WalkOptions,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, WalkOutcome> {
        match self {
            Self::Authorized(fs) => fs.walk(path, options),
            Self::NativeFullRead(fs) => fs.walk(path, options, None),
        }
    }
    fn write_file<'a>(
        &'a self,
        _: &'a PathUri,
        _: Vec<u8>,
        _: WriteFileOptions,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, ()> {
        denied_write()
    }
    fn create_directory<'a>(
        &'a self,
        _: &'a PathUri,
        _: CreateDirectoryOptions,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, ()> {
        denied_write()
    }
    fn remove<'a>(
        &'a self,
        _: &'a PathUri,
        _: RemoveOptions,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, ()> {
        denied_write()
    }
    fn copy<'a>(
        &'a self,
        _: &'a PathUri,
        _: &'a PathUri,
        _: CopyOptions,
        _: Option<&'a FileSystemSandboxContext>,
    ) -> ExecutorFileSystemFuture<'a, ()> {
        denied_write()
    }
}

fn denied_write<'a>() -> ExecutorFileSystemFuture<'a, ()> {
    Box::pin(async {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "canonical discovery is read-only",
        ))
    })
}

impl ReadAccess<'_> {
    fn metadata<'a>(&'a self, path: &'a PathUri) -> ExecutorFileSystemFuture<'a, FileMetadata> {
        let options = GetMetadataOptions::default();
        self.get_metadata(path, options, None)
    }
    fn stream<'a>(
        &'a self,
        path: &'a PathUri,
    ) -> ExecutorFileSystemFuture<'a, FileSystemReadStream> {
        self.read_file_stream(path, None)
    }
}

async fn read_text(fs: &ReadAccess<'_>, path: &PathUri) -> Result<String> {
    let metadata = fs.metadata(path).await?;
    ensure!(
        metadata.is_file && metadata.size <= SOURCE_LIMIT as u64,
        "expected a regular text file of at most 32768 bytes"
    );
    let mut stream = fs.stream(path).await?;
    let mut bytes = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        ensure!(
            bytes.len() + chunk.len() <= SOURCE_LIMIT,
            "source exceeds 32768 bytes"
        );
        ensure!(!chunk.contains(&0), "text contains NUL");
        bytes.extend_from_slice(&chunk);
    }
    Ok(String::from_utf8(bytes).context("text is not UTF-8")?)
}

async fn project_root(fs: &ReadAccess<'_>, cwd: &PathUri, markers: &[String]) -> Result<PathUri> {
    let mut ancestor = Some(cwd.clone());
    while let Some(path) = ancestor {
        for marker in markers {
            let candidate = path.join(marker)?;
            match fs.metadata(&candidate).await {
                Ok(metadata) => {
                    if marker == ".git" && metadata.is_directory {
                        match fs.metadata(&candidate.join("HEAD")?).await {
                            Ok(_) => {}
                            Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
                            Err(e) => return Err(e.into()),
                        }
                    }
                    return Ok(path);
                }
                Err(e) if e.kind() == io::ErrorKind::NotFound => {}
                Err(e) => return Err(e.into()),
            }
        }
        ancestor = path.parent();
    }
    Ok(cwd.clone())
}

async fn load_rules(
    fs: &ReadAccess<'_>,
    root: &PathUri,
    cwd_alias: Option<&PathUri>,
    paths: &HashSet<PathUri>,
) -> Result<String> {
    // SDK-private project metadata, separate from Core's AGENTS.md discovery.
    let directory = root.join(".agents/rules")?;
    let no_follow = GetMetadataOptions {
        follow_symlinks: false,
    };
    match fs.get_metadata(&directory, no_follow, None).await {
        Ok(m) if m.is_symlink => return Ok(String::new()),
        Ok(m) => ensure!(m.is_directory, ".agents/rules is not a directory"),
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(String::new()),
        Err(e) => return Err(e.into()),
    }
    // Native walk omits nonregular entries and file symlinks entirely, and silently
    // stops at max_depth. The guide needs all .md sources validated or an error.
    let mut pending = VecDeque::from([(directory, 0usize)]);
    let mut directories = 1usize;
    let mut entries = 0usize;
    let mut files = Vec::new();
    while let Some((dir, depth)) = pending.pop_front() {
        for entry in fs.read_directory(&dir, None).await? {
            entries += 1;
            ensure!(entries <= 1024, "rules discovery exceeds 1024 entries");
            let path = dir.join(&entry.file_name)?;
            // Default metadata identifies links; no-follow metadata rejects them.
            // Match native walk without reading linked rule contents.
            if fs.metadata(&path).await?.is_symlink {
                continue;
            }
            let meta = fs.get_metadata(&path, no_follow, None).await?;
            if meta.is_directory {
                directories += 1;
                ensure!(
                    directories <= 256,
                    "rules discovery exceeds 256 directories"
                );
                ensure!(depth < 64, "rules discovery depth exceeds 64");
                pending.push_back((path, depth + 1));
            } else if entry.file_name.ends_with(".md") {
                files.push(path);
            }
        }
    }
    files.sort_by_key(ToString::to_string);
    let windows = root.infer_path_convention() == Some(PathConvention::Windows);
    let relative: Vec<_> = paths
        .iter()
        .filter_map(|path| {
            cwd_alias
                .and_then(|alias| path.relative_path_from(alias))
                .or_else(|| path.relative_path_from(root))
        })
        .map(|s| if windows { s.replace('\\', "/") } else { s })
        .collect();
    let mut result = String::new();
    for path in files {
        let label = path
            .relative_path_from(root)
            .context("rule outside project")?;
        let source = read_text(fs, &path)
            .await
            .with_context(|| format!("invalid {label}"))?;
        let (body, applies) =
            applicable(&source, &relative, windows).with_context(|| format!("invalid {label}"))?;
        if applies {
            let labeled = format!("\n{label}:\n{body}\n");
            ensure!(
                result.len() + labeled.len() <= CONTEXT_LIMIT,
                "applicable rules exceed 3000 bytes"
            );
            result.push_str(&labeled);
        }
    }
    Ok(result)
}

fn applicable<'a>(source: &'a str, paths: &[String], windows: bool) -> Result<(&'a str, bool)> {
    let mut lines = source.split_inclusive('\n');
    let first = lines.next().unwrap_or_default();
    if first.trim_end_matches(['\r', '\n']) != "---" {
        return Ok((source, true));
    }
    let start = first.len();
    let mut end = start;
    for line in lines.by_ref() {
        if line.trim_end_matches(['\r', '\n']) == "---" {
            let header = &source[start..end];
            let value: serde_yaml::Value =
                serde_yaml::from_str(header).context("invalid YAML header")?;
            let body = &source[end + line.len()..];
            if value.is_null() && header.trim().is_empty() {
                return Ok((body, true));
            }
            let mapping = value
                .as_mapping()
                .context("frontmatter must be a mapping")?;
            let Some(globs) = mapping.get(serde_yaml::Value::String("paths".into())) else {
                return Ok((body, true));
            };
            let globs = globs
                .as_sequence()
                .context("paths must be a non-empty YAML list")?;
            ensure!(!globs.is_empty(), "paths must be a non-empty YAML list");
            let mut matches = false;
            for glob in globs {
                let pattern = glob.as_str().context("paths entries must be strings")?;
                ensure!(
                    !pattern.is_empty()
                        && pattern.trim() == pattern
                        && !pattern.starts_with(['!', '~', '/'])
                        && !pattern.contains(['\\', ':', '\0'])
                        && !pattern.split('/').any(|c| matches!(c, "" | "." | ".."))
                        && !["?(", "*(", "+(", "@(", "!("]
                            .iter()
                            .any(|s| pattern.contains(s)),
                    "invalid paths glob"
                );
                let matcher = GlobBuilder::new(pattern)
                    .literal_separator(true)
                    .backslash_escape(false)
                    .case_insensitive(windows)
                    .build()
                    .context("invalid paths glob")?
                    .compile_matcher();
                matches |= paths.iter().any(|p| matcher.is_match(p));
            }
            return Ok((body, matches));
        }
        end += line.len();
    }
    bail!("unclosed YAML frontmatter")
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_exec_server::LocalFileSystem;

    #[tokio::test]
    async fn loads_project_rules_for_matching_paths() -> Result<()> {
        let project = tempfile::tempdir()?;
        let rules = project.path().join(".agents/rules");
        std::fs::create_dir_all(&rules)?;
        std::fs::write(
            rules.join("python.md"),
            "---\npaths:\n  - src/**/*.py\n---\nPreserve callback ordering.\n",
        )?;
        let root = PathUri::from_host_native_path(project.path())?;
        let filesystem = LocalFileSystem::unsandboxed();
        let access = ReadAccess::NativeFullRead(&filesystem);

        let matching = HashSet::from([root.join("src/agents/session.py")?]);
        let expected = if cfg!(windows) {
            "\n.agents\\rules\\python.md:\nPreserve callback ordering.\n\n"
        } else {
            "\n.agents/rules/python.md:\nPreserve callback ordering.\n\n"
        };
        assert_eq!(load_rules(&access, &root, None, &matching).await?, expected);

        let nonmatching = HashSet::from([root.join("docs/session.md")?]);
        assert!(
            load_rules(&access, &root, None, &nonmatching)
                .await?
                .is_empty()
        );
        Ok(())
    }
}
