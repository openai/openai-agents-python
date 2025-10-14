---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改善に伴い、破壊的変更が入る場合があります。

## 概要

リアルタイム エージェントは会話フローを可能にし、音声とテキストの入力をリアルタイムで処理し、リアルタイム音声で応答します。 OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声対話や、割り込みへのスムーズな対応を実現します。

## アーキテクチャ

### コアコンポーネント

リアルタイム システムはいくつかの重要なコンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、 ユーザー が会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイム セッションは次のフローに従います。

1. **RealtimeAgent を作成** し、instructions、tools、ハンドオフを設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと設定オプションを指定します。
3. `await runner.run()` を使って **セッションを開始** し、RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使って **音声またはテキスト メッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントの発話にかぶせて話した場合に **割り込みを処理** します。これにより現在の音声生成が自動的に停止します。

セッションは会話履歴を保持し、リアルタイム モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。 API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] のリファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデル選択はエージェント レベルではなくセッション レベルで設定します。
-   structured outputs はサポートされません（`outputType` はサポートされません）。
-   ボイスはエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   tools、ハンドオフ、instructions などの他の機能は同様に機能します。

## セッション設定

### モデル設定

セッション設定では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイスの選択（alloy、echo、fable、onyx、nova、shimmer）、サポートするモダリティ（テキストおよび/または音声）を構成できます。音声フォーマットは入力と出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように処理するかを制御します。Whisper などのモデルを使用した入力音声の書き起こしを設定し、言語設定や、専門用語の精度を高めるための書き起こしプロンプトを指定できます。ターン検出の設定により、ボイスアクティビティのしきい値、無音時間、検出された発話前後のパディングなどのオプションで、エージェントがいつ応答を開始・停止すべきかを制御します。

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

ハンドオフにより、専門化されたエージェント間で会話を引き継ぐことができます。

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

セッションは、セッションオブジェクトを反復処理することでリッスン可能なイベントをストリーム配信します。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。主に処理すべきイベントは次のとおりです。

-   **audio** : エージェントの応答からの raw 音声データ
-   **audio_end** : エージェントの発話の終了
-   **audio_interrupted** : ユーザー がエージェントを割り込み
-   **tool_start/tool_end** : ツール実行のライフサイクル
-   **handoff** : エージェントのハンドオフが発生
-   **error** : 処理中にエラーが発生

イベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力 ガードレール のみです。パフォーマンス問題を避けるため、これらの ガードレール はデバウンスされ、（単語ごとではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、構成可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` で指定できます。両方の経路からの ガードレール は一緒に実行されます。

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

ガードレール がトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込むことがあります。デバウンスの挙動により、安全性とリアルタイムのパフォーマンス要件のバランスを取ります。テキスト エージェントとは異なり、リアルタイム エージェントは ガードレール が作動しても Exception をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストをセッションに送信します。

音声出力については、`audio` イベントをリッスンし、好みの音声ライブラリで音声データを再生します。ユーザー がエージェントを割り込んだ際に即座に再生を停止し、キューにある音声をクリアするため、`audio_interrupted` イベントを必ずリッスンしてください。

## 直接モデルアクセス

基盤となるモデルにアクセスして、カスタム リスナーを追加したり高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルに制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

完全な動作する code examples は、UI コンポーネントの有無それぞれのデモを含む [examples/realtime directory](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。