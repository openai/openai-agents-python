---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK のリアルタイム機能を使用して音声対応 AI エージェントを構築する方法を詳しく解説します。

!!! warning "Beta feature"
リアルタイム エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

リアルタイム エージェントは、音声とテキスト入力をリアルタイムで処理し、リアルタイム音声で応答する会話フローを実現します。 OpenAI の Realtime API と永続的に接続することで、低レイテンシーかつ割り込みに強い自然な音声対話を提供します。

## アーキテクチャ

### コアコンポーネント

リアルタイム システムは次の主要コンポーネントで構成されます。

-   **RealtimeAgent** : インストラクション、ツール、ハンドオフで構成された エージェント です。  
-   **RealtimeRunner** : 設定を管理します。 `runner.run()` を呼び出して セッション を取得できます。  
-   **RealtimeSession** : 1 つの対話セッションを表します。通常、 ユーザー が会話を開始するたびに作成し、会話が終了するまで保持します。  
-   **RealtimeModel** : 基盤となるモデル インターフェース (通常は OpenAI の WebSocket 実装) です。  

### セッションフロー

典型的なリアルタイム セッションの流れは次のとおりです。

1. **RealtimeAgent** をインストラクション、ツール、ハンドオフ付きで作成します。  
2. その エージェント と設定オプションを使って **RealtimeRunner** をセットアップします。  
3. `await runner.run()` を呼び出して **セッションを開始** します。これにより RealtimeSession が返されます。  
4. `send_audio()` または `send_message()` を使用して **音声またはテキスト** をセッションへ送信します。  
5. セッションをイテレートして **イベントを受信** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどが含まれます。  
6. ユーザー が重ねて話した場合は **割り込み** を処理し、現在の音声生成を自動停止させます。  

セッションは会話履歴を保持し、リアルタイム モデルとの永続接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスとほぼ同様に動作しますが、いくつか重要な違いがあります。詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API リファレンスをご覧ください。

主な違い:

-   モデルの選択は エージェント レベルではなくセッション レベルで設定します。  
-   適切な形式のデータ (structured outputs) はサポートされません (`outputType` は使用不可)。  
-   音声は エージェント 単位で設定できますが、最初の エージェント が話した後は変更できません。  
-   ツール、ハンドオフ、インストラクションなどその他の機能は同じ方法で利用できます。  

## セッションの設定

### モデル設定

セッション設定では基盤となるリアルタイム モデルの動作を制御できます。モデル名 (例: `gpt-4o-realtime-preview`)、音声 (alloy, echo, fable, onyx, nova, shimmer) の選択、対応モダリティ (テキスト / 音声) を指定できます。入力・出力ともに音声フォーマットを設定でき、デフォルトは PCM16 です。

### オーディオ設定

音声設定では、セッションが音声入出力をどのように扱うかを制御します。Whisper などのモデルを使った入力音声の文字起こし、言語設定、ドメイン固有用語の精度向上のための文字起こしプロンプトを指定できます。ターン検出では、音声活動検出のしきい値、無音時間、検出された音声前後のパディングなどを調整できます。

## ツールと関数

### ツールの追加

通常の エージェント と同様に、リアルタイム エージェントでも会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、会話を専門化された エージェント 間で移譲できます。

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

セッションはイベントを ストリーミング し、セッション オブジェクトをイテレートして受信できます。主なイベントは以下のとおりです。

-   **audio** : エージェント 応答の raw 音声データ  
-   **audio_end** : エージェント が話し終えたことを示します  
-   **audio_interrupted** : ユーザー による割り込み  
-   **tool_start/tool_end** : ツール実行の開始 / 終了  
-   **handoff** : エージェント ハンドオフが発生  
-   **error** : 処理中にエラーが発生  

完全なイベント一覧は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

リアルタイム エージェントでは出力ガードレールのみサポートされます。パフォーマンス低下を避けるためデバウンス処理が行われ、リアルタイム生成中に毎語ではなく定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定で変更可能です。

ガードレールが発火すると `guardrail_tripped` イベントが生成され、 エージェント の現在の応答を割り込むことがあります。テキスト エージェント と異なり、リアルタイム エージェントではガードレール発火時に例外は送出されません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信できます。

音声出力を扱うには `audio` イベントを受信して任意のオーディオ ライブラリで再生してください。 ユーザー が割り込んだ際には `audio_interrupted` イベントを検知し、再生を即座に停止してキューにある音声をクリアする必要があります。

## モデルへの直接アクセス

下位レベルの制御が必要な場合、基盤となるモデルにアクセスしてカスタム リスナーを追加したり高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスでき、接続をより細かく制御できます。

## コード例

動作する完全な例については、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。 UI コンポーネントあり・なしのデモが含まれています。