---
search:
  exclude: true
---
# ガイド

本ガイドでは、 OpenAI Agents SDK の realtime 機能を使用して音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装を改良する過程で互換性が壊れる変更が入る可能性があります。

## 概要

Realtime エージェントは、音声とテキスト入力をリアルタイムに処理し、リアルタイム音声で応答することで対話フローを実現します。 OpenAI の Realtime API と永続的に接続を維持し、低レイテンシで自然な音声会話と割り込みへのスムーズな対応を可能にします。

## アーキテクチャ

### コアコンポーネント

リアルタイムシステムは、以下の主要コンポーネントで構成されています。

-   **RealtimeAgent**: instructions、tools、handoffs で構成されるエージェントです。  
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すことでセッションを取得できます。  
-   **RealtimeSession**: 1 回の対話セッションを表します。ユーザーが会話を開始するたびに作成し、会話が終了するまで保持します。  
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

典型的なリアルタイムセッションは次の流れで進みます。

1. **RealtimeAgent** を instructions、tools、handoffs と共に作成します。  
2. エージェントと各種設定を指定して **RealtimeRunner** をセットアップします。  
3. `await runner.run()` を実行してセッション (**RealtimeSession**) を開始します。  
4. `send_audio()` または `send_message()` を使用して音声またはテキストを送信します。  
5. セッションを反復処理してイベントを受信します。イベントには音声出力、トランスクリプト、ツール呼び出し、ハンドオフ、エラーなどが含まれます。  
6. ユーザーがエージェントの発話中に話し始めた場合は **割り込み** が発生し、現在の音声生成が自動で停止します。  

セッションは会話履歴を保持し、リアルタイムモデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスとほぼ同じですが、いくつか重要な違いがあります。詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスをご覧ください。

主な違い:

-   モデルの選択はエージェントではなくセッション単位で指定します。  
-   structured outputs (`outputType`) はサポートされていません。  
-   ボイスはエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。  
-   tools、handoffs、instructions などその他の機能は通常のエージェントと同様に機能します。  

## セッション設定

### モデル設定

セッション設定では基盤となるリアルタイムモデルの挙動を制御できます。モデル名（`gpt-4o-realtime-preview` など）、ボイス（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（text / audio）を指定できます。入力・出力の音声フォーマットは PCM16 がデフォルトですが変更可能です。

### オーディオ設定

オーディオ設定では音声入力・出力の扱いを制御します。Whisper などのモデルで音声をトランスクリプトし、言語設定やドメイン固有語の精度を上げる transcription prompt を指定できます。ターン検出では、音声活動検出のしきい値、無音継続時間、検出前後のパディングなどを設定して、エージェントがいつ応答を開始・終了すべきかを調整します。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、Realtime エージェントでも会話中に実行される function tools を追加できます。

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

ハンドオフを利用すると、会話を専門エージェントへ引き継げます。

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

セッションを反復処理することでストリーミングされるイベントを監視できます。主なイベントは以下のとおりです。

-   **audio**: エージェント応答の raw 音声データ  
-   **audio_end**: エージェントの発話終了  
-   **audio_interrupted**: ユーザーによる割り込み発話  
-   **tool_start / tool_end**: ツール実行の開始・終了  
-   **handoff**: エージェント間のハンドオフ発生  
-   **error**: 処理中に発生したエラー  

詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみサポートされます。パフォーマンス維持のため、ガードレールはデバウンスされて定期的に（すべての単語ではなく）評価されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールが発火すると `guardrail_tripped` イベントが生成され、エージェントの現在の応答が中断される場合があります。リアルタイム性能と安全性を両立させるための挙動です。テキストエージェントと異なり、Realtime エージェントではガードレール発火時に Exception は送出されません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] で音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] でテキストをセッションに送信できます。

音声出力を再生するには、`audio` イベントを監視して取得したデータをお好みのオーディオライブラリで再生してください。ユーザーが割り込んだ際には `audio_interrupted` イベントを受信して即座に再生を停止し、キューに溜まった音声をクリアする必要があります。

## 直接モデルへアクセス

より低レベルの制御やカスタムリスナー追加など、高度な用途では基盤モデルへ直接アクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースを直接操作できます。

## 例

動作する完全なサンプルは、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。 UI あり／なしのデモが含まれています。