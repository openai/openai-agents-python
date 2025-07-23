---
search:
  exclude: true
---
# エージェント

エージェントはアプリケーションの主要な構成要素です。エージェントとは、instructions と tools で構成された大規模言語モデル (LLM) です。

## 基本設定

エージェントでよく設定するプロパティは次のとおりです。

-   `name`: エージェントを識別する必須の文字列です。  
-   `instructions`: developer メッセージまたは system prompt とも呼ばれます。  
-   `model`: 使用する LLM を指定します。任意の `model_settings` で temperature、top_p などのモデル調整パラメーターを設定できます。  
-   `tools`: エージェントがタスクを達成するために使用できる tools です。  

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

エージェントは汎用的に `context` 型を取り込みます。コンテキストは dependency-injection (依存性注入) 用のオブジェクトで、あなたが作成して `Runner.run()` に渡すことで、すべてのエージェント、tool、ハンドオフなどに共有されます。実行中の依存関係や状態をまとめて保持する入れ物として機能し、任意の Python オブジェクトを渡せます。

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

デフォルトでは、エージェントはプレーンテキスト (つまり `str`) を出力します。特定の型で出力させたい場合は、`output_type` パラメーターを使用してください。よく使われる選択肢として [Pydantic](https://docs.pydantic.dev/) オブジェクトがありますが、Pydantic の [TypeAdapter](https://docs.pydantic.dev/latest/api/type_adapter/) でラップできる型であれば、dataclass、list、TypedDict など何でも対応しています。

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

    `output_type` を渡すと、モデルは通常のプレーンテキスト応答ではなく、[structured outputs](https://platform.openai.com/docs/guides/structured-outputs) を使用するようになります。

## ハンドオフ

ハンドオフは、エージェントが委任できるサブエージェントです。ハンドオフのリストを渡すと、エージェントは必要に応じてそれらに委任できます。これは、単一タスクに特化したモジュール化されたエージェントをオーケストレーションする強力なパターンです。詳しくは [ハンドオフ](handoffs.md) のドキュメントをご覧ください。

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

多くの場合、エージェント作成時に instructions を渡せますが、関数を介して動的に instructions を生成することも可能です。その関数は agent と context を受け取り、プロンプトを返さなければなりません。同期関数と `async` 関数のどちらも利用できます。

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

エージェントのライフサイクルを監視したい場合があります。たとえば、イベントをログに記録したり、特定のイベント発生時にデータを事前取得したりできます。`hooks` プロパティを使ってエージェントのライフサイクルにフックできます。[`AgentHooks`][agents.lifecycle.AgentHooks] クラスを継承し、関心のあるメソッドをオーバーライドしてください。

## ガードレール

ガードレールを利用すると、エージェントの実行と並行してユーザー入力のチェックやバリデーションを行えます。たとえば、ユーザー入力の関連性をスクリーニングできます。詳細は [ガードレール](guardrails.md) のドキュメントをご覧ください。

## エージェントのクローン／コピー

エージェントの `clone()` メソッドを使うと、既存のエージェントを複製し、必要に応じて任意のプロパティを変更できます。

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

tools のリストを渡しても、LLM が必ずしもツールを使用するとは限りません。[`ModelSettings.tool_choice`][agents.model_settings.ModelSettings.tool_choice] を設定することで、ツール使用を強制できます。有効な値は次のとおりです。

1. `auto`：LLM がツールを使うかどうかを判断します。  
2. `required`：LLM にツール使用を必須とします (ただしどのツールを使うかは賢く選択します)。  
3. `none`：LLM にツールを使用しないことを必須とします。  
4. 具体的な文字列 (例: `my_tool`) を設定すると、LLM はそのツールを必ず使用します。  

!!! note

    無限ループを防ぐため、フレームワークはツール呼び出し後に自動で `tool_choice` を "auto" にリセットします。この挙動は [`agent.reset_tool_choice`][agents.agent.Agent.reset_tool_choice] で設定可能です。ツールの実行結果が再び LLM に送られ、`tool_choice` の設定により新たなツール呼び出しが発生し続けるのが無限ループの原因です。

    ツール呼び出し後に自動モードで継続せず完全に停止したい場合は、[`Agent.tool_use_behavior="stop_on_first_tool"`] を設定してください。ツールの出力をそのまま最終応答として使用し、追加の LLM 処理を行いません。