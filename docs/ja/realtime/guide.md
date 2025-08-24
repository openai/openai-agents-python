---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API との永続接続を維持し、低レイテンシで自然な音声会話や割り込みへの優雅な対応を実現します。

## アーキテクチャ

### 中核コンポーネント

realtime システムはいくつかの主要コンポーネントで構成されます。

-  **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェント。
-  **RealtimeRunner**: 構成を管理します。`runner.run()` を呼び出すとセッションを取得できます。
-  **RealtimeSession**: 単一の対話セッション。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで維持します。
-  **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

典型的な realtime セッションは次のフローに従います。

1. **RealtimeAgent を作成** し、instructions、tools、ハンドオフを設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと構成オプションを渡します。
3. **セッションを開始** します。`await runner.run()` を使用すると RealtimeSession が返ります。
4. **音声またはテキスト メッセージを送信** します。`send_audio()` または `send_message()` を使用します。
5. **イベントを監視** します。セッションを反復処理して、音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどのイベントを受け取ります。
6. **割り込みに対応** します。ユーザーがエージェントの発話にかぶせた場合、現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。完全な API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-  モデルの選択はエージェント レベルではなくセッション レベルで構成します。
-  structured outputs はサポートしません（`outputType` は非対応）。
-  音声はエージェントごとに設定できますが、最初のエージェントが話した後に変更することはできません。
-  その他、tools、ハンドオフ、instructions などの機能は同じように動作します。

## セッション構成

### モデル設定

セッション構成では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストや音声）を設定できます。音声フォーマットは入力と出力の両方に対して設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように処理するかを制御します。Whisper などのモデルを用いた入力音声の文字起こし、言語設定、ドメイン固有用語の精度を高めるための文字起こしプロンプトを構成できます。ターン検出の設定では、音声活動検出のしきい値、無音時間、検出された音声の前後パディングなどにより、エージェントがいつ応答を開始・停止すべきかを制御します。

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

セッションは、セッション オブジェクトを反復処理することで監視できるイベントをストリーミングします。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。特に次のイベントを処理してください。

-  **audio**: エージェントの応答からの raw 音声データ
-  **audio_end**: エージェントの発話が完了
-  **audio_interrupted**: ユーザーがエージェントを割り込んだ
-  **tool_start/tool_end**: ツール実行のライフサイクル
-  **handoff**: エージェントのハンドオフが発生
-  **error**: 処理中にエラーが発生

完全なイベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされます。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` から提供できます。両方のソースからのガードレールは併用して実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込むことがあります。デバウンス動作は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキスト エージェントと異なり、realtime エージェントはガードレールがトリップしても 例外 を発生させません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用してセッションに音声を送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントを監視し、任意の音声ライブラリでデータを再生してください。ユーザーがエージェントを割り込んだ際に即座に再生を停止し、キューにある音声をクリアできるよう、`audio_interrupted` イベントを必ず監視してください。

## 直接的なモデルアクセス

基盤となるモデルにアクセスして、カスタム リスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作するサンプルについては、UI コンポーネントあり・なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。