---
search:
  exclude: true
---
# ガードレール

ガードレールはエージェントと _並列_ に実行され、ユーザー入力のチェックと検証を行います。たとえば、顧客対応に非常に賢い（そのため遅く/高価な）モデルを使うエージェントがあるとします。悪意のあるユーザーがそのモデルに数学の宿題を手伝わせるよう依頼するのは避けたいはずです。そこで、速く/安価なモデルでガードレールを走らせます。ガードレールが不正利用を検出した場合、即座にエラーを発生させ、高価なモデルの実行を止め、時間やコストを節約できます。

ガードレールには 2 種類あります:

1. 入力ガードレールは最初のユーザー入力に対して実行されます
2. 出力ガードレールは最終的なエージェントの出力に対して実行されます

## 入力ガードレール

入力ガードレールは 3 ステップで実行されます:

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合は [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が発生し、ユーザーへの適切な応答や例外処理が可能になります。

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを意図しているため、エージェントのガードレールはそのエージェントが「最初の」エージェントの場合にのみ実行されます。なぜ `guardrails` プロパティがエージェント側にあり、`Runner.run` に渡されないのかと疑問に思うかもしれません。これは、ガードレールが実際のエージェントに密接に関連する傾向があるためです。エージェントごとに異なるガードレールを実行するため、コードを同じ場所に置くことで可読性が向上します。

## 出力ガードレール

出力ガードレールは 3 ステップで実行されます:

1. まず、ガードレールはエージェントが生成した出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、これが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合は [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が発生し、ユーザーへの適切な応答や例外処理が可能になります。

!!! Note

    出力ガードレールは最終的なエージェントの出力に対して実行されることを意図しているため、エージェントのガードレールはそのエージェントが「最後の」エージェントの場合にのみ実行されます。入力ガードレールと同様に、ガードレールは実際のエージェントに関連する傾向があるため、コードを同じ場所に置くことで可読性が向上します。

## トリップワイヤー

入力または出力がガードレールに失敗した場合、ガードレールはトリップワイヤーでそれを示せます。トリップワイヤーが作動したガードレールを検出した時点で、直ちに `{Input,Output}GuardrailTripwireTriggered` 例外を発生させ、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。次の例では、その裏でエージェントを実行して実現します。

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
3. ガードレール結果に追加情報を含められます。
4. これはワークフローを定義する実際のエージェントです。

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

1. これは実際のエージェントの出力型です。
2. これはガードレールの出力型です。
3. これはエージェントの出力を受け取り、結果を返すガードレール関数です。
4. これはワークフローを定義する実際のエージェントです。