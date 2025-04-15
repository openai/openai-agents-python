# エージェント

エージェントは、アプリケーションの中核となる基本コンポーネントです。エージェントとは、instructions とツールで構成された大規模言語モデル（LLM）のことです。

## 基本設定

エージェントで最も一般的に設定するプロパティは以下の通りです。

-   `instructions`：developer message や システムプロンプト(system prompt)とも呼ばれます。
-   `model`：どの LLM を使用するか、また `model_settings` で temperature や top_p などのモデル調整パラメーターを設定できます。
-   `tools`：エージェントがタスクを達成するために使用できるツールです。

```python
from agents import Agent, ModelSettings, function_tool

@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny"

agent = Agent(
    name="Haiku agent",
    instructions="Always respond in haiku form",
    model="o3-mini",
    tools=[get_weather],
)
```

## コンテキスト

エージェントは `context` 型に対して汎用的です。コンテキストは依存性注入ツールであり、`Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、ツール、ハンドオフなどに渡され、エージェント実行時の依存関係や状態をまとめて管理します。任意の Python オブジェクトを context として指定できます。

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

デフォルトでは、エージェントはプレーンテキスト（つまり `str`）出力を生成します。特定の型の出力をエージェントに生成させたい場合は、`output_type` パラメーターを使用できます。一般的な選択肢として [Pydantic](https://docs.pydantic.dev/) オブジェクトがありますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型（dataclasses、リスト、TypedDict など）であればサポートしています。

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

    `output_type` を指定すると、モデルは通常のプレーンテキスト応答の代わりに [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するよう指示されます。

## ハンドオフ

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフのリストを指定すると、エージェントは必要に応じてそれらに処理を委任できます。これは、単一タスクに特化したモジュール型のエージェントをオーケストレーションする強力なパターンです。詳細は [handoffs](handoffs.md) ドキュメントをご覧ください。

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

多くの場合、エージェント作成時に instructions を指定できますが、関数を使って動的に instructions を提供することも可能です。この関数はエージェントと context を受け取り、プロンプトを返す必要があります。通常の関数と `async` 関数の両方が利用可能です。

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

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントを記録したり、特定のイベント発生時にデータを事前取得したりしたい場合です。`hooks` プロパティを使ってエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスをサブクラス化し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使うと、エージェントの実行と並行して user 入力のチェックやバリデーションを行えます。たとえば、user の入力が関連性のある内容かどうかをスクリーニングできます。詳細は [guardrails](guardrails.md) ドキュメントをご覧ください。

## エージェントのクローン／コピー

エージェントの `clone()` メソッドを使うことで、エージェントを複製し、任意のプロパティを変更できます。

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

ツールのリストを指定しても、必ずしも LLM がツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツールの使用を強制できます。有効な値は以下の通りです。

1. `auto`：LLM がツールを使うかどうかを自動で判断します。
2. `required`：LLM にツールの使用を必須とします（どのツールを使うかは賢く選択されます）。
3. `none`：LLM にツールを _使わない_ ことを要求します。
4. 特定の文字列（例：`my_tool`）を指定すると、その特定のツールの使用を必須とします。

!!! note

    無限ループを防ぐため、フレームワークはツール呼び出し後に自動的に `tool_choice` を "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループは、ツールの execution results が LLM に送信され、`tool_choice` のために再度ツール呼び出しが発生し、これが繰り返されることで発生します。

    ツール呼び出し後にエージェントを完全に停止させたい場合（auto モードで継続させたくない場合）は、[`Agent.tool_use_behavior="stop_on_first_tool"`] を設定できます。これにより、ツールの出力がそのまま最終応答として使用され、以降の LLM 処理は行われません。