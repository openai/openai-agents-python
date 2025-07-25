---
search:
  exclude: true
---
# エージェント

エージェントはアプリの中心となる構成要素です。エージェントは、大規模言語モデル ( LLM ) を instructions とツールで設定したものです。

## 基本設定

エージェントで最もよく設定するプロパティは以下のとおりです。

- `name` : エージェントを識別する必須の文字列です。
- `instructions` : developer メッセージまたは system prompt とも呼ばれます。
- `model` : 使用する LLM を指定します。また、`temperature` 、`top_p` などのモデル調整パラメーターを設定する `model_settings` をオプションで指定できます。
- `tools` : エージェントがタスクを達成するために使用できるツールです。

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

エージェントは `context` 型についてジェネリックです。コンテキストは依存性注入用のツールで、`Runner.run()` に渡すオブジェクトです。このオブジェクトはすべてのエージェント、ツール、ハンドオフなどに渡され、実行中の依存関係や状態をまとめて保持します。任意の Python オブジェクトをコンテキストとして渡せます。

```python
@dataclass
class UserContext:
    uid: str
    is_pro_user: bool

    async def fetch_purchases() -> list[Purchase]:
        return ...

agent = Agent[UserContext](
    ...,
)
```

## 出力タイプ

デフォルトでは、エージェントはプレーンテキスト（つまり `str`）を出力します。特定の型で出力を受け取りたい場合は、`output_type` パラメーターを使用します。よく使われるのは Pydantic オブジェクトですが、Pydantic TypeAdapter でラップできる型であれば何でも対応しています。たとえば dataclasses 、lists 、TypedDict などです。

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

    `output_type` を渡すと、モデルに通常のプレーンテキストではなく structured outputs を使用するよう指示します。

## ハンドオフ

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフのリストを渡すと、エージェントは必要に応じてそれらに処理を委任できます。これは、単一のタスクに特化したモジュール化されたエージェントをオーケストレーションする強力なパターンです。詳細は [handoffs](handoffs.md) ドキュメントを参照してください。

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

## 動的 instructions

ほとんどの場合、エージェント作成時に instructions を指定できます。ただし、関数を通じて動的 instructions を提供することもできます。この関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。同期関数と `async` 関数の両方が使用可能です。

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

## ライフサイクルイベント (hooks)

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりしたい場合です。`hooks` プロパティを使用してエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、興味のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使用すると、エージェントの実行と並行してユーザー入力に対してチェックやバリデーションを実行できます。たとえば、ユーザー入力の関連性をフィルタリングすることが可能です。詳細は [guardrails](guardrails.md) ドキュメントをご覧ください。

## エージェントのクローン/コピー

`clone()` メソッドを使用すると、エージェントを複製し、任意のプロパティを変更できます。

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

ツールのリストを渡しても、 LLM が必ずツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツールの使用を強制できます。有効な値は次のとおりです。

1. `auto` : LLM がツールを使用するかどうかを判断します。  
2. `required` : LLM にツール使用を必須とします（どのツールを使うかは LLM が判断します）。  
3. `none` : LLM にツールを使用しないよう要求します。  
4. 具体的な文字列を指定する（`my_tool` など）: LLM にそのツールを必ず使用させます。

!!! note

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に `auto` にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループとは、ツールの結果が LLM に送られ、その後 `tool_choice` により再びツール呼び出しが生成される、というサイクルが延々と続くことを指します。

    ツール呼び出し後に auto モードで続行せず、完全に停止させたい場合は、[`Agent.tool_use_behavior="stop_on_first_tool"`] を設定してください。ツール出力をそのまま最終応答として使用し、追加の LLM 処理を行いません。