//! Live callbacks acknowledge lifecycle without acquiring transfer authority.
use super::*;
use codex_extension_api::AgentLifecycleInput;

pub(crate) struct Bindings(pub(crate) Vec<String>);

pub(crate) fn enabled(store: &ExtensionData, profile: &str) -> bool {
    store
        .get::<Bindings>()
        .is_some_and(|bindings| bindings.0.iter().any(|id| id == profile))
}

impl PythonTools {
    pub(crate) async fn agent_lifecycle(
        self: &Arc<Self>,
        input: AgentLifecycleInput<'_>,
    ) -> Result<()> {
        let profile = input
            .source
            .map(|source| source.receipt.profile_id.as_str())
            .unwrap_or("");
        if !enabled(input.thread_store, profile) {
            return Ok(());
        }
        crate::agent_profiles::validate_lifecycle_handoff(&input)?;
        let owner = self.admission_ready(input.turn_id)?;
        let step = ExtensionData::new(format!("agent-lifecycle:{}", input.turn_id));
        if let Some(profile) = input.source {
            step.insert(profile.clone());
            step.insert(
                profile
                    .profile
                    .data
                    .get::<ProfileCatalog>()
                    .context("Lifecycle source lacks its captured catalog")?
                    .as_ref()
                    .clone(),
            );
        }
        let source = self.source_metadata(input.source.map(|_| &step))?;
        self.exchange_preparation(owner, None, Some(input.cancellation.clone()),
            ["Expired Agent lifecycle", "Agent lifecycle reader closed", "Agent lifecycle cancelled"],
            PreparationReply::AgentLifecycle,
            |id| {
                let mut request = json!({"id":input.turn_id,"msg":{
                    "type":"_sdk_agent_lifecycle","phase":if input.target_profile_id.is_some(){"handoff"}else{"start"},
                    "turn_id":input.turn_id,"thread":input.thread_store.level_id(),"instance":self.instance,"step_id":id,
                    "target_profile_id":input.target_profile_id,
                }});
                if let Some(instance) = &self.instance {
                    request["subagent_id"] = json!(input.thread_store.level_id());
                    request["incarnation"] = json!(instance);
                }
                request["msg"].as_object_mut().expect("constructed lifecycle request").extend(source.clone());
                request
            },
            |reply: std::result::Result<(), ()>| reply.map_err(|_| anyhow::anyhow!("Agent lifecycle callback failed")),
        ).await
    }

    pub(crate) fn reply_agent_lifecycle(
        self: &Arc<Self>,
        id: u64,
        error: bool,
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
                Some(PreparationReply::AgentLifecycle(_))
            ),
            "Invalid Agent lifecycle reply"
        );
        let Some(PreparationReply::AgentLifecycle(reply)) = slot.reply.take() else {
            unreachable!()
        };
        drop(preparations);
        reply
            .send(if error { Err(()) } else { Ok(()) })
            .map_err(|_| anyhow::anyhow!("Expired Agent lifecycle reply"))
    }
}
