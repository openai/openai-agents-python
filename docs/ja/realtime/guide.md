---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK のリアルタイム機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改善に伴い、互換性のない変更が発生する可能性があります。

## 概要

リアルタイム エージェントは、会話フローを可能にし、音声とテキスト入力をリアルタイムで処理し、リアルタイム音声で応答します。OpenAI の Realtime API と永続接続を維持し、低レイテンシで自然な音声対話を実現し、中断にもスムーズに対応します。

## アーキテクチャ

### 中核コンポーネント

リアルタイム システムは、いくつかの重要なコンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェント。
- **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
- **RealtimeSession**: 単一の対話セッション。通常、ユーザーが会話を開始するたびに 1 つ作成し、会話が終わるまで維持します。
- **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイム セッションは次のフローに従います。

1. instructions、tools、ハンドオフを指定して **RealtimeAgent を作成** します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使って **セッションを開始** し、RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使って **音声またはテキスト メッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話にかぶせたときの **割り込み処理** を行います。現在の音声生成は自動的に停止します。

セッションは会話履歴を維持し、リアルタイム モデルとの永続接続を管理します。

## エージェントの設定

RealtimeAgent は、通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

- モデルの選択はエージェント レベルではなくセッション レベルで設定します。
- structured output のサポートはありません（`outputType` はサポートされません）。
- 音声はエージェントごとに設定できますが、最初にエージェントが話し始めた後は変更できません。
- ツール、ハンドオフ、instructions などの他の機能は同じように動作します。

## セッションの設定

### モデル設定

セッション設定では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（`gpt-realtime` など）、音声（alloy、echo、fable、onyx、nova、shimmer）の選択、サポートするモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声入出力をどのように扱うかを制御します。Whisper のようなモデルを使った入力音声の書き起こし、言語設定、専門用語の精度向上のための書き起こしプロンプトを設定できます。ターン検出設定では、エージェントがいつ応答を開始・停止するかを制御し、音声活動検出のしきい値、無音時間、検出音声の前後のパディングなどを指定できます。

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

ハンドオフにより、専門化されたエージェント間で会話を引き継げます。

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

セッションはイベントをストリーミングし、セッション オブジェクトを反復処理してリッスンできます。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。特にハンドルすべき主なイベントは次のとおりです。

- **audio**: エージェントの応答からの Raw 音声データ
- **audio_end**: エージェントが話し終えた
- **audio_interrupted**: ユーザーがエージェントを割り込んだ
- **tool_start/tool_end**: ツール実行のライフサイクル
- **handoff**: エージェントのハンドオフが発生
- **error**: 処理中にエラーが発生

詳細なイベント情報は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力ガードレールのみです。パフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースのガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断できます。デバウンス処理により、安全性とリアルタイム性能要件のバランスを取ります。テキスト エージェントと異なり、リアルタイム エージェントはガードレールがトリップしても Exception を送出しません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで音声データを再生してください。ユーザーがエージェントを割り込んだときに即座に再生を停止し、キューにある音声をクリアするため、`audio_interrupted` イベントを必ずリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタム リスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作するコード例については、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。