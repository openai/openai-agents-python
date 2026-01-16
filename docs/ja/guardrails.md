---
search:
  exclude: true
---
# ガードレール

ガードレールは、 ユーザー 入力や エージェント 出力のチェックと検証を可能にします。たとえば、非常に賢い（つまり遅く/高価な）モデルを使ってカスタマーリクエストを支援する エージェント があるとします。悪意のある ユーザー がそのモデルに数学の宿題を手伝わせるよう求めることは避けたいでしょう。そのため、速く/安価なモデルでガードレールを実行できます。ガードレールが悪意のある使用を検出した場合、即座にエラーを発生させて高価なモデルの実行を防ぎ、時間と費用を節約できます （**blocking ガードレールを使用する場合。parallel ガードレールでは、ガードレールの完了前に高価なモデルがすでに実行を開始している可能性があります。詳細は下記「実行モード」を参照してください**）。

ガードレールには 2 種類あります。

1. 入力ガードレールは最初の ユーザー 入力で実行されます
2. 出力ガードレールは最終的な エージェント 出力で実行されます

## 入力ガードレール

入力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールは エージェント に渡されたのと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それを [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] でラップします
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が送出され、 ユーザー に適切に応答するか、例外を処理できます。

!!! Note

    入力ガードレールは ユーザー 入力で実行されることを想定しているため、 エージェント のガードレールはその エージェント が「最初の」 エージェント の場合にのみ実行されます。なぜ `guardrails` プロパティが エージェント にあり、`Runner.run` に渡さないのか不思議に思うかもしれません。これは、ガードレールが実際の Agent に密接に関連する傾向があるためです。エージェント ごとに異なるガードレールを実行するので、コードを同じ場所に置くことは可読性に役立ちます。

### 実行モード

入力ガードレールは 2 つの実行モードをサポートします。

- **Parallel execution**（デフォルト、`run_in_parallel=True`）: ガードレールは エージェント の実行と同時に並行して動作します。両者が同時に開始するため、待ち時間に最も有利です。ただし、ガードレールが失敗した場合、取り消されるまでに エージェント がすでにトークンを消費し、ツールを実行している可能性があります。

- **Blocking execution**（`run_in_parallel=False`）: ガードレールは エージェント の開始「前に」実行と完了を行います。ガードレールのトリップワイヤーが発火した場合、 エージェント は実行されず、トークン消費とツール実行を防ぎます。これはコスト最適化や、ツール呼び出しによる副作用を回避したい場合に理想的です。

## 出力ガードレール

出力ガードレールは 3 ステップで実行されます。

1. まず、ガードレールは エージェント によって生成された出力を受け取ります。
2. 次に、ガードレール関数が実行され、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それを [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] でラップします
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が送出され、 ユーザー に適切に応答するか、例外を処理できます。

!!! Note

    出力ガードレールは最終的な エージェント 出力で実行されることを想定しているため、 エージェント のガードレールはその エージェント が「最後の」 エージェント の場合にのみ実行されます。入力ガードレールと同様に、ガードレールは実際の Agent に関連する傾向があるため、コードを同じ場所に置くことは可読性に役立ちます。

    出力ガードレールは常に エージェント の完了後に実行されるため、`run_in_parallel` パラメーターはサポートしません。

## ツール ガードレール

ツール ガードレールは、 **関数ツール** をラップし、実行の前後でツール呼び出しを検証またはブロックできるようにします。これはツール自体に設定され、そのツールが呼び出されるたびに実行されます。

- 入力ツール ガードレールはツールの実行前に動作し、呼び出しをスキップしたり、出力をメッセージに置き換えたり、トリップワイヤーを発火させたりできます。
- 出力ツール ガードレールはツールの実行後に動作し、出力を置き換えたり、トリップワイヤーを発火させたりできます。
- ツール ガードレールは、[`function_tool`][agents.function_tool] で作成された 関数ツール にのみ適用されます。ホスト型ツール（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）およびローカルランタイムツール（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）はこのガードレールのパイプラインを使用しません。

詳細は次のコードスニペットを参照してください。

## トリップワイヤー

入力または出力がガードレールに失敗した場合、ガードレールはトリップワイヤーでそれを通知できます。トリップワイヤーが発火したガードレールを検出するとすぐに、`{Input,Output}GuardrailTripwireTriggered` 例外を送出し、Agent の実行を停止します。

## ガードレールの実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。以下の例では、内部的に エージェント を実行してこれを行います。

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

最後に、ツール ガードレールの例を示します。

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