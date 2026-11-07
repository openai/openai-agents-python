//! Reject unsupported or incomplete output from a model generation stream.

use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};

use codex_extension_api::{
    ModelRequestContributor, ModelRequestInput, ModelRequestKind, ModelResponseError,
    ModelResponseInterceptor, ModelResponseStream, ResponseEvent,
};
use codex_protocol::error::ResponseIntegrityReason;
use codex_protocol::models::{ContentItem, ResponseItem};
use futures_util::Stream;

pub(crate) const UNSUPPORTED_OUTPUT: &str =
    "Native model response ended without complete supported output";

#[derive(Debug)]
pub(crate) struct Contributor(pub Arc<crate::native_interception::Boundary>);

impl ModelRequestContributor for Contributor {
    fn request(&self, input: ModelRequestInput<'_>) -> Option<Box<dyn ModelResponseInterceptor>> {
        (input.kind == ModelRequestKind::Generation).then(|| {
            Box::new(Interceptor {
                thread: input.thread_id.to_owned(),
                admission: self.0.response_admission(input.thread_id),
            }) as Box<dyn ModelResponseInterceptor>
        })
    }
}

struct Interceptor {
    thread: String,
    admission: Option<Arc<crate::python_tools::TurnAdmission>>,
}

impl ModelResponseInterceptor for Interceptor {
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
                ResponseEvent::OutputItemAdded(item) | ResponseEvent::OutputItemDone(item)
                    if matches!(
                        item,
                        ResponseItem::FunctionCallOutput {
                            call_id: Some(_),
                            ..
                        } | ResponseItem::CustomToolCallOutput { .. }
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
                        | ResponseItem::CustomToolCall { .. }
                        | ResponseItem::WebSearchCall { .. }
                        | ResponseItem::FileSearchCall { .. }
                        | ResponseItem::CodeInterpreterCall { .. }
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
