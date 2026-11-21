//! Idle goal storage. No thread manager or goal runtime is started.
use codex_core::{
    config::{ConfigBuilder, ConfigOverrides, ThreadStoreConfig},
    context::{ContextualUserFragment, UserGoalUpdate},
};
use codex_goal_extension::{
    GoalObjectiveUpdate, GoalService, GoalServiceError, GoalSetRequest, GoalTokenBudgetUpdate,
};
use codex_protocol::{
    ThreadId,
    models::BaseInstructions,
    protocol::{SessionSource, ThreadGoalStatus, ThreadHistoryMode, ThreadMemoryMode},
};
use codex_rollout::{RolloutItem, state_db::StateDbHandle};
use codex_thread_store::{
    CreateThreadParams, LiveThread, LocalThreadStore, LocalThreadStoreConfig, PersistContext,
    ResumeThreadParams, ThreadPersistenceMetadata,
};
use pyo3::{
    exceptions::{PyRuntimeError, PyValueError},
    prelude::*,
};
use pyo3_async_runtimes::tokio::future_into_py;
use std::{path::PathBuf, sync::Arc};

const SOURCE: &str = "agents-sdk-goal";
fn storage_error<T>(_: T) -> PyErr {
    PyRuntimeError::new_err("Local goal store is unavailable or busy")
}
#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
struct Lease {
    id: ThreadId,
    live: LiveThread,
    db: StateDbHandle,
    maximum: Option<i64>,
}

#[pyfunction]
pub(super) fn open_goal(
    py: Python<'_>,
    home: PathBuf,
    id: Option<String>,
) -> PyResult<Bound<'_, PyAny>> {
    super::runtime();
    future_into_py(py, async move {
        let config = ConfigBuilder::default()
            .codex_home(home.clone())
            .harness_overrides(ConfigOverrides {
                cwd: Some(home.clone()),
                ..Default::default()
            })
            .build()
            .await
            .map_err(storage_error)?;
        config
            .config_layer_stack
            .ensure_config_server_applied()
            .map_err(storage_error)?;
        let sqlite = config.sqlite_config();
        if !matches!(config.experimental_thread_store, ThreadStoreConfig::Local)
            || sqlite.home() != home
            || config.codex_home.to_path_buf() != home
        {
            return Err(storage_error(()));
        }
        // StateRuntime initializes missing databases. Never mistake a lost
        // store for an empty goal when opening an existing profile.
        if id.is_some() || home.join("sessions").exists() {
            for path in [sqlite.goals_db_path(), sqlite.state_db_path()] {
                let file = path.metadata().map_err(storage_error)?;
                if !file.is_file() || file.len() == 0 {
                    return Err(storage_error(()));
                }
            }
        }
        // Open the stock runtime directly. Rollout's try_init can backfill
        // unrelated sessions before we verify the requested root.
        type StateRuntime = <StateDbHandle as std::ops::Deref>::Target;
        let db = StateRuntime::init(sqlite.clone(), config.model_provider_id.clone())
            .await
            .map_err(storage_error)?;
        let store = Arc::new(LocalThreadStore::new(
            LocalThreadStoreConfig::from_config(&config),
            Some(db.clone()),
        ));
        let metadata = ThreadPersistenceMetadata {
            cwd: Some(home.clone()),
            model_provider: config.model_provider_id.clone(),
            memory_mode: ThreadMemoryMode::Disabled,
        };
        let mode = ThreadHistoryMode::Legacy;
        let (tid, live) = if let Some(id) = id {
            let tid = ThreadId::from_string(&id)
                .map_err(|_| PyValueError::new_err("Invalid thread ID"))?;
            let (live, history) = LiveThread::resume(
                store,
                mode,
                ResumeThreadParams {
                    thread_id: tid,
                    rollout_path: None,
                    history: None,
                    history_revision: None,
                    include_archived: false,
                    metadata,
                },
                None,
            )
            .await
            .map_err(storage_error)?;
            let own = matches!(history.first(), Some(RolloutItem::SessionMeta(m))
                if m.meta.id == tid && ThreadId::from(m.meta.session_id) == tid
                    && m.meta.forked_from_id.is_none() && m.meta.parent_thread_id.is_none()
                    && !m.meta.attached_thread && m.meta.source == SessionSource::Custom(SOURCE.into())
                    && m.meta.originator == SOURCE && m.meta.history_mode == mode && m.meta.cwd == home);
            if !own {
                live.shutdown().await.map_err(storage_error)?;
                return Err(PyValueError::new_err(
                    "Expected a dedicated local goal thread",
                ));
            }
            (tid, live)
        } else {
            let tid = ThreadId::new();
            let live = LiveThread::create(
                store,
                CreateThreadParams {
                    session_id: tid.into(),
                    thread_id: tid,
                    creator_user_id: None,
                    creator_account_id: None,
                    extra_config: None,
                    forked_from_id: None,
                    attached_thread: false,
                    parent_thread_id: None,
                    source: SessionSource::Custom(SOURCE.into()),
                    thread_source: None,
                    originator: SOURCE.into(),
                    base_instructions: BaseInstructions {
                        text: String::new(),
                        provenance: None,
                    },
                    dynamic_tools: vec![],
                    selected_capability_roots: vec![],
                    multi_agent_version: None,
                    history_mode: mode,
                    history_base: None,
                    subagent_history_start_ordinal: None,
                    initial_window_id: tid.to_string(),
                    runtime_workspace_roots: None,
                    metadata,
                },
            )
            .await
            .map_err(storage_error)?;
            if live.persist(PersistContext::Standard).await.is_err() {
                live.discard().await.map_err(storage_error)?;
                return Err(storage_error(()));
            }
            (tid, live)
        };
        Python::attach(|py| {
            Py::new(
                py,
                Lease {
                    id: tid,
                    live,
                    db,
                    maximum: config.max_goal_token_budget,
                },
            )
        })
    })
}

#[pymethods]
impl Lease {
    #[getter]
    fn thread_id(&self) -> String {
        self.id.to_string()
    }

    fn get<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let own = self.clone();
        future_into_py(py, async move {
            let goal = GoalService::new()
                .get_thread_goal(&own.db, own.id)
                .await
                .map_err(storage_error)?;
            serde_json::to_string(&goal).map_err(storage_error)
        })
    }

    fn set_paused<'py>(
        &self,
        py: Python<'py>,
        objective: String,
        budget: Option<i64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let own = self.clone();
        future_into_py(py, async move {
            let svc = GoalService::new();
            let before = svc
                .get_thread_goal(&own.db, own.id)
                .await
                .map_err(storage_error)?;
            if before.as_ref().is_some_and(|g| {
                !matches!(
                    g.status,
                    ThreadGoalStatus::Paused | ThreadGoalStatus::BudgetLimited
                )
            }) {
                return Err(PyValueError::new_err("Goal is not editable"));
            }
            // GoalService validates supplied budgets, but Keep can retain an invalid grant.
            if let (None, Some(goal)) = (budget, before) {
                let valid = match goal.token_budget {
                    Some(n) => n > 0 && own.maximum.is_none_or(|max| n <= max),
                    None => own.maximum.is_none(),
                };
                if !valid {
                    return Err(PyValueError::new_err(
                        "Supply a positive budget within the current profile limit",
                    ));
                }
            }
            let status = Some(ThreadGoalStatus::Paused);
            let objective = objective.trim();
            let request = GoalSetRequest {
                thread_id: own.id,
                objective: GoalObjectiveUpdate::Set(objective),
                status,
                token_budget: budget.map_or(GoalTokenBudgetUpdate::Keep, |n| {
                    GoalTokenBudgetUpdate::Set(Some(n))
                }),
                max_goal_token_budget: own.maximum,
            };
            let outcome = svc
                .set_thread_goal(&own.db, request, async {
                    let item = ContextualUserFragment::into(UserGoalUpdate::Set {
                        objective: Some(objective.into()),
                        status,
                    });
                    let err =
                        |_| GoalServiceError::Internal("Cannot record goal instruction".into());
                    own.live
                        .append_items(&[RolloutItem::ResponseItem(item.into())])
                        .await
                        .map_err(err)?;
                    own.live.flush().await.map_err(err)
                })
                .await
                .map_err(|e| match e {
                    GoalServiceError::InvalidRequest(_) => {
                        PyValueError::new_err("Invalid objective or token budget")
                    }
                    _ => PyRuntimeError::new_err(
                        "Goal update failed. Read the current goal before retrying.",
                    ),
                })?;
            let err =
                |_| PyRuntimeError::new_err("Goal changed but its receipt could not be saved");
            own.live
                .append_items(&[outcome.thread_goal_updated_item()])
                .await
                .map_err(err)?;
            own.live.flush().await.map_err(err)?;
            serde_json::to_string(&outcome.goal).map_err(storage_error)
        })
    }

    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let live = self.live.clone();
        future_into_py(
            py,
            async move { live.shutdown().await.map_err(storage_error) },
        )
    }
}
