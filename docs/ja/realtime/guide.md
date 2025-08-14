---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を使用して音声対応 AI エージェントを構築する方法を詳細に説明します。

!!! warning "Beta 機能"
Realtime エージェントは Beta 版です。実装改善に伴い、破壊的変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキスト入力をリアルタイムで処理し、音声で応答する会話フローを実現します。OpenAI の Realtime API と持続的に接続し、低レイテンシで自然な音声対話を提供し、割り込みにもスムーズに対応できます。

## アーキテクチャ

### 主要コンポーネント

realtime システムは、次の重要なコンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント。  
- **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得します。  
- **RealtimeSession**: 単一の対話セッション。通常、ユーザーが会話を開始するたびに作成し、会話終了まで保持します。  
- **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）  

### セッションフロー

典型的な realtime セッションは次の流れで進みます。

1. **RealtimeAgent を作成**: instructions、tools、handoffs を設定します。  
2. **RealtimeRunner をセットアップ**: エージェントと構成オプションを渡します。  
3. **セッション開始**: `await runner.run()` を実行して `RealtimeSession` を取得します。  
4. **音声またはテキスト送信**: `send_audio()` または `send_message()` でセッションに送信します。  
5. **イベント受信**: セッションをイテレートしてイベントを受信します。イベントには音声出力、文字起こし、tool 呼び出し、handoff、エラーなどがあります。  
6. **割り込み処理**: ユーザーがエージェントの発話を遮った場合、自動的に音声生成を停止します。  

セッションは会話履歴を保持し、realtime モデルとの持続的接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと似ていますが、いくつか重要な違いがあります。詳細な API は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] を参照してください。

主な違い:

- モデル選択はエージェントではなくセッションレベルで設定します。  
- structured outputs (`outputType`) はサポートされていません。  
- Voice はエージェント単位で設定できますが、最初のエージェントが発話した後に変更できません。  
- tools、handoffs、instructions などその他の機能は同じように動作します。  

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、Voice 選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキスト／音声）を設定できます。音声の入出力フォーマットは PCM16 がデフォルトですが変更可能です。

### 音声設定

音声設定では、音声入力と出力の取り扱いを指定します。Whisper などのモデルで入力音声を文字起こしし、言語や専門用語向けの transcription prompt を設定できます。ターン検出設定では、音声アクティビティ検出の閾値、無音時間、検出された音声前後のパディングなど、エージェントが応答を開始・停止する条件を制御します。

## Tools と Functions

### Tools の追加

通常のエージェントと同様に、realtime エージェントでも会話中に実行する function tools をサポートします。

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

handoffs により、会話を専門化されたエージェント間で引き継ぐことができます。

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

セッションはイベントをストリーム配信し、セッションオブジェクトをイテレートして受信できます。主なイベントは以下のとおりです。

- **audio**: エージェント応答の raw 音声データ  
- **audio_end**: エージェントの発話終了  
- **audio_interrupted**: ユーザーがエージェントを割り込み  
- **tool_start/tool_end**: tool 実行ライフサイクル  
- **handoff**: エージェント間の handoff  
- **error**: 処理中にエラー発生  

完全なイベント定義は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでは output ガードレールのみサポートします。パフォーマンスへの影響を抑えるため、単語ごとではなくデバウンスして定期的に実行されます。デフォルトのデバウンス長は 100 文字で、設定可能です。

ガードレールは `RealtimeAgent` に直接付与するか、セッションの `run_config` で指定できます。両方に設定した場合は併せて実行されます。

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

ガードレールが発火すると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンス動作により、安全性とリアルタイム性能のバランスを保ちます。テキストエージェントと異なり、realtime エージェントではガードレール発火時に Exception は発生しません。

## 音声処理

音声は [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] で送信し、テキストは [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] で送信します。

音声出力を受信するには `audio` イベントを監視し、お好みの音声ライブラリで再生してください。`audio_interrupted` イベントを監視して、ユーザーが割り込んだ際に即座に再生を停止し、キューにある音声をクリアするようにしてください。

## 直接的なモデルアクセス

カスタムリスナーの追加や高度な操作を行うため、基盤モデルへ直接アクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスでき、低レベルの接続制御が必要な高度なユースケースに対応できます。

## 例

動作する完全な例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI あり／なしのデモを含みます。