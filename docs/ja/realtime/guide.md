---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK のリアルタイム機能を用いて、音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイムエージェントはベータ版です。実装の改善に伴い、互換性に影響する変更が入る可能性があります。

## 概要

リアルタイムエージェントは、音声とテキスト入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API と持続的に接続し、低レイテンシで自然な音声対話や割り込みへの優雅な対応を実現します。

## アーキテクチャ

### コアコンポーネント

リアルタイムシステムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェイス（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイムセッションは次のフローに従います。

1. **RealtimeAgent を作成** し、instructions、tools、ハンドオフを設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと設定オプションを指定します。
3. **セッションを開始** します。`await runner.run()` を使用すると RealtimeSession が返されます。
4. **音声またはテキストメッセージを送信** します。`send_audio()` または `send_message()` を使用します。
5. **イベントをリッスン** します。セッションを反復処理してイベントを受け取ります。イベントには、音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. **割り込みに対応** します。ユーザーがエージェントの発話中に話し始めると、現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、リアルタイムモデルとの持続的な接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつかの重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] のリファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェントレベルではなくセッションレベルで設定します。
-   structured outputs はサポートされません（`outputType` はサポート対象外）。
-   ボイスはエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   それ以外の機能（tools、ハンドオフ、instructions）は同様に機能します。

## セッションの設定

### モデル設定

セッション設定により、基盤となるリアルタイムモデルの動作を制御できます。モデル名（例: `gpt-realtime`）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方に設定可能で、既定は PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように扱うかを制御します。Whisper のようなモデルを用いた入力音声の文字起こし、言語設定、およびドメイン固有用語の精度向上のための文字起こしプロンプトを指定できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出された発話周辺のパディングなど、エージェントが応答を開始・停止するタイミングを調整できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、リアルタイムエージェントは会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、会話を専門特化したエージェント間で引き継ぐことができます。

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

セッションは、セッションオブジェクトを反復処理することでリッスンできるイベントをストリーミングします。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。特に扱うべき主なイベントは以下です。

-   **audio**: エージェントの応答からの raw 音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザーがエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイムエージェントでサポートされるのは出力ガードレールのみです。パフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて指定できます。両方のソースから提供されたガードレールは併せて実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断することがあります。デバウンス動作は、安全性とリアルタイム性能要件のバランスに役立ちます。テキストエージェントと異なり、リアルタイムエージェントはガードレールが作動しても例外を発生させません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンして、任意の音声ライブラリで音声データを再生してください。ユーザーがエージェントを割り込んだ際に即座に再生を停止し、キュー済み音声をクリアするため、`audio_interrupted` イベントも必ず監視してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェイスへ直接アクセスできます。

## コード例

完全な動作コードについては、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。