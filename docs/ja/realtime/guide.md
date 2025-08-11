---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の リアルタイム 機能を利用して、音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装を改善する過程で、破壊的変更が入る可能性があります。

## 概要

リアルタイム エージェントは、音声とテキスト入力をリアルタイムで処理し、音声で応答できる会話フローを実現します。OpenAI の Realtime API と持続的に接続し、低レイテンシで自然な音声対話を行い、割り込みにもスムーズに対応します。

## アーキテクチャ

### コアコンポーネント

リアルタイム システムは、次の主要コンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、handoffs で設定されたエージェントです。  
- **RealtimeRunner**: 設定の管理を行います。`runner.run()` を呼び出してセッションを取得します。  
- **RealtimeSession**: 1 回の対話セッションを表します。ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。  
- **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）です。  

### セッションフロー

一般的なリアルタイム セッションの流れは次のとおりです。

1. **RealtimeAgent** を instructions、tools、handoffs で作成します。  
2. **RealtimeRunner** をエージェントと設定オプションで準備します。  
3. `await runner.run()` を実行して **セッションを開始** し、RealtimeSession を取得します。  
4. `send_audio()` または `send_message()` で **音声またはテキストメッセージを送信** します。  
5. セッションをイテレートして **イベントを受信** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。  
6. ユーザーがエージェントの発話に割り込んだ場合は **割り込みを処理** します。現在の音声生成が自動で停止します。  

セッションは会話履歴を保持し、リアルタイム モデルとの持続的接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと似ていますが、いくつかの重要な違いがあります。詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスをご覧ください。

通常のエージェントとの主な違い:

- モデル選択はエージェントではなくセッション単位で設定します。  
- structured outputs (`outputType`) はサポートされません。  
- 音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。  
- tools、handoffs、instructions などその他の機能は同じ方法で利用できます。  

## セッション設定

### モデル設定

セッション設定では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、音声（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（text / audio）の指定が可能です。入出力の音声フォーマットは PCM16 がデフォルトですが変更できます。

### オーディオ設定

オーディオ設定では音声入力と出力の扱いを制御します。Whisper などのモデルで入力音声を文字起こしし、言語設定やドメイン固有用語の精度向上のための transcription prompts を指定できます。ターン検出の設定により、エージェントが応答を開始・終了するタイミングを制御できます（音声活動検出のしきい値、無音時間、検出前後のパディングなど）。

## ツールと関数

### ツールの追加

通常のエージェントと同様、リアルタイム エージェントでは会話中に実行される function tools をサポートします。

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

ハンドオフにより、会話を専門エージェント間で転送できます。

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

セッションはストリーミングでイベントを生成し、セッションオブジェクトをイテレートすることで受信できます。主なイベントは次のとおりです。

- **audio**: エージェントの応答の raw 音声データ  
- **audio_end**: エージェントの発話が終了  
- **audio_interrupted**: ユーザーがエージェントを割り込み  
- **tool_start / tool_end**: ツール実行の開始・終了  
- **handoff**: エージェントのハンドオフ発生  
- **error**: 処理中にエラー発生  

完全なイベント一覧は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] をご参照ください。

## ガードレール

リアルタイム エージェントでは出力ガードレールのみサポートされます。パフォーマンスを保つため、ガードレールはデバウンスされて定期的（毎文字ではなく）に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接、またはセッションの `run_config` を通じて追加できます。両方から指定した場合は一緒に実行されます。

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

ガードレールが発動すると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンスにより、安全性とリアルタイム性能のバランスを取ります。テキスト エージェントと異なり、リアルタイム エージェントではガードレール発動時に Exception は送出されません。

## オーディオ処理

音声を送信するには [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を、テキストを送信するには [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

音声出力を受信するには `audio` イベントをリッスンし、任意のオーディオライブラリで再生してください。ユーザーが割り込んだ際は `audio_interrupted` イベントを検知して即時に再生を停止し、キューにある音声をクリアする必要があります。

## 直接モデルアクセス

下層のモデルにアクセスしてカスタムリスナーを追加したり、高度な操作を行うことも可能です。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、低レベルで接続を制御する必要がある高度なユースケースに向けて [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

動作する完全なコード例は、UI 付き・無しデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。