//! One owned preparation reply edits presentation, never the accepted transfer receipt.
use super::*;
use codex_protocol::agent_profile::{
    AgentProfileHandoffInputRequest, AgentProfileHandoffPresentation, HandoffSummary,
};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Groups {
    pub input: Vec<Value>,
    pub previous: Vec<Value>,
    pub generated: Vec<Value>,
    #[serde(default)]
    pub summaries: Vec<HandoffSummary>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recorded_generated_indices: Option<Vec<usize>>,
    #[serde(default)]
    pub recorded_assistant_text: Vec<Vec<String>>,
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
                "summaries":proposed.summaries,
            }}),
            |reply: std::result::Result<Groups, ()>| reply.map_err(|_| anyhow::anyhow!("Handoff input preparation failed")),
        ).await?;
        if let Some(indices) = &groups.recorded_generated_indices {
            ensure!(
                indices.windows(2).all(|pair| pair[0] < pair[1])
                    && indices
                        .iter()
                        .all(|index| *index < proposed.generated.len()),
                "Canonical handoff history has changed source occurrences"
            );
            store.insert(codex_extension_api::HandoffGeneratedHistorySelection {
                handoff: request.handoff.clone(),
                keep: indices.clone(),
                append_assistant_text: groups.recorded_assistant_text,
            });
        }
        Ok(AgentProfileHandoffPresentation {
            input: groups.input,
            previous: groups.previous,
            generated: groups.generated,
            summaries: groups.summaries,
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
            matches!(slot.reply.as_ref(), Some(PreparationReply::HandoffInput(_))),
            "Invalid handoff input reply"
        );
        let Some(PreparationReply::HandoffInput(reply)) = slot.reply.take() else {
            unreachable!()
        };
        drop(preparations);
        reply
            .send(groups.ok_or(()))
            .map_err(|_| anyhow::anyhow!("Expired handoff input reply"))
    }
}
