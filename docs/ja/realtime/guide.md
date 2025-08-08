---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の  realtime  機能を使用して音声対応 AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta feature"
 realtime エージェントはベータ版です。実装を改善する過程で、破壊的変更が行われる可能性があります。

## 概要

 realtime エージェントは、会話の流れを実現し、音声とテキスト入力をリアルタイムに処理し、リアルタイム音声で応答します。これらは OpenAI の Realtime API と永続的に接続し、低遅延で自然な音声対話を可能にし、割り込みにもスムーズに対応します。

## アーキテクチャ

### 主要コンポーネント

 realtime システムは、以下の主要コンポーネントで構成されています。

- ** RealtimeAgent **: instructions、tools、handoffs で設定されたエージェント  
- ** RealtimeRunner **: 設定を管理します。`runner.run()` を呼び出すとセッションを取得できます。  
- ** RealtimeSession **: 単一の対話セッション。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。  
- ** RealtimeModel **: 基盤となるモデルインターフェース (通常は OpenAI の WebSocket 実装)  

### セッションフロー

一般的な  realtime  セッションは以下の流れで進みます。

1. ** RealtimeAgent ** を instructions、tools、handoffs と共に作成します。  
2. ** RealtimeRunner ** をエージェントと設定オプションでセットアップします。  
3. `await runner.run()` を使用して **セッションを開始** し、 RealtimeSession を取得します。  
4. `send_audio()` または `send_message()` で **音声またはテキストメッセージを送信** します。  
5. セッションを反復処理して **イベントを受信** します — イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。  
6. ユーザーがエージェントの発話に割り込んだ際は **割り込みを処理** し、現在の音声生成を自動的に停止します。  

セッションは会話履歴を保持し、 realtime モデルとの永続接続を管理します。

## エージェント設定

 RealtimeAgent は通常の Agent クラスとほぼ同じ方法で動作しますが、いくつか重要な違いがあります。完全な API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] をご覧ください。

主な相違点:

- モデルの選択はエージェントレベルではなくセッションレベルで設定します。  
- structured outputs は非対応です (`outputType` はサポートされません)。  
- 音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。  
- その他の機能 (tools、handoffs、instructions など) は同じように動作します。  

## セッション設定

### モデル設定

セッション設定では、基盤となる  realtime  モデルの動作を制御できます。モデル名 (例: `gpt-4o-realtime-preview`)、音声 (alloy、echo、fable、onyx、nova、shimmer)、対応モダリティ (テキストおよび / または音声) を設定可能です。入力と出力の両方で音声フォーマットを設定でき、デフォルトは PCM16 です。

### オーディオ設定

オーディオ設定では、音声入力と出力の取り扱いを制御します。Whisper などのモデルを使用した入力音声の文字起こし、言語設定、ドメイン固有用語の精度向上のための transcription prompt を設定できます。ターン検出設定では、エージェントが応答を開始・終了すべきタイミングを制御し、音声活動検出しきい値、無音時間、検出音声周辺のパディングなどが設定できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、 realtime エージェントでも会話中に実行される function tools をサポートしています。

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

ハンドオフを使用すると、会話を専門エージェント間で引き継ぐことができます。

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

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理することでイベントを受信できます。イベントには音声出力チャンク、文字起こし結果、ツール実行開始・終了、エージェントハンドオフ、エラーが含まれます。主に処理すべきイベントは以下のとおりです。

- **audio**: エージェントの応答からの raw 音声データ  
- **audio_end**: エージェントの発話が完了  
- **audio_interrupted**: ユーザーがエージェントに割り込み  
- **tool_start/tool_end**: ツール実行のライフサイクル  
- **handoff**: エージェントのハンドオフが発生  
- **error**: 処理中にエラーが発生  

完全なイベント詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] をご覧ください。

## ガードレール

 realtime エージェントでは出力ガードレールのみサポートされます。パフォーマンス低下を避けるため、ガードレールはデバウンスされ、リアルタイム生成中に毎単語ではなく一定間隔で実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` で指定できます。両方から提供されたガードレールは一緒に実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断できます。デバウンス動作により、安全性とリアルタイム性能のバランスを取ります。テキストエージェントと異なり、 realtime エージェントはガードレールがトリップしても Exception を発生させません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] で音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] でテキストをセッションに送信します。

音声出力を再生するには `audio` イベントをリッスンし、お好みのオーディオライブラリでデータを再生してください。ユーザーが割り込んだ際には `audio_interrupted` イベントを受信し、すぐに再生を停止し、キューにある音声をクリアするようにしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスし、カスタムリスナーを追加したり高度な操作を行ったりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、高度なユースケース向けに接続を低レベルで制御するための [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースに直接アクセスできます。

## 例

完全な動作例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。UI あり・なしのデモが含まれています。