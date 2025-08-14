---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改善に伴い、破壊的な変更が発生する可能性があります。

## 概要

リアルタイム エージェントは、会話型のフローを可能にし、音声とテキストの入力をリアルタイムに処理して、リアルタイム音声で応答します。これらは  OpenAI の Realtime API との永続的な接続を維持し、低遅延で自然な音声会話や割り込みへのスムーズな対応を実現します。

## アーキテクチャ

### コアコンポーネント

リアルタイム システムはいくつかの重要なコンポーネントで構成されます。

-   **RealtimeAgent** : instructions、tools、ハンドオフで構成されたエージェント。
-   **RealtimeRunner** : 構成を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession** : 単一の対話セッション。通常は ユーザー が会話を開始するたびに作成し、会話が終了するまで生かしておきます。
-   **RealtimeModel** : 基盤となるモデル インターフェース（一般的には  OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイム セッションは次のフローに従います。

1. **RealtimeAgent を作成**: instructions、tools、ハンドオフを設定します。
2. **RealtimeRunner を設定**: エージェントと構成オプションを指定します。
3. **セッションを開始**: `await runner.run()` を使用して開始し、RealtimeSession が返されます。
4. **音声またはテキストの送信**: `send_audio()` または `send_message()` を使用してセッションに送信します。
5. **イベントの受信**: セッションを反復処理してイベントを待ち受けます。音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます。
6. **割り込みの処理**: ユーザー がエージェントの発話に割り込んだ場合、現在の音声生成が自動的に停止します。

セッションは会話履歴を維持し、リアルタイム モデルとの永続接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスとほぼ同様に動作しますが、いくつか重要な相違点があります。完全な API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] を参照してください。

通常のエージェントとの主な相違点:

-   モデルの選択はエージェント レベルではなく、セッション レベルで構成します。
-   structured output はサポートされません（`outputType` はサポートされません）。
-   ボイスはエージェントごとに設定できますが、最初のエージェントが話し始めた後に変更することはできません。
-   tools、ハンドオフ、instructions などのその他の機能は同じように動作します。

## セッション構成

### モデル設定

セッション構成では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（`gpt-4o-realtime-preview` など）、ボイス選択（ alloy、echo、fable、onyx、nova、shimmer）、およびサポートされるモダリティ（テキストや音声）を構成できます。音声フォーマットは入力と出力の両方に設定でき、既定は  PCM16 です。

### 音声設定

音声設定は、セッションが音声入出力をどのように扱うかを制御します。 Whisper などのモデルを使用した入力音声の文字起こし、言語設定、ドメイン特有の用語の精度向上のための文字起こしプロンプトを構成できます。ターン検出設定では、エージェントがいつ応答を開始・停止すべきかを制御し、音声活動検出のしきい値、無音時間、検出された発話の前後のパディングなどを調整できます。

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

ハンドオフにより、専門特化したエージェント間で会話を引き継ぐことができます。

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

セッションは、セッション オブジェクトを反復処理することで待ち受け可能なイベントを ストリーミング します。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始・終了、エージェントのハンドオフ、エラーなどが含まれます。特に扱うべき主なイベントは次のとおりです。

-   **audio** : エージェントの応答からの raw 音声データ
-   **audio_end** : エージェントの発話が終了
-   **audio_interrupted** : ユーザー によるエージェントの割り込み
-   **tool_start/tool_end** : ツール実行のライフサイクル
-   **handoff** : エージェントのハンドオフが発生
-   **error** : 処理中にエラーが発生

完全なイベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力 ガードレール のみです。リアルタイム生成中のパフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、（毎語ではなく）定期的に実行されます。既定のデバウンス長は  100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` から提供できます。両方のソースからのガードレールは併せて実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断することがあります。デバウンス動作により、安全性とリアルタイム パフォーマンス要件のバランスが取られます。テキスト エージェントと異なり、リアルタイム エージェントはガードレールがトリップしても例外を発生させません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントを待ち受け、好みの音声ライブラリで再生してください。ユーザー がエージェントを割り込んだ際に即座に再生を停止し、キューにある音声をクリアできるよう、`audio_interrupted` イベントを必ず監視してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタム リスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作するコード例については、 UI コンポーネントあり/なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。