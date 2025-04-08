# ガードレール

ガードレールは、エージェント と _並行して_ 実行され、ユーザー入力のチェックや検証を可能にします。例えば、顧客のリクエストを処理するために非常に高性能（したがって遅く高価）なモデルを使用する エージェント があるとします。悪意のあるユーザーがそのモデルに数学の宿題を解かせるようなことは避けたいでしょう。そのため、高速で安価なモデルを使った ガードレール を実行できます。ガードレール が悪意のある使用を検知すると、即座にエラーを発生させ、高価なモデルの実行を停止し、時間とコストを節約できます。

ガードレール には 2 種類あります：

1. 入力ガードレール：最初のユーザー入力に対して実行されます。
2. 出力ガードレール：最終的な エージェント の出力に対して実行されます。

## 入力ガードレール

入力ガードレール は次の 3 ステップで実行されます：

1. 最初に、ガードレール は エージェント に渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が発生し、ユーザーへの適切な応答や例外処理が可能になります。

!!! Note

    入力ガードレール はユーザー入力に対して実行されるため、エージェント の ガードレール は、その エージェント が *最初の* エージェント である場合にのみ実行されます。なぜ `guardrails` プロパティが エージェント にあり、`Runner.run` に渡されないのか疑問に思うかもしれませんが、これは ガードレール が実際の エージェント に関連付けられる傾向があるためです。異なる エージェント には異なる ガードレール を実行するため、コードを同じ場所に配置することで可読性が向上します。

## 出力ガードレール

出力ガードレール は次の 3 ステップで実行されます：

1. 最初に、ガードレール は エージェント に渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が発生し、ユーザーへの適切な応答や例外処理が可能になります。

!!! Note

    出力ガードレール は最終的な エージェント の出力に対して実行されるため、エージェント の ガードレール は、その エージェント が *最後の* エージェント である場合にのみ実行されます。入力ガードレール と同様に、これは ガードレール が実際の エージェント に関連付けられる傾向があるためです。異なる エージェント には異なる ガードレール を実行するため、コードを同じ場所に配置することで可読性が向上します。

## トリップワイヤ（Tripwires）

入力または出力が ガードレール を通過できない場合、ガードレール はトリップワイヤを使ってこれを通知します。トリップワイヤがトリガーされた ガードレール を検知すると、即座に `{Input,Output}GuardrailTripwireTriggered` 例外を発生させ、エージェント の実行を停止します。

## ガードレール の実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を提供する必要があります。この例では、内部で エージェント を実行することでこれを実現します。

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

1. この エージェント を ガードレール関数 内で使用します。
2. これは エージェント の入力とコンテキストを受け取り、結果を返す ガードレール関数 です。
3. ガードレール の結果に追加情報を含めることができます。
4. これはワークフローを定義する実際の エージェント です。

出力ガードレール も同様です。

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

1. これは実際の エージェント の出力タイプです。
2. これは ガードレール の出力タイプです。
3. これは エージェント の出力を受け取り、結果を返す ガードレール関数 です。
4. これはワークフローを定義する実際の エージェント です。