---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、互換性のない変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話フローを可能にし、音声とテキスト入力をリアルタイムに処理して realtime 音声で応答します。 OpenAI の Realtime API と永続的に接続を維持し、低レイテンシで自然な音声会話と、割り込みに対する優雅なハンドリングを実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムはいくつかの主要コンポーネントで構成されます:

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェントです。
-   **RealtimeRunner**: 構成を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、 ユーザー が会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（一般的には OpenAI の WebSocket 実装）です。

### セッションフロー

一般的な realtime セッションは次のフローに従います:

1. **RealtimeAgent を作成** し、instructions、tools、ハンドオフを設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと構成オプションを指定します。
3. **セッションを開始** `await runner.run()` を使用して開始し、RealtimeSession が返されます。
4. **音声またはテキスト メッセージを送信** `send_audio()` または `send_message()` でセッションへ送信します。
5. **イベントをリッスン** セッションをイテレートしてイベントを受け取ります。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. **割り込みを処理** ユーザー がエージェントの発話に被せたとき、進行中の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。完全な API 詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの差分:

-   モデルの選択はエージェント レベルではなく、セッション レベルで構成します。
-   structured output のサポートはありません（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが発話した後に変更することはできません。
-   それ以外の機能（tools、ハンドオフ、instructions）は同様に動作します。

## セッション設定

### モデル設定

セッション構成では、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-4o-realtime-preview` など）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力・出力の両方に設定でき、既定は PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを使った入力音声の文字起こし、言語設定、ドメイン固有用語の精度向上のための文字起こしプロンプトを設定できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出音声の前後パディングなどにより、エージェントが応答を開始・停止すべきタイミングを制御します。

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

ハンドオフにより、専門特化したエージェント間で会話を移譲できます。

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

セッションは、セッション オブジェクトをイテレートすることでリッスンできるイベントを ストリーミング します。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。特にハンドリングすべき主なイベントは次のとおりです:

-   **audio**: エージェントの応答からの raw オーディオ データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザー による割り込みを検知
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェント間のハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力ガードレールのみです。パフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、リアルタイム生成中に（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` から提供できます。両方のソースからのガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込むことがあります。デバウンスの動作により、安全性とリアルタイム性能要件のバランスをとります。テキスト エージェントと異なり、realtime エージェントはガードレールにかかった場合でも Exception を送出しません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用してセッションに音声を送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意のオーディオ ライブラリで音声データを再生します。ユーザー がエージェントを割り込んだ際に即時再生停止とキュー済み音声のクリアを行うため、`audio_interrupted` イベントを必ずリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスし、カスタム リスナーの追加や高度な操作を実行できます:

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルに制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作するサンプルは、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。