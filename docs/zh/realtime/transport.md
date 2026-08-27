---
search:
  exclude: true
---
# 实时传输

使用本页面确定如何将实时智能体集成到 Python 应用程序中。

!!! note "Python SDK 边界"

    Python SDK **不**包含浏览器 WebRTC 传输。本页面仅介绍 Python SDK 的传输选择：服务器端 WebSocket 和 SIP 接入流程。浏览器 WebRTC 属于独立的平台主题，相关内容请参阅官方 [Realtime API 与 WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/)指南。

## 选择指南 {#decision-guide}

| 目标 | 入门资源 | 原因 |
| --- | --- | --- |
| 构建由服务器管理的实时应用 | [快速入门](quickstart.md) | 默认的 Python 路径是由 `RealtimeRunner` 管理的服务器端 WebSocket 会话。 |
| 了解应选择的传输方式和部署形态 | 本页面 | 在确定传输方式或部署形态之前，请先阅读本页面。 |
| 将智能体接入电话或 SIP 通话 | [实时指南](guide.md)和 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | 该仓库提供了由 `call_id` 驱动的 SIP 接入流程。 |

## 默认的 Python 路径：服务器端 WebSocket {#server-side-websocket-is-the-default-python-path}

除非传入自定义 `RealtimeModel`，否则 `RealtimeRunner` 会使用 `OpenAIRealtimeWebSocketModel`。

这意味着标准 Python 拓扑如下：

1. 您的 Python 服务创建一个 `RealtimeRunner`。
2. `await runner.run()` 返回一个 `RealtimeSession`。
3. 将 `RealtimeSession` 作为异步上下文管理器进入，然后发送文本、结构化消息或音频。
4. 消费 `RealtimeSessionEvent` 项，并将音频或转录文本转发到您的应用程序。

核心演示应用、CLI 示例和 Twilio Media Streams 示例均使用此拓扑：

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

当您的服务器负责音频管线、工具执行、审批流程和历史记录处理时，请使用此路径。

### 底层 WebSocket 调优 {#low-level-websocket-tuning}

需要调优底层服务器端 WebSocket 连接时，请将 `transport_config` 传递给 `OpenAIRealtimeWebSocketModel`：

```python
from agents.realtime import (
    OpenAIRealtimeWebSocketModel,
    RealtimeAgent,
    RealtimeRunner,
)

agent = RealtimeAgent(name="Assistant")
model = OpenAIRealtimeWebSocketModel(
    transport_config={
        "ping_interval": 20.0,
        "ping_timeout": 60.0,
        "handshake_timeout": 30.0,
        "max_size": 8 * 1024 * 1024,
    }
)
runner = RealtimeRunner(starting_agent=agent, model=model)
```

支持的选项包括：

-   `ping_interval`：客户端保活 ping 之间的秒数。设置为 `None` 可禁用 ping。
-   `ping_timeout`：断开连接前等待 pong 的秒数。设置为 `None` 可容忍延迟的 pong，而不会触发心跳超时。
-   `handshake_timeout`：等待初始连接握手的秒数。
-   `max_size`：传入 WebSocket 消息的最大字节数。SDK 默认值为 `None`，即不限制传入消息的大小；如需限制每条消息的内存使用量，请设置明确的上限。

这些设置配置的是客户端连接，而不是 Realtime API 会话。端点、身份验证、通话接入和播放设置仍应使用 `RealtimeModelConfig`。

## 电话通信路径：SIP 接入 {#sip-attach-is-the-telephony-path}

对于本仓库中记录的电话通信流程，Python SDK 通过 `call_id` 接入现有的实时通话。

此拓扑如下：

1. OpenAI 向您的服务发送 Webhook，例如 `realtime.call.incoming`。
2. 您的服务通过 Realtime Calls API 接听通话。
3. 您的 Python 服务启动一个 `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())`。
4. 会话通过 `model_config={"call_id": ...}` 建立连接，然后像其他实时会话一样处理事件。

[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) 展示了此拓扑。

更广泛的 Realtime API 也会将 `call_id` 用于某些服务器端控制模式，但本仓库提供的接入示例使用的是 SIP。

## SDK 范围之外的浏览器 WebRTC {#browser-webrtc-is-outside-this-sdk}

如果您的应用主要使用 Realtime WebRTC 浏览器客户端：

-   请将其视为不在本仓库 Python SDK 文档的范围内。
-   有关客户端流程和事件模型，请参阅官方 [Realtime API 与 WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/)和[实时对话](https://developers.openai.com/api/docs/guides/realtime-conversations/)文档。
-   如果除浏览器 WebRTC 客户端外还需要旁路服务器连接，请参阅官方[实时服务器端控制](https://developers.openai.com/api/docs/guides/realtime-server-controls/)指南。
-   不要期望本仓库提供浏览器端 `RTCPeerConnection` 抽象或现成的浏览器 WebRTC 示例。

本仓库目前也未提供浏览器 WebRTC 与 Python 旁路连接结合使用的示例。

## 自定义端点和接入点 {#custom-endpoints-and-attach-points}

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig] 中的传输配置接口允许您自定义默认传输行为：

-   `url`：覆盖 WebSocket 端点
-   `headers`：提供显式请求头，例如 Azure 身份验证请求头
-   `api_key`：直接传入 API 密钥，或通过回调传入
-   `call_id`：接入现有实时通话。本仓库记录的示例使用 SIP。
-   `playback_tracker`：报告实际播放进度，以便处理中断

选择拓扑后，请参阅[实时智能体指南](guide.md)，了解详细的生命周期和功能接口。