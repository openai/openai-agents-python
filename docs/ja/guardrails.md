---
search:
  exclude: true
---
# ガードレール

ガードレールは、 ユーザー 入力と エージェント 出力に対してチェックと検証を行えるようにします。例えば、カスタマーリクエストを支援するために非常に賢い（そのぶん遅く/高価な）モデルを使う エージェント を想像してください。悪意のある ユーザー がそのモデルに数学の宿題を手伝わせるような依頼をするのは避けたいはずです。そこで、速く/安価なモデルでガードレールを実行できます。ガードレールが不正使用を検知した場合、即座にエラーを発生させて高価なモデルの実行を防ぎ、時間とコストを節約できます（**ブロッキング型のガードレール使用時。並列ガードレールでは、ガードレール完了前に高価なモデルがすでに実行を開始している可能性があります。詳細は下記「実行モード」を参照**）。

ガードレールには 2 種類あります:

1. 入力ガードレールは初期の ユーザー 入力で実行されます
2. 出力ガードレールは最終的な エージェント 出力で実行されます

## 入力ガードレール

入力ガードレールは 3 ステップで動作します:

1. まず、ガードレールは エージェント に渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出されるため、 ユーザー への適切な応答や例外処理が可能です。

!!! Note

    入力ガードレールは ユーザー 入力に対して実行されることを意図しているため、 エージェント のガードレールは、その エージェント が「最初の」 エージェント の場合にのみ実行されます。なぜ `guardrails` プロパティが エージェント 側にあり、`Runner.run` に渡さないのかと疑問に思うかもしれません。これは、ガードレールが実際の エージェント に密接に関連する傾向があるからです。 エージェント ごとに異なるガードレールを実行するため、コードを同じ場所に置いておくと可読性が向上します。

### 実行モード

入力ガードレールは 2 つの実行モードをサポートします:

- **並列実行**（デフォルト、`run_in_parallel=True`）: ガードレールは エージェント の実行と同時に並行して実行されます。両者が同時に開始するため、レイテンシは最小になります。ただし、ガードレールが失敗した場合でも、キャンセルされる前に エージェント がすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行**（`run_in_parallel=False`）: ガードレールは エージェント の開始「前」に実行・完了します。ガードレールのトリップワイヤーが発火した場合、 エージェント は実行されず、トークン消費やツール実行を防げます。これはコスト最適化や、ツール呼び出しによる副作用を避けたい場合に最適です。

## 出力ガードレール

出力ガードレールは 3 ステップで動作します:

1. まず、ガードレールは エージェント によって生成された出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます。
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出されるため、 ユーザー への適切な応答や例外処理が可能です。

!!! Note

    出力ガードレールは最終的な エージェント の出力に対して実行されることを意図しているため、 エージェント のガードレールは、その エージェント が「最後の」 エージェント の場合にのみ実行されます。入力ガードレールと同様に、ガードレールは実際の エージェント に密接に関連する傾向があるため、コードを同じ場所に置いておくと可読性が向上します。

    出力ガードレールは常に エージェント の完了後に実行されるため、`run_in_parallel` パラメーターはサポートされません。

## トリップワイヤー

入力または出力がガードレールに不合格となった場合、ガードレールはトリップワイヤーでそれを通知できます。トリップワイヤーが発火したガードレールを検知するとすぐに、{Input,Output}GuardrailTripwireTriggered 例外を送出し、 エージェント の実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、水面下で エージェント を実行することでこれを行います。

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
2. これは エージェント の入力/コンテキストを受け取り、結果を返すガードレール関数です。
3. ガードレール結果に追加情報を含めることができます。
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