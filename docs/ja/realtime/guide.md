---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK のリアルタイム機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改善に伴い破壊的変更が入る可能性があります。

## 概要

リアルタイム エージェントは、会話フローを可能にし、音声とテキストの入力をリアルタイムで処理し、リアルタイム音声で応答します。OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声対話と、割り込みへの優雅な対応を実現します。

## アーキテクチャ

### コアコンポーネント

リアルタイム システムは、次の主要なコンポーネントで構成されます。

-   **RealtimeAgent**: `instructions`、`tools`、`handoffs` を設定したエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、ユーザーが会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイム セッションは次のフローに従います。

1. **RealtimeAgent を作成** し、`instructions`、`tools`、`handoffs` を設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと設定オプションを渡します。
3. `await runner.run()` を使って **セッションを開始** し、RealtimeSession を受け取ります。
4. `send_audio()` または `send_message()` を使用して **音声またはテキスト メッセージを送信** します。
5. セッションをイテレートして **イベントをリッスン** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話に被せて話したときに発生する **割り込みに対応** します。これにより現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、リアルタイム モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な相違点があります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェント レベルではなく、セッション レベルで設定します。
-   structured output のサポートはありません（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   ツール、ハンドオフ、instructions など、その他の機能は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（例: `gpt-realtime`）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、サポートするモダリティ（テキストおよび/または音声）を設定できます。音声のフォーマットは入力・出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声の入出力をどのように扱うかを制御します。Whisper などのモデルを使用した入力音声の文字起こし、言語設定、ドメイン特有の用語に対する精度を高めるための書き起こしプロンプトを指定できます。ターン検出の設定により、音声活動検出の閾値、無音時間、検出された音声の前後パディングなどを通じて、エージェントがいつ応答を開始・終了すべきかを制御できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、リアルタイム エージェントは会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、会話を専門化されたエージェント間で移譲できます。

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

セッションはストリーミングでイベントを配信し、セッション オブジェクトをイテレートしてリッスンできます。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。特に次のイベントをハンドルしてください。

-   **audio**: エージェントの応答からの raw な音声データ
-   **audio_end**: エージェントが話し終えました
-   **audio_interrupted**: ユーザーがエージェントを割り込みました
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生しました
-   **error**: 処理中にエラーが発生しました

完全なイベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力 ガードレール のみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースからのガードレールは併せて実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を割り込むことがあります。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキスト エージェントとは異なり、リアルタイム エージェントはガードレールに引っかかっても例外をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで音声データを再生します。ユーザーがエージェントを割り込んだ際に即座に再生を停止し、キューにある音声をクリアできるよう、`audio_interrupted` イベントを必ずリッスンしてください。

## 直接的なモデルアクセス

基盤となるモデルにアクセスして、カスタム リスナーを追加したり高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作するサンプルは、UI コンポーネントあり/なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。