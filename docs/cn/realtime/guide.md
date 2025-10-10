---
search:
  exclude: true
---
# 指南

本指南深入介绍如何使用 OpenAI Agents SDK 的实时功能构建支持语音的 AI 智能体。

!!! warning "测试版功能"
实时智能体处于测试版。随着我们改进实现，可能会出现重大更改。

## 概述

实时智能体支持对话流程，实时处理音频和文本输入，并以实时音频进行响应。它们与 OpenAI 的实时 API 保持持久连接，实现低延迟的自然语音对话，并能优雅地处理打断情况。

## 架构

### 核心组件

实时系统由以下关键组件组成：

- **RealtimeAgent**: 由指令、工具、交接组成的智能体。
- **RealtimeRunner**: 管理配置。你可以调用 `runner.run()` 来获取会话。
- **RealtimeSession**: 单个交互会话。通常，每次用户开始对话时创建一个，并在对话结束时保持活动状态。
- **RealtimeModel**: 底层模型接口（通常是 OpenAI 的 WebSocket 实现）

### 会话流程

典型的实时会话流程如下：

1. 使用指令、工具、交接**创建 RealtimeAgent**。
2. 使用智能体和配置选项**设置 RealtimeRunner**。
3. 使用 `await runner.run()`**开始会话**，返回 RealtimeSession。
4. 使用 `send_audio()` 或 `send_message()`**发送音频或文本消息**。
5. 通过迭代会话**监听事件**。事件包括音频输出、转录、工具调用、交接、错误。
6. **处理打断**，当用户在智能体说话时覆盖说话，这将自动停止当前音频生成。

会话维护对话历史并管理与实时模型的持久连接。

## エージェント設定

RealtimeAgent は、通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご確認ください。

通常のエージェントとの主な違い:

- モデルの選択はエージェント レベルではなく、セッション レベルで設定します。
- structured outputs のサポートはありません（`outputType` はサポートされません）。
- 声質はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
- ツール、ハンドオフ、instructions などのその他の機能は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-realtime` など）、声質（alloy、echo、fable、onyx、nova、shimmer）およびサポートするモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方に設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを使用した入力音声の書き起こし、言語設定、ドメイン固有用語の精度を高めるための書き起こしプロンプトを設定できます。発話区間検出（turn detection）の設定では、エージェントが応答を開始・終了するタイミングを制御でき、音声活動検出のしきい値、無音時間、検出された音声の前後に付与するパディングなどのオプションがあります。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントは会話中に実行される 関数ツール をサポートします:

```python
from agents import function_tool

@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    # Your weather API logic here
    return f"The weather in {city} is sunny, 72°F"

@function_tool
def book_appointment(date: str, time: str, service: str) -> str:
    """Book an appointment."""
    # Your booking logic here
    return f"Appointment booked for {service} on {date} at {time}"

agent = RealtimeAgent(
    name="Assistant",
    instructions="You can help with weather and appointments.",
    tools=[get_weather, book_appointment],
)
```

## ハンドオフ

### ハンドオフの作成

ハンドオフにより、専門化されたエージェント間で会話を引き継ぐことができます。

```python
from agents.realtime import realtime_handoff

# Specialized agents
billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing and payment issues.",
)

technical_agent = RealtimeAgent(
    name="Technical Support",
    instructions="You handle technical troubleshooting.",
)

# Main agent with handoffs
main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="You are the main customer service agent. Hand off to specialists when needed.",
    handoffs=[
        realtime_handoff(billing_agent, tool_description="Transfer to billing support"),
        realtime_handoff(technical_agent, tool_description="Transfer to technical support"),
    ]
)
```

## イベント処理

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理してリッスンできます。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始・終了、エージェントのハンドオフ、エラーが含まれます。特に処理すべき主要イベントは次のとおりです:

- **audio**: エージェントの応答からの生の音声データ
- **audio_end**: エージェントの発話が完了
- **audio_interrupted**: ユーザー によるエージェントの発話の割り込み
- **tool_start/tool_end**: ツール実行のライフサイクル
- **handoff**: エージェントのハンドオフが発生
- **error**: 処理中にエラーが発生

完全なイベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでサポートされるのは出力用ガードレールのみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるため、（単語ごとではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースのガードレールは併用して実行されます。

```python
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail

def sensitive_data_check(context, agent, output):
    return GuardrailFunctionOutput(
        tripwire_triggered="password" in output,
        output_info=None,
    )

agent = RealtimeAgent(
    name="Assistant",
    instructions="...",
    output_guardrails=[OutputGuardrail(guardrail_function=sensitive_data_check)],
)
```

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキスト エージェントと異なり、realtime エージェントはガードレールが作動しても **Exception** は発生させません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用してセッションに音声を送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで音声データを再生してください。ユーザー がエージェントを割り込んだ場合に即座に再生を停止し、キューにある音声をクリアするため、`audio_interrupted` イベントも必ずリッスンしてください。

## 直接モデルアクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり高度な操作を実行したりできます:

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

動作する完全な例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI コンポーネントの有無それぞれのデモが含まれています。