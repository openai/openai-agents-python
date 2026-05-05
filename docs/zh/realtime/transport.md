---
search:
  exclude: true
---
# Realtime 传输

使用本页面来判断 realtime 智能体如何适配你的 Python 应用。

!!! note "Python SDK 边界"

    Python SDK **不**包含浏览器 WebRTC 传输。本页面仅介绍 Python SDK 的传输选择：服务端 WebSockets 和 SIP 附加流程。浏览器 WebRTC 是独立的平台主题，文档见官方指南 [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/)。

## 决策指南

| 目标 | 起步项 | 原因 |
| --- | --- | --- |
| 构建由服务端管理的 realtime 应用 | [Quickstart](quickstart.md) | 默认的 Python 路径是由 `RealtimeRunner` 管理的服务端 WebSocket 会话。 |
| 理解应选择哪种传输和部署形态 | 本页面 | 在你确定传输或部署形态之前先参考此页。 |
| 将智能体附加到电话或 SIP 通话 | [Realtime guide](guide.md) 和 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | 仓库提供了由 `call_id` 驱动的 SIP 附加流程。 |

## 服务端 WebSocket 是默认 Python 路径

除非你传入自定义 `RealtimeModel`，否则 `RealtimeRunner` 使用 `OpenAIRealtimeWebSocketModel`。

这意味着标准的 Python 拓扑如下：

1. 你的 Python 服务创建一个 `RealtimeRunner`。
2. `await runner.run()` 返回一个 `RealtimeSession`。
3. 进入该会话并发送文本、结构化消息或音频。
4. 消费 `RealtimeSessionEvent` 项，并将音频或转录转发到你的应用。

这是核心演示应用、CLI 示例和 Twilio Media Streams 示例使用的拓扑：

-   [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
-   [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
-   [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

当你的服务负责音频管线、工具执行、审批流程和历史记录处理时，请使用此路径。

## SIP 附加是电话路径

对于本仓库中记录的电话流程，Python SDK 通过 `call_id` 附加到现有 realtime 通话。

该拓扑如下：

1. OpenAI 向你的服务发送 webhook，例如 `realtime.call.incoming`。
2. 你的服务通过 Realtime Calls API 接受通话。
3. 你的 Python 服务启动 `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())`。
4. 会话使用 `model_config={"call_id": ...}` 建立连接，然后像其他 realtime 会话一样处理事件。

这是 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) 中展示的拓扑。

更广义的 Realtime API 也会在某些服务端控制模式中使用 `call_id`，但本仓库提供的附加示例是 SIP。

## 浏览器 WebRTC 不属于此 SDK 范围

如果你应用的主要客户端是使用 Realtime WebRTC 的浏览器：

-   将其视为超出本仓库 Python SDK 文档范围。
-   使用官方文档 [Realtime API with WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/) 和 [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/) 来了解客户端流程和事件模型。
-   如果你需要在浏览器 WebRTC 客户端之上使用 sideband 服务端连接，请使用官方指南 [Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/)。
-   不要期待本仓库提供浏览器侧 `RTCPeerConnection` 抽象或现成的浏览器 WebRTC 示例。

本仓库目前也未提供浏览器 WebRTC 加 Python sideband 的示例。

## 自定义端点和附加点

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig] 中的传输配置接口让你可以调整默认路径：

-   `url`: 覆盖 WebSocket 端点
-   `headers`: 提供显式请求头，例如 Azure 认证请求头
-   `api_key`: 直接传递 API key 或通过回调传递
-   `call_id`: 附加到现有 realtime 通话。在本仓库中，文档化示例是 SIP。
-   `playback_tracker`: 上报实际播放进度以处理中断

选定拓扑后，详细的生命周期和能力接口请参见 [Realtime agents guide](guide.md)。