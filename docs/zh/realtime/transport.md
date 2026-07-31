---
search:
  exclude: true
---
# 实时传输

本页帮助您确定如何将实时智能体集成到 Python 应用中。

!!! note "Python SDK 边界"

    Python SDK **不**包含浏览器 WebRTC 传输。本页仅介绍 Python SDK 的传输方式：服务端 WebSocket 和 SIP 接入流程。浏览器 WebRTC 属于单独的平台主题，详见官方[Realtime API 与 WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/)指南。

## 决策指南

| 目标 | 入门文档 | 原因 |
| --- | --- | --- |
| 构建由服务端管理的实时应用 | [快速入门](quickstart.md) | Python 的默认路径是由 `RealtimeRunner` 管理的服务端 WebSocket 会话。 |
| 了解应选择的传输方式和部署架构 | 本页 | 在确定传输方式或部署架构之前，请先阅读本页。 |
| 将智能体接入电话或 SIP 通话 | [实时指南](guide.md)和[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) | 代码仓库提供了由 `call_id` 驱动的 SIP 接入流程。 |

## 服务端 WebSocket：Python 的默认路径

除非传入自定义 `RealtimeModel`，否则 `RealtimeRunner` 会使用 `OpenAIRealtimeWebSocketModel`。

这意味着标准 Python 拓扑结构如下：

1. 您的 Python 服务创建一个 `RealtimeRunner`。
2. `await runner.run()` 返回一个 `RealtimeSession`。
3. 进入会话并发送文本、结构化消息或音频。
4. 使用 `RealtimeSessionEvent` 项，并将音频或转录文本转发到您的应用。

核心演示应用、CLI 代码示例和 Twilio Media Streams 代码示例均使用此拓扑结构：

- [`examples/realtime/app`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app)
- [`examples/realtime/cli`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/cli)
- [`examples/realtime/twilio`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio)

当您的服务负责音频管线、工具执行、审批流程和历史记录处理时，请使用此路径。

### 底层 WebSocket 调优

如需调整底层服务端 WebSocket 连接，请将 `transport_config` 传递给 `OpenAIRealtimeWebSocketModel`：

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

- `ping_interval`：客户端保活 ping 之间的秒数。设置为 `None` 可禁用 ping。
- `ping_timeout`：断开连接前等待 pong 的秒数。设置为 `None` 可容忍 pong 延迟，而不会触发心跳超时。
- `handshake_timeout`：等待初始连接握手的秒数。
- `max_size`：传入 WebSocket 消息的最大字节数。SDK 默认值为 `None`，即不限制传入消息的大小；如需限制单条消息的内存用量，请设置明确的上限。

这些设置用于配置客户端连接，而不是 Realtime API 会话。对于端点、身份验证、通话接入和播放设置，请继续使用 `RealtimeModelConfig`。

## SIP 接入：电话通信路径

对于此代码仓库中记录的电话通信流程，Python SDK 会通过 `call_id` 接入现有的实时通话。

此拓扑结构如下：

1. OpenAI 向您的服务发送 `realtime.call.incoming` 等 webhook。
2. 您的服务通过 Realtime Calls API 接听通话。
3. 您的 Python 服务启动 `RealtimeRunner(..., model=OpenAIRealtimeSIPModel())`。
4. 会话使用 `model_config={"call_id": ...}` 建立连接，随后像其他实时会话一样处理事件。

[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) 展示了此拓扑结构。

更广泛的 Realtime API 也会在某些服务端控制模式中使用 `call_id`，但此代码仓库提供的接入代码示例采用 SIP。

## SDK 范围之外的浏览器 WebRTC

如果应用的主要客户端是使用实时 WebRTC 的浏览器：

- 应将其视为超出此代码仓库中 Python SDK 文档的范围。
- 客户端流程和事件模型请参阅官方[Realtime API 与 WebRTC](https://developers.openai.com/api/docs/guides/realtime-webrtc/)和[实时对话](https://developers.openai.com/api/docs/guides/realtime-conversations/)文档。
- 如果需要在浏览器 WebRTC 客户端之外建立旁路服务端连接，请参阅官方[实时服务端控制](https://developers.openai.com/api/docs/guides/realtime-server-controls/)指南。
- 不应期望此代码仓库提供浏览器端 `RTCPeerConnection` 抽象或现成的浏览器 WebRTC 代码示例。

此代码仓库目前也未提供浏览器 WebRTC 与 Python 旁路连接组合使用的代码示例。

## 自定义端点与接入点

[`RealtimeModelConfig`][agents.realtime.model.RealtimeModelConfig] 中的传输配置选项可用于调整默认路径：

- `url`：覆盖 WebSocket 端点
- `headers`：提供明确的标头，例如 Azure 身份验证标头
- `api_key`：直接传入 API 密钥或通过回调传入
- `call_id`：接入现有的实时通话。此代码仓库中记录的代码示例采用 SIP。
- `playback_tracker`：报告实际播放进度，以便处理中断

选择拓扑结构后，请参阅[实时智能体指南](guide.md)，了解详细的生命周期和功能范围。