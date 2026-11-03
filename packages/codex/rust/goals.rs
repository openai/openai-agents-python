//! Explicitly paused goals backed by the same native service as its tools.

use std::sync::Arc;

use anyhow::{Context, Result, ensure};
use chrono::DateTime;
use codex_agent_graph_store::ThreadSpawnEdgeStatus;
use codex_core::context::UserGoalUpdate;
use codex_core_api::{CodexThread, Config, Feature, StateDbHandle};
use codex_goal_extension::{
    GoalObjectiveUpdate, GoalService, GoalServiceError, GoalSetRequest, GoalTokenBudgetUpdate,
};
use codex_protocol::ThreadId;
use codex_protocol::protocol::{
    AgentStatus, ThreadGoal, ThreadGoalStatus, validate_thread_goal_objective,
};
use serde::{Deserialize, Serialize};

use crate::engine::{EngineState, ThreadState};

/// Constructed only from current application runtime configuration. Keeping the
/// pair together means tools and app methods can never select different stores.
pub struct GoalBackend {
    pub state_db: StateDbHandle,
    pub service: Arc<GoalService>,
}

impl GoalBackend {
    /// Native goals reference a native SQLite thread row. The SDK owns history
    /// in memory; install only the required metadata through the public state
    /// API, never a second rollout or a caller-selected storage location.
    async fn ensure_thread(
        &self,
        engine: &EngineState,
        config: &Config,
        id: ThreadId,
    ) -> Result<()> {
        if self.state_db.get_thread(id).await?.is_some() {
            return Ok(());
        }
        let thread = engine.read_stored_thread(id).await?;
        let mut metadata = codex_state::ThreadMetadataBuilder::new(
            id,
            config
                .codex_home
                .as_path()
                .join(format!("sdk-memory-{id}.jsonl")),
            thread.created_at,
            thread.source,
        );
        metadata.history_mode = thread.history_mode;
        metadata.cwd = thread.cwd;
        metadata.originator = thread.originator;
        metadata.model_provider = Some(thread.model_provider.clone());
        metadata.updated_at = Some(thread.updated_at);
        metadata.recency_at = Some(thread.recency_at);
        metadata.agent_role = thread.agent_role;
        metadata.agent_nickname = thread.agent_nickname;
        metadata.agent_path = thread.agent_path;
        // This path is an SDK-owned identity for SQLite's required non-null
        // column. No reader, writer, or authority uses it as a rollout path.
        self.state_db
            .insert_thread_if_absent(&metadata.build(&thread.model_provider))
            .await?;
        Ok(())
    }
}

/// Portable goal state. Goal identity and model-request authority are never imported.
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GoalSnapshot {
    thread_id: ThreadId,
    objective: String,
    status: ThreadGoalStatus,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    token_budget: Option<i64>,
    #[serde(default)]
    tokens_used: i64,
    #[serde(default)]
    time_used_seconds: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    created_at: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    updated_at: Option<i64>,
}

impl GoalSnapshot {
    pub fn validate(&self, id: ThreadId) -> Result<()> {
        ensure!(
            self.thread_id == id,
            "checkpoint goal thread identity mismatch"
        );
        ensure!(
            matches!(
                self.status,
                ThreadGoalStatus::Paused | ThreadGoalStatus::Complete
            ),
            "checkpoint goals must be paused or complete"
        );
        validate_thread_goal_objective(self.objective.trim()).map_err(anyhow::Error::msg)?;
        ensure!(
            self.token_budget.is_none_or(|budget| budget > 0)
                && self.tokens_used >= 0
                && self.time_used_seconds >= 0
                && self.created_at.is_none_or(|time| time >= 0)
                && self.updated_at.is_none_or(|time| time >= 0),
            "invalid checkpoint goal budget, usage, or timestamps"
        );
        Ok(())
    }

    pub async fn capture(backend: &GoalBackend, id: ThreadId) -> Result<Option<Self>> {
        let goal = backend
            .service
            .get_thread_goal(&backend.state_db, id)
            .await?;
        goal.map(|goal| {
            let snapshot = Self {
                thread_id: goal.thread_id,
                objective: goal.objective,
                status: goal.status,
                token_budget: goal.token_budget,
                tokens_used: goal.tokens_used,
                time_used_seconds: goal.time_used_seconds,
                created_at: Some(goal.created_at),
                updated_at: Some(goal.updated_at),
            };
            snapshot.validate(id)?;
            Ok(snapshot)
        })
        .transpose()
    }

    pub async fn restore(self, engine: &EngineState, config: &Config) -> Result<()> {
        // Validate before importing any records. This second check keeps the
        // method safe for a future internal caller without checkpoint restore.
        self.validate(self.thread_id)?;
        let backend = engine
            .goals
            .as_ref()
            .context("native goal storage is not enabled")?;
        ensure!(
            backend
                .service
                .get_thread_goal(&backend.state_db, self.thread_id)
                .await?
                .is_none(),
            "checkpoint goal already exists in this engine"
        );
        backend
            .ensure_thread(engine, config, self.thread_id)
            .await?;
        let outcome = backend
            .service
            .set_thread_goal(
                &backend.state_db,
                GoalSetRequest {
                    thread_id: self.thread_id,
                    objective: GoalObjectiveUpdate::Set(&self.objective),
                    status: Some(self.status),
                    token_budget: GoalTokenBudgetUpdate::Set(self.token_budget),
                    max_goal_token_budget: config.max_goal_token_budget,
                },
                // Its original user-goal record is already in restored history.
                // Do not issue a duplicate user instruction or submit a turn.
                async { Ok(()) },
            )
            .await?;
        let mut fresh = backend
            .state_db
            .thread_goals()
            .get_thread_goal(self.thread_id)
            .await?
            .context("newly created checkpoint goal is missing")?;
        fresh.tokens_used = self.tokens_used;
        fresh.time_used_seconds = self.time_used_seconds;
        if let Some(created_at) = self.created_at {
            fresh.created_at = DateTime::from_timestamp(created_at, 0)
                .context("checkpoint goal created_at exceeds native timestamp range")?;
        }
        if let Some(updated_at) = self.updated_at {
            fresh.updated_at = DateTime::from_timestamp(updated_at, 0)
                .context("checkpoint goal updated_at exceeds native timestamp range")?;
        }
        // Install saved data in the fresh native identity before a runtime can start.
        // This native write also defers automatic continuation.
        backend
            .state_db
            .thread_goals()
            .replace_thread_goal_snapshot(&fresh)
            .await?;
        outcome.apply_runtime_effects(&backend.service).await;
        Ok(())
    }
}

// The caller holds ThreadState.mutations until the read/write has completed.
// Native submissions (including recovery) hold the same lock through receipt.
async fn checked(
    inner: &ThreadState,
    subagent_id: Option<ThreadId>,
) -> Result<(&GoalBackend, ThreadId)> {
    let backend = inner
        .engine
        .goals
        .as_ref()
        .context("native goal storage is not enabled on this Codex client")?;
    let config = inner.thread.config().await;
    ensure!(
        config.features.enabled(Feature::Goals),
        "native goals require features.goals=true"
    );
    ensure!(
        !config.ephemeral,
        "native goals require a persistent thread"
    );
    // This checks exact subtree ownership and flushes loaded runtimes only.
    // Stored descendants remain readable without acquiring a live runtime.
    crate::subagents::read_history(inner, subagent_id).await?;
    Ok((backend, subagent_id.unwrap_or(inner.id)))
}

enum GoalWriter<'a> {
    Live(Arc<CodexThread>),
    Stored(crate::storage::StoredGoalWriter<'a>),
}

impl GoalWriter<'_> {
    async fn config(&self, inner: &ThreadState) -> Arc<Config> {
        match self {
            Self::Live(thread) => thread.config().await,
            Self::Stored(_) => inner.thread.config().await,
        }
    }

    async fn record(&self, update: UserGoalUpdate) -> Result<(), GoalServiceError> {
        let result: anyhow::Result<()> = match self {
            Self::Live(thread) => thread
                .record_user_goal_update(update)
                .await
                .map_err(Into::into),
            Self::Stored(writer) => writer.append(update).await.map_err(Into::into),
        };
        result.map_err(|err| {
            GoalServiceError::Internal(format!("failed to record goal instruction: {err}"))
        })
    }
}

async fn writer(inner: &ThreadState, id: ThreadId) -> Result<GoalWriter<'_>> {
    match inner.engine.manager.get_thread(id).await {
        Ok(thread)
            if !matches!(
                thread.agent_status().await,
                AgentStatus::Shutdown | AgentStatus::NotFound
            ) && (id == inner.id
                || !inner
                    .engine
                    .agent_graph
                    .edge(id)
                    .await
                    .is_some_and(|edge| edge.status == ThreadSpawnEdgeStatus::Closed)) =>
        {
            return Ok(GoalWriter::Live(thread));
        }
        Ok(_) => {}
        Err(error)
            if matches!(
                error.details(),
                codex_protocol::error::CodexErrorDetails::ThreadNotFound(_)
            ) => {}
        Err(error) => return Err(error.into()),
    }
    ensure!(
        id != inner.id,
        "native goal writes require an open root thread"
    );
    let store = inner
        .engine
        .store
        .as_any()
        .downcast_ref::<crate::storage::MemoryThreadStore>()
        .context("stored goal writes require the SDK-owned thread store")?;
    Ok(GoalWriter::Stored(store.stored_goal_writer(id).await?))
}

pub async fn read(
    inner: &ThreadState,
    subagent_id: Option<ThreadId>,
) -> Result<Option<ThreadGoal>> {
    let (backend, id) = checked(inner, subagent_id).await?;
    Ok(backend
        .service
        .get_thread_goal(&backend.state_db, id)
        .await?)
}

pub async fn read_checklist(
    inner: &ThreadState,
    subagent_id: Option<ThreadId>,
) -> Result<Option<crate::storage::ChecklistState>> {
    ensure!(
        !inner.thread.config_snapshot().await.ephemeral,
        "native checklist requires a persistent thread"
    );
    crate::subagents::read_history(inner, subagent_id).await?;
    let id = subagent_id.unwrap_or(inner.id);
    if let Some(store) = &inner.engine.local_checklist_store {
        Ok(store.checklist(id).await?)
    } else {
        Ok(inner
            .engine
            .store
            .as_any()
            .downcast_ref::<crate::storage::MemoryThreadStore>()
            .context("checklist requires the SDK-owned thread store")?
            .checklist(id)
            .await?)
    }
}

pub async fn set_paused(
    inner: &ThreadState,
    objective: &str,
    subagent_id: Option<ThreadId>,
    token_budget: Option<i64>,
) -> Result<ThreadGoal> {
    let (backend, id) = checked(inner, subagent_id).await?;
    let writer = writer(inner, id).await?;
    let config = writer.config(inner).await;
    backend.ensure_thread(&inner.engine, &config, id).await?;
    let outcome = backend
        .service
        .set_thread_goal(
            &backend.state_db,
            GoalSetRequest {
                thread_id: id,
                objective: GoalObjectiveUpdate::Set(objective),
                status: Some(ThreadGoalStatus::Paused),
                token_budget: token_budget
                    .map(|budget| GoalTokenBudgetUpdate::Set(Some(budget)))
                    .unwrap_or(GoalTokenBudgetUpdate::Keep),
                max_goal_token_budget: config.max_goal_token_budget,
            },
            writer.record(UserGoalUpdate::Set {
                objective: Some(objective.to_owned()),
                status: Some(ThreadGoalStatus::Paused),
            }),
        )
        .await?;
    drop(writer);
    outcome.apply_runtime_effects(&backend.service).await;
    Ok(outcome.goal)
}

pub async fn complete(inner: &ThreadState, subagent_id: Option<ThreadId>) -> Result<ThreadGoal> {
    let (backend, id) = checked(inner, subagent_id).await?;
    let writer = writer(inner, id).await?;
    let config = writer.config(inner).await;
    let existing = backend
        .service
        .get_thread_goal(&backend.state_db, id)
        .await?
        .context("no paused native goal exists")?;
    ensure!(
        existing.status == ThreadGoalStatus::Paused,
        "only an existing paused goal can be completed"
    );
    let outcome = backend
        .service
        .set_thread_goal(
            &backend.state_db,
            GoalSetRequest {
                thread_id: id,
                objective: GoalObjectiveUpdate::Keep,
                status: Some(ThreadGoalStatus::Complete),
                token_budget: GoalTokenBudgetUpdate::Keep,
                max_goal_token_budget: config.max_goal_token_budget,
            },
            writer.record(UserGoalUpdate::Set {
                objective: None,
                status: Some(ThreadGoalStatus::Complete),
            }),
        )
        .await?;
    drop(writer);
    outcome.apply_runtime_effects(&backend.service).await;
    Ok(outcome.goal)
}
