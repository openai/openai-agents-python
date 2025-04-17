---
search:
  exclude: true
---
# クイックスタート

## プロジェクトと仮想環境の作成

これは一度だけ行えば十分です。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 仮想環境の有効化

新しいターミナルセッションを開始するたびに実行してください。

```bash
source .venv/bin/activate
```

### Agents SDK のインストール

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API キーの設定

まだお持ちでない場合は、[こちらの手順](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)に従って OpenAI API キーを作成してください。

```bash
export OPENAI_API_KEY=sk-...
```

## 最初のエージェントの作成

エージェントは instructions 、名前、`model_config` などのオプション設定で定義します。

```python
from agents import Agent

agent = Agent(
    name="Math Tutor",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)
```

## さらにエージェントを追加

追加のエージェントも同様の方法で定義できます。`handoff_descriptions` はハンドオフのルーティングを判断するための追加コンテキストを提供します。

```python
from agents import Agent

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You provide assistance with historical queries. Explain important events and context clearly.",
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)
```

## ハンドオフの定義

各エージェントに対して、タスクを進める際に選択できるハンドオフ先の一覧を定義できます。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="You determine which agent to use based on the user's homework question",
    handoffs=[history_tutor_agent, math_tutor_agent]
)
```

## エージェントオーケストレーションの実行

ワークフローが実行され、トリアージエージェントが 2 つの専門エージェント間で正しくルーティングすることを確認しましょう。

```python
from agents import Runner

async def main():
    result = await Runner.run(triage_agent, "What is the capital of France?")
    print(result.final_output)
```

## ガードレールの追加

入力または出力に対して実行されるカスタムガードレールを定義できます。

```python
from agents import GuardrailFunctionOutput, Agent, Runner
from pydantic import BaseModel

class HomeworkOutput(BaseModel):
    is_homework: bool
    reasoning: str

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the user is asking about homework.",
    output_type=HomeworkOutput,
)

async def homework_guardrail(ctx, agent, input_data):
    result = await Runner.run(guardrail_agent, input_data, context=ctx.context)
    final_output = result.final_output_as(HomeworkOutput)
    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=not final_output.is_homework,
    )
```

## すべてをまとめる

ハンドオフと入力ガードレールを組み合わせて、ワークフロー全体を実行してみましょう。

```python
from agents import Agent, InputGuardrail, GuardrailFunctionOutput, Runner
from pydantic import BaseModel
import asyncio

class HomeworkOutput(BaseModel):
    is_homework: bool
    reasoning: str

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the user is asking about homework.",
    output_type=HomeworkOutput,
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You provide help with math problems. Explain your reasoning at each step and include examples",
)

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You provide assistance with historical queries. Explain important events and context clearly.",
)


async def homework_guardrail(ctx, agent, input_data):
    result = await Runner.run(guardrail_agent, input_data, context=ctx.context)
    final_output = result.final_output_as(HomeworkOutput)
    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=not final_output.is_homework,
    )

triage_agent = Agent(
    name="Triage Agent",
    instructions="You determine which agent to use based on the user's homework question",
    handoffs=[history_tutor_agent, math_tutor_agent],
    input_guardrails=[
        InputGuardrail(guardrail_function=homework_guardrail),
    ],
)

async def main():
    result = await Runner.run(triage_agent, "who was the first president of the united states?")
    print(result.final_output)

    result = await Runner.run(triage_agent, "what is life")
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

## トレースの表示

エージェントの実行内容を確認するには、[OpenAI ダッシュボードの Trace viewer](https://platform.openai.com/traces) に移動してトレースを閲覧してください。

## 次のステップ

より複雑なエージェントフローの構築方法を学びましょう。

-   [エージェント](agents.md) の設定方法を学ぶ。
-   [エージェントの実行](running_agents.md) について学ぶ。
-   [ツール](tools.md)、[ガードレール](guardrails.md)、[モデル](models/index.md) について学ぶ。