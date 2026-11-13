//! One owned preparation reply edits presentation, never the accepted transfer receipt.
use super::*;
use codex_protocol::agent_profile::{
    AgentProfileHandoffInputRequest, AgentProfileHandoffPresentation,
};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Groups {
    pub input: Vec<Value>,
    pub previous: Vec<Value>,
    pub generated: Vec<Value>,
}

impl PythonTools {
    pub(crate) async fn prepare_handoff_input(
        self: &Arc<Self>,
        store: &ExtensionData,
        _thread: &str,
        turn: &str,
        source: &codex_extension_api::CapturedAgentProfile,
        request: &AgentProfileHandoffInputRequest,
        proposed: &AgentProfileHandoffPresentation,
    ) -> Result<AgentProfileHandoffPresentation> {
        ensure!(
            source.receipt == request.handoff.source && request.turn_id == turn,
            "Handoff preparation has another source owner"
        );
        let owner = store
            .get::<Arc<TurnAdmission>>()
            .map(|owner| Arc::clone(owner.as_ref()))
            .context("Handoff preparation lost its operation")?;
        let generation = source
            .profile
            .data
            .get::<ProfileCatalog>()
            .context("Handoff preparation lacks its captured source catalog")?
            .generation
            .id;
        let groups = self.exchange_preparation(
            owner.clone(), None, None,
            ["Expired handoff input preparation", "Handoff input reader closed", "Handoff input cancelled"],
            PreparationReply::HandoffInput,
            |id| json!({"id":turn,"msg": {
                "type":"_sdk_handoff_input", "step_id":id,"turn_id":turn,
                "thread":owner.thread_id,"instance":self.instance,
                "profile_id":source.receipt.profile_id,"profile_epoch":source.receipt.epoch,
                "generation":generation,"tool_name":request.tool_name,
                "target_profile_id":request.handoff.target_profile_id,
                "input":proposed.input,"previous":proposed.previous,"generated":proposed.generated,
            }}),
            |reply: std::result::Result<Groups, ()>| reply.map_err(|_| anyhow::anyhow!("Handoff input preparation failed")),
        ).await?;
        Ok(AgentProfileHandoffPresentation {
            input: groups.input,
            previous: groups.previous,
            generated: groups.generated,
            ..proposed.clone()
        })
    }

    pub(crate) fn reply_handoff_input(
        self: &Arc<Self>,
        id: u64,
        data: Option<&str>,
        thread: Option<&str>,
        instance: Option<&str>,
    ) -> Result<()> {
        // Match the native SDK aggregate JSON ceiling before deserializing an application reply.
        ensure!(
            data.is_none_or(|data| data.len() <= 8 * 1024 * 1024),
            "Handoff input exceeds 8 MiB"
        );
        let groups = data.map(serde_json::from_str::<Groups>).transpose()?;
        let owner = self.step_owner(thread, instance)?;
        let mut preparations = owner.preparation_replies.lock()
            .expect("Python preparation replies poisoned");
        let slot = preparations.as_mut().context("Expired preparation")?
            .get_mut(&id).filter(|slot| !slot.is_closed()).context("Expired preparation")?;
        ensure!(matches!(slot.reply.as_ref(), Some(PreparationReply::HandoffInput(_))),
            "Invalid handoff input reply");
        let Some(PreparationReply::HandoffInput(reply)) = slot.reply.take() else { unreachable!() };
        drop(preparations);
        reply
            .send(groups.ok_or(()))
            .map_err(|_| anyhow::anyhow!("Expired handoff input reply"))
    }
}
