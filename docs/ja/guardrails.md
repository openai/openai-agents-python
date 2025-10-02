---
search:
  exclude: true
---
# ガードレール

ガードレールは _並行して_ あなたの エージェント と動作し、ユーザー入力のチェックと検証を行えます。例えば、非常に賢い（そのため遅く / 高価な）モデルで顧客対応を行う エージェント があるとします。悪意ある ユーザー がそのモデルに数学の宿題を手伝わせるよう求めるのは避けたいはずです。そのため、速く / 低コストなモデルでガードレールを実行できます。ガードレールが悪意ある使用を検知した場合は、直ちにエラーを発生させ、高価なモデルの実行を止めて時間とコストを節約できます。

ガードレールには 2 つの種類があります:

1. 入力ガードレールは最初の ユーザー 入力に対して実行されます
2. 出力ガードレールは最終的な エージェント の出力に対して実行されます

## 入力ガードレール

入力ガードレールは 3 つのステップで動作します:

1. まず、ガードレールは エージェント に渡されるのと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出され、ユーザーへの適切な応答や例外処理が可能になります。

!!! Note

    入力ガードレールは ユーザー 入力に対して実行されることを意図しているため、ある エージェント のガードレールは、その エージェント が「最初の」エージェントである場合にのみ実行されます。「なぜ `guardrails` プロパティは エージェント 側にあり、`Runner.run` に渡さないのか」と疑問に思われるかもしれません。これは、ガードレールが実際の Agent に密接に関係する傾向があるからです。エージェント ごとに異なるガードレールを実行するため、コードを同じ場所に置くことで可読性が向上します。

## 出力ガードレール

出力ガードレールは 3 つのステップで動作します:

1. まず、ガードレールは エージェント によって生成された出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出され、ユーザーへの適切な応答や例外処理が可能になります。

!!! Note

    出力ガードレールは最終的な エージェント の出力に対して実行されることを意図しているため、ある エージェント のガードレールは、その エージェント が「最後の」エージェントである場合にのみ実行されます。入力ガードレールと同様に、ガードレールは実際の Agent に密接に関係する傾向があるため、エージェント ごとに異なるガードレールを実行でき、コードを同じ場所に置くことが可読性に有用です。

## トリップワイヤー

入力または出力がガードレールに合格しなかった場合、ガードレールはトリップワイヤーでそれを示せます。トリップワイヤーが作動したガードレールが確認された時点で、直ちに `{Input,Output}GuardrailTripwireTriggered` 例外を送出し、Agent の実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、内部で エージェント を実行することでこれを行います。

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

1. この エージェント をガードレール関数内で使用します。
2. これは エージェント の入力 / コンテキストを受け取り、結果を返すガードレール関数です。
3. ガードレール結果に追加情報を含められます。
4. これはワークフローを定義する実際の エージェント です。

出力ガードレールも同様です。

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

1. これは実際の エージェント の出力型です。
2. これはガードレールの出力型です。
3. これは エージェント の出力を受け取り、結果を返すガードレール関数です。
4. これはワークフローを定義する実際の エージェント です。