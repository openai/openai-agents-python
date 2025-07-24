---
search:
  exclude: true
---
# ガードレール

ガードレールはエージェントと _並列_ に実行され、ユーザー入力のチェックおよびバリデーションを行えるようにします。たとえば、カスタマーリクエストを処理するために非常に高性能（つまり遅くて高価）なモデルを使用する エージェント があるとしましょう。悪意のある ユーザー がそのモデルに数学の宿題を手伝わせようとするのは避けたいはずです。そこで、低コストかつ高速なモデルでガードレールを実行できます。ガードレールが悪意ある使用を検知した場合、直ちにエラーを発生させ、高価なモデルの実行を止めて時間とコストを節約できます。

ガードレールには 2 種類あります。

1. 入力ガードレール: 初期ユーザー入力に対して実行されます  
2. 出力ガードレール: 最終エージェント出力に対して実行されます  

## 入力ガードレール

入力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] でラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が発生し、ユーザーへの適切な対応や例外処理を行えます。  

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを想定しているため、エージェントが *最初* のエージェントである場合にのみガードレールが実行されます。「guardrails」プロパティがエージェント上にある理由は何でしょうか？ `Runner.run` に渡さないのは、ガードレールが実際の Agent に密接に関係する傾向があるためです。エージェントごとに異なるガードレールを実行することが多く、コードを同じ場所に置くことで可読性が向上します。

## 出力ガードレール

出力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。  
2. 次に、ガードレール関数が実行され [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] でラップされます。  
3. 最後に [`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が発生し、ユーザーへの適切な対応や例外処理を行えます。  

!!! Note

    出力ガードレールは最終エージェント出力に対して実行されることを想定しているため、エージェントが *最後* のエージェントである場合にのみガードレールが実行されます。入力ガードレールと同様に、ガードレールは実際の Agent に密接に関係するため、コードを同じ場所に置くことで可読性が向上します。

## トリップワイヤ

入力または出力がガードレールに失敗した場合、ガードレールはトリップワイヤでそれを通知できます。トリップワイヤが発火したガードレールを検知するとすぐに `{Input,Output}GuardrailTripwireTriggered` 例外を発生させ、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。次の例では、その内部で エージェント を実行してガードレールを実装します。

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
4. こちらが実際のエージェントで、ワークフローを定義します。  

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
4. こちらが実際のエージェントで、ワークフローを定義します。