//! A conversation owns its mutations and shutdown; its Provider owns shared services.
use super::*;

impl EngineState {
    pub(crate) fn scoped(
        parent: Arc<Self>,
        service_config: Config,
        config_json: String,
        runtime_json: String,
        profile: codex_core::config::ManagedExecution,
    ) -> anyhow::Result<Self> {
        ensure!(
            parent.runtime_parent.is_none()
                && parent.local_checklist_store.is_none()
                && parent.goals.is_none()
                && parent.codex_account.is_none()
                && !parent.deferred_environment
                && parent
                    .store
                    .as_any()
                    .is::<crate::storage::MemoryThreadStore>()
                && !service_config.ephemeral,
            "shared runtime requires an isolated in-memory conversation"
        );
        ensure!(
            !parent.closed.load(Ordering::Acquire),
            "shared runtime is closed"
        );
        Ok(Self {
            manager: parent.manager.clone(),
            context_gate: Arc::new(AsyncRwLock::new(())),
            context_retired: AtomicBool::new(false),
            store: parent.store.clone(),
            local_checklist_store: None,
            // These subscribers belong to one scope, even when no children exist.
            agent_graph: Arc::new(crate::storage::MemoryAgentGraph::default()),
            goals: None,
            deferred_environment: false,
            local_environment_error: parent.local_environment_error.clone(),
            native_turn_control: parent.native_turn_control,
            sdk_user_input_enabled: parent.sdk_user_input_enabled,
            attached_environment: RwLock::new(None),
            environment_connection: Arc::new(Mutex::new(())),
            config_json,
            runtime_json,
            closed: AtomicBool::new(false),
            lifecycle: Mutex::new(()),
            event_owners: Mutex::new(HashMap::new()),
            managed_thread_ids: Mutex::new(HashSet::new()),
            native_threads: std::sync::Mutex::new(Vec::new()),
            startup_warnings: parent.startup_warnings.clone(),
            codex_account: None,
            extension_events: parent.extension_events.clone(),
            cloud_config: parent.cloud_config.clone(),
            service_config,
            model_http_pool: parent.model_http_pool.clone(),
            initial_config_available: AtomicBool::new(true),
            interception: parent.interception.clone(),
            restored_creation_times: Mutex::new(HashMap::new()),
            managed_execution: Some(profile),
            runtime_parent: Some(parent),
        })
    }

    // The caller holds this scope's lifecycle gate. Never retire another scope.
    pub(super) async fn close_scope(&self) -> anyhow::Result<()> {
        if self.closed.load(Ordering::Acquire) {
            return Ok(());
        }
        let threads = {
            let slots = self.native_threads.lock().expect("native threads poisoned");
            slots
                .iter()
                .filter_map(|slot| {
                    slot.upgrade().and_then(|slot| {
                        slot.lock()
                            .expect("native thread poisoned")
                            .live
                            .as_ref()
                            .cloned()
                    })
                })
                .collect::<Vec<_>>()
        };
        for thread in threads {
            thread.close().await?;
        }
        let ids = self
            .managed_thread_ids
            .lock()
            .await
            .iter()
            .copied()
            .collect::<Vec<_>>();
        for id in &ids {
            // Opening can fail before a NativeThread owns the Core handle.
            if let Ok(thread) = self.manager.get_thread(*id).await {
                tokio::time::timeout(CLOSE_TIMEOUT, async {
                    let _ = thread.request_agent_tree_shutdown();
                    thread.shutdown_and_wait().await?;
                    thread
                        .shutdown_agent_tree_and_check_managed_processes()
                        .await?;
                    Ok::<_, anyhow::Error>(())
                })
                .await
                .context("shared conversation shutdown timed out")??;
                self.manager.remove_thread_if_matches(id, &thread).await;
            }
            match self
                .store
                .delete_thread(DeleteThreadParams { thread_id: *id })
                .await
            {
                Ok(()) | Err(ThreadStoreError::ThreadNotFound { .. }) => {}
                Err(error) => return Err(error.into()),
            }
            self.interception.clear_code_state(*id);
        }
        self.agent_graph.remove_checkpoint_edges(&ids).await;
        self.managed_thread_ids.lock().await.clear();
        self.closed.store(true, Ordering::Release);
        Ok(())
    }
}
