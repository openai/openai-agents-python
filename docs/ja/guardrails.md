---
search:
  exclude: true
---
# ガードレール

ガードレールはエージェントと _並列で_ 実行され、ユーザー入力のチェックとバリデーションを行います。たとえば、非常に賢い（そのため遅くて高コストな）モデルを使ってカスタマーリクエストを処理するエージェントがあるとします。悪意あるユーザーがそのモデルに数学の宿題を手伝わせようとするのは避けたいでしょう。そこで、低コストかつ高速なモデルでガードレールを実行できます。もしガードレールが悪意ある使用を検知した場合、直ちにエラーを発生させることで高コストなモデルの実行を停止し、時間と費用を節約できます。

ガードレールには 2 種類あります。

1. Input ガードレールは最初のユーザー入力で実行されます  
2. Output ガードレールは最終的なエージェント出力で実行されます  

## 入力ガードレール

入力ガードレールは 3 段階で実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。  
3. 最後に [.tripwire_triggered][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出されるため、ユーザーへの応答や例外処理を適切に行えます。  

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを意図しているため、エージェントが *最初の* エージェントである場合にのみ実行されます。「guardrails」プロパティがエージェントにあるのに、なぜ `Runner.run` に渡さないのか疑問に思うかもしれません。これはガードレールが実際の Agent と密接に関連していることが多く、エージェントごとに異なるガードレールを実行するため、コードを同じ場所に置いておくと可読性が向上するためです。

## 出力ガードレール

出力ガードレールも 3 段階で実行されます。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。  
3. 最後に [.tripwire_triggered][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出されるため、ユーザーへの応答や例外処理を適切に行えます。  

!!! Note

    出力ガードレールは最終的なエージェント出力に対して実行されることを意図しているため、エージェントが *最後の* エージェントである場合にのみ実行されます。入力ガードレールと同様、ガードレールは実際の Agent と密接に関連しているため、エージェントごとに異なるガードレールを実行することになり、コードを同じ場所に置いておくと可読性が向上します。

## トリップワイヤ

入力または出力がガードレールに失敗した場合、ガードレールはトリップワイヤでそれを通知できます。トリップワイヤが作動したガードレールを検出すると、ただちに `{Input,Output}GuardrailTripwireTriggered` 例外を送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を提供する必要があります。この例では、内部でエージェントを実行してそれを行います。

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
2. こちらがエージェントの入力／コンテキストを受け取り、結果を返すガードレール関数です。  
3. ガードレール結果に追加情報を含めることもできます。  
4. こちらがワークフローを定義する実際のエージェントです。  

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

1. こちらが実際のエージェントの出力型です。  
2. こちらがガードレールの出力型です。  
3. こちらがエージェントの出力を受け取り、結果を返すガードレール関数です。  
4. こちらがワークフローを定義する実際のエージェントです。