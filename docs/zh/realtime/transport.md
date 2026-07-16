---
search:
  exclude: true
---
# 实时传输

使用本页判断实时智能体如何适配你的 Python 应用程序。

!!! note "Python SDK 边界"

    Python SDK **不**包含浏览器 WebRTC 传输。本页仅讨论 Python SDK 的传输选择：服务端 WebSocket 和 SIP 接入流程。浏览器 WebRTC 是一个单独的平台主题，记录在官方 [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/) 指南中。

## 决策指南

| 目标 | 从这里开始 | 原因 |
| --- | --- | --- |
| 构建由服务端管理的实时应用 | [快速入门](quickstart.md) | 默认的 Python 路径是由 `RealtimeRunner` 管理的服务端 WebSocket 会话。 |
| 了解应选择哪种传输和部署形态 | 本页 | 在确定传输或部署形态之前使用本页。 |
| 将智能体接入电话或 SIP 通话 | [实时指南](guide.md) 和 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | 该仓库提供了由 `call_id` 驱动的 SIP 接入流程。 |

## 默认 Python 路径：服务端 WebSocket

除非你传入自定义 `RealtimeModel`，否则 `RealtimeRunner` 会使用 `OpenAIRealtimeWebSocketModel`。

这意味着标准 Python 拓扑如下：

1. 你的 Python 服务创建一个 `RealtimeRunner`。
2. `await runner.run()` 返回一个 `RealtimeSession`。
3. 进入会话并发送文本、结构化消息或音频。
4. 消费 `RealtimeSessionEvent` 项，并将音频或转录文本转发到你的应用程序。

这是核心演示应用、CLI 示例和 Twilio Media Streams 示例所使用的拓扑：

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

当你的服务负责音频管道、工具执行、审批流程和历史记录处理时，请使用此路径。

## 电话路径：SIP 接入

对于此仓库中记录的电话流程，Python SDK 会通过 `call_id` 接入现有的实时通话。

该拓扑如下：

1. OpenAI 向你的服务发送一个 webhook，例如 `realtime.call.incoming`。
2. 你的服务通过 Realtime Calls API 接受该通话。
3. 你的 Python 服务启动一个 `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())`。
4. 会话使用 `model_config={"call_id": ...}` 连接，然后像任何其他实时会话一样处理事件。

这是 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) 中展示的拓扑。

更广泛的 Realtime API 也会在一些服务端控制模式中使用 `call_id`，但此仓库提供的接入示例是 SIP。

## 本 SDK 范围之外的浏览器 WebRTC

如果你的应用的主要客户端是使用 Realtime WebRTC 的浏览器：

-   将其视为不在此仓库的 Python SDK 文档范围内。
-   使用官方 [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/) 和 [实时对话](https://developers.openai.com/api/docs/guides/realtime-conversations/)文档，了解客户端流程和事件模型。
-   如果你需要在浏览器 WebRTC 客户端之上使用旁路服务端连接，请使用官方 [Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/) 指南。
-   不要期望此仓库提供浏览器端 `RTCPeerConnection` 抽象或现成的浏览器 WebRTC 示例。

此仓库目前也未提供浏览器 WebRTC 加 Python 旁路服务端示例。

## 自定义端点和接入点

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig] 中的传输配置界面允许你调整默认路径：

-   `url`：覆盖 WebSocket 端点
-   `headers`：提供显式标头，例如 Azure 身份验证标头
-   `api_key`：直接传入 API key，或通过回调传入
-   `call_id`：接入现有实时通话。在此仓库中，记录的示例是 SIP。
-   `playback_tracker`：报告实际播放进度，以便处理中断

选择拓扑后，请参阅[实时智能体指南](guide.md)，了解详细生命周期和能力界面。