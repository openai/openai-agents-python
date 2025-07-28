---
search:
  exclude: true
---
# エージェント

エージェントは、アプリにおける中核的なビルディングブロックです。エージェントは、インストラクションとツールで構成された LLM です。

## 基本設定

エージェントで最もよく設定するプロパティは次のとおりです:

-   `name`: エージェントを識別する必須の文字列です。
-   `instructions`: 開発者メッセージ、または system prompt とも呼ばれます。
-   `model`: 使用する LLM を指定します。`model_settings` を併用すると temperature、top_p などのモデル調整パラメーターを設定できます。
-   `tools`: エージェントがタスク達成のために使用できるツールです。

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
     """returns weather info for the specified city."""
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="o3-mini",
    tools=[get_weather],
)
```

## コンテキスト

エージェントは `context` 型に対してジェネリックです。コンテキストは依存性注入の仕組みで、`Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行時の依存関係と状態を格納する入れ物として機能します。コンテキストには任意の Python オブジェクトを指定できます。

```python
@dataclass
class UserContext:
    name: str
    uid: str
    is_pro_user: bool

    async def fetch_purchases() -> list[Purchase]:
        return ...

agent = Agent[UserContext](
    ...,
)
```

## 出力タイプ

既定では、エージェントはプレーンテキスト (つまり `str`) を出力します。特定の型で出力させたい場合は `output_type` パラメーターを使用できます。よく使われるのは Pydantic オブジェクトですが、Pydantic の TypeAdapter でラップできる型 — dataclass、list、TypedDict など — であれば何でもサポートしています。

```python
from pydantic import BaseModel
from agents import Agent


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]

agent = Agent(
    name="Calendar extractor",
    instructions="Extract calendar events from text",
    output_type=CalendarEvent,
)
```

!!! note

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答の代わりに structured outputs を使用するよう指示されます。

## ハンドオフ

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフのリストを渡すと、必要に応じてエージェントがそれらに処理を委譲できます。これは、単一タスクに特化したモジュール式のエージェントをオーケストレーションする強力なパターンです。詳細は [handoffs](handoffs.md) ドキュメントをご覧ください。

```python
from agents import Agent

booking_agent = Agent(...)
refund_agent = Agent(...)

triage_agent = Agent(
    name="Triage agent",
    instructions=(
        "Help the user with their questions."
        "If they ask about booking, handoff to the booking agent."
        "If they ask about refunds, handoff to the refund agent."
    ),
    handoffs=[booking_agent, refund_agent],
)
```

## 動的インストラクション

通常はエージェント作成時にインストラクションを指定しますが、関数を介して動的に渡すこともできます。その関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が利用可能です。

```python
def dynamic_instructions(
    context: RunContextWrapper[UserContext], agent: Agent[UserContext]
) -> str:
    return f"The user's name is {context.context.name}. Help them with their questions."


agent = Agent[UserContext](
    name="Triage agent",
    instructions=dynamic_instructions,
)
```

## ライフサイクルイベント（フック）

エージェントのライフサイクルを観察したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータをプリフェッチしたりするケースです。そのようなときは `hooks` プロパティを使ってライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスを継承し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使用すると、エージェントの実行と並行してユーザー入力のチェックやバリデーションを行えます。たとえば、ユーザー入力の関連性をスクリーニングできます。詳細は [guardrails](guardrails.md) ドキュメントを参照してください。

## エージェントのクローン/コピー

エージェントの `clone()` メソッドを使用すると、エージェントを複製し、任意のプロパティを変更できます。

```python
pirate_agent = Agent(
    name="Pirate",
    instructions="Write like a pirate",
    model="o3-mini",
)

robot_agent = pirate_agent.clone(
    name="Robot",
    instructions="Write like a robot",
)
```

## ツール使用の強制

ツールのリストを指定しても、 LLM が必ずしもツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。指定可能な値は次のとおりです:

1. `auto` — LLM がツールを使うかどうかを判断します。
2. `required` — LLM にツールの使用を必須とします (ただしどのツールを使うかは自動で判断)。
3. `none` — LLM にツールを使用しないことを要求します。
4. 具体的な文字列 (例: `my_tool`) — LLM にその特定のツールを使用させます。

!!! note

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループは、ツール結果が LLM に送られ、それにより `tool_choice` の設定でさらにツール呼び出しが生成される、というサイクルが延々と続くために発生します。

    ツール呼び出し後に (auto モードで続行するのではなく) エージェントを完全に停止させたい場合は、[`Agent.tool_use_behavior="stop_on_first_tool"`] を設定してください。これにより、ツールの出力をそのまま最終レスポンスとして使用し、追加の LLM 処理を行いません。