---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、リアルタイムで音声とテキスト入力を処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声対話と、割り込みへのスムーズな対応を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは次の主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、ユーザー が会話を開始するたびに作成し、会話が完了するまで維持します。
-   **RealtimeModel**: 基盤のモデルインターフェース（一般的には OpenAI の WebSocket 実装）

### セッションフロー

典型的な realtime セッションは次の流れに従います。

1. **RealtimeAgent を作成** し、instructions、tools、ハンドオフを設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと設定オプションを指定します。
3. `await runner.run()` を用いて **セッションを開始** し、RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントの発話に被せて話した際の **割り込み処理** を行います。これにより現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続的な接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェント レベルではなくセッション レベルで設定します。
-   structured outputs はサポートされません（`outputType` は未対応）。
-   ボイスはエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   その他の機能（tools、ハンドオフ、instructions）は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、および対応モダリティ（テキストや音声）を設定できます。音声フォーマットは入力と出力の両方で設定でき、既定は PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように扱うかを制御します。Whisper のようなモデルを使用した入力音声の文字起こし、言語設定、ドメイン固有用語の精度を高めるための文字起こしプロンプトを構成できます。発話区間検出の設定により、エージェントがいつ応答を開始・停止すべきかを制御でき、音声活動検出のしきい値、無音継続時間、検出された発話の前後のパディングを指定できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントは会話中に実行される 関数ツール をサポートします。

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

## イベントハンドリング

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理してこれらをリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。特にハンドルすべき主要イベントは以下です。

-   **audio**: エージェントの応答からの raw 音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザー がエージェントを割り込み
-   **tool_start/tool_end**: ツール実行ライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでは出力の ガードレール のみがサポートされます。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるため、（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースからのガードレールは併用されて実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンスの挙動は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキスト エージェントと異なり、realtime エージェントはガードレールがトリップしても例外をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで音声データを再生してください。ユーザー がエージェントを割り込んだ際に即座に再生を停止し、キュー済み音声をクリアするため、`audio_interrupted` イベントも必ず監視してください。

## モデルへの直接アクセス

基盤のモデルにアクセスして、カスタムリスナーの追加や高度な操作を行えます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルで制御する必要がある高度なユースケースに対して、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

完全な動作する code examples は、UI コンポーネントの有無のデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。