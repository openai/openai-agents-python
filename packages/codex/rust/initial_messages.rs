//! Fresh opening data. Only the managed initializer can install this batch.
use anyhow::{Context, Result, ensure};
use codex_core_api::CodexThread;
use codex_history::RolloutItem;
use codex_protocol::ResponseItemId;
use codex_protocol::models::{ContentItem, ResponseItem};
use codex_protocol::protocol::EventMsg;
use serde_json::Value;
use std::collections::HashSet;

pub(crate) struct Batch(Vec<ResponseItem>);

impl Batch {
    pub(crate) fn items(&self) -> &[ResponseItem] {
        &self.0
    }

    /// Validate the whole caller batch before starting a native thread.
    pub(crate) fn capture(json: &str) -> Result<Self> {
        let values: Vec<Value> = serde_json::from_str(json).context("invalid initial messages")?;
        let mut items = Vec::with_capacity(values.len());
        for value in values {
            let object = value
                .as_object()
                .context("initial message must be an object")?;
            ensure!(
                object
                    .keys()
                    .all(|key| matches!(key.as_str(), "role" | "content" | "type")),
                "initial message contains unsupported fields"
            );
            ensure!(
                object.get("type").is_none_or(|kind| kind == "message"),
                "initial message must have type message"
            );
            let role = object
                .get("role")
                .and_then(Value::as_str)
                .context("initial message requires a role")?;
            let content: Vec<ContentItem> =
                match role {
                    "assistant" => {
                        let text = object
                            .get("content")
                            .and_then(Value::as_str)
                            .context("assistant seed requires settled text")?;
                        vec![ContentItem::OutputText {
                            text: text.to_owned(),
                            annotations: None,
                        }]
                    }
                    "user" => {
                        let parts = object
                            .get("content")
                            .and_then(Value::as_array)
                            .context("user seed requires captured content")?;
                        ensure!(!parts.is_empty(), "user seed content must not be empty");
                        for part in parts {
                            let object = part
                                .as_object()
                                .context("initial user content must be an object")?;
                            let kind = object
                                .get("type")
                                .and_then(Value::as_str)
                                .context("initial user content requires a type")?;
                            let allowed: &[&str] = match kind {
                                "input_text" => &["type", "text"],
                                "input_audio" => &["type", "audio_url"],
                                "input_video" => &["type", "video_url"],
                                "input_image" => &["type", "image_url", "file_id", "detail"],
                                "input_file" => {
                                    &["type", "file_id", "file_data", "file_url", "filename"]
                                }
                                _ => anyhow::bail!("unsupported initial user content"),
                            };
                            ensure!(
                                object.keys().all(|key| allowed.contains(&key.as_str())),
                                "initial user content contains unsupported fields"
                            );
                            if kind == "input_text" {
                                ensure!(
                                    object.get("text").is_some_and(Value::is_string),
                                    "initial text requires a string"
                                );
                            } else {
                                let sources: &[&str] = if kind == "input_audio" {
                                    &["audio_url"]
                                } else if kind == "input_video" {
                                    &["video_url"]
                                } else if kind == "input_image" {
                                    &["image_url", "file_id"]
                                } else {
                                    &["file_id", "file_data", "file_url"]
                                };
                                ensure!(
                                    sources
                                        .iter()
                                        .filter(|key| object.contains_key(**key))
                                        .count()
                                        == 1,
                                    "initial media requires exactly one source"
                                );
                                ensure!(
                                    sources.iter().filter_map(|key| object.get(*key)).all(
                                        |value| value.as_str().is_some_and(|text| !text.is_empty())
                                    ),
                                    "initial media requires a nonempty source"
                                );
                                if let Some(name) = object.get("filename") {
                                    ensure!(name.is_string(), "initial filename must be text");
                                }
                                if let Some(detail) = object.get("detail") {
                                    ensure!(
                                        detail.is_null()
                                            || detail.as_str().is_some_and(|value| matches!(
                                                value,
                                                "auto" | "low" | "high" | "original"
                                            )),
                                        "invalid initial image detail"
                                    );
                                }
                            }
                        }
                        // The current Core media owner validates the actual source shape.
                        serde_json::from_value(Value::Array(parts.clone()))
                            .context("invalid initial user content")?
                    }
                    _ => anyhow::bail!("initial messages accept only user and assistant roles"),
                };
            items.push(ResponseItem::Message {
                id: Some(ResponseItemId::new("msg")),
                role: role.to_owned(),
                content,
                phase: None,
                internal_chat_message_metadata_passthrough: None,
            });
        }
        Ok(Self(items))
    }

    /// The initializer holds the native operation and mutation gates throughout.
    pub(crate) async fn publish(self, thread: &CodexThread) -> Result<()> {
        // Fresh constructor admission owns this permit. Initial Core context can
        // include user-shaped world state; it is not an earlier conversation.
        let before = thread.load_history(true).await?;
        ensure!(
            before.items.iter().all(|row| match row {
                RolloutItem::EventMsg(EventMsg::TurnStarted(_)) => false,
                _ => true,
            }),
            "initial message authority no longer belongs to a fresh conversation"
        );
        if self.0.is_empty() {
            return Ok(());
        }
        let prepared = thread
            .inject_response_items_for_initialization(self.0.clone())
            .await?;
        let requested_ids: Vec<_> = self.0.iter().map(ResponseItem::id).collect();
        let requested_id_set: HashSet<_> = requested_ids.iter().copied().collect();
        let prepared_requested_ids: Vec<_> = prepared
            .iter()
            .filter(|item| requested_id_set.contains(&item.id()))
            .map(ResponseItem::id)
            .collect();
        ensure!(
            prepared_requested_ids == requested_ids,
            "initial message identities were not preserved by preparation"
        );
        thread.flush_rollout().await?;
        let live = thread.conversation_history_snapshot().await;
        confirm(live.items(), &prepared)?;
        let stored = thread.load_history(true).await?;
        confirm(
            stored.items.iter().filter_map(|row| match row {
                RolloutItem::ResponseItem(envelope) => Some(&envelope.item),
                _ => None,
            }),
            &prepared,
        )
    }
}

fn confirm<'a>(
    actual: impl IntoIterator<Item = &'a ResponseItem>,
    expected: &[ResponseItem],
) -> Result<()> {
    let expected_ids: HashSet<_> = expected.iter().map(ResponseItem::id).collect();
    let actual: Vec<_> = actual
        .into_iter()
        .filter(|item| expected_ids.contains(&item.id()))
        .collect();
    ensure!(
        actual.len() == expected.len(),
        "initial message batch was not confirmed"
    );
    for (actual, expected) in actual.into_iter().zip(expected) {
        match (actual, expected) {
            (
                ResponseItem::Message {
                    id: a_id,
                    role: a_role,
                    content: a_content,
                    ..
                },
                ResponseItem::Message {
                    id: e_id,
                    role: e_role,
                    content: e_content,
                    ..
                },
            ) => {
                ensure!(
                    a_id == e_id && a_role == e_role && a_content == e_content,
                    "initial message order or content was not confirmed"
                );
            }
            _ => anyhow::bail!("initial message batch contains a non-message item"),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn initial_messages_preserve_order_empty_duplicate_text_and_allocate_ids() {
        let input = r#"[{"role":"assistant","content":""},{"role":"assistant","content":"same"},{"role":"user","content":[{"type":"input_text","text":"same"}]}]"#;
        let first = Batch::capture(input).unwrap();
        let second = Batch::capture(input).unwrap();
        assert_eq!(first.0.len(), 3);
        assert!(first.0.iter().zip(&second.0).all(|(a, b)| a.id() != b.id()));
        confirm(&first.0, &first.0).unwrap();
        let mut stamped = first.0.clone();
        if let ResponseItem::Message {
            internal_chat_message_metadata_passthrough,
            ..
        } = &mut stamped[0]
        {
            let mut metadata =
                codex_protocol::models::InternalChatMessageMetadataPassthrough::default();
            metadata.turn_id = Some("core-owned-turn".into());
            *internal_chat_message_metadata_passthrough = Some(metadata);
        }
        confirm(&stamped, &first.0).unwrap();

        let mut reversed = first.0.clone();
        reversed.reverse();
        assert!(confirm(&reversed, &first.0).is_err());
        assert!(confirm(first.0.iter().take(2), &first.0).is_err());
        let mut duplicate = first.0.clone();
        duplicate.push(first.0[0].clone());
        assert!(confirm(&duplicate, &first.0).is_err());
        let mut changed = first.0.clone();
        if let ResponseItem::Message { content, .. } = &mut changed[0] {
            *content = vec![ContentItem::OutputText {
                text: "changed".into(),
                annotations: None,
            }];
        }
        assert!(confirm(&changed, &first.0).is_err());
    }
    #[test]
    fn initial_messages_reject_authority_and_malformed_data_before_publication() {
        for input in [
            r#"[{"role":"system","content":"x"}]"#,
            r#"[{"role":"assistant","content":[]}]"#,
            r#"[{"role":"assistant","content":"x","id":"caller"}]"#,
            r#"[{"role":"assistant","content":"x","metadata":{}}]"#,
            r#"[{"role":"user","content":[{"type":"output_text","text":"x"}]}]"#,
            r#"[{"role":"user","content":[{"type":"input_image","image_url":"x","file_id":"y"}]}]"#,
            r#"[{"role":"user","content":[{"type":"input_image","file_id":""}]}]"#,
            r#"[{"role":"user","content":[{"type":"input_text","text":"x","call_id":"foreign"}]}]"#,
        ] {
            assert!(Batch::capture(input).is_err(), "{input}");
        }
        for part in [
            r#"{"type":"input_audio"}"#,
            r#"{"type":"input_audio","audio_url":""}"#,
            r#"{"type":"input_audio","audio_url":null}"#,
            r#"{"type":"input_audio","audio_url":42}"#,
            r#"{"type":"input_audio","audio_url":"source","file_id":"other"}"#,
        ] {
            let input = format!(r#"[{{"role":"user","content":[{part}]}}]"#);
            assert!(Batch::capture(&input).is_err());
        }
        let audio = Batch::capture(r#"[{"role":"user","content":[{"type":"input_text","text":"before"},{"type":"input_audio","audio_url":"opaque:audio-source"},{"type":"input_text","text":"after"}]}]"#).unwrap();
        let ResponseItem::Message { content, .. } = &audio.0[0] else {
            panic!("message");
        };
        assert!(
            matches!(&content[1], ContentItem::InputAudio { audio_url } if audio_url == "opaque:audio-source")
        );
        assert_eq!(content.len(), 3);
        assert!(Batch::capture("[]").unwrap().0.is_empty());
    }
}
