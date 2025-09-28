---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を使用して音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する場合があります。

## 概要

リアルタイム エージェントは、音声とテキスト入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API と持続的な接続を維持し、低レイテンシで自然な音声会話と、割り込みへの優雅な対応を実現します。

## アーキテクチャ

### 中核コンポーネント

リアルタイム システムは、いくつかの主要コンポーネントで構成されます。

- **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント。
- **RealtimeRunner**: 構成を管理します。`runner.run()` を呼ぶとセッションを取得できます。
- **RealtimeSession**: 単一の対話セッション。通常は ユーザー が会話を開始するたびに作成し、会話が終了するまで維持します。
- **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイム セッションは次のフローに従います。

1.  **Create your RealtimeAgent(s)** に instructions、tools、handoffs を設定します。
2.  エージェントと構成オプションで **RealtimeRunner をセットアップ** します。
3.  `await runner.run()` を使用して **セッションを開始** し、RealtimeSession を受け取ります。
4.  `send_audio()` または `send_message()` を使用して **音声またはテキスト メッセージを送信** します。
5.  セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6.  ユーザー がエージェントにかぶせて話した場合の **割り込み処理** を行います。現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、リアルタイム モデルとの持続的な接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスをご覧ください。

通常のエージェントとの主な相違点:

- モデルの選択はエージェント レベルではなくセッション レベルで設定します。
- structured output はサポートされません（`outputType` はサポートされません）。
- 音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
- tools、ハンドオフ、instructions など他の機能は同様に動作します。

## セッション構成

### モデル設定

セッション構成では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイス選択（alloy, echo, fable, onyx, nova, shimmer）、および対応するモダリティ（テキストや音声）を設定できます。音声フォーマットは入力と出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように扱うかを制御します。Whisper のようなモデルを使用した入力音声の書き起こし、言語設定、ドメイン固有用語の精度を高めるための書き起こしプロンプトを設定できます。ターン検出の設定では、音声活動検出のしきい値、無音時間、検出音声の前後パディングなど、エージェントが応答を開始・停止するタイミングを制御します。

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

ハンドオフにより、特化したエージェント間で会話を引き継げます。

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

セッションはイベントをストリーミングし、セッション オブジェクトを反復処理することでリッスンできます。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。特に処理すべき主なイベントは次のとおりです。

- **audio**: エージェントの応答からの raw 音声データ
- **audio_end**: エージェントが話し終えた
- **audio_interrupted**: ユーザー がエージェントを割り込んだ
- **tool_start/tool_end**: ツール実行のライフサイクル
- **handoff**: エージェントのハンドオフが発生
- **error**: 処理中にエラーが発生

完全なイベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力ガードレールのみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（単語ごとではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断することがあります。デバウンス動作は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキスト エージェントとは異なり、リアルタイム エージェントはガードレール作動時に Exception をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで音声データを再生してください。ユーザー がエージェントを割り込んだ際に、すぐに再生を停止しキュー済み音声をクリアするため、必ず `audio_interrupted` イベントもリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタム リスナーを追加したり、高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作サンプルについては、UI コンポーネントの有無両方のデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。