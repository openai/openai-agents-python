---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的な変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話フローを実現し、音声およびテキスト入力をリアルタイムに処理して、リアルタイム音声で応答します。 OpenAI の Realtime API への永続的な接続を維持し、低遅延で自然な音声会話と、割り込みへの優雅な対応を可能にします。

## アーキテクチャ

### コアコンポーネント

realtime システムはいくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すことでセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常は ユーザー が会話を開始するたびに作成し、会話が完了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

典型的な realtime セッションは次のフローに従います。

1. instructions、tools、handoffs を用いて **RealtimeAgent を作成** します。
2. エージェントと設定オプションを使って **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使用して **セッションを開始** し、 RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使用して **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、トランスクリプト、ツール呼び出し、ハンドオフ、エラーなどが含まれます。
6. ユーザー がエージェントの発話に被せて話す場合の **割り込みに対応** します。現在の音声生成は自動的に停止します。

セッションは会話履歴を維持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。 API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデル選択はエージェントレベルではなくセッションレベルで設定します。
-   structured outputs はサポートしません（`outputType` はサポート対象外です）。
-   音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   その他の機能（tools、handoffs、instructions）は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-realtime` など）、音声選択（alloy、echo、fable、onyx、nova、shimmer）、およびサポートするモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように扱うかを制御します。 Whisper などのモデルを使用した入力音声の文字起こし、言語設定、ドメイン固有用語の精度向上のためのトランスクリプションプロンプトの指定が可能です。ターン検出設定では、エージェントがいつ応答を開始・停止するかを制御し、音声活動検出のしきい値、無音時間、検出された発話の前後のパディングなどのオプションがあります。

## ツールと関数

### ツールの追加

通常のエージェント同様、realtime エージェントは会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、会話を専門化されたエージェント間で引き継げます。

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

セッションは、セッションオブジェクトの反復処理でリッスンできるイベントをストリーミングします。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーなどが含まれます。特に対応すべき主なイベントは次のとおりです。

-   **audio**: エージェントの応答からの生の音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザー によるエージェントの割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力の ガードレール のみです。パフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、（単語ごとではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` で提供できます。両方のソースからのガードレールは併せて実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込むことがあります。デバウンス動作は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキストエージェントと異なり、realtime エージェントはガードレールがトリップしても **Exception を発生させません**。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声を、または [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストをセッションに送信します。

音声出力については、`audio` イベントをリッスンして、任意の音声ライブラリで音声データを再生します。ユーザー がエージェントを割り込んだ際にすぐに再生を停止し、キューにある音声をクリアできるよう、`audio_interrupted` イベントを必ずリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり、高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作コード例は、 UI コンポーネントあり/なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。