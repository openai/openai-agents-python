---
search:
  exclude: true
---
# ヒューマンインザループ

ヒューマンインザループ (HITL) フローを使用すると、機密性の高いツール呼び出しを人が承認または拒否するまで、エージェントの実行を一時停止できます。ツールは承認が必要となる条件を宣言し、実行結果では保留中の承認が割り込みとして提示されます。また、`RunState` を使用すると、一時停止した実行をシリアライズし、判断後に再開できます。

この承認フローは実行全体に適用され、現在の最上位エージェントだけに限定されません。ツールが現在のエージェント、ハンドオフ先のエージェント、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行のいずれに属する場合でも、同じパターンが適用されます。ネストされた `Agent.as_tool()` の場合も、割り込みは外側の実行に提示されるため、外側の `RunState` で承認または拒否し、元の最上位の実行を再開します。

`Agent.as_tool()` では、承認が 2 つの異なる層で発生する可能性があります。エージェントツール自体が `Agent.as_tool(..., needs_approval=...)` を介して承認を要求できるほか、ネストされた実行の開始後に、その内部のツールが独自の承認を要求することもできます。どちらも、同じ外側の実行の割り込みフローで処理されます。

このページでは、`interruptions` を介した手動承認フローを中心に説明します。アプリがコード内で判断できる場合、一部のツールタイプではプログラムによる承認コールバックもサポートされているため、実行を一時停止せずに続行できます。

## 承認が必要なツールの指定 {#marking-tools-that-need-approval}

常に承認を要求するには `needs_approval` を `True` に設定し、呼び出しごとに判断するには非同期関数を指定します。この callable は、実行コンテキスト、解析済みのツールパラメーター、ツール呼び出し ID を受け取ります。

SDK が引数を安全に検査できない場合、callable の承認ルールは安全側に倒れます。引数が不正な JSON、有効な JSON ではあるもののオブジェクトではないもの（たとえば、`null` やリスト）、または `NaN`、`Infinity`、`-Infinity` などの非標準定数を含む場合、callable は呼び出されず、その呼び出しには手動承認が必要となります。この動作は、Runner と Realtime のツール呼び出しで共通です。

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

`needs_approval` は、[`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で利用できます。ローカル MCP サーバーでも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を介して承認をサポートしています。ホスト型 MCP サーバーでは、`tool_config={"require_approval": "always"}` と任意の `on_approval_request` コールバックを指定した [`HostedMCPTool`][agents.tool.HostedMCPTool] を介して承認をサポートしています。Shell ツールと apply_patch ツールでは、割り込みを提示せずに自動承認または自動拒否する場合、`on_approval` コールバックを利用できます。

## 承認フローの仕組み {#how-the-approval-flow-works}

1. モデルがツール呼び出しを出力すると、ランナーはその承認ルール（`needs_approval`、`require_approval`、またはホスト型 MCP に相当するもの）を評価します。
2. そのツール呼び出しに対する承認判断がすでに [`RunContextWrapper`][agents.run_context.RunContextWrapper] に保存されている場合、ランナーは確認せずに処理を続行します。呼び出し単位の承認は、特定の呼び出し ID に限定されます。実行の残りの期間中、同じツール識別情報に対する今後の呼び出しにも同じ判断を保持するには、`always_approve=True` または `always_reject=True` を渡します。
3. 承認ルールで承認が必要と判断され、そのツール呼び出しに対する判断が保存されていない場合、実行は一時停止します。`RunResult.interruptions`（または `RunResultStreaming.interruptions`）には、`agent.name`、`tool_name`、`arguments` などの詳細を含む [`ToolApprovalItem`][agents.items.ToolApprovalItem] エントリが格納されます。これには、ハンドオフ後またはネストされた `Agent.as_tool()` の実行内で発生した承認も含まれます。
4. `result.to_state()` を使用して実行結果を `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出した後、`Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。ここで、`agent` はその実行の元の最上位エージェントです。
5. 再開された実行は中断箇所から続行され、新しい承認が必要になると、このフローに再度入ります。

`always_approve=True` または `always_reject=True` で作成された継続的な判断は実行状態に保存されるため、後から同じ一時停止済みの実行を再開する際に、`state.to_string()` / `RunState.from_string(...)` および `state.to_json()` / `RunState.from_json(...)` を経ても保持されます。

[`HostedMCPTool`][agents.tool.HostedMCPTool] からの承認リクエストについて、Agents SDK は `server_label` とツール名の組み合わせによって、継続的なツール判断を識別します。あるホスト型 MCP サーバー上の `lookup_account` に対する常時承認の判断によって、別のサーバー上にある同名のツールが承認されることはありません。Agents SDK が常時承認または常時拒否の判断を保持するのは、ホスト型 MCP の承認リクエストに空ではない両方の識別フィールドが含まれている場合のみです。

保留中の承認をすべて同じ処理内で解決する必要はありません。`interruptions` には、通常の関数ツール、ホスト型 MCP の承認、ネストされた `Agent.as_tool()` の承認を混在させることができます。一部の項目だけを承認または拒否して再実行すると、解決済みの呼び出しは続行できますが、未解決のものは `interruptions` に残り、実行は再び一時停止します。

## カスタム拒否メッセージ {#custom-rejection-messages}

デフォルトでは、拒否されたツール呼び出しについて、SDK の標準的な拒否テキストが実行に返されます。このメッセージは、次の 2 つの層でカスタマイズできます。

-   実行全体のフォールバック: [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter] を設定すると、実行全体にわたって、承認拒否時にモデルへ提示されるデフォルトメッセージを制御できます。
-   呼び出し単位のオーバーライド: 特定の 1 つの拒否されたツール呼び出しに異なるメッセージを提示するには、`state.reject(...)` に `rejection_message=...` を渡します。

両方が指定されている場合、呼び出し単位の `rejection_message` が実行全体のフォーマッターより優先されます。

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

両方の層を組み合わせた完全な例については、[`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py) を参照してください。

## 自動承認判断 {#automatic-approval-decisions}

手動の `interruptions` は最も汎用的なパターンですが、唯一の方法ではありません。

-   ローカルの [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] では、`on_approval` を使用して、コード内ですぐに承認または拒否できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] では、`tool_config={"require_approval": "always"}` と `on_approval_request` を組み合わせて、同様にプログラムで判断できます。
-   通常の [`function_tool`][agents.tool.function_tool] ツールと [`Agent.as_tool()`][agents.agent.Agent.as_tool] では、このページで説明する手動割り込みフローを使用します。

これらのコールバックが判断を返すと、人の応答を待つために一時停止することなく実行が続行されます。Realtime API と音声セッション API については、[Realtime ガイド](realtime/guide.md)の承認フローを参照してください。

## ストリーミングとセッション {#streaming-and-sessions}

同じ割り込みフローをストリーミング実行でも利用できます。ストリーミング実行が一時停止した後も、イテレーターが終了するまで [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] を消費し続け、[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] を確認して解決します。再開後の出力でもストリーミングを継続する場合は、[`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] で再開します。このパターンのストリーミング版については、[ストリーミング](streaming.md)を参照してください。

セッションも使用している場合は、`RunState` から再開するときに同じセッションインスタンスを引き続き渡すか、同じセッション ID とバッキングストア向けに構成された別のセッションオブジェクトを渡します。再開されたターンは、同じ保存済み会話履歴に追加されます。セッションのライフサイクルの詳細については、[セッション](sessions/index.md)を参照してください。

## 一時停止、承認、再開の例 {#example-pause-approve-resume}

以下のスニペットは JavaScript の HITL ガイドと同様に、ツールに承認が必要な場合に一時停止し、状態をディスクに保持して再読み込みし、判断を取得した後に再開します。

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

この例では、`prompt_approval` は `input()` を使用し、`run_in_executor(...)` で実行されるため、同期関数になっています。承認元がすでに非同期である場合（たとえば、HTTP リクエストや非同期データベースクエリ）、`async def` 関数を使用し、それを直接 `await` できます。

承認のために一時停止する可能性がある実行でストリーミングを使用するには、`Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を消費した後、上記と同じ `result.to_state()` および再開の手順に従います。

## リポジトリのパターンとコード例 {#repository-patterns-and-examples}

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで消費し、保留中のツール呼び出しを承認してから `Runner.run_streamed(agent, state)` で再開する方法を示します。
- **カスタム拒否テキスト**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py` は、承認が拒否されたときに、実行レベルの `tool_error_formatter` と呼び出し単位の `rejection_message` オーバーライドを組み合わせる方法を示します。
- **ツールとしてのエージェントの承認**: `Agent.as_tool(..., needs_approval=...)` は、委任されたエージェントのタスクにレビューが必要な場合にも同じ割り込みフローを適用します。ネストされた割り込みも外側の実行に提示されるため、ネストされたエージェントではなく、元の最上位エージェントを再開します。
- **ローカルの Shell ツールと apply_patch ツール**: `ShellTool` と `ApplyPatchTool` も `needs_approval` をサポートしています。実行の残りの期間中、そのツールに対する今後の呼び出しにも判断をキャッシュするには、`state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使用します。自動判断には `on_approval` を指定し（`examples/tools/shell.py` を参照）、手動判断では割り込みを処理します（`examples/tools/shell_human_in_the_loop.py` を参照）。ホスト型 Shell 環境では、`needs_approval` または `on_approval` はサポートされていません。[ツールガイド](tools.md)を参照してください。
- **ローカル MCP サーバー**: MCP ツール呼び出しを制御するには、`MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` で `require_approval` を使用します（`examples/mcp/get_all_mcp_tools_example/main.py` と `examples/mcp/tool_filter_example/main.py` を参照）。
- **ホスト型 MCP サーバー**: HITL を強制するには `HostedMCPTool` に `tool_config={"require_approval": "always"}` を設定します。必要に応じて、自動承認または自動拒否するための `on_approval_request` を指定できます（`examples/hosted_mcp/human_in_the_loop.py` と `examples/hosted_mcp/on_approval.py` を参照）。信頼済みのサーバーには `"never"` を使用します（`examples/hosted_mcp/simple.py`）。
- **セッションとメモリ**: 承認と会話履歴を複数のターンにわたって保持するには、`Runner.run` にセッションを渡します。SQLite および OpenAI Conversations のセッションバリアントは、`examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: Realtime デモでは、`RealtimeSession` の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開しています（サーバー側のハンドラーについては `examples/realtime/app/server.py`、API サーフェスについては [Realtime ガイド](realtime/guide.md#tool-approvals)を参照）。

## 長時間にわたる承認 {#long-running-approvals}

`RunState` は永続性を考慮して設計されています。保留中の処理をデータベースやキューに保存するには `state.to_json()` または `state.to_string()` を使用し、後から再作成するには `RunState.from_json(...)` または `RunState.from_string(...)` を使用します。

便利なシリアライズオプションは次のとおりです。

-   `context_serializer`: マッピングではないコンテキストオブジェクトをシリアライズする方法をカスタマイズします。
-   `context_deserializer`: `RunState.from_json(...)` または `RunState.from_string(...)` で状態を読み込む際に、マッピングではないコンテキストオブジェクトを再構築します。
- `strict_context=True`: コンテキストがすでにマッピングであるか、`context_serializer` が指定されていない限り、シリアライズを失敗させます。また、コンテキストがすでにマッピングであるか、`context_deserializer` が指定されていない限り、デシリアライズを失敗させます。
- `context_override`: 状態の読み込み時に、シリアライズ済みのコンテキストを置き換えます。元のコンテキストオブジェクトを復元したくない場合に便利ですが、すでにシリアライズ済みのペイロードからそのコンテキストが削除されるわけではありません。
- `include_tracing_api_key=True`: 再開した処理でも同じ認証情報でトレースをエクスポートし続ける必要がある場合、シリアライズ済みのトレースペイロードにトレーシング API キーを含めます。

シリアライズ済みの実行状態には、アプリのコンテキストに加えて、承認、使用量、シリアライズ済みの `tool_input`、ネストされたツールとしてのエージェントの再開情報、トレースメタデータ、サーバー管理の会話設定など、SDK が管理するランタイムメタデータが含まれます。シリアライズ済みの状態を保存または送信する場合は、`RunContextWrapper.context` を永続化データとして扱い、意図的に状態とともに移動させる場合を除き、そこにシークレットを格納しないでください。

## 保留中タスクのバージョニング {#versioning-pending-tasks}

承認が長期間保留される可能性がある場合は、シリアライズ済みの状態とともに、エージェント定義または SDK のバージョンマーカーを保存します。これにより、モデル、プロンプト、またはツール定義が変更された場合でも、対応するコードパスにデシリアライズを振り分け、非互換性を回避できます。