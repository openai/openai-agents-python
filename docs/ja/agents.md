---
search:
  exclude: true
---
# エージェント

エージェントはアプリの中心的な構成要素です。エージェントは、instructions と tools で設定された大規模言語モデル ( LLM ) です。

## 基本設定

エージェントでよく設定するプロパティは次のとおりです。

- `name`: エージェントを識別する必須の文字列です。
- `instructions`: developer message（開発者メッセージ）または システムプロンプト とも呼ばれます。
- `model`: 使用する LLM を指定します。また、temperature や top_p などのモデル調整パラメーターを設定する `model_settings` を任意で指定できます。
- `tools`: エージェントがタスクを達成するために使用できる tools です。

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

エージェントは汎用的な `context` 型を取ります。コンテキストは依存性注入用のツールで、あなたが作成して `Runner.run()` に渡すオブジェクトです。これはすべてのエージェント、tool、handoff などに渡され、実行時に必要な依存関係や状態を格納する入れ物として機能します。コンテキストには任意の Python オブジェクトを渡せます。

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

デフォルトでは、エージェントはプレーンテキスト、つまり `str` を出力します。特定の型で出力させたい場合は、`output_type` パラメーターを使用します。一般的には [Pydantic](https://docs.pydantic.dev/) オブジェクトを使うことが多いですが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型であれば何でもサポートされています。たとえば dataclass、list、TypedDict などです。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答ではなく structured outputs を使用するよう指示されます。

## ハンドオフ

ハンドオフは、エージェントが委譲できるサブエージェントです。ハンドオフのリストを渡すと、エージェントは関連性がある場合にそれらへ委譲できます。これは、単一タスクに特化したモジュール型エージェントを編成する強力なパターンです。詳細は [ハンドオフ](handoffs.md) のドキュメントをご覧ください。

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

通常はエージェント作成時に instructions を渡しますが、関数を通じて動的に instructions を提供することもできます。この関数はエージェントとコンテキストを受け取り、プロンプトを返す必要があります。同期関数でも `async` 関数でも利用可能です。

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

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりするケースです。`hooks` プロパティを使うことでエージェントのライフサイクルにフックを追加できます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスを継承し、必要なメソッドをオーバーライドしてください。

## ガードレール

ガードレールを使うと、エージェントの実行と並行してユーザー入力に対するチェック／バリデーションを実行できます。たとえば、ユーザー入力の関連性をフィルタリングすることが可能です。詳細は [guardrails](guardrails.md) のドキュメントをご参照ください。

## エージェントのクローンとコピー

エージェントの `clone()` メソッドを使用すると、エージェントを複製し、必要に応じて任意のプロパティを変更できます。

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

tools のリストを渡しても、LLM が必ずツールを使用するわけではありません。`ModelSettings.tool_choice` を設定することでツール使用を強制できます。設定可能な値は次のとおりです。

1. `auto` : LLM がツールを使用するかどうかを判断します。  
2. `required` : LLM にツールの使用を必須とします (どのツールを使うかは LLM が判断)。  
3. `none` : LLM にツールを使用しないことを要求します。  
4. 特定の文字列 (例: `my_tool`) を設定すると、そのツールの使用を必須とします。  

!!! note

    無限ループを防ぐため、フレームワークはツール呼び出し後に `tool_choice` を自動的に "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定できます。無限ループが発生するのは、ツールの結果が LLM へ送られ、`tool_choice` により再びツール呼び出しが生成されるというサイクルが続くためです。

    ツール呼び出し後に自動モードへ移行せず完全に処理を終了したい場合は、[`Agent.tool_use_behavior="stop_on_first_tool"`] を設定してください。これにより、ツールの出力をそのまま最終応答として使用し、追加の LLM 処理を行いません。