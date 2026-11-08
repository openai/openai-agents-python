//! The unchanged Codex v3 Live codec, with SDK-owned admission of text work.

use std::sync::Arc;
use std::time::Duration;

use codex_api::{
    Provider, RealtimeEventParser, RealtimeOutputModality, RealtimeSessionConfig,
    RealtimeSessionMode, RealtimeWebsocketClient, RealtimeWebsocketConnection, RetryConfig,
};
use codex_http_client::{HttpClientFactory, OutboundProxyPolicy};
use codex_protocol::protocol::{ConversationTextRole, RealtimeAudioFrame, RealtimeVoice};
use http::{HeaderMap, HeaderValue, header::AUTHORIZATION};
use pyo3::prelude::*;

use crate::{async_bridge, config, py_error};

#[pyclass(module = "agents._native")]
pub(crate) struct NativeLiveSession {
    connection: Arc<RealtimeWebsocketConnection>,
}

impl Drop for NativeLiveSession {
    fn drop(&mut self) {
        // The Python awaiter may be cancelled after connect but before the
        // bridge publishes this owner. Explicit SDK close is still awaited;
        // Codex's writer makes this last-owner safeguard idempotent.
        let connection = Arc::clone(&self.connection);
        pyo3_async_runtimes::tokio::get_runtime().spawn(async move {
            let _ = connection.close().await;
        });
    }
}

#[pyfunction]
pub(crate) fn _connect_live<'py>(
    py: Python<'py>,
    base_url: String,
    api_key: String,
    instructions: String,
    model: String,
    voice: String,
) -> PyResult<Bound<'py, PyAny>> {
    let voice: RealtimeVoice = config::decode(serde_json::Value::String(voice))
        .map_err(|_| py_error("Unsupported Live voice"))?;
    let mut headers = HeaderMap::new();
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str(&format!("Bearer {api_key}"))
            .map_err(|_| py_error("Invalid Live API key"))?,
    );
    let provider = Provider {
        name: "Agents SDK Live".into(),
        base_url,
        query_params: None,
        headers: HeaderMap::new(),
        retry: RetryConfig {
            max_attempts: 0,
            base_delay: Duration::from_millis(100),
            retry_429: false,
            retry_5xx: false,
            retry_transport: false,
        },
        stream_idle_timeout: Duration::from_secs(60),
    };
    let session = RealtimeSessionConfig {
        instructions,
        initial_items: Vec::new(),
        delegation_ack_filler: None,
        model: Some(model),
        session_id: None,
        event_parser: RealtimeEventParser::FramelessBidi,
        session_mode: RealtimeSessionMode::Conversational,
        output_modality: RealtimeOutputModality::Audio,
        voice,
        v2: None,
        function_tools: Vec::new(),
    };
    async_bridge::future_into_py(py, async move {
        let client = RealtimeWebsocketClient::new(
            provider,
            HttpClientFactory::new(OutboundProxyPolicy::ReqwestDefault),
        );
        let connection = client
            .connect(session, headers, HeaderMap::new())
            .await
            .map_err(|_| py_error("Live connection failed"))?;
        Ok(NativeLiveSession {
            connection: Arc::new(connection),
        })
    })
}

#[pymethods]
impl NativeLiveSession {
    fn next_event<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let connection = Arc::clone(&self.connection);
        async_bridge::future_into_py(py, async move {
            let event = connection
                .next_event()
                .await
                .map_err(|_| py_error("Live connection read failed"))?;
            event
                .map(|event| serde_json::to_string(&event).map_err(py_error))
                .transpose()
        })
    }

    fn send_audio<'py>(&self, py: Python<'py>, audio: String) -> PyResult<Bound<'py, PyAny>> {
        let connection = Arc::clone(&self.connection);
        async_bridge::future_into_py(py, async move {
            connection
                .send_audio_frame(RealtimeAudioFrame {
                    data: audio,
                    sample_rate: 24_000,
                    num_channels: 1,
                    samples_per_channel: None,
                    item_id: None,
                    format: None,
                    response_id: None,
                    content_index: None,
                    output_index: None,
                    profile_receipt: None,
                    output_guard: Default::default(),
                })
                .await
                .map_err(|_| py_error("Live audio send failed"))
        })
    }

    fn send_text<'py>(&self, py: Python<'py>, text: String) -> PyResult<Bound<'py, PyAny>> {
        let connection = Arc::clone(&self.connection);
        async_bridge::future_into_py(py, async move {
            connection
                .send_conversation_item_create(text, ConversationTextRole::User)
                .await
                .map_err(|_| py_error("Live text send failed"))
        })
    }

    fn send_result<'py>(
        &self,
        py: Python<'py>,
        delegation_id: String,
        output: String,
    ) -> PyResult<Bound<'py, PyAny>> {
        let connection = Arc::clone(&self.connection);
        async_bridge::future_into_py(py, async move {
            connection
                .writer()
                .send_conversation_handoff_append(delegation_id, output)
                .await
                .map_err(|_| py_error("Live delegation output send failed"))
        })
    }

    fn close<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let connection = Arc::clone(&self.connection);
        async_bridge::future_into_py(py, async move {
            connection
                .close()
                .await
                .map_err(|_| py_error("Live connection close failed"))
        })
    }
}
