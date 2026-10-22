---
search:
  exclude: true
---
# ガードレール

ガードレールを使用すると、ユーザー入力とエージェント出力のチェックおよび検証を行えます。たとえば、非常に高性能である一方、低速でコストも高いモデルを使用して、顧客からの依頼に対応するエージェントがあるとします。悪意のあるユーザーに、数学の宿題を手伝うようモデルへ依頼されるのは望ましくありません。そこで、高速で低コストのモデルを使用してガードレールを実行できます。ガードレールが悪意のある利用を検出すると、すぐにエラーを発生させ、時間とコストを節約できます。ブロッキング実行では、高コストのモデルが開始されないことが保証されます。一方、並列実行では、ガードレールが完了する前に高コストのモデルがすでに開始されている場合があります。詳細については、以下の「実行モード」を参照してください。

ガードレールには、次の 2 種類があります。

1. 入力ガードレールは、最初のユーザー入力に対して実行されます
2. 出力ガードレールは、最終的なエージェント出力に対して実行されます

## ワークフローの境界 {#workflow-boundaries}

ガードレールはエージェントやツールに設定されますが、すべてがワークフロー内の同じ時点で実行されるわけではありません。

-   **入力ガードレール** は、チェーン内の最初のエージェントに対してのみ実行されます。
-   **出力ガードレール** は、最終出力を生成するエージェントに対してのみ実行されます。
-   **ツールガードレール** は、サーバーでガードレールが設定されているローカル MCP ツールを含め、ガードレールが設定された関数ツールが呼び出されるたびに実行されます。入力ガードレールは実行前に、出力ガードレールは実行後に実行されます。

マネージャー、ハンドオフ、または処理を委任された専門エージェントを含むワークフローで、カスタム関数ツールを呼び出すたびに前後のチェックが必要な場合は、エージェントレベルの入力ガードレールや出力ガードレールだけに依存せず、ツールガードレールを使用してください。

## 入力ガードレール {#input-guardrails}

ガードレールは外部の分類器を直接呼び出すことができ、別のエージェントを実行する必要はありません。ネイティブの OpenAI Decisions クライアントの例については、[Decisions API](models/index.md#decisions-api) を参照してください。

入力ガードレールは、次の 3 ステップで実行されます。

1. まず、ガードレールはエージェントに渡されたものと同じ入力を受け取ります。
2. 次に、ガードレール関数が実行されて [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`InputGuardrailResult`][agents.guardrail.InputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`InputGuardrailTripwireTriggered`][agents.exceptions.InputGuardrailTripwireTriggered] 例外が発生するため、ユーザーへ適切に応答したり、例外を処理したりできます。

!!! Note

    入力ガードレールはユーザー入力に対して実行されることを想定しているため、エージェントのガードレールは、そのエージェントが *最初の* エージェントである場合にのみ実行されます。なぜ `guardrails` プロパティが `Runner.run` に渡されるのではなく、エージェント上にあるのか疑問に思うかもしれません。これは、ガードレールが実際のエージェントに関連する傾向があるためです。エージェントごとに異なるガードレールを実行するため、コードを同じ場所に配置すると可読性が向上します。

### 実行モード {#execution-modes}

入力ガードレールは、次の 2 つの実行モードをサポートしています。

- **並列実行** （デフォルト、 `run_in_parallel=True` ）: ガードレールはエージェントの実行と並行して実行されます。両方が同時に開始されるため、レイテンシーを最小限に抑えられます。ただし、ガードレールのトリップワイヤーが作動した場合でも、キャンセルされるまでにエージェントがすでにトークンを消費し、ツールを実行している可能性があります。

- **ブロッキング実行** （ `run_in_parallel=False` ）: ガードレールは、エージェントが開始される *前に* 実行され、完了します。ガードレールのトリップワイヤーが作動した場合、エージェントは一切実行されないため、トークンの消費やツールの実行を防止できます。これは、コストを最適化したい場合や、ツール呼び出しによる潜在的な副作用を回避したい場合に最適です。

## 出力ガードレール {#output-guardrails}

出力ガードレールは、次の 3 ステップで実行されます。

1. まず、ガードレールはエージェントが生成した出力を受け取ります。
2. 次に、ガードレール関数が実行されて [`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を生成し、それが [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] にラップされます
3. 最後に、[`.tripwire_triggered`][agents.guardrail.GuardrailFunctionOutput.tripwire_triggered] が true かどうかを確認します。true の場合、[`OutputGuardrailTripwireTriggered`][agents.exceptions.OutputGuardrailTripwireTriggered] 例外が発生するため、ユーザーへ適切に応答したり、例外を処理したりできます。

!!! Note

    出力ガードレールはエージェントの最終出力に対して実行されることを想定しているため、エージェントのガードレールは、そのエージェントが *最後の* エージェントである場合にのみ実行されます。入力ガードレールと同様に、これはガードレールが実際のエージェントに関連する傾向があるためです。エージェントごとに異なるガードレールを実行するため、コードを同じ場所に配置すると可読性が向上します。

    出力ガードレールは常にエージェントの完了後に実行されるため、 `run_in_parallel` パラメーターはサポートされません。

出力トリップワイヤーとガードレール関数が発生させる例外では、セッションの動作が異なります。トリップワイヤーは、最終出力の候補を拒否します。トリップワイヤーが作動すると、ランナーは設定済みのセッションに対し、拒否された最終出力の候補を除外しながら、すでに完了したツール呼び出しおよびツール出力項目と、それらの呼び出しの再実行に必要な推論コンテキストを保存するよう要求します。ランナーは、このトリップワイヤーのルールをストリーミング実行と非ストリーミング実行の両方に適用します。ガードレール関数がトリップワイヤーの実行結果を返す代わりに例外を発生させた場合、ランナーは判定を不明として扱い、ガードレール例外を公開する前に、完了した最終ターンの項目を保存するよう設定済みのセッションに要求します。そのセッションへの書き込みも失敗した場合は、セッション書き込みエラーが優先されます。ストリーミング実行でも、非ストリーミング実行と同じ保存順序を使用し、 `stream_events()` から終端例外を発生させます。出力ガードレールの実行中に [`RunResultStreaming.cancel()`][agents.result.RunResultStreaming.cancel] を即座に呼び出すと、実行中のガードレールがキャンセルされ、最終ターンのセッション書き込みは開始されません。

終端となる関数ツール出力には追加の処理が必要です。これは、エージェントレベルの出力ガードレールが値をチェックする前に、ツールがすでに実行されているためです。[`Agent.tool_use_behavior`][agents.agent.Agent.tool_use_behavior] によってそのツールの実行結果が最終出力になり、出力トリップワイヤーがそれを拒否した場合、検証済みフィールドから関数呼び出しと出力のペアを再構築できる場合に限り、SDK は再実行可能なペアを保持します。保持される `function_call_output` ペイロードは、デフォルトテキストの `"Output withheld by an output guardrail."` に置き換えられます。元のツール出力ペイロードは、セッション、 `RunState` 、ストリーミングされた実行結果の状態、サンドボックスメモリ入力のいずれにも保持されません。SDK は、関数の引数を含め、再実行に必要な検証済みの関数呼び出しメタデータを保持します。そのため、このメタデータには、拒否された出力にも含まれていたデータが含まれる場合があります。現在のレスポンスの [`OutputGuardrailResult`][agents.guardrail.OutputGuardrailResult] オブジェクトでも、 `agent_output` は解決済みのプレースホルダーに置き換えられ、 `output_info` はクリアされます。現在のレスポンスの [`ToolOutputGuardrailResult`][agents.tool_guardrails.ToolOutputGuardrailResult] オブジェクトでは、許可または拒否の動作タイプは保持されますが、ペイロードを含む `output_info` と拒否メッセージは同じプレースホルダーに置き換えられます。以前に承認されたターンとガードレールの実行結果は変更されません。レスポンスに推論や、SDK が安全にサニタイズできない別の構造が含まれている場合、SDK は拒否された出力ペイロードを保持せず、現在のレスポンスのサフィックス全体を破棄します。例外を発生させたガードレール関数は拒否の判定を返していないため、完了した終端ツールのターンには、前述の例外時の保存動作が適用されます。

アプリケーションでデータを含まない別のプレースホルダーが必要な場合は、[`RunConfig.output_guardrail_blocked_message`][agents.run.RunConfig.output_guardrail_blocked_message] に空でない文字列または同期フォーマッターを設定します。フォーマッターは、SDK のデフォルト値、ガードレール名、エージェント、アクティブな実行コンテキストを含む [`OutputGuardrailBlockedMessageArgs`][agents.run.OutputGuardrailBlockedMessageArgs] を受け取ります。拒否されたツール出力やガードレールの `output_info` を受け取ることはありません。返されたテキストは、SDK がサニタイズ済みの終端ツールのターンを保持するすべての場所で保存および再実行されるため、機密データを含めず、実行コンテキストからシークレットをコピーしないでください。フォーマッターが例外を発生させる、 `None` を返す、空または文字列以外の値を返す、あるいは awaitable を生成する場合、SDK はデフォルトのプレースホルダーを使用します。 `RunConfig` の構築時には、非同期フォーマッター関数は拒否されます。

```python
from agents import OutputGuardrailBlockedMessageArgs, RunConfig


def blocked_message(args: OutputGuardrailBlockedMessageArgs[dict[str, str]]) -> str:
    return f"Output blocked by policy: {args.guardrail_name}."


run_config = RunConfig(output_guardrail_blocked_message=blocked_message)
```

## ツールガードレール {#tool-guardrails}

ツールガードレールは **`FunctionTool` インスタンス** をラップし、これらのツールの呼び出しを実行前後に検証またはブロックできるようにします。ツール自体に設定され、そのツールが呼び出されるたびに実行されます。

- 入力ツールガードレールはツールの実行前に実行され、呼び出しのスキップ、出力のメッセージへの置き換え、またはトリップワイヤーの作動が可能です。
- 出力ツールガードレールはツールの実行後に実行され、出力の置き換えまたはトリップワイヤーの作動が可能です。
- 関数ツールで承認が必要な場合、入力ツールガードレールは通常、承認後かつ実行直前に実行されます。保留中の承認による中断が通知される前に入力チェックを実行する場合は、[`RunConfig.tool_execution`][agents.run.RunConfig.tool_execution] を [`ToolExecutionConfig(pre_approval_tool_input_guardrails=True)`][agents.run.ToolExecutionConfig] に設定します。この承認前チェックに合格した呼び出しも、ツールの実行前に承認後のチェックを再度受けます。
- ツールガードレールは、 `FunctionTool` 実行パイプラインを使用します。 `tool` または [`function_tool`][agents.tool.function_tool] で作成したカスタムツールに直接設定できます。ローカル MCP サーバーに `tool_input_guardrails` と `tool_output_guardrails` を設定することもできます。SDK は、これらのリストをそのサーバーが公開するすべてのツールに設定します。ハンドオフは関数ツールパイプラインではなく SDK のハンドオフパイプラインを通じて実行されるため、ツールガードレールはハンドオフ呼び出し自体には適用されません。ホスト型ツール（ `WebSearchTool` 、 `FileSearchTool` 、 `HostedMCPTool` 、 `CodeInterpreterTool` 、 `ImageGenerationTool` ）および組み込み実行ツール（ `ComputerTool` 、 `ShellTool` 、 `ApplyPatchTool` 、 `LocalShellTool` ）は、このガードレールパイプラインを使用しません。また、[`Agent.as_tool()`][agents.agent.Agent.as_tool] は現在、ツールガードレールのオプションを直接公開していません。ローカル MCP の設定については、[MCP サーバーのツールガードレール](mcp.md#tool-guardrails)を参照してください。

詳細については、以下のコードスニペットを参照してください。

## トリップワイヤー {#tripwires}

エージェントの入力または出力がガードレールに合格しなかった場合、ガードレールはトリップワイヤーを使用して通知できます。ランナーは直ちに `InputGuardrailTripwireTriggered` または `OutputGuardrailTripwireTriggered` 例外を発生させ、エージェントの実行を停止します。ツールガードレールでは、対応する `ToolInputGuardrailTripwireTriggered` および `ToolOutputGuardrailTripwireTriggered` 例外が使用されます。

エージェントレベルのトリップワイヤーでは、例外の `guardrail_result` によって、トリップワイヤーを作動させたガードレールを特定できます。ランナーが発生させた入力トリップワイヤーの場合、 `exception.run_data.input_guardrail_results` には、実行が停止する前に完了したすべての入力ガードレールの実行結果が含まれます。これには、トリップワイヤーを作動させた実行結果も含まれます。出力トリップワイヤーでは、 `exception.run_data.output_guardrail_results` を通じて同等の累積結果が提供されます。

一方、ツールのトリップワイヤー例外では、トリップワイヤーを作動させた `guardrail` と `output` が直接公開されます。これらの `run_data.tool_input_guardrail_results` および `run_data.tool_output_guardrail_results` リストには、失敗前に完了したターンから累積された実行結果が保持されます。トリップワイヤーを作動させた実行結果は、例外の `output` から取得できます。 `MaxTurnsExceeded` など、ランナーによって管理されるその他の失敗でも、完了したツールガードレールの実行結果がこれらのリストに保持されます。 `stream_events()` が例外を発生させた後、ストリーミングされた実行結果では、同じエージェントおよびツールガードレールの累積結果リストが公開されます。ランナーが管理する実行パスの外部で例外が発生した場合、 `run_data` は `None` になることがあります。

## ガードレールの実装 {#implementing-a-guardrail}

入力を受け取り、[`GuardrailFunctionOutput`][agents.guardrail.GuardrailFunctionOutput] を返す関数を用意する必要があります。この例では、内部でエージェントを実行することでこれを実現します。

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
)
from agents.decorators import input_guardrail


class MathHomeworkOutput(BaseModel):
    is_math_homework: bool
    reasoning: str


guardrail_agent = Agent(  # (1)!
    name="Guardrail check",
    instructions="Check if the user is asking you to do their math homework.",
    output_type=MathHomeworkOutput,
)


@input_guardrail
async def math_guardrail(  # (2)!
    ctx: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    result = await Runner.run(guardrail_agent, input, context=ctx.context)

    return GuardrailFunctionOutput(
        output_info=result.final_output,  # (3)!
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

1. このエージェントをガードレール関数で使用します。
2. これは、エージェントの入力とコンテキストを受け取り、実行結果を返すガードレール関数です。
3. ガードレールの実行結果には追加情報を含めることができます。
4. これは、ワークフローを定義する実際のエージェントです。

出力ガードレールも同様です。

```python
from pydantic import BaseModel
from agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
)
from agents.decorators import output_guardrail


class MessageOutput(BaseModel):  # (1)!
    response: str


class MathOutput(BaseModel):  # (2)!
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


agent = Agent(  # (4)!
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

1. これは、実際のエージェントの出力型です。
2. これは、ガードレールの出力型です。
3. これは、エージェントの出力を受け取り、実行結果を返すガードレール関数です。
4. これは、ワークフローを定義する実際のエージェントです。

最後に、ツールガードレールの例を示します。

```python
import json
from agents import (
    Agent,
    Runner,
    ToolGuardrailFunctionOutput,
)
from agents.decorators import tool, tool_input_guardrail, tool_output_guardrail


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


@tool(
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