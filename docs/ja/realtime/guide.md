---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を用いて音声対応の AI エージェントを構築する方法を詳しく解説します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改善に伴い、後方互換性のない変更が発生する場合があります。

## 概要

リアルタイム エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを実現します。 OpenAI の Realtime API への永続的な接続を維持し、低遅延で自然な音声対話と、割り込みへの柔軟な対応を可能にします。

## アーキテクチャ

### コアコンポーネント

このリアルタイム システムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されるエージェントです。
-   **RealtimeRunner**: 構成を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、ユーザーが会話を開始するたびに作成し、会話が完了するまで維持します。
-   **RealtimeModel**: 基盤となるモデル インターフェース（一般的には OpenAI の WebSocket 実装）

### セッション フロー

一般的なリアルタイム セッションの流れは次のとおりです。

1.  **RealtimeAgent を作成** します。instructions、tools、ハンドオフを指定します。
2.  **RealtimeRunner をセットアップ** します。エージェントと構成オプションを渡します。
3.  **セッションを開始** します。`await runner.run()` を使うと RealtimeSession が返ります。
4.  **音声またはテキスト メッセージを送信** します。`send_audio()` または `send_message()` を使用します。
5.  **イベントを待ち受け** ます。セッションを反復処理して受け取り、イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6.  **割り込みを処理** します。ユーザーがエージェントにかぶせて話した場合、進行中の音声生成は自動的に停止します。

セッションは会話履歴を保持し、リアルタイム モデルとの永続接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスとほぼ同様に動作しますが、いくつかの重要な違いがあります。詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスをご覧ください。

通常のエージェントとの主な相違点:

-   モデルの選択はエージェント レベルではなくセッション レベルで設定します。
-   structured outputs のサポートはありません（`outputType` は未対応）。
-   音声（ボイス）はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   それ以外の機能（tools、ハンドオフ、instructions など）は同様に動作します。

## セッション構成

### モデル設定

セッション構成では、基盤となるリアルタイム モデルの挙動を制御できます。モデル名（例：`gpt-4o-realtime-preview`）、ボイスの選択（ alloy、echo、fable、onyx、nova、shimmer ）、対応モダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力・出力の双方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声の入出力をどのように扱うかを制御します。Whisper などのモデルを使った入力音声の文字起こし、言語設定、専門用語の精度向上のための文字起こしプロンプトを指定できます。ターン検出設定では、エージェントがいつ応答を開始・停止すべきかを制御し、音声活動検出のしきい値、無音の長さ、検出された発話の前後のパディングなどを設定できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、リアルタイム エージェントは会話中に実行される関数ツールをサポートします:

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

ハンドオフにより、専門化したエージェント間で会話を引き継げます。

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

セッションはイベントをストリーミングし、セッション オブジェクトを反復処理して待ち受けできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーなどが含まれます。特に扱うべき主要なイベントは次のとおりです。

-   **audio**: エージェントの応答からの生の音声データ
-   **audio_end**: エージェントの発話終了
-   **audio_interrupted**: ユーザーがエージェントに割り込み
-   **tool_start/tool_end**: ツール実行ライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

完全なイベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力用のガードレールのみです。ガードレールはデバウンスされ、リアルタイム生成時のパフォーマンス問題を避けるために定期的（単語ごとではなく）に実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` で指定できます。両方の経路から与えたガードレールは併用されます。

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

ガードレールが発火すると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンスの挙動により、安全性とリアルタイム性能要件のバランスを取ります。テキスト エージェントと異なり、リアルタイム エージェントはガードレール作動時に例外（Exception）を発生させることは **ありません**。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] でセッションに音声を送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] でテキストを送信します。

音声出力については、`audio` イベントを待ち受けて任意の音声ライブラリで再生します。ユーザーがエージェントに割り込んだ際には、すぐに再生を停止しキュー済みの音声をクリアできるよう、`audio_interrupted` イベントも必ず監視してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタム リスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルに制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作するコード例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI コンポーネントの有無それぞれのデモが含まれています。