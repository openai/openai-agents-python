---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK のリアルタイム機能を用いて、音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改善に伴い、互換性のない変更が入る可能性があります。

## 概要

リアルタイム エージェントは、会話フローを可能にし、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答します。OpenAI の Realtime API との永続的な接続を維持し、低遅延で自然な音声対話と、中断へのスムーズな対応を実現します。

## アーキテクチャ

### コアコンポーネント

リアルタイム システムは次の主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント。
-   **RealtimeRunner**: 構成を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的なリアルタイム セッションは次のフローに従います。

1. instructions、tools、handoffs を用いて **RealtimeAgent を作成** します。
2. エージェントと構成オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使用して **セッションを開始** し、RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使用して **音声またはテキスト メッセージを送信** します。
5. セッションを反復処理して **イベントを監視** します。イベントには、音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話に被せた場合の **割り込み処理** を行います。現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、リアルタイム モデルとの永続接続を管理します。

## エージェント構成

RealtimeAgent は、通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェント レベルではなく、セッション レベルで構成します。
-   structured outputs はサポートされません（`outputType` はサポート対象外）。
-   声質（ボイス）はエージェントごとに設定できますが、最初のエージェントが話した後は変更できません。
-   ツール、ハンドオフ、instructions などのその他の機能は同様に動作します。

## セッション構成

### モデル設定

セッション構成では、基盤となるリアルタイム モデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、サポートするモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方で設定可能で、既定は PCM16 です。

### オーディオ設定

オーディオ設定は、セッションが音声入力と出力をどのように扱うかを制御します。Whisper のようなモデルを用いた入力音声の書き起こし、言語設定、ドメイン固有用語の精度向上のための書き起こしプロンプトを構成できます。ターン検出設定では、ボイスアクティビティ検出のしきい値、無音時間、検出された発話の前後のパディングなど、エージェントが応答を開始・終了するタイミングを制御します。

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

セッションは、セッション オブジェクトを反復処理することで監視できるイベントをストリーム配信します。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、およびエラーが含まれます。主に扱うべきイベントは次のとおりです。

-   **audio**: エージェントの応答からの raw 音声データ
-   **audio_end**: エージェントが話し終えた
-   **audio_interrupted**: ユーザーがエージェントを中断した
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの完全な詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでサポートされるのは出力 ガードレール のみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために定期的に（各単語ごとではなく）実行されます。既定のデバウンス長は 100 文字ですが、構成可能です。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断できます。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキスト エージェントと異なり、リアルタイム エージェントはガードレールがトリップしても例外を **発生させません**。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントを監視し、任意のオーディオ ライブラリで音声データを再生します。ユーザーがエージェントを中断した際に即座に再生を停止し、キューにある音声をクリアするため、`audio_interrupted` イベントを必ず監視してください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信する電話にリアルタイム エージェントを接続できます。SDK には、SIP 上でメディアのネゴシエーションを行いながら同じエージェント フローを再利用する [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] が用意されています。

使用する際は、モデル インスタンスを runner に渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は、着信を通知する webhook によって渡されます。

```python
from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(
    starting_agent=agent,
    model=OpenAIRealtimeSIPModel(),
)

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
        "initial_model_settings": {
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        },
    },
) as session:
    async for event in session:
        ...
```

発信者が電話を切ると SIP セッションが終了し、リアルタイム接続は自動的に閉じられます。完全なテレフォニーのコード例は [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタム リスナーを追加したり高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルに制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作するコード例については、UI コンポーネントの有無両方のデモが含まれる [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。