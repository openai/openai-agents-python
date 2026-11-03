//! Stored goals must settle before a native writer resumes from old history.

use super::*;
use codex_core_api::{AbsolutePathBuf, SqliteConfig};
use codex_goal_extension::{
    GoalObjectiveUpdate, GoalService, GoalServiceError, GoalSetRequest, GoalTokenBudgetUpdate,
};
use codex_protocol::models::{BaseInstructions, ContentItem};
use codex_protocol::protocol::{SessionSource, ThreadGoalStatus};
use std::sync::atomic::Ordering;
use std::time::Duration;

fn thread_params(id: ThreadId) -> CreateThreadParams {
    CreateThreadParams {
        creator_user_id: None,
        creator_account_id: None,
        session_id: id.into(),
        thread_id: id,
        extra_config: None,
        forked_from_id: None,
        parent_thread_id: None,
        source: SessionSource::Exec,
        thread_source: None,
        originator: "stored-goal-test".into(),
        base_instructions: BaseInstructions::default(),
        dynamic_tools: vec![],
        selected_capability_roots: vec![],
        multi_agent_version: None,
        history_mode: ThreadHistoryMode::Legacy,
        history_base: None,
        subagent_history_start_ordinal: None,
        initial_window_id: id.to_string(),
        runtime_workspace_roots: None,
        metadata: ThreadPersistenceMetadata {
            cwd: None,
            model_provider: "fixture-provider".into(),
            memory_mode: ThreadMemoryMode::Disabled,
        },
    }
}

fn resume_params(params: &CreateThreadParams, history: Vec<RolloutItem>) -> ResumeThreadParams {
    ResumeThreadParams {
        thread_id: params.thread_id,
        rollout_path: None,
        history: Some(Arc::new(history)),
        history_revision: None,
        include_archived: true,
        metadata: params.metadata.clone(),
    }
}

async fn history(store: &MemoryThreadStore, id: ThreadId) -> Vec<RolloutItem> {
    store
        .inner
        .load_history(LoadThreadHistoryParams {
            thread_id: id,
            include_archived: true,
        })
        .await
        .expect("load canonical stored history")
        .items
}

#[tokio::test]
async fn stored_goal_commit_fences_stale_resume_until_history_and_metadata_agree() {
    let home = tempfile::TempDir::new().unwrap();
    let db = codex_state::StateRuntime::init(
        SqliteConfig::from_sqlite_home(
            AbsolutePathBuf::try_from(home.path().to_path_buf()).unwrap(),
        ),
        "fixture-provider".into(),
    )
    .await
    .unwrap();
    let service = GoalService::new();
    let store = MemoryThreadStore::default();
    let id = ThreadId::new();
    let params = thread_params(id);
    store
        .create_checkpoint_thread(params.clone(), &mut vec![])
        .await
        .unwrap();
    let metadata = codex_state::ThreadMetadataBuilder::new(
        id,
        home.path().join("stored.jsonl"),
        SystemTime::now().into(),
        SessionSource::Exec,
    )
    .build("fixture-provider");
    db.insert_thread_if_absent(&metadata).await.unwrap();
    let stale = history(&store, id).await;
    assert!(matches!(stale.first(), Some(RolloutItem::SessionMeta(meta)) if meta.meta.id == id));
    let original_length = stale.len();
    let writer = store.stored_goal_writer(id).await.unwrap();
    let resume = store.resume_thread(resume_params(&params, stale));
    tokio::pin!(resume);
    tokio::select! {
        biased;
        _ = &mut resume => panic!("native resume passed an unsettled stored goal"),
        _ = tokio::task::yield_now() => {}
    }
    let accepted = service
        .set_thread_goal(
            &db,
            GoalSetRequest {
                thread_id: id,
                objective: GoalObjectiveUpdate::Set("Finish the stored report"),
                status: Some(ThreadGoalStatus::Paused),
                token_budget: GoalTokenBudgetUpdate::Set(Some(750)),
                max_goal_token_budget: None,
            },
            async {
                writer
                    .append(UserGoalUpdate::Set {
                        objective: Some("Finish the stored report".into()),
                        status: Some(ThreadGoalStatus::Paused),
                    })
                    .await
                    .map_err(|error| GoalServiceError::Internal(error.to_string()))
            },
        )
        .await
        .unwrap();
    assert_eq!(accepted.goal.objective, "Finish the stored report");
    assert_eq!(accepted.goal.token_budget, Some(750));
    tokio::select! {
        biased;
        _ = &mut resume => panic!("native resume escaped before the goal writer dropped"),
        _ = tokio::task::yield_now() => {}
    }
    drop(writer);
    let refreshed = tokio::time::timeout(Duration::from_secs(1), resume)
        .await
        .expect("resume after stored goal settlement")
        .unwrap();
    let goal = service.get_thread_goal(&db, id).await.unwrap().unwrap();
    assert_eq!(goal.objective, "Finish the stored report");
    assert_eq!(goal.status, ThreadGoalStatus::Paused);
    assert_eq!(goal.token_budget, Some(750));
    assert_eq!(refreshed.len(), original_length + 1);
    let Some(RolloutItem::ResponseItem(envelope)) = refreshed.last() else {
        panic!("goal update missing from resumed history");
    };
    let ResponseItem::Message {
        role,
        content,
        internal_chat_message_metadata_passthrough: Some(annotation),
        ..
    } = &envelope.item
    else {
        panic!("goal update must be a host-annotated message");
    };
    assert_eq!(role, "user");
    let kinds = annotation.content_item_kinds.as_ref().unwrap();
    assert_eq!(kinds.len(), 1);
    assert_eq!(kinds[0].0, "user.goal");
    assert!(matches!(
        content.as_slice(),
        [ContentItem::InputText { text }]
            if text.contains("User set the goal: \"Finish the stored report\"")
                && text.contains("User set goal status: \"paused\".")
    ));
    store.shutdown_thread(id).await.unwrap();
    db.close().await;
}

#[tokio::test]
async fn stored_goals_reject_unknown_and_live_native_writers_then_allow_retired_ones() {
    let store = MemoryThreadStore::default();
    let id = ThreadId::new();
    assert!(matches!(
        store.stored_goal_writer(id).await,
        Err(ThreadStoreError::ThreadNotFound { thread_id }) if thread_id == id
    ));
    let params = thread_params(id);
    store.create_thread(params.clone()).await.unwrap();
    assert!(store.stored_goal_writer(id).await.is_err());
    store.shutdown_thread(id).await.unwrap();
    drop(
        store
            .stored_goal_writer(id)
            .await
            .expect("native writer retired"),
    );
    let old = history(&store, id).await;
    store
        .resume_thread(resume_params(&params, old))
        .await
        .unwrap();
    assert!(store.stored_goal_writer(id).await.is_err());
    store.discard_thread(id).await.unwrap();
    drop(
        store
            .stored_goal_writer(id)
            .await
            .expect("resumed writer retired"),
    );
}

#[tokio::test]
async fn failed_append_and_cancelled_stored_writer_release_resume_without_extra_history() {
    let store = Arc::new(MemoryThreadStore::default());
    let id = ThreadId::new();
    let params = thread_params(id);
    store
        .create_checkpoint_thread(params.clone(), &mut vec![])
        .await
        .unwrap();
    let old = history(&store, id).await;
    let writer = store.stored_goal_writer(id).await.unwrap();
    store.fail_appends.store(true, Ordering::SeqCst);
    assert!(
        writer
            .append(UserGoalUpdate::Set {
                objective: Some("This must not reach history".into()),
                status: Some(ThreadGoalStatus::Paused),
            })
            .await
            .is_err()
    );
    drop(writer);
    store.fail_appends.store(false, Ordering::SeqCst);
    let (entered, observed) = tokio::sync::oneshot::channel();
    let holder = tokio::spawn({
        let store = Arc::clone(&store);
        async move {
            let _writer = store.stored_goal_writer(id).await.unwrap();
            entered.send(()).unwrap();
            std::future::pending::<()>().await;
        }
    });
    observed.await.unwrap();
    let resume = store.resume_thread(resume_params(&params, old.clone()));
    tokio::pin!(resume);
    tokio::select! {
        biased;
        _ = &mut resume => panic!("resume passed a held stored writer"),
        _ = tokio::task::yield_now() => {}
    }
    holder.abort();
    assert!(holder.await.unwrap_err().is_cancelled());
    let refreshed = tokio::time::timeout(Duration::from_secs(1), resume)
        .await
        .expect("cancelled goal owner must release native resume")
        .unwrap();
    assert_eq!(serde_json::json!(&*refreshed), serde_json::json!(old));
    assert!(store.stored_goal_writer(id).await.is_err());
    store.shutdown_thread(id).await.unwrap();
}
