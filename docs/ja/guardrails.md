---
search:
  exclude: true
---
# ガードレール

ガードレールは エージェント と _並列_ に実行され、 ユーザー入力 のチェックとバリデーションを行います。たとえば、顧客からのリクエストを支援するために非常に賢い (そのため遅く / 高価な) モデルを使うエージェントがあるとします。悪意のある ユーザー がモデルに数学の宿題を手伝わせようとするのは避けたいですよね。その場合、 高速 / 低コスト のモデルでガードレールを実行できます。ガードレールが悪意のある利用を検知した場合、即座にエラーを送出して高価なモデルの実行を停止し、時間と費用を節約できます。

ガードレールには 2 種類あります。

1. Input ガードレールは最初の ユーザー入力 に対して実行されます  
2. Output ガードレールは最終的なエージェント出力に対して実行されます  

## Input ガードレール

Input ガードレールは 3 つのステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] でラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出されるので、 ユーザー への適切な応答や例外処理を行えます。  

!!! Note

    Input ガードレールは ユーザー入力 に対して実行されることを想定しているため、エージェントのガードレールが実行されるのはそのエージェントが *最初* のエージェントである場合だけです。「なぜ `guardrails` プロパティがエージェントにあり、 `Runner.run` に渡さないのか？」と思うかもしれません。ガードレールは実際の エージェント に密接に関連する場合が多く、エージェントごとに異なるガードレールを実行するため、コードを同じ場所に置くことで可読性が向上するからです。

## Output ガードレール

Output ガードレールは 3 つのステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] でラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出されるので、 ユーザー への適切な応答や例外処理を行えます。  

!!! Note

    Output ガードレールは最終的なエージェント出力に対して実行されることを想定しているため、エージェントのガードレールが実行されるのはそのエージェントが *最後* のエージェントである場合だけです。Input ガードレール同様、ガードレールは実際の エージェント に密接に関連するため、コードを同じ場所に置くことで可読性が向上します。

## トリップワイヤ

入力または出力がガードレールに失敗した場合、ガードレールはトリップワイヤを用いてそれを通知できます。ガードレールがトリップワイヤを発火したことを検知すると、ただちに `{Input,Output}GuardrailTripwireTriggered` 例外を送出してエージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。次の例では、内部で エージェント を実行してこれを行います。

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
2. これはエージェントの入力 / コンテキストを受け取り、結果を返すガードレール関数です。  
3. ガードレール結果に追加情報を含めることができます。  
4. これはワークフローを定義する実際のエージェントです。  

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

1. これは実際のエージェントの出力型です。  
2. これはガードレールの出力型です。  
3. これはエージェントの出力を受け取り、結果を返すガードレール関数です。  
4. これはワークフローを定義する実際のエージェントです。