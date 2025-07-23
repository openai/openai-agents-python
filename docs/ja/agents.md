---
search:
  exclude: true
---
# エージェント

エージェントはアプリケーションの中心的な構成要素です。エージェントは、instructions とツールで設定された大規模言語モデル ( LLM ) です。

## 基本設定

エージェントで最も一般的に設定するプロパティは次のとおりです。

-   `name`: 必須の文字列で、エージェントを識別します。  
-   `instructions`: developer メッセージまたは system prompt とも呼ばれます。  
-   `model`: 使用する LLM と、temperature や top_p などのチューニングパラメーターを設定するための任意の `model_settings`。  
-   `tools`: エージェントがタスクを遂行するために使用できるツール。  

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

## Context

エージェントはその `context` 型に対してジェネリックです。Context は依存性注入ツールで、`Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、ツール、ハンドオフなどに渡され、実行時の依存関係や状態をまとめて保持します。任意の Python オブジェクトを context として提供できます。

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

デフォルトでは、エージェントはプレーンテキスト ( `str` ) を出力します。特定の型の出力が必要な場合は `output_type` パラメーターを使用できます。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトがよく使われますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型—dataclass、list、TypedDict など—であれば何でもサポートします。

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

    `output_type` を渡すと、モデルは通常のプレーンテキストではなく [structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用して応答します。

## ハンドオフ

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフのリストを渡すと、エージェントは必要に応じてそれらに委譲できます。これは、単一タスクに特化したモジュール型エージェントをオーケストレーションする強力なパターンです。詳細は [handoffs](handoffs.md) のドキュメントをご覧ください。

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

通常はエージェント作成時に instructions を渡しますが、関数を介して動的に instructions を生成することもできます。この関数はエージェントと context を受け取り、プロンプトを返さなければなりません。通常の関数と `async` 関数の両方を使用できます。

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

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントをログに残したり、特定のイベント発生時にデータを事前取得したりできます。`hooks` プロパティを使うことでエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスを継承し、必要なメソッドをオーバーライドしてください。

## ガードレール

ガードレールは、エージェントの実行と並行してユーザー入力に対するチェックやバリデーションを行えます。たとえば、ユーザー入力の関連性をフィルタリングすることができます。詳細は [guardrails](guardrails.md) のドキュメントをご確認ください。

## エージェントのクローン／コピー

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

ツールのリストを渡しても、LLM が必ずしもツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することでツール使用を強制できます。有効な値は次のとおりです。

1. `auto`: ツールを使うかどうかを LLM に任せます。  
2. `required`: LLM にツールの使用を必須とします (どのツールを使うかは LLM が判断)。  
3. `none`: LLM にツールを使わないことを要求します。  
4. 特定の文字列 (例: `my_tool`): その特定のツールを使用することを LLM に要求します。  

!!! note

    無限ループを防ぐために、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この動作は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。無限ループとは、ツール結果が LLM に送られ、`tool_choice` により再びツール呼び出しが生成される、というサイクルを指します。

    ツール呼び出し後にエージェントを完全に停止させたい (auto モードで続行させたくない) 場合は、[`Agent.tool_use_behavior="stop_on_first_tool"`] を設定すると、ツールの出力をそのまま最終応答として使用し、追加の LLM 処理を行いません。