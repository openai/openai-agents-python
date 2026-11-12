//! Source-presence edits through the existing native settings/publication owners.
use anyhow::Result;
use codex_core_api::CodexThread;
use codex_protocol::model_request_controls::ModelRequestControls;
use codex_protocol::protocol::ThreadSettingsOverrides;

pub(crate) async fn explicit_inputs(
    thread: &CodexThread,
    instructions: bool,
    tools: bool,
) -> Result<Option<ThreadSettingsOverrides>> {
    let controls = if let Some(source) = thread.captured_agent_profile().await? {
        source.profile.model.request_controls.clone()
    } else {
        thread.thread_settings_snapshot().await.request_controls
    };
    let Some(Some(mut prompt)) = controls.hosted_prompt else {
        return Ok(None);
    };
    if instructions {
        prompt.omit_instructions = false;
    }
    if tools {
        prompt.omit_tools = false;
    }
    Ok(Some(ThreadSettingsOverrides {
        request_controls: Some(ModelRequestControls {
            hosted_prompt: Some(Some(prompt)),
            ..Default::default()
        }),
        ..Default::default()
    }))
}

pub(crate) async fn explicit_root_tools(thread: &CodexThread) -> Result<()> {
    // Captured profiles publish their model/catalog edit together.
    if thread.captured_agent_profile().await?.is_none() {
        if let Some(settings) = explicit_inputs(thread, false, true).await? {
            thread.update_thread_settings(settings).await?;
        }
    }
    Ok(())
}
