---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話フローを可能にし、音声およびテキスト入力をリアルタイムに処理し、リアルタイム音声で応答します。OpenAI の Realtime API と永続的な接続を維持し、低レイテンシで自然な音声対話や割り込みへのスムーズな対応ができます。

## アーキテクチャ

### コアコンポーネント

realtime システムは複数の主要コンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェントです。
- **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
- **RealtimeSession**: 単一の対話セッションです。通常は ユーザー が会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
- **RealtimeModel**: 基盤のモデルインターフェース（通常は OpenAI の WebSocket 実装）です。

### セッションフロー

一般的な realtime セッションは次のフローに従います。

1. instructions、tools、handoffs を指定して **RealtimeAgent を作成** します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使って **セッションを開始** し、RealtimeSession を受け取ります。
4. `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントを受信** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントにかぶせて話した場合の **割り込みを処理** します。現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

- モデルの選択はエージェントレベルではなくセッションレベルで設定します。
- structured outputs はサポートされません（`outputType` は未対応）。
- 音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
- その他の機能（tools、handoffs、instructions）は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、対応するモダリティ（テキストや音声）を設定できます。音声フォーマットは入力と出力の両方で設定可能で、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを使った入力音声の文字起こし、言語設定、ドメイン固有用語の精度向上のための文字起こしプロンプトを指定できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出された音声の前後のパディングなどを調整して、エージェントがいつ応答を開始・終了するかを制御します。

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

ハンドオフにより、専門特化したエージェント間で会話を引き継げます。

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

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理することでリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。主に処理すべきイベントは次のとおりです。

- **audio**: エージェントの応答からの raw 音声データ
- **audio_end**: エージェントの発話終了
- **audio_interrupted**: ユーザー によるエージェントの割り込み
- **tool_start/tool_end**: ツール実行のライフサイクル
- **handoff**: エージェントのハンドオフが発生
- **error**: 処理中にエラーが発生

完全なイベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力用の ガードレール のみがサポートされます。これらのガードレールはデバウンスされ、リアルタイム生成時のパフォーマンス問題を避けるために（すべての単語ごとではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて指定できます。両方のソースからのガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンス動作により、安全性とリアルタイムのパフォーマンス要件のバランスをとります。テキストエージェントと異なり、realtime エージェントはガードレールが発動しても **Exception** をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで再生します。ユーザー がエージェントを割り込んだ際に即時に再生を停止し、キューにある音声をクリアするため、`audio_interrupted` イベントを必ずリッスンしてください。

## 直接モデルアクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり、高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケースに向けて、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

完全な動作する code examples は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI コンポーネントの有無それぞれのデモが含まれます。