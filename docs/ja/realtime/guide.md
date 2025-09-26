---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、後方互換性のない変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する対話フローを可能にします。 OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声対話を実現し、割り込みにもスムーズに対応します。

## アーキテクチャ

### コアコンポーネント

realtime システムは次の主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェント。
-   **RealtimeRunner**: 構成を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一のインタラクションセッション。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは、次のフローに従います。

1. **RealtimeAgent を作成** し、instructions、tools、ハンドオフを設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと構成オプションを指定します。
3. **セッションを開始** します。`await runner.run()` を使用すると RealtimeSession が返されます。
4. `send_audio()` または `send_message()` を使用して、**音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには、音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます。
6. ユーザーがエージェントにかぶせて話す **割り込みへの対応**。現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。詳細な API は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] のリファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェントレベルではなくセッションレベルで構成します。
-   structured outputs は非対応です（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   tools、ハンドオフ、instructions などの他の機能は同様に機能します。

## セッション構成

### モデル設定

セッション構成では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-realtime`）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、サポートするモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方で設定でき、既定は PCM16 です。

### オーディオ設定

オーディオ設定は、セッションが音声の入出力をどのように扱うかを制御します。 Whisper などのモデルを使った入力音声の書き起こし、言語設定、ドメイン固有用語の精度向上のための書き起こしプロンプトを構成できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出した発話の前後パディングなど、エージェントがいつ応答を開始/終了すべきかを制御できます。

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

ハンドオフにより、会話を専門特化したエージェント間で転送できます。

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

セッションは、セッションオブジェクトを反復処理することでリッスンできるイベントをストリーミングします。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始/終了、エージェントのハンドオフ、エラーが含まれます。特にハンドルすべき主要イベントは次のとおりです。

-   **audio**: エージェントの応答からの raw 音声データ
-   **audio_end**: エージェントの発話が完了
-   **audio_interrupted**: ユーザーがエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの完全な詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力 ガードレール のみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` 経由で指定できます。両方のソースのガードレールが併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断できます。デバウンス動作は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキストエージェントと異なり、realtime エージェントはガードレール発火時に Exception をスローしません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意のオーディオライブラリで音声データを再生します。ユーザーがエージェントを割り込んだ際に即座に再生を停止し、キューされた音声をクリアするために、`audio_interrupted` イベントを必ずリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続の低レベル制御が必要な高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

動作する完全なコード例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。 UI コンポーネントあり/なしのデモが含まれます。