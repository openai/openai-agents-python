---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて、音声対応 AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、一部に破壊的変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する対話フローを可能にします。OpenAI の Realtime API との永続接続を維持し、低レイテンシで自然な音声対話と、中断へのスムーズな対応を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは次の主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェント。
-   **RealtimeRunner**: 構成を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、ユーザーが会話を開始するたびに 1 つ作成し、会話が終了するまで保持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（一般的には OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは次の流れに従います。

1. instructions、tools、ハンドオフを指定して **RealtimeAgent を作成** します。
2. エージェントと構成オプションを指定して **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使用して **セッションを開始** し、RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントを受信** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話にかぶせた場合の **中断処理** を行います。現在の音声生成は自動で停止します。

セッションは会話履歴を維持し、realtime モデルとの永続接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスに似ていますが、いくつか重要な違いがあります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご参照ください。

通常のエージェントとの主な相違点:

-   モデルの選択はエージェントレベルではなくセッションレベルで構成します。
-   structured outputs は未対応（`outputType` はサポートされません）。
-   音声はエージェントごとに構成できますが、最初のエージェントが話し始めた後は変更できません。
-   その他の機能（tools、ハンドオフ、instructions）は同様に動作します。

## セッション構成

### モデル設定

セッション構成では、基盤の realtime モデルの動作を制御できます。モデル名（例: `gpt-realtime`）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、および対応モダリティ（テキストおよび/または音声）を構成可能です。音声フォーマットは入力と出力の両方に設定でき、デフォルトは PCM16 です。

### 音声構成

音声設定では、セッションが音声入力と出力をどのように処理するかを制御します。Whisper のようなモデルを使った入力音声の文字起こし、言語設定、ドメイン特有の用語に対する精度向上のための文字起こしプロンプトを構成できます。ターン検出の設定では、音声活動の検出しきい値、無音時間、検出された音声の前後パディングなど、エージェントが応答を開始・停止すべきタイミングを制御します。

## ツールと関数

### ツールの追加

通常のエージェントと同様、realtime エージェントは会話中に実行される関数ツールをサポートします。

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

ハンドオフにより、会話を専門化されたエージェント間で転送できます。

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

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理してリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始/終了、エージェントのハンドオフ、エラーが含まれます。特にハンドルすべきイベントは次のとおりです。

-   **audio**: エージェントの応答からの生の音声データ
-   **audio_end**: エージェントの発話が完了
-   **audio_interrupted**: ユーザーがエージェントを中断
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力ガードレールのみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるため、（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースからのガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンスの動作により、安全性とリアルタイム性能要件のバランスを取ります。テキストエージェントと異なり、realtime エージェントはガードレールが作動しても **Exception** を送出しません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストをセッションに送信します。

音声出力については `audio` イベントをリッスンし、任意の音声ライブラリで音声データを再生してください。ユーザーがエージェントを中断した際に即座に再生を停止し、キュー済みの音声をクリアするため、`audio_interrupted` イベントも必ずリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスし、カスタムリスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルに制御する必要がある高度なユースケースに対して、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作するコード例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI コンポーネントの有無それぞれのデモが含まれています。