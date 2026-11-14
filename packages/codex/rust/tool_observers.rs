//! Informational callbacks on the exact native start and recorded-output owners.
use super::*;

pub(crate) struct Bindings(pub(crate) bool);

struct Observer {
    tools: Weak<PythonTools>,
    owner: Weak<TurnAdmission>,
    event: Value,
    deferred: bool,
    mcp_entry: bool,
    started: std::sync::atomic::AtomicBool,
}

impl Observer {
    async fn deliver(&self, mut event: Value) -> Result<()> {
        let tools = self.tools.upgrade().context("Expired tool lifecycle owner")?;
        let owner = self.owner.upgrade().context("Expired tool lifecycle owner")?;
        ensure!(owner.is_current(), "Expired tool lifecycle owner");
        tools.exchange_preparation(
            owner, None, None,
            ["Expired tool lifecycle", "Tool lifecycle reader closed", "Tool lifecycle cancelled"],
            PreparationReply::ToolLifecycle,
            |id| {
                event["msg"]["step_id"] = json!(id);
                event.clone()
            },
            |reply: std::result::Result<(), ()>| reply.map_err(|_| anyhow::anyhow!("Tool lifecycle callback failed")),
        ).await
    }
}

impl codex_extension_api::RecordedToolOutputObserver for Observer {
    fn on_mcp_entry(&self) -> ExtensionFuture<'_, std::result::Result<(), String>> {
        Box::pin(async move {
            if self.mcp_entry && !self.started.load(Ordering::Acquire) {
                self.deliver(self.event.clone()).await.map_err(|error| error.to_string())?;
                self.started.store(true, Ordering::Release);
            }
            Ok(())
        })
    }
    fn on_handler_output(&self, output: &dyn ToolOutput) {
        if self.deferred {
            if let Some(output) = output.as_any().and_then(|output| output.downcast_ref::<ReplyOutput<PythonOutput>>()) {
                self.started.store(output.lifecycle_started, Ordering::Release);
            }
        }
    }
    fn on_output<'a>(
        &'a self,
        output: &'a codex_history::ResponseItemEnvelope,
        succeeded: bool,
    ) -> ExtensionFuture<'a, std::result::Result<(), String>> {
        Box::pin(async move {
            if !self.started.load(Ordering::Acquire) { return Ok(()); }
            let mut event = self.event.clone();
            event["msg"]["phase"] = json!("end");
            event["msg"]["output"] = serde_json::to_value(&output.item)
                .map_err(|_| "Tool lifecycle output is unavailable".to_owned())?;
            event["msg"]["succeeded"] = json!(succeeded);
            self.deliver(event).await.map_err(|error| error.to_string())
        })
    }
}

impl PythonTools {
    pub(crate) async fn start_tool_observer(
        self: &Arc<Self>,
        input: ToolStartInput<'_>,
    ) -> Result<Option<Arc<dyn codex_extension_api::RecordedToolOutputObserver>>> {
        if !input.thread_store.get::<Bindings>().is_some_and(|bindings| bindings.0)
            || input.source != codex_tools::ToolCallSource::Direct {
            return Ok(None);
        }
        let owner = self.admission_ready(input.turn_id)?;
        let source = self.source_metadata(input.step_store)?;
        let arguments = match input.payload {
            ToolPayload::Function { arguments } => Some(arguments.as_str()),
            ToolPayload::Custom { input } => Some(input.as_str()),
            _ => None,
        };
        let mut event = json!({"id":input.turn_id,"msg": {
            "type":"_sdk_tool_lifecycle","phase":"start",
            "turn_id":input.turn_id,"thread":input.thread_store.level_id(),
            "instance":self.instance,"call_id":input.call_id,
            "name":input.tool_name.name,
            "namespace":if input.tool_name.is_default_namespace() { None } else { input.tool_name.namespace.as_deref() },
            "arguments":arguments,
        }});
        event["msg"].as_object_mut().expect("constructed tool lifecycle").extend(source);
        if let Some(instance) = &self.instance {
            event["subagent_id"] = json!(input.thread_store.level_id());
            event["incarnation"] = json!(instance);
        }
        let mcp_entry = input.mcp_tool.is_some();
        let deferred = self.generation_for_step(input.step_store)?
            .managed_lifecycle_start(input.tool_name);
        let observer = Arc::new(Observer {
            tools: Arc::downgrade(self), owner: Arc::downgrade(&owner), event,
            deferred, mcp_entry, started: std::sync::atomic::AtomicBool::new(false),
        });
        if !deferred && !mcp_entry {
            observer.deliver(observer.event.clone()).await?;
            observer.started.store(true, Ordering::Release);
        }
        Ok(Some(observer))
    }

    pub(crate) fn reply_tool_lifecycle(
        self: &Arc<Self>, id: u64, error: bool,
        thread: Option<&str>, instance: Option<&str>,
    ) -> Result<()> {
        let owner = self.step_owner(thread, instance)?;
        let mut preparations = owner.preparation_replies.lock()
            .expect("Python preparation replies poisoned");
        let slot = preparations.as_mut().context("Expired preparation")?
            .get_mut(&id).filter(|slot| !slot.is_closed()).context("Expired preparation")?;
        ensure!(matches!(slot.reply.as_ref(), Some(PreparationReply::ToolLifecycle(_))),
            "Invalid tool lifecycle reply");
        let Some(PreparationReply::ToolLifecycle(reply)) = slot.reply.take() else { unreachable!() };
        drop(preparations);
        reply.send(if error { Err(()) } else { Ok(()) })
            .map_err(|_| anyhow::anyhow!("Expired tool lifecycle reply"))
    }
}
