//! Reject unsupported or incomplete output from a model generation stream.

use std::collections::VecDeque;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};

use codex_extension_api::{
    ModelInvocationDeadlineState, ModelRequestContributor, ModelRequestInput, ModelRequestKind,
    ModelResponseError, ModelResponseInterceptor, ModelResponseStream, ModelToolAdmission,
    ResponseEvent, TurnSamplingStop,
};
use codex_protocol::error::ResponseIntegrityReason;
use codex_protocol::models::{ContentItem, ResponseItem};
use codex_protocol::protocol::ResponseObservationStatus;
use futures_util::{Stream, StreamExt, stream};

pub(crate) const UNSUPPORTED_OUTPUT: &str =
    "Native model response ended without complete supported output";

#[derive(Debug)]
pub(crate) struct Contributor {
    pub(crate) boundary: Arc<crate::native_interception::Boundary>,
    pub(crate) registered_plan: bool,
}

impl ModelRequestContributor for Contributor {
    fn request(&self, input: ModelRequestInput<'_>) -> Option<Box<dyn ModelResponseInterceptor>> {
        (input.kind == ModelRequestKind::Generation).then(|| {
            Box::new(Interceptor {
                thread: input.thread_id.to_owned(),
                admission: self.boundary.response_admission(input.thread_id),
                registered_plan: self.registered_plan,
                text_demand: self.boundary.text_delta_demand(input.thread_id),
            }) as Box<dyn ModelResponseInterceptor>
        })
    }
}

struct Interceptor {
    text_demand: Option<Arc<codex_api::TextDeltaDemand>>,
    thread: String,
    admission: Option<Arc<crate::python_tools::TurnAdmission>>,
    registered_plan: bool,
}

impl ModelResponseInterceptor for Interceptor {
    fn text_delta_demand(&self) -> Option<Arc<codex_api::TextDeltaDemand>> {
        (!self.registered_plan)
            .then(|| self.text_demand.clone())
            .flatten()
    }

    fn preserves_provider_message_window(&self) -> bool {
        // Accepted events are unchanged; unsupported output rejects the stream.
        true
    }

    fn intercept_with_tool_admission(
        self: Box<Self>,
        stream: ModelResponseStream,
        admission: Option<Arc<dyn ModelToolAdmission>>,
    ) -> ModelResponseStream {
        if !self.registered_plan {
            return self.intercept(stream);
        }
        let stop = admission
            .as_ref()
            .and_then(|admission| admission.turn_store().get::<TurnSamplingStop>());
        let deadline = stop.as_ref().and_then(|stop| stop.capture_model_deadline());
        let gate = PlanStream {
            upstream: Some(self.intercept(stream)),
            pending: VecDeque::new(),
            completed_response: None,
            admission,
            stop,
            deadline,
        };
        Box::pin(stream::unfold(gate, |mut gate| async move {
            gate.next().await.map(|event| (event, gate))
        }))
    }

    fn intercept(self: Box<Self>, stream: ModelResponseStream) -> ModelResponseStream {
        Box::pin(Generation {
            stream,
            owner: *self,
            pending_assistant: false,
            last_assistant_has_text: false,
            completed_native_item: false,
            rejected: false,
        })
    }
}

const MAX_PENDING_PLAN_EVENTS: usize = 65_536;

struct PlanStream {
    upstream: Option<ModelResponseStream>,
    pending: VecDeque<Result<ResponseEvent, ModelResponseError>>,
    completed_response: Option<String>,
    admission: Option<Arc<dyn ModelToolAdmission>>,
    stop: Option<Arc<TurnSamplingStop>>,
    deadline: Option<Arc<ModelInvocationDeadlineState>>,
}

impl PlanStream {
    async fn next(&mut self) -> Option<Result<ResponseEvent, ModelResponseError>> {
        let Some(upstream) = &mut self.upstream else {
            return self.pending.pop_front();
        };
        // Hold the suffix in its original order. In particular, a retained
        // successful observation must follow its calls for manual recovery.
        let terminal = loop {
            match upstream.next().await {
                Some(Ok(event)) => {
                    match &event {
                        ResponseEvent::ResponseObserved(observation) => {
                            self.completed_response =
                                if observation.status == ResponseObservationStatus::Completed {
                                    observation.response_id.clone()
                                } else {
                                    None
                                };
                        }
                        ResponseEvent::Completed { response_id, .. } => {
                            // The SSE parser also emits Completed for interrupted
                            // responses. Only a matching successful observation
                            // authorizes release. Use Core's captured clock before
                            // admitting callbacks that precede Core's terminal event.
                            let accepted = self.completed_response.as_ref() == Some(response_id)
                                && (self.pending.is_empty()
                                    || (!self.stop.as_ref().is_some_and(|stop| stop.requested())
                                        && self
                                            .deadline
                                            .as_ref()
                                            .is_none_or(|deadline| deadline.accept_outcome())));
                            break Some(if accepted {
                                Ok(event)
                            } else {
                                Err(ModelResponseError::Stream(
                                    "Sign in with ChatGPT response did not complete successfully"
                                        .into(),
                                ))
                            });
                        }
                        _ => {}
                    }
                    if self.pending.is_empty()
                        && !starts_tool_call(&event, self.admission.as_deref())
                    {
                        return Some(Ok(event));
                    }
                    if self.pending.len() == MAX_PENDING_PLAN_EVENTS {
                        break Some(Err(ModelResponseError::Stream(
                            "Sign in with ChatGPT response exceeded its pending tool event limit"
                                .into(),
                        )));
                    }
                    self.pending.push_back(Ok(event));
                }
                terminal => break terminal,
            }
        };
        self.upstream = None;
        if !matches!(&terminal, Some(Ok(ResponseEvent::Completed { .. }))) {
            // Received facts describe what the provider reported, even when
            // no provisional output is safe to retain or execute.
            self.pending.retain(|event| {
                matches!(
                    event,
                    Ok(ResponseEvent::ResponseObserved(_) | ResponseEvent::ResponseFact(_))
                )
            });
        }
        self.pending.extend(terminal);
        self.pending.pop_front()
    }
}

fn starts_tool_call(event: &ResponseEvent, admission: Option<&dyn ModelToolAdmission>) -> bool {
    let item = match event {
        ResponseEvent::OutputItemAdded(item) | ResponseEvent::OutputItemDone(item) => item,
        _ => return false,
    };
    matches!(
        item,
        ResponseItem::FunctionCall { .. }
            | ResponseItem::LocalShellCall { .. }
            | ResponseItem::CustomToolCall { .. }
            | ResponseItem::ApplyPatchCall { .. }
            | ResponseItem::ComputerCall { .. }
            | ResponseItem::McpApprovalRequest { .. }
    ) || matches!(
        item,
        ResponseItem::ToolSearchCall { call_id: Some(_), execution, .. } if execution == "client"
    ) || (matches!(item, ResponseItem::ShellCall { .. })
        && admission.is_some_and(|admission| admission.admits(item)))
}

struct Generation {
    stream: ModelResponseStream,
    owner: Interceptor,
    pending_assistant: bool,
    last_assistant_has_text: bool,
    completed_native_item: bool,
    rejected: bool,
}

impl Stream for Generation {
    type Item = Result<ResponseEvent, ModelResponseError>;

    fn poll_next(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Self::Item>> {
        let this = self.get_mut();
        if this.rejected {
            return Poll::Ready(None);
        }
        let next = this.stream.as_mut().poll_next(cx);
        if let Poll::Ready(Some(Ok(event))) = &next {
            match event {
                ResponseEvent::OutputItemAdded(ResponseItem::McpApprovalRequest { id, .. })
                | ResponseEvent::OutputItemDone(ResponseItem::McpApprovalRequest { id, .. })
                    if id.as_ref().is_none_or(|id| id.as_str().is_empty()) =>
                {
                    // Approval correlation must come from the provider. Core's
                    // generated local item IDs cannot authorize a reply.
                    this.rejected = true;
                    let reason = ResponseIntegrityReason::UndecodableOutputItem;
                    if let Some(receipt) = &this.owner.admission {
                        receipt.reject_model(&this.owner.thread, reason);
                    }
                    return Poll::Ready(Some(Err(ModelResponseError::ResponseIntegrity {
                        reason,
                    })));
                }
                ResponseEvent::OutputItemAdded(item) | ResponseEvent::OutputItemDone(item)
                    if matches!(
                        item,
                        ResponseItem::FunctionCallOutput {
                            call_id: Some(_),
                            ..
                        } | ResponseItem::CustomToolCallOutput { .. }
                            | ResponseItem::ApplyPatchCallOutput { .. }
                            | ResponseItem::ComputerCallOutput { .. }
                            | ResponseItem::McpApprovalResponse { .. }
                    ) || matches!(
                        item,
                        ResponseItem::ToolSearchOutput {
                            call_id: Some(_),
                            execution,
                            ..
                        } if execution != "server"
                    ) =>
                {
                    // Core records all completed items and orphan-checks owned
                    // receipts on the next turn. Do not accept these from the
                    // provider. Unpaired external context and server search
                    // outputs belong to Core and still pass through.
                    this.rejected = true;
                    let reason = ResponseIntegrityReason::ProviderOwnedToolReceipt;
                    if let Some(receipt) = &this.owner.admission {
                        receipt.reject_model(&this.owner.thread, reason);
                    }
                    return Poll::Ready(Some(Err(ModelResponseError::ResponseIntegrity {
                        reason,
                    })));
                }
                ResponseEvent::OutputItemAdded(ResponseItem::Message { role, .. })
                    if role == "assistant" =>
                {
                    this.pending_assistant = true;
                }
                ResponseEvent::OutputItemDone(item) => {
                    match item {
                        ResponseItem::Message { role, content, .. } if role == "assistant" => {
                            // A canonical completion supersedes older transient
                            // openings that Core did not retain as final output.
                            this.pending_assistant = false;
                            // Replace, not accumulate: a visible final message
                            // without text cannot inherit an earlier answer. Core
                            // accepts either text type, including real blank text.
                            this.last_assistant_has_text = content.iter().any(|part| {
                                matches!(
                                    part,
                                    ContentItem::InputText { .. } | ContentItem::OutputText { .. }
                                )
                            });
                        }
                        // Core dispatches these calls or exposes a completed
                        // WebSearch turn item. A later empty placeholder must
                        // not suppress that genuine native progress.
                        ResponseItem::FunctionCall { .. }
                        | ResponseItem::ApplyPatchCall { .. }
                        | ResponseItem::ComputerCall { .. }
                        | ResponseItem::CustomToolCall { .. }
                        | ResponseItem::WebSearchCall { .. }
                        | ResponseItem::McpListTools { .. }
                        | ResponseItem::McpCall { .. }
                        | ResponseItem::McpApprovalRequest { .. }
                        | ResponseItem::FileSearchCall { .. }
                        | ResponseItem::CodeInterpreterCall { .. }
                        | ResponseItem::ShellCall { .. }
                        | ResponseItem::ShellCallOutput { .. }
                        | ResponseItem::ImageGenerationCall { .. } => {
                            this.pending_assistant = false;
                            this.completed_native_item = true;
                        }
                        // ToolRouter only handles client search with a call ID.
                        // Malformed client arguments still produce a real Core
                        // response and request follow-up, so Core validates them.
                        ResponseItem::ToolSearchCall {
                            call_id: Some(_),
                            execution,
                            ..
                        } if execution == "client" => {
                            this.pending_assistant = false;
                            this.completed_native_item = true;
                        }
                        // Ignored native variants cannot hide an unfinished
                        // assistant message.
                        _ => {}
                    }
                }
                ResponseEvent::Completed { end_turn, .. }
                    if *end_turn != Some(false)
                        && ((!this.last_assistant_has_text && !this.completed_native_item)
                            || this.pending_assistant) =>
                {
                    // Core's parser can drop an undecodable output_item.done
                    // (including refusal content), and Completed has no raw
                    // output. Never claim the response was empty success. Do
                    // not include model text, IDs, or provider details in errors.
                    this.rejected = true;
                    let reason = if this.pending_assistant {
                        ResponseIntegrityReason::UnfinishedAssistantItem
                    } else {
                        ResponseIntegrityReason::MissingSupportedTerminalOutput
                    };
                    if let Some(receipt) = &this.owner.admission {
                        receipt.reject_model(&this.owner.thread, reason);
                    }
                    return Poll::Ready(Some(Err(ModelResponseError::ResponseIntegrity {
                        reason,
                    })));
                }
                _ => {}
            }
        }
        next
    }
}

#[cfg(test)]
mod registered_plan_tests {
    use super::*;
    use codex_protocol::models::{LocalShellAction, LocalShellExecAction, LocalShellStatus};
    use codex_protocol::protocol::{ResponseIncompleteReason, ResponseObservation};
    use futures_util::{pin_mut, poll};
    use tokio::sync::oneshot;

    #[tokio::test]
    async fn shell_and_function_items_wait_for_registered_plan_completion() {
        let local_shell = ResponseItem::LocalShellCall {
            id: None,
            call_id: Some("synthetic-shell".into()),
            status: LocalShellStatus::Completed,
            action: LocalShellAction::Exec(LocalShellExecAction {
                command: Vec::new(),
                timeout_ms: None,
                working_directory: None,
                env: None,
                user: None,
            }),
            internal_chat_message_metadata_passthrough: None,
        };
        let exec_command = ResponseItem::FunctionCall {
            caller: None,
            id: None,
            name: "exec_command".into(),
            namespace: None,
            arguments: "{}".into(),
            encrypted_function_args: None,
            call_id: "synthetic-function".into(),
            internal_chat_message_metadata_passthrough: None,
            asynchronous: None,
            status: None,
        };
        for (item, registered_plan, completed) in [
            (&local_shell, true, true),
            (&local_shell, true, false),
            (&local_shell, false, false),
            (&exec_command, true, true),
            (&exec_command, true, false),
            (&exec_command, false, false),
        ] {
            let observation = ResponseEvent::ResponseObserved(ResponseObservation {
                response_id: Some("synthetic-response".into()),
                upstream_request_id: None,
                status: if completed {
                    ResponseObservationStatus::Completed
                } else {
                    ResponseObservationStatus::Incomplete
                },
                incomplete_reason: (!completed).then_some(ResponseIncompleteReason::Interrupted),
                usage: None,
            });
            let terminal = ResponseEvent::Completed {
                response_id: "synthetic-response".into(),
                token_usage: None,
                usage_metadata: None,
                end_turn: Some(false),
            };
            let (release_terminal, wait_for_terminal) = oneshot::channel();
            let source = stream::iter([
                Ok(ResponseEvent::OutputItemAdded(item.clone())),
                Ok(ResponseEvent::OutputItemDone(item.clone())),
            ])
            .chain(
                stream::once(async move {
                    wait_for_terminal.await.unwrap();
                    stream::iter([Ok(observation), Ok(terminal)])
                })
                .flatten(),
            );
            let interceptor = Box::new(Interceptor {
                text_demand: None,
                thread: "synthetic-thread".into(),
                admission: None,
                registered_plan,
            });
            let mut delivered = Vec::new();
            let mut output = interceptor
                .intercept_with_tool_admission(Box::pin(source), None)
                .inspect(|event| {
                    if let Ok(ResponseEvent::OutputItemDone(item)) = event {
                        delivered.push(item.clone());
                    }
                });
            let first = {
                let next = output.next();
                pin_mut!(next);
                let before_terminal = poll!(next.as_mut());
                assert_eq!(before_terminal.is_pending(), registered_plan);
                release_terminal.send(()).unwrap();
                match before_terminal {
                    Poll::Ready(event) => event,
                    Poll::Pending => next.await,
                }
            };

            if registered_plan && !completed {
                assert!(matches!(
                    first,
                    Some(Ok(ResponseEvent::ResponseObserved(_)))
                ));
                assert!(matches!(
                    output.next().await,
                    Some(Err(ModelResponseError::Stream(_)))
                ));
            } else {
                assert!(matches!(first, Some(Ok(ResponseEvent::OutputItemAdded(_)))));
                assert!(matches!(
                    output.next().await,
                    Some(Ok(ResponseEvent::OutputItemDone(_)))
                ));
                assert!(matches!(
                    output.next().await,
                    Some(Ok(ResponseEvent::ResponseObserved(_)))
                ));
                assert!(matches!(
                    output.next().await,
                    Some(Ok(ResponseEvent::Completed { .. }))
                ));
            }
            assert!(output.next().await.is_none());
            drop(output);
            if registered_plan && !completed {
                assert!(delivered.is_empty());
            } else {
                assert_eq!(delivered, [item.clone()]);
            }
        }
    }
}

#[cfg(test)]
mod computer_guard_tests {
    use super::*;
    use futures_util::StreamExt;
    use serde_json::json;

    #[tokio::test]
    async fn computer_progress_completes_but_provider_computer_receipts_never_grant_authority() {
        for receipt in [false, true] {
            let value = if receipt {
                json!({"type":"computer_call_output","call_id":"computer-only","output":{"type":"computer_screenshot","image_url":"data:image/png;base64,private-image"},"acknowledged_safety_checks":[]})
            } else {
                json!({"type":"computer_call","call_id":"computer-only","actions":[{"type":"wait"}]})
            };
            for added in [false, true] {
                let item = serde_json::from_value(value.clone()).unwrap();
                let mut generation = Generation {
                    stream: Box::pin(futures_util::stream::iter(vec![
                        Ok(if added {
                            ResponseEvent::OutputItemAdded(item)
                        } else {
                            ResponseEvent::OutputItemDone(item)
                        }),
                        Ok(ResponseEvent::Completed {
                            response_id: "computer".into(),
                            token_usage: None,
                            usage_metadata: None,
                            end_turn: None,
                        }),
                    ])),
                    owner: Interceptor {
                        text_demand: None,
                        thread: "computer-guard".into(),
                        admission: None,
                        registered_plan: false,
                    },
                    pending_assistant: false,
                    last_assistant_has_text: false,
                    completed_native_item: false,
                    rejected: false,
                };
                let first = generation.next().await.unwrap();
                if receipt {
                    assert!(matches!(
                        first,
                        Err(ModelResponseError::ResponseIntegrity {
                            reason: ResponseIntegrityReason::ProviderOwnedToolReceipt
                        })
                    ));
                    assert!(generation.next().await.is_none());
                } else if !added {
                    assert!(first.is_ok());
                    assert!(generation.next().await.unwrap().is_ok());
                }
            }
        }
    }
}
