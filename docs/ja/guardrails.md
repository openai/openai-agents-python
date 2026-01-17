---
search:
  exclude: true
---
# ガードレール

ガードレールは、ユーザー入力やエージェント出力のチェックと検証を可能にします。たとえば、非常に賢い（つまり遅く/高価な）モデルで顧客からのリクエストに対応するエージェントがあるとします。悪意のあるユーザーが数学の宿題を手伝うようそのモデルに依頼するのは避けたいはずです。そこで、迅速/低コストなモデルでガードレールを実行できます。ガードレールが悪意ある利用を検知した場合、即座にエラーを発生させて高価なモデルの実行を防ぎ、時間とコストを節約できます（ **ブロッキング型のガードレール使用時。並列型ガードレールでは、ガードレールの完了前に高価なモデルが実行を開始している可能性があります。詳細は下記「実行モード」を参照** ）。

ガードレールには 2 種類あります。

1. 入力ガードレールは最初のユーザー入力で実行されます
2. 出力ガードレールは最終的なエージェント出力で実行されます

## 入力ガードレール

入力ガードレールは次の 3 ステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出され、適切にユーザーへ応答するか、例外処理ができます。

!!! Note

    入力ガードレールはユーザー入力での実行を想定しているため、エージェントのガードレールはそのエージェントが「最初の」エージェントである場合にのみ実行されます。なぜ `guardrails` プロパティがエージェント側にあり、`Runner.run` に渡さないのかと疑問に思うかもしれません。これは、ガードレールが実際のエージェントと密接に関連する傾向があるためです。エージェントごとに異なるガードレールを実行することになるので、コードを同じ場所に置く方が可読性に優れます。

### 実行モード

入力ガードレールは 2 つの実行モードをサポートします。

- **並列実行**（デフォルト、`run_in_parallel=True`）: ガードレールはエージェントの実行と同時に実行されます。両者が同時に開始されるためレイテンシが最小になります。ただし、ガードレールが失敗した場合でも、キャンセルされるまでにエージェントがすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行**（`run_in_parallel=False`）: ガードレールはエージェントが開始する「前に」実行・完了します。ガードレールのトリップワイヤーが発火した場合、エージェントは実行されず、トークン消費やツール実行を防げます。コスト最適化や、ツール呼び出しによる副作用を避けたい場合に最適です。

## 出力ガードレール

出力ガードレールは次の 3 ステップで実行されます。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出され、適切にユーザーへ応答するか、例外処理ができます。

!!! Note

    出力ガードレールは最終的なエージェント出力での実行を想定しているため、エージェントのガードレールはそのエージェントが「最後の」エージェントである場合にのみ実行されます。入力ガードレールと同様に、ガードレールは実際のエージェントと関連する傾向があるため、コードを同じ場所に置く方が可読性に優れます。

    出力ガードレールは常にエージェントの完了後に実行されるため、`run_in_parallel` パラメーターはサポートしません。

## ツールのガードレール

ツールのガードレールは **関数ツール** をラップし、実行の前後でツール呼び出しを検証またはブロックできます。これはツール自体に設定され、そのツールが呼び出されるたびに実行されます。

- 入力ツールガードレールはツール実行前に走り、呼び出しをスキップしたり、出力をメッセージに置き換えたり、トリップワイヤーを発火させたりできます。
- 出力ツールガードレールはツール実行後に走り、出力を置き換えたり、トリップワイヤーを発火させたりできます。
- ツールのガードレールは、[`function_tool`][agents.function_tool] で作成された関数ツールにのみ適用されます。ホスト型ツール（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）およびローカルランタイムツール（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）は、このガードレールのパイプラインを使用しません。

詳細は以下のコードスニペットを参照してください。

## トリップワイヤー

入力または出力がガードレールに不合格となった場合、ガードレールはトリップワイヤーでそれを通知できます。トリップワイヤーが発火したガードレールを検知するとすぐに、`{Input,Output}GuardrailTripwireTriggered` 例外を送出し、エージェントの実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。次の例では、内部でエージェントを実行してこれを行います。

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
3. ガードレール結果に追加情報を含めることができます。
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

最後に、ツールのガードレールの例です。

```python
import json
from agents import (
    Agent,
    Runner,
    ToolGuardrailFunctionOutput,
    function_tool,
    tool_input_guardrail,
    tool_output_guardrail,
)

@tool_input_guardrail
def block_secrets(data):
    args = json.loads(data.context.tool_arguments or "{}")
    if "sk-" in json.dumps(args):
        return ToolGuardrailFunctionOutput.reject_content(
            "Remove secrets before calling this tool."
        )
    return ToolGuardrailFunctionOutput.allow()


@tool_output_guardrail
def redact_output(data):
    text = str(data.output or "")
    if "sk-" in text:
        return ToolGuardrailFunctionOutput.reject_content("Output contained sensitive data.")
    return ToolGuardrailFunctionOutput.allow()


@function_tool(
    tool_input_guardrails=[block_secrets],
    tool_output_guardrails=[redact_output],
)
def classify_text(text: str) -> str:
    """Classify text for internal routing."""
    return f"length:{len(text)}"


agent = Agent(name="Classifier", tools=[classify_text])
result = Runner.run_sync(agent, "hello world")
print(result.final_output)
```