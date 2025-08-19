---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話型のフローを可能にし、音声およびテキスト入力をリアルタイムに処理して、リアルタイム音声で応答します。OpenAI の Realtime API と持続的な接続を維持し、低レイテンシで自然な音声対話と、割り込みへのスムーズな対応を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、以下の主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。一般に、ユーザーが会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは、次のフローに従います。

1. **RealtimeAgent を作成** し、instructions、tools、handoffs を設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと設定オプションを渡します。
3. **セッションを開始** します。`await runner.run()` を使用すると RealtimeSession が返ります。
4. **音声またはテキストメッセージを送信** します。`send_audio()` または `send_message()` を使用します。
5. **イベントをリッスン** します。セッションを反復処理して、音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどのイベントを受け取ります。
6. **割り込みに対応** します。ユーザーがエージェントの発話にかぶせた場合、現在の音声生成が自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの持続的な接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつかの重要な相違点があります。API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] のリファレンスをご覧ください。

通常のエージェントとの差分:

-   モデルの選択はエージェントレベルではなく、セッションレベルで設定します。
-   structured outputs は非対応です（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   それ以外の機能（tools、handoffs、instructions）は同じように動作します。

## セッションの設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、および対応モダリティ（text および/または audio）を設定できます。音声の入出力フォーマットは設定可能で、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声の入出力をどのように扱うかを制御します。Whisper などのモデルを用いた入力音声の文字起こし、言語設定、ドメイン固有用語の精度を高めるための文字起こし用プロンプトを設定できます。ターン検出の設定では、エージェントがいつ応答を開始・停止するかを制御でき、音声活動検出のしきい値、無音時間、検出された発話の前後のパディングなどのオプションがあります。

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

ハンドオフにより、特化したエージェント間で会話を引き継ぐことができます。

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

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理することでリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーなどが含まれます。特に扱うべき主なイベントは次のとおりです。

-   **audio**: エージェントの応答からの raw 音声データ
-   **audio_end**: エージェントの発話が完了
-   **audio_interrupted**: ユーザーがエージェントを割り込んだ
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでサポートされるのは出力 ガードレール のみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` 経由で提供できます。両方のソースからのガードレールは併せて実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断できます。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキストエージェントと異なり、realtime エージェントはガードレールがトリップしても例外をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで音声データを再生します。ユーザーがエージェントを割り込んだ際に即座に再生を停止し、キューの音声をクリアできるよう、`audio_interrupted` イベントを必ず監視してください。

## モデルへの直接アクセス

基盤となるモデルへアクセスして、カスタムリスナーを追加したり、高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

動作する完全な code examples は、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。