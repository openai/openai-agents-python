---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェント を構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェント はベータ版です。実装の改善に伴い、互換性のない変更が発生する可能性があります。

## 概要

Realtime エージェント は、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API と永続接続を維持し、低レイテンシで自然な音声会話を提供し、割り込みにも適切に対応します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、次の主要コンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、handoffs で構成された エージェント。
- **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
- **RealtimeSession**: 単一の対話セッション。通常、ユーザー が会話を開始するたびに作成し、会話が完了するまで維持します。
- **RealtimeModel**: 基盤となるモデルのインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは次のフローに従います。

1. instructions、tools、handoffs を用いて **RealtimeAgent を作成** します。
2. エージェントと構成オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使用して **セッションを開始** します。これにより RealtimeSession が返されます。
4. `send_audio()` または `send_message()` を使用して **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントの発話に被せた際の **割り込みを処理** します。現在の音声生成は自動で停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつかの重要な違いがあります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

- モデル選択はエージェント レベルではなくセッション レベルで設定します。
- structured output のサポートはありません（`outputType` はサポートされません）。
- 音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
- tools、handoffs、instructions などの他の機能は同様に動作します。

## セッション構成

### モデル設定

セッション構成では、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-realtime` など）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、およびサポートされるモダリティ（テキストや音声）を設定できます。音声フォーマットは入力と出力の両方に設定でき、既定は PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように扱うかを制御します。Whisper のようなモデルを使った入力音声の書き起こし、言語設定、ドメイン固有の用語の精度を高めるための書き起こしプロンプトを設定できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出された発話の前後のパディングなど、エージェント がいつ応答を開始・終了すべきかを制御できます。

## ツールと関数

### ツールの追加

通常の エージェント と同様に、realtime エージェント は会話中に実行される 関数ツール をサポートします。

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

ハンドオフ により、専門特化した エージェント 間で会話を転送できます。

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

セッションは、セッションオブジェクトを反復処理することでリッスンできるイベントを ストリーミング します。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェント のハンドオフ、エラーが含まれます。主に処理すべきイベントは次のとおりです。

- **audio**: エージェント の応答からの raw 音声データ
- **audio_end**: エージェント の発話が完了
- **audio_interrupted**: ユーザー がエージェント を割り込んだ
- **tool_start/tool_end**: ツール実行のライフサイクル
- **handoff**: エージェント のハンドオフが発生
- **error**: 処理中にエラーが発生

完全なイベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェント でサポートされるのは出力 ガードレール のみです。これらの ガードレール はデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレール は `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースからの ガードレール は一緒に実行されます。

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

ガードレール がトリガーされると、`guardrail_tripped` イベントを生成し、エージェント の現在の応答を中断することがあります。デバウンスの動作により、安全性とリアルタイム性能要件のバランスを取ります。テキスト エージェント と異なり、realtime エージェント は ガードレール が作動しても Exception をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、好みのオーディオライブラリを通じて音声データを再生します。ユーザー が エージェント を割り込んだ場合に即座に再生を停止し、キューにある音声をクリアするために、`audio_interrupted` イベントも必ずリッスンしてください。

## 直接モデルアクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

完全に動作するサンプルは、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。