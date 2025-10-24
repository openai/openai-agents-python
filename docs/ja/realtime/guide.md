---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装の改善に伴い破壊的変更が入る可能性があります。

## 概要

Realtime エージェントは会話のフローを実現し、音声およびテキスト入力をリアルタイムに処理して realtime 音声で応答します。OpenAI の Realtime API との永続接続を維持し、低レイテンシで自然な音声会話と、割り込みへの優雅な対応を可能にします。

## アーキテクチャ

### コアコンポーネント

realtime システムは次の主要コンポーネントで構成されます。

-   **RealtimeAgent** : instructions、tools、handoffs を設定したエージェント
-   **RealtimeRunner** : 構成を管理します。`runner.run()` を呼び出すとセッションを取得できます。
-   **RealtimeSession** : 単一の対話セッション。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel** : 基盤となるモデルインターフェース（一般的には OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは次の流れで進みます。

1. **RealtimeAgent を作成** し、instructions、tools、handoffs を設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと構成オプションを渡します。
3. `await runner.run()` を使って **セッションを開始** し、RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントを受信** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話に被せたときに **割り込みを処理** します。これにより現在の音声生成が自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な相違点:

-   モデルの選択はエージェントレベルではなくセッションレベルで構成します。
-   structured output のサポートはありません（`outputType` は非対応）。
-   声質はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   tools、handoffs、instructions などのその他の機能は同様に動作します。

## セッションの設定

### モデル設定

セッション設定では基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-realtime`）、声の選択（alloy、echo、fable、onyx、nova、shimmer）、サポートするモダリティ（テキストおよび/または音声）を構成できます。音声フォーマットは入力と出力の両方で設定でき、既定は PCM16 です。

### 音声設定

音声設定では、セッションが音声入力と出力をどのように処理するかを制御します。Whisper のようなモデルで入力音声の書き起こしを構成し、言語設定や、ドメイン固有用語の精度を高めるための書き起こしプロンプトを指定できます。ターン検出設定では、エージェントがいつ応答を開始・停止すべきかを制御し、音声活動検出のしきい値、無音の長さ、検出音声の前後パディングなどのオプションがあります。

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

ハンドオフにより、専門化されたエージェント間で会話を転送できます。

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

セッションはストリーミングでイベントを送出します。セッションオブジェクトを反復処理してリッスンできます。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーなどが含まれます。主に処理すべきイベントは次のとおりです。

-   **audio** : エージェントの応答からの raw 音声データ
-   **audio_end** : エージェントの発話が完了
-   **audio_interrupted** : ユーザーがエージェントを割り込み
-   **tool_start/tool_end** : ツール実行のライフサイクル
-   **handoff** : エージェントのハンドオフが発生
-   **error** : 処理中にエラーが発生

完全なイベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力ガードレールのみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、構成可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースからのガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込むことがあります。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキストエージェントと異なり、realtime エージェントはガードレールが作動しても Exception を **発生させません**。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声を送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントをリッスンして、任意の音声ライブラリで再生します。ユーザーがエージェントを割り込んだときに即座に再生を停止し、キューにある音声をクリアするために、`audio_interrupted` イベントを確実にリッスンしてください。

## 直接的なモデルアクセス

基盤となるモデルにアクセスして、カスタムリスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

完全に動作するサンプルは、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。