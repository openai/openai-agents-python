//! Prepare captured workspace data before the accepted destination is admitted.
use super::*;

pub(crate) struct AdmissionState {
    owner: Weak<PythonTools>,
    expected: codex_extension_api::AgentProfileReceipt,
    target: String,
    id: OnceLock<u64>,
    decision: watch::Sender<Option<bool>>,
}

impl AdmissionState {
    fn resolve(&self, accepted: bool) {
        self.decision.send_if_modified(|decision| {
            if decision.is_some() {
                return false;
            }
            *decision = Some(accepted);
            true
        });
    }

    pub(crate) async fn wait(&self) -> Result<bool> {
        let mut receive = self.decision.subscribe();
        loop {
            if let Some(accepted) = *receive.borrow_and_update() {
                return Ok(accepted);
            }
            receive
                .changed()
                .await
                .context("Workspace admission owner closed")?;
        }
    }
}

struct AdmissionOwner(Arc<AdmissionState>);

impl codex_extension_api::AgentProfileAdmissionObserver for AdmissionOwner {
    fn accepted(&self, receipt: &codex_extension_api::AgentProfileReceipt) {
        self.0.resolve(
            receipt.profile_id == self.0.target
                && self.0.expected.epoch.checked_add(1) == Some(receipt.epoch),
        );
    }
    fn refused(&self) {
        self.0.resolve(false);
    }
}

impl Drop for AdmissionOwner {
    fn drop(&mut self) {
        self.0.resolve(false);
    }
}

impl PythonTools {
    pub(crate) async fn prepare_agent_workspace(
        self: &Arc<Self>,
        input: &codex_extension_api::AgentProfileBoundaryInput<'_>,
        profile: &codex_extension_api::CapturedAgentProfile,
        target: &str,
    ) -> Result<(
        String,
        codex_extension_api::AgentProfileAdmissionObserverOwner,
    )> {
        let (decision, _) = watch::channel(None);
        let admission = Arc::new(AdmissionState {
            owner: Arc::downgrade(self),
            expected: profile.receipt.clone(),
            target: target.to_owned(),
            id: OnceLock::new(),
            decision,
        });
        let admission_owner = Arc::new(AdmissionOwner(Arc::clone(&admission)));
        let owner = self.admission_ready(input.turn_id)?;
        let stop =
            crate::native_interception::capture_step_stop(input.thread_store, input.turn_store)
                .context("Workspace preparation requires its exact live turn")?;
        let step = ExtensionData::new(format!("agent-workspace:{}", input.turn_id));
        step.insert(profile.clone());
        step.insert(
            profile
                .profile
                .data
                .get::<ProfileCatalog>()
                .context("Workspace source lacks its captured catalog")?
                .as_ref()
                .clone(),
        );
        let source = self.source_metadata(Some(&step))?;
        let result = self.exchange_preparation(owner, None, Some(stop.cancellation()),
            ["Expired Agent workspace", "Agent workspace reader closed", "Agent workspace cancelled"],
            PreparationReply::AgentWorkspace,
            |id| {
                let _ = admission.id.set(id);
                self.family.workspace_admissions.lock().expect("Workspace admissions poisoned")
                    .insert(id, Arc::clone(&admission));
                let mut request = json!({"id":input.turn_id,"msg":{
                    "type":"_sdk_agent_workspace", "turn_id":input.turn_id,
                    "thread":input.thread_store.level_id(),"instance":self.instance,"step_id":id,
                    "target_profile_id":target,
                }});
                if let Some(instance) = &self.instance {
                    request["subagent_id"] = json!(input.thread_store.level_id());
                    request["incarnation"] = json!(instance);
                }
                request["msg"].as_object_mut().expect("constructed workspace request").extend(source.clone());
                request
            },
            |reply: std::result::Result<String, ()>| reply.map_err(|_| anyhow::anyhow!("Agent workspace callback failed")),
        ).await;
        match result {
            Ok(guidance) => Ok((
                guidance,
                codex_extension_api::AgentProfileAdmissionObserverOwner(admission_owner),
            )),
            Err(error) => {
                if let Some(id) = admission.id.get() {
                    self.family
                        .workspace_admissions
                        .lock()
                        .expect("Workspace admissions poisoned")
                        .remove(id);
                }
                Err(error)
            }
        }
    }

    pub(crate) fn take_workspace_admission(
        self: &Arc<Self>,
        id: u64,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<Arc<AdmissionState>> {
        let owner = self.step_owner(thread, instance)?;
        let mut pending = self
            .family
            .workspace_admissions
            .lock()
            .expect("Workspace admissions poisoned");
        let admission = pending.get(&id).context("Expired workspace admission")?;
        ensure!(
            admission
                .owner
                .upgrade()
                .is_some_and(|expected| Arc::ptr_eq(&owner, &expected)),
            "Workspace admission belongs to another owner"
        );
        Ok(pending.remove(&id).expect("validated workspace admission"))
    }

    pub(crate) fn reply_agent_workspace(
        self: &Arc<Self>,
        id: u64,
        guidance: Option<String>,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<()> {
        let owner = self.step_owner(thread, instance)?;
        let mut preparations = owner
            .preparation_replies
            .lock()
            .expect("Python preparation replies poisoned");
        let slot = preparations
            .as_mut()
            .context("Expired preparation")?
            .get_mut(&id)
            .filter(|slot| !slot.is_closed())
            .context("Expired preparation")?;
        ensure!(
            matches!(
                slot.reply.as_ref(),
                Some(PreparationReply::AgentWorkspace(_))
            ),
            "Invalid Agent workspace reply"
        );
        let Some(PreparationReply::AgentWorkspace(reply)) = slot.reply.take() else {
            unreachable!()
        };
        drop(preparations);
        reply
            .send(guidance.ok_or(()))
            .map_err(|_| anyhow::anyhow!("Expired Agent workspace reply"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_extension_api::AgentProfileAdmissionObserver;

    fn pending() -> Arc<AdmissionState> {
        let (decision, _) = watch::channel(None);
        Arc::new(AdmissionState {
            owner: Weak::new(),
            expected: codex_extension_api::AgentProfileReceipt {
                profile_id: "source".into(),
                epoch: 4,
            },
            target: "target".into(),
            id: OnceLock::new(),
            decision,
        })
    }

    #[tokio::test]
    async fn cancelled_passive_waiter_does_not_retire_the_producer() {
        let state = pending();
        let producer = AdmissionOwner(state.clone());
        let waiter_state = state.clone();
        let waiter = tokio::spawn(async move { waiter_state.wait().await });
        tokio::task::yield_now().await;
        waiter.abort();
        assert!(waiter.await.unwrap_err().is_cancelled());
        assert_eq!(*state.decision.borrow(), None);
        drop(producer);
        assert!(!state.wait().await.unwrap());
    }

    #[tokio::test]
    async fn accepted_publication_survives_producer_drop_and_duplicate_refusal() {
        let state = pending();
        let producer = AdmissionOwner(state.clone());
        producer.accepted(&codex_extension_api::AgentProfileReceipt {
            profile_id: "target".into(),
            epoch: 5,
        });
        producer.refused();
        drop(producer);
        assert!(state.wait().await.unwrap());
    }

    #[tokio::test]
    async fn wrong_target_or_epoch_never_authorizes_the_workspace() {
        for (profile, epoch) in [("other", 5), ("target", 4), ("target", 6)] {
            let state = pending();
            let producer = AdmissionOwner(state.clone());
            producer.accepted(&codex_extension_api::AgentProfileReceipt {
                profile_id: profile.into(),
                epoch,
            });
            assert!(!state.wait().await.unwrap());
        }
    }
}
