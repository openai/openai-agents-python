---
search:
  exclude: true
---
# ガードレール

ガードレールを使うと、ユーザー入力とエージェント出力のチェックや検証を行えます。たとえば、顧客リクエスト対応のために非常に高性能（したがって低速 / 高コスト）なモデルを使うエージェントがあるとします。悪意のあるユーザーに、そのモデルで数学の宿題を手伝わせたくはありません。そのため、高速 / 低コストなモデルでガードレールを実行できます。ガードレールが悪意のある利用を検知した場合、すぐにエラーを発生させて高コストなモデルの実行を防げます。これにより時間とコストを節約できます（ **blocking guardrails** を使う場合。並列ガードレールでは、ガードレール完了前に高コストなモデルがすでに実行を開始している可能性があります。詳細は下記の「実行モード」を参照してください）。

ガードレールには 2 種類あります。

1. Input ガードレールは最初のユーザー入力で実行されます
2. Output ガードレールは最終的なエージェント出力で実行されます

## ワークフロー境界

ガードレールはエージェントとツールにアタッチされますが、ワークフロー内の同じタイミングで実行されるわけではありません。

-   **Input ガードレール** はチェーン内の最初のエージェントに対してのみ実行されます。
-   **Output ガードレール** は最終出力を生成するエージェントに対してのみ実行されます。
-   **ツールガードレール** はカスタム関数ツールの呼び出しごとに実行され、Input ガードレールは実行前、Output ガードレールは実行後に実行されます。

manager、ハンドオフ、または委譲された specialist を含むワークフローで、カスタム関数ツール呼び出しごとにチェックが必要な場合は、エージェントレベルの Input / Output ガードレールのみに頼るのではなく、ツールガードレールを使用してください。

## Input ガードレール

Input ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行されて [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合は [`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が発生するため、ユーザーへの適切な応答や例外処理を行えます。

!!! Note

    Input ガードレールはユーザー入力に対して実行することを想定しているため、エージェントのガードレールはそのエージェントが *最初* のエージェントである場合にのみ実行されます。`guardrails` プロパティが `Runner.run` に渡されるのではなくエージェント側にある理由は何か、と疑問に思うかもしれません。これは、ガードレールが実際の Agent に関連することが多く、エージェントごとに異なるガードレールを実行するため、コードを同じ場所に置くことで可読性が向上するためです。

### 実行モード

Input ガードレールは 2 つの実行モードをサポートしています。

- **並列実行**（デフォルト、`run_in_parallel=True`）: ガードレールはエージェント実行と同時に並行して実行されます。両方が同時に開始されるため、レイテンシの面で最も有利です。ただし、ガードレールが失敗した場合、キャンセルされる前にエージェントがすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行**（`run_in_parallel=False`）: ガードレールはエージェント開始 *前* に実行され、完了します。ガードレールの tripwire がトリガーされた場合、エージェントは実行されないため、トークン消費とツール実行を防げます。これはコスト最適化に理想的で、ツール呼び出しによる潜在的な副作用を避けたい場合にも適しています。

## Output ガードレール

Output ガードレールは 3 ステップで実行されます。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。
2. 次に、ガードレール関数が実行されて [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合は [`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が発生するため、ユーザーへの適切な応答や例外処理を行えます。

!!! Note

    Output ガードレールは最終的なエージェント出力に対して実行することを想定しているため、エージェントのガードレールはそのエージェントが *最後* のエージェントである場合にのみ実行されます。Input ガードレールと同様に、これはガードレールが実際の Agent に関連することが多く、エージェントごとに異なるガードレールを実行するため、コードを同じ場所に置くことで可読性が向上するためです。

    Output ガードレールは常にエージェント完了後に実行されるため、`run_in_parallel` パラメーターはサポートしていません。

## ツールガードレール

ツールガードレールは **function tools** をラップし、実行の前後でツール呼び出しを検証またはブロックできます。設定はツール自体に対して行い、そのツールが呼び出されるたびに実行されます。

- Input ツールガードレールはツール実行前に実行され、呼び出しをスキップする、メッセージで出力を置き換える、または tripwire を発生させることができます。
- Output ツールガードレールはツール実行後に実行され、出力を置き換えるか、tripwire を発生させることができます。
- ツールガードレールは [`function_tool`][agents.tool.function_tool] で作成された関数ツールにのみ適用されます。ハンドオフは通常の関数ツールパイプラインではなく SDK のハンドオフパイプラインを通るため、ツールガードレールはハンドオフ呼び出し自体には適用されません。Hosted ツール（`WebSearchTool`、`FileSearchTool`、`HostedMCPTool`、`CodeInterpreterTool`、`ImageGenerationTool`）および組み込み実行ツール（`ComputerTool`、`ShellTool`、`ApplyPatchTool`、`LocalShellTool`）もこのガードレールパイプラインを使用せず、[`Agent.as_tool()`][agents.agent.Agent.as_tool] でも現在はツールガードレールオプションを直接公開していません。

詳細は以下のコードスニペットを参照してください。

## トリップワイヤー

入力または出力がガードレールに失敗した場合、Guardrail は tripwire でこれを通知できます。tripwire がトリガーされたガードレールを検知すると、直ちに `{Input,Output}GuardrailTripwireTriggered` 例外を発生させ、Agent の実行を停止します。

## ガードレール実装

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を提供する必要があります。この例では、内部で Agent を実行してこれを実現します。

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
2. これはエージェントの入力 / コンテキストを受け取り、結果を返すガードレール関数です。
3. ガードレール結果には追加情報を含められます。
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

最後に、ツールガードレールの例を示します。

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