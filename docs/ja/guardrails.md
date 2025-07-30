---
search:
  exclude: true
---
# ガードレール

ガードレールはエージェントと _並列_ で実行され、ユーザー入力のチェックとバリデーションを行うことができます。  
たとえば、非常に賢い（そのぶん遅く／高価な）モデルを使って顧客リクエストを処理するエージェントがあると想像してください。悪意のあるユーザーがこのモデルに数学の宿題を解かせようとするのは避けたいはずです。そこで、高速かつ低コストのモデルでガードレールを走らせます。ガードレールが悪用を検知した場合、即座にエラーを発生させることで高価なモデルの実行を止め、時間とコストを節約できます。

ガードレールには 2 種類あります。

1. 入力ガードレールは最初のユーザー入力に対して実行されます
2. 出力ガードレールは最終的なエージェント出力に対して実行されます

## 入力ガードレール

入力ガードレールは 3 段階で実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が `true` かどうかを確認します。`true` の場合は [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出され、ユーザーへの応答や例外処理を適切に行えます。

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを想定しているため、エージェントが *最初* のエージェントである場合にのみそのガードレールが実行されます。  
    「なぜ `guardrails` プロパティがエージェント側にあり、`Runner.run` に渡さないのか？」と疑問に思うかもしれません。それは、ガードレールが実際の Agent に密接に関連しているからです。エージェントごとに異なるガードレールを実行するため、コードを同じ場所に置くことで可読性が向上します。

## 出力ガードレール

出力ガードレールも 3 段階で実行されます。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が `true` かどうかを確認します。`true` の場合は [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出され、ユーザーへの応答や例外処理を適切に行えます。

!!! Note

    出力ガードレールは最終的なエージェント出力に対して実行されることを想定しているため、エージェントが *最後* のエージェントである場合にのみそのガードレールが実行されます。  
    入力ガードレールと同様に、ガードレールは実際の Agent に密接に関連しているため、コードを同じ場所に置くことで可読性が向上します。

## トリップワイヤー

入力または出力がガードレールを通過できない場合、ガードレールはトリップワイヤーでその事実を通知できます。トリップワイヤーが発動したガードレールを検知した瞬間に `{Input,Output}GuardrailTripwireTriggered` 例外を直ちに送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。以下の例では、この処理を内部でエージェントを実行する形で行います。

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
2. これはエージェントの入力／コンテキストを受け取り、結果を返すガードレール関数です。  
3. ガードレールの結果には追加情報を含めることができます。  
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

1. こちらは実際のエージェントの出力型です。  
2. こちらはガードレールの出力型です。  
3. これはエージェントの出力を受け取り、結果を返すガードレール関数です。  
4. こちらがワークフローを定義する実際のエージェントです。