---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いた音声対応 AI エージェントの構築について詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的な変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話フローを実現し、音声とテキストの入力をリアルタイムに処理して realtime 音声で応答します。OpenAI の Realtime API との永続接続を維持し、低遅延で自然な音声会話と、割り込みへのスムーズな対応を可能にします。

## アーキテクチャ

### 中核コンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます:

-   **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、ユーザーが会話を開始するたびに作成し、会話が完了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェースです (通常は OpenAI の WebSocket 実装)。

### セッションフロー

一般的な realtime セッションは次のフローに従います:

1. instructions、tools、handoffs を用いて **RealtimeAgent を作成** します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使用して **セッションを開始** し、RealtimeSession が返されます。
4. `send_audio()` または `send_message()` を使って **音声またはテキストのメッセージを送信** します。
5. セッションを反復処理して **イベントを監視** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます。
6. ユーザーがエージェントの発話にかぶせて話した場合の **割り込みを処理** します。これにより、現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスとほぼ同様に動作しますが、いくつか重要な違いがあります。API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェントレベルではなくセッションレベルで設定します。
-   structured outputs のサポートはありません ( `outputType` は未対応です )。
-   音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   tools、handoffs、instructions などのその他の機能は同様に動作します。

## セッションの設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名 (例: `gpt-4o-realtime-preview`) の設定、音声の選択 ( alloy、echo、fable、onyx、nova、shimmer )、およびサポートするモダリティ (テキストや音声) を構成できます。音声フォーマットは入力・出力の両方に対して指定でき、既定値は PCM16 です。

### オーディオ設定

オーディオ設定では、セッションが音声の入出力をどのように扱うかを制御します。Whisper などのモデルを用いた入力音声の書き起こし、言語設定、専門用語の精度を高めるための書き起こしプロンプトを指定できます。ターン検出の設定では、音声活動検出 (VAD) のしきい値、無音時間、検出された発話の前後パディングなどにより、エージェントがいつ応答を開始・終了すべきかを制御します。

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

ハンドオフにより、専門特化したエージェント間で会話を転送できます。

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

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理することで監視できます。イベントには音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーなどが含まれます。主要なイベントは次のとおりです:

-   **audio**: エージェントの応答からの raw オーディオデータ
-   **audio_end**: エージェントの発話が完了
-   **audio_interrupted**: ユーザーがエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでサポートされるのは出力ガードレールのみです。パフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、リアルタイム生成中に毎単語ではなく一定間隔で実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` 経由で提供できます。両方の経路からのガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断する場合があります。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキストエージェントと異なり、realtime エージェントはガードレール作動時に Exception をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントを監視し、任意の音声ライブラリで再生してください。ユーザーがエージェントを割り込んだ際にはすぐに再生を停止し、キューにある音声をクリアするために `audio_interrupted` イベントを必ず監視してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーの追加や高度な操作を実行できます:

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケースに向けて、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

完全な動作する code examples は、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。