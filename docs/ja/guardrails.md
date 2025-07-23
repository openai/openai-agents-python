---
search:
  exclude: true
---
# ガードレール

ガードレールはエージェントと _並行して_ 実行され、ユーザー入力のチェックとバリデーションを行えます。たとえば、非常に高性能（その分、低速かつ高コスト）のモデルを用いて顧客の問い合わせを処理するエージェントがあるとします。悪意のあるユーザーに、そのモデルを使って数学の宿題を解かせたくはありません。そこで、低コストかつ高速なモデルでガードレールを実行します。ガードレールが不正利用を検知した場合、ただちにエラーを発生させ、高価なモデルの実行を止めて時間とコストを節約できます。

ガードレールには 2 種類あります:

1. Input ガードレールは最初のユーザー入力に対して実行されます  
2. Output ガードレールは最終的なエージェント出力に対して実行されます

## Input ガードレール

Input ガードレールは 3 段階で実行されます:

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。  
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成します。その結果は [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] でラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が `true` かどうかを確認します。`true` の場合は [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出されるため、ユーザーへ適切に応答するか、例外を処理できます。

!!! Note

    Input ガードレールはユーザー入力に対して実行されることを想定しています。そのためガードレールはエージェントが *最初の* エージェントである場合にのみ実行されます。「`guardrails` プロパティがエージェントにあるのはなぜで、`Runner.run` に渡さないのか？」と思うかもしれません。ガードレールは実際のエージェントと強く関連する傾向があるため、エージェントごとに異なるガードレールを設定します。コードを同じ場所に置くことで可読性が向上します。

## Output ガードレール

Output ガードレールは 3 段階で実行されます:

1. まず、ガードレールはエージェントが生成した出力を受け取ります。  
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成します。その結果は [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] でラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が `true` かどうかを確認します。`true` の場合は [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出されるため、ユーザーへ適切に応答するか、例外を処理できます。

!!! Note

    Output ガードレールは最終的なエージェント出力に対して実行されることを想定しています。そのためガードレールはエージェントが *最後の* エージェントである場合にのみ実行されます。Input ガードレールと同様に、ガードレールは実際のエージェントと強く関連するため、コードを同じ場所に置くことで可読性が向上します。

## Tripwires

入力または出力がガードレールに失敗した場合、ガードレールはトリップワイヤーでこれを通知できます。トリップワイヤーが発火したガードレールを検知すると、ただちに `{Input,Output}GuardrailTripwireTriggered` 例外を送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。以下の例では、その裏でエージェントを実行してこれを行います。

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    input_guardrail,
)

class MathHomeworkOutput(BaseModel):
    is_math_homework: bool
    reasoning: str

guardrail_agent = Agent( # (1)!
    name="Guardrail check",
    instructions="Check if the user is asking you to do their math homework.",
    output_type=MathHomeworkOutput,
)


@input_guardrail
async def math_guardrail( # (2)!
    ctx: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    result = await Runner.run(guardrail_agent, input, context=ctx.context)

    return GuardrailFunctionOutput(
        output_info=result.final_output, # (3)!
        tripwire_triggered=result.final_output.is_math_homework,
    )


agent = Agent(  # (4)!
    name="Customer support agent",
    instructions="You are a customer support agent. You help customers with their questions.",
    input_guardrails=[math_guardrail],
)

async def main():
    # This should trip the guardrail
    try:
        await Runner.run(agent, "Hello, can you help me solve for x: 2x + 3 = 11?")
        print("Guardrail didn't trip - this is unexpected")

    except InputGuardrailTripwireTriggered:
        print("Math homework guardrail tripped")
```

1. このエージェントをガードレール関数内で使用します。  
2. これはエージェントの入力/コンテキストを受け取り、結果を返すガードレール関数です。  
3. ガードレール結果には追加情報を含めることができます。  
4. こちらがワークフローを定義する実際のエージェントです。

Output ガードレールも同様です。

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    output_guardrail,
)
class MessageOutput(BaseModel): # (1)!
    response: str

class MathOutput(BaseModel): # (2)!
    reasoning: str
    is_math: bool

guardrail_agent = Agent(
    name="Guardrail check",
    instructions="Check if the output includes any math.",
    output_type=MathOutput,
)

@output_guardrail
async def math_guardrail(  # (3)!
    ctx: RunContextWrapper, agent: Agent, output: MessageOutput
) -> GuardrailFunctionOutput:
    result = await Runner.run(guardrail_agent, output.response, context=ctx.context)

    return GuardrailFunctionOutput(
        output_info=result.final_output,
        tripwire_triggered=result.final_output.is_math,
    )

agent = Agent( # (4)!
    name="Customer support agent",
    instructions="You are a customer support agent. You help customers with their questions.",
    output_guardrails=[math_guardrail],
    output_type=MessageOutput,
)

async def main():
    # This should trip the guardrail
    try:
        await Runner.run(agent, "Hello, can you help me solve for x: 2x + 3 = 11?")
        print("Guardrail didn't trip - this is unexpected")

    except OutputGuardrailTripwireTriggered:
        print("Math output guardrail tripped")
```

1. こちらは実際のエージェントの出力型です。  
2. こちらはガードレールの出力型です。  
3. これはエージェントの出力を受け取り、結果を返すガードレール関数です。  
4. こちらがワークフローを定義する実際のエージェントです。