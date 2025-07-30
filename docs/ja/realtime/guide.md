---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を利用して音声対応の AI エージェントを構築する方法を詳しく解説します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、音声とテキストの入力をリアルタイムで処理し、音声で応答できる会話フローを実現します。 OpenAI の Realtime API と永続的に接続することで、低遅延かつ自然な音声対話が可能になり、ユーザーによる割り込みにもスムーズに対応できます。

## アーキテクチャ

### コアコンポーネント

Realtime システムは次の主要コンポーネントで構成されています。

-   **RealtimeAgent**: instructions、 tools、 handoffs を設定したエージェント
-   **RealtimeRunner**: 設定を管理します。 `runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

典型的な realtime セッションの流れは次のとおりです。

1. **RealtimeAgent** を instructions、 tools、 handoffs とともに作成します。
2. **RealtimeRunner** をエージェントと設定オプションでセットアップします。
3. `await runner.run()` を使用して **セッションを開始** し、 RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信** します。
5. セッションをイテレートして **イベントをリッスン** します。イベントには音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます。
6. ユーザーがエージェントの発話中に話し始めた場合は **割り込みを処理** し、現在の音声生成を自動的に停止します。

セッションは会話履歴を保持し、 realtime モデルとの永続的接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスとほぼ同じですが、いくつかの重要な違いがあります。詳細な API は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] を参照してください。

主な違いは次のとおりです。

-   モデルの選択はエージェントレベルではなくセッションレベルで設定します。
-   structured outputs（ `outputType` ）はサポートされていません。
-   音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   tools、 handoffs、 instructions などその他の機能は同じように動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、音声の選択（alloy、 echo、 fable、 onyx、 nova、 shimmer）、対応モダリティ（テキスト／音声）を設定可能です。入力と出力の音声形式は設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、音声入力および出力の処理方法を制御します。Whisper などのモデルを用いた入力音声の書き起こし、言語設定、ドメイン固有の用語認識向上のための書き起こしプロンプトを指定できます。ターン検知設定では、音声活動検出のしきい値、無音時間、検出した音声の前後のパディングなど、エージェントがいつ応答を開始・終了すべきかを調整できます。

## Tools と Functions

### Tools の追加

通常のエージェントと同様に、 realtime エージェントは会話中に実行される function tools をサポートします。

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

ハンドオフを利用すると、専門化されたエージェント間で会話を引き継ぐことができます。

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

セッションはストリーミングでイベントを返します。セッションオブジェクトをイテレートしてイベントをリッスンします。イベントには音声出力チャンク、書き起こし結果、ツール実行の開始・終了、エージェントのハンドオフ、エラーなどが含まれます。主なイベントは次のとおりです。

-   **audio**: エージェントの応答から得られる raw 音声データ
-   **audio_end**: エージェントの発話が終了した
-   **audio_interrupted**: ユーザーがエージェントを割り込んだ
-   **tool_start/tool_end**: ツール実行ライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

完全なイベント詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされています。パフォーマンス低下を防ぐため、ガードレールはデバウンスされ、リアルタイム生成の度にではなく定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールが発動すると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンス動作により、安全性とリアルタイム性能のバランスを保ちます。テキストエージェントと異なり、 realtime エージェントではガードレール発動時に Exception は発生しません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声を、 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストをセッションに送信します。

音声出力を再生するには `audio` イベントをリッスンし、好みの音声ライブラリでデータを再生してください。ユーザーが割り込んだ際には `audio_interrupted` イベントを受け取って再生を即座に停止し、キューにある音声をクリアするようにしてください。

## 直接モデルへのアクセス

より低レベルの制御が必要な場合、基盤となるモデルへアクセスしてカスタムリスナーを追加したり高度な操作を行えます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、 [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスでき、接続の詳細を細かく制御する高度なユースケースに対応します。

## 例

完全な動作例は [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。 UI あり／なしのデモが含まれています。