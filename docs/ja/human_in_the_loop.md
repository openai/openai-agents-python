---
search:
  exclude: true
---
# ヒューマン・イン・ザ・ループ

ヒューマン・イン・ザ・ループ（HITL）フローを使用すると、機密性の高いツール呼び出しを人が承認または拒否するまで、エージェントの実行を一時停止できます。ツールは承認が必要となる条件を宣言し、実行結果は保留中の承認を中断として提示します。また、`RunState` を使用すると、一時停止した実行をシリアライズし、判断後に再開できます。

この承認インターフェースは実行全体に適用され、現在の最上位エージェントだけに限定されません。ツールが現在のエージェントに属している場合、ハンドオフ先のエージェントに属している場合、ネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行に属している場合のいずれにも、同じパターンが適用されます。ネストされた `Agent.as_tool()` の場合でも、中断は外側の実行に提示されるため、外側の `RunState` で承認または拒否し、元の最上位エージェントの実行を再開します。

`Agent.as_tool()` では、承認が 2 つの異なるレイヤーで発生する可能性があります。エージェントツール自体が `Agent.as_tool(..., needs_approval=...)` による承認を必要とする場合と、ネストされた実行の開始後に、そのエージェント内のツールが独自の承認を要求する場合です。どちらも同じ外側の実行の中断フローで処理されます。

このページでは、`interruptions` を使用する手動承認フローを中心に説明します。アプリがコード内で判断できる場合、一部のツールタイプではプログラムによる承認コールバックもサポートされており、実行を一時停止せずに続行できます。

## 承認が必要なツールの指定 {#marking-tools-that-need-approval}

常に承認を必要とするには `needs_approval` を `True` に設定し、呼び出しごとに判断するには非同期関数を指定します。この呼び出し可能オブジェクトは、実行コンテキスト、解析済みのツールパラメーター、ツール呼び出し ID を受け取ります。

SDK が引数を安全に検査できない場合、呼び出し可能な承認ルールは安全側に倒して承認を必須とします。引数が存在しない、空である、空白文字のみを含む、不正な JSON である、有効な JSON でもオブジェクトではない（たとえば `null` やリスト）、または `NaN`、`Infinity`、`-Infinity` などの非標準定数を含む場合、呼び出し可能オブジェクトは呼び出されず、その呼び出しには手動承認が必要になります。この動作は、Runner と Realtime のツール呼び出しで同じです。

```python
from agents import Agent
from agents.decorators import tool


@tool(needs_approval=True)
async def cancel_order(order_id: int) -> str:
    return f"Cancelled order {order_id}"


async def requires_review(_ctx, params, _call_id) -> bool:
    return "refund" in params.get("subject", "").lower()


@tool(needs_approval=requires_review)
async def send_email(subject: str, body: str) -> str:
    return f"Sent '{subject}'"


agent = Agent(
    name="Support agent",
    instructions="Handle tickets and ask for approval when needed.",
    tools=[cancel_order, send_email],
)
```

`needs_approval` は、[`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で使用できます。ローカル MCP サーバーでも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を通じて承認をサポートします。ホスト型 MCP サーバーでは、[`HostedMCPTool`][agents.tool.HostedMCPTool] で `tool_config={"require_approval": "always"}` と任意の `on_approval_request` コールバックを使用して承認をサポートします。Shell ツールと apply_patch ツールでは、中断を提示せずに自動承認または自動拒否する場合、`on_approval` コールバックを指定できます。

## 承認フローの仕組み {#how-the-approval-flow-works}

1. モデルがツール呼び出しを出力すると、ランナーはその承認ルール（`needs_approval`、`require_approval`、またはホスト型 MCP で対応するもの）を評価します。
2. そのツール呼び出しに対する承認判断が [`RunContextWrapper`][agents.run_context.RunContextWrapper] にすでに保存されている場合、ランナーは確認を求めずに処理を続行します。呼び出しごとの承認は、特定の呼び出し ID に限定されます。実行の残りの期間中、同じツール識別情報に対する今後の呼び出しにも同じ判断を適用するには、`always_approve=True` または `always_reject=True` を渡します。
3. 承認ルールで承認が必要とされ、そのツール呼び出しに対する判断が保存されていない場合、実行は一時停止します。`RunResult.interruptions`（または `RunResultStreaming.interruptions`）には、`agent.name`、`tool_name`、`arguments` などの詳細を持つ [`ToolApprovalItem`][agents.items.ToolApprovalItem] エントリが含まれます。これには、ハンドオフ後やネストされた `Agent.as_tool()` の実行内で要求された承認も含まれます。
4. `result.to_state()` を使用して実行結果を `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出してから、`Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。ここで `agent` は、その実行の元の最上位エージェントです。
5. 再開された実行は中断した位置から続行され、新たな承認が必要になった場合は、このフローに再度入ります。

`always_approve=True` または `always_reject=True` で作成された継続的な判断は実行状態に保存されます。そのため、同じ一時停止済み実行を後から再開する場合でも、`state.to_string()` / `RunState.from_string(...)` および `state.to_json()` / `RunState.from_json(...)` を通じて保持されます。

[`HostedMCPTool`][agents.tool.HostedMCPTool] からの承認リクエストについて、Agents SDK は `server_label` とツール名の組み合わせによって、継続的なツール判断を識別します。あるホスト型 MCP サーバー上の `lookup_account` に対する常時承認の判断によって、別のサーバー上にある同名のツールが承認されることはありません。Agents SDK が常時承認または常時拒否の判断を永続化するのは、ホスト型 MCP の承認リクエストに、空ではない両方の識別フィールドが含まれている場合のみです。

保留中の承認をすべて同じ処理で解決する必要はありません。`interruptions` には、通常の関数ツール、ホスト型 MCP の承認、ネストされた `Agent.as_tool()` の承認を混在させることができます。一部の項目のみを承認または拒否して再実行すると、解決済みの呼び出しは続行できる一方、未解決の項目は `interruptions` に残り、実行は再び一時停止します。

## カスタム拒否メッセージ {#custom-rejection-messages}

デフォルトでは、拒否されたツール呼び出しについて、SDK の標準的な拒否テキストが実行に返されます。このメッセージは、次の 2 つのレイヤーでカスタマイズできます。

-   実行全体のフォールバック: [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter] を設定すると、実行全体で承認が拒否された場合にモデルへ表示されるデフォルトメッセージを制御できます。
-   呼び出しごとのオーバーライド: 特定の拒否されたツール呼び出しに別のメッセージを提示する場合は、`state.reject(...)` に `rejection_message=...` を渡します。

両方が指定されている場合、呼び出しごとの `rejection_message` が実行全体のフォーマッターより優先されます。

```python
from agents import RunConfig, ToolErrorFormatterArgs


def format_rejection(args: ToolErrorFormatterArgs[None]) -> str | None:
    if args.kind != "approval_rejected":
        return None
    return "Publish action was canceled because approval was rejected."


run_config = RunConfig(tool_error_formatter=format_rejection)

# Later, while resolving a specific interruption:
state.reject(
    interruption,
    rejection_message="Publish action was canceled because the reviewer denied approval.",
)
```

両方のレイヤーを組み合わせて示す完全なコード例については、[`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py) を参照してください。

## 自動承認の判定 {#automatic-approval-decisions}

手動の `interruptions` は最も汎用的なパターンですが、唯一の方法ではありません。

-   ローカルの [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] では、`on_approval` を使用してコード内ですぐに承認または拒否できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] では、`tool_config={"require_approval": "always"}` と `on_approval_request` を併用して、同様にプログラムから判断できます。
-   通常の [`function_tool`][agents.tool.function_tool] ツールと [`Agent.as_tool()`][agents.agent.Agent.as_tool] では、このページで説明する手動中断フローを使用します。

これらのコールバックが判断を返すと、人による応答を待つために一時停止することなく、実行が続行されます。Realtime および音声セッション API については、[Realtime ガイド](realtime/guide.md)の承認フローを参照してください。

## ストリーミングとセッション {#streaming-and-sessions}

同じ中断フローは、ストリーミング実行でも機能します。ストリーミング実行が一時停止した後、イテレーターが完了するまで [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] の取得を続け、[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] を確認して解決します。再開後の出力でもストリーミングを継続する場合は、[`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] で再開します。このパターンのストリーミング版については、[ストリーミング](streaming.md)を参照してください。

セッションも使用している場合は、`RunState` から再開するときに同じセッションインスタンスを引き続き渡すか、同じセッション ID とバッキングストアを使用するよう構成された別のセッションオブジェクトを渡します。再開されたターンは、同じ保存済み会話履歴に追加されます。セッションのライフサイクルの詳細については、[セッション](sessions/index.md)を参照してください。

## 例: 一時停止、承認、再開 {#example-pause-approve-resume}

次のスニペットは JavaScript の HITL ガイドと同じ流れです。ツールに承認が必要になると一時停止し、状態をディスクに永続化して再読み込みし、判断を受け取った後に再開します。

```python
import asyncio
import json
from pathlib import Path

from agents import Agent, Runner, RunState
from agents.decorators import tool


async def needs_oakland_approval(_ctx, params, _call_id) -> bool:
    return "Oakland" in params.get("city", "")


@tool(needs_approval=needs_oakland_approval)
async def get_temperature(city: str) -> str:
    return f"The temperature in {city} is 20° Celsius"


agent = Agent(
    name="Weather assistant",
    instructions="Answer weather questions with the provided tools.",
    tools=[get_temperature],
)

STATE_PATH = Path(".cache/hitl_state.json")


def prompt_approval(tool_name: str, arguments: str | None) -> bool:
    answer = input(f"Approve {tool_name} with {arguments}? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


async def main() -> None:
    result = await Runner.run(agent, "What is the temperature in Oakland?")

    while result.interruptions:
        # Persist the paused state.
        state = result.to_state()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(state.to_string())

        # Load the state later (could be a different process).
        stored = json.loads(STATE_PATH.read_text())
        state = await RunState.from_json(agent, stored)

        for interruption in result.interruptions:
            approved = await asyncio.get_running_loop().run_in_executor(
                None, prompt_approval, interruption.name or "unknown_tool", interruption.arguments
            )
            if approved:
                state.approve(interruption, always_approve=False)
            else:
                state.reject(interruption)

        result = await Runner.run(agent, state)

    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

このコード例では、`input()` を使用し、`run_in_executor(...)` で実行されるため、`prompt_approval` は同期的です。承認元がすでに非同期の場合（たとえば、HTTP リクエストや非同期データベースクエリ）、代わりに `async def` 関数を使用し、`await` で直接待機できます。

承認のために一時停止する可能性がある実行でストリーミングを使用するには、`Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を取得してから、前述と同じ `result.to_state()` と再開の手順に従います。

## リポジトリのパターンとコード例 {#repository-patterns-and-examples}

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで取得し、保留中のツール呼び出しを承認してから `Runner.run_streamed(agent, state)` で再開する方法を示します。
- **カスタム拒否テキスト**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py` は、承認が拒否された場合に、実行レベルの `tool_error_formatter` と呼び出しごとの `rejection_message` オーバーライドを組み合わせる方法を示します。
- **ツールとしてのエージェントの承認**: `Agent.as_tool(..., needs_approval=...)` は、委任されたエージェントタスクにレビューが必要な場合にも、同じ中断フローを適用します。ネストされた中断も外側の実行に提示されるため、ネストされたエージェントではなく、元の最上位エージェントを再開します。
- **ローカルの Shell ツールと apply_patch ツール**: `ShellTool` と `ApplyPatchTool` でも `needs_approval` をサポートします。実行の残りの期間中、そのツールに対する今後の呼び出し用に判断をキャッシュするには、`state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使用します。自動判断には `on_approval` を指定し（`examples/tools/shell.py` を参照）、手動判断では中断を処理します（`examples/tools/shell_human_in_the_loop.py` を参照）。ホスト型 Shell 環境では、`needs_approval` または `on_approval` をサポートしていません。[ツールガイド](tools.md)を参照してください。
- **ローカル MCP サーバー**: MCP ツール呼び出しを制御するには、`MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` で `require_approval` を使用します（`examples/mcp/get_all_mcp_tools_example/main.py` と `examples/mcp/tool_filter_example/main.py` を参照）。
- **ホスト型 MCP サーバー**: HITL を強制するには `HostedMCPTool` に `tool_config={"require_approval": "always"}` を設定し、必要に応じて、自動承認または自動拒否するための `on_approval_request` を指定します（`examples/hosted_mcp/human_in_the_loop.py` と `examples/hosted_mcp/on_approval.py` を参照）。信頼できるサーバーには `"never"` を使用します（`examples/hosted_mcp/simple.py`）。
- **セッションとメモリ**: 承認と会話履歴を複数のターンにわたって保持するには、`Runner.run` にセッションを渡します。SQLite および OpenAI Conversations のセッションバリアントは、`examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: Realtime のデモでは、`RealtimeSession` の `approve_tool_call` / `reject_tool_call` を通じてツール呼び出しを承認または拒否する WebSocket メッセージを公開しています（サーバー側のハンドラーについては `examples/realtime/app/server.py`、API インターフェースについては [Realtime ガイド](realtime/guide.md#tool-approvals)を参照）。

## 長時間にわたる承認 {#long-running-approvals}

`RunState` は、永続的な利用を想定して設計されています。保留中の処理をデータベースまたはキューに保存するには `state.to_json()` または `state.to_string()` を使用し、後から `RunState.from_json(...)` または `RunState.from_string(...)` で再作成します。

便利なシリアライズオプションは次のとおりです。

-   `context_serializer`: マッピングではないコンテキストオブジェクトのシリアライズ方法をカスタマイズします。
-   `context_deserializer`: `RunState.from_json(...)` または `RunState.from_string(...)` で状態を読み込む際に、マッピングではないコンテキストオブジェクトを再構築します。
- `strict_context=True`: コンテキストがすでにマッピングであるか、`context_serializer` が指定されていない限り、シリアライズを失敗させます。また、コンテキストがすでにマッピングであるか、`context_deserializer` が指定されていない限り、デシリアライズを失敗させます。
- `context_override`: 状態を読み込む際に、シリアライズされたコンテキストを置き換えます。元のコンテキストオブジェクトを復元したくない場合に便利ですが、すでにシリアライズされたペイロードからそのコンテキストを削除するものではありません。
- `include_tracing_api_key=True`: 再開後の処理でも同じ認証情報でトレースをエクスポートし続ける必要がある場合に、シリアライズされたトレースペイロードへトレーシング API キーを含めます。

シリアライズされた実行状態には、アプリのコンテキストに加え、承認、使用量、シリアライズされた `tool_input`、ネストされたツールとしてのエージェントの再開情報、トレースメタデータ、サーバー管理の会話設定など、SDK が管理するランタイムメタデータが含まれます。シリアライズされた状態を保存または送信する予定がある場合は、`RunContextWrapper.context` を永続化されるデータとして扱い、状態とともに意図的に移動させる場合を除き、そこにシークレットを配置しないでください。

## 保留中タスクのバージョン管理 {#versioning-pending-tasks}

承認が長期間保留される可能性がある場合は、エージェント定義または SDK のバージョンマーカーを、シリアライズされた状態とともに保存してください。これにより、デシリアライズを対応するコードパスへルーティングでき、モデル、プロンプト、ツール定義が変更された場合の非互換性を回避できます。