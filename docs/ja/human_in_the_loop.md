---
search:
  exclude: true
---
# ヒューマン・イン・ザ・ループ

人間が承認または拒否するまでエージェントの実行を一時停止するには、ヒューマン・イン・ザ・ループ (HITL) フローを使用します。ツールは承認が必要となる条件を宣言し、実行結果では保留中の承認が中断として提示されます。また、`RunState` を使用すると、決定後に実行をシリアライズして再開できます。

この承認の適用範囲は実行全体であり、現在のトップレベルエージェントだけに限定されません。ツールが現在のエージェントに属する場合、ハンドオフ先のエージェントに属する場合、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行に属する場合でも、同じパターンが適用されます。ネストされた `Agent.as_tool()` の場合も、中断は外側の実行に提示されるため、外側の `RunState` で承認または拒否し、元のトップレベル実行を再開します。

`Agent.as_tool()` では、承認が 2 つの異なるレイヤーで発生する可能性があります。エージェントツール自体が `Agent.as_tool(..., needs_approval=...)` による承認を必要とする場合と、ネストされた実行の開始後に、ネストされたエージェント内のツールが独自の承認を要求する場合です。どちらも、外側の実行における同じ中断フローで処理されます。

このページでは、`interruptions` を使用する手動承認フローに焦点を当てます。アプリケーションがコード内で判断できる場合、一部のツールタイプではプログラムによる承認コールバックもサポートされており、実行を一時停止せずに続行できます。

## 承認が必要なツールの指定

常に承認を要求するには、`needs_approval` を `True` に設定します。または、呼び出しごとに判断する非同期関数を指定します。この呼び出し可能オブジェクトは、実行コンテキスト、解析済みのツールパラメーター、ツール呼び出し ID を受け取ります。

SDK が引数を安全に検査できない場合、呼び出し可能な承認ルールは安全側に倒れ、承認を必須とします。引数が不正な JSON である場合、有効な JSON でもオブジェクトではない場合（たとえば、`null` やリスト）、または `NaN`、`Infinity`、`-Infinity` などの非標準定数が含まれる場合、呼び出し可能オブジェクトは実行されず、その呼び出しには手動承認が必要です。この動作は、Runner と Realtime のツール呼び出しで同じです。

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

`needs_approval` は、[`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で使用できます。ローカル MCP サーバーも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を通じて承認をサポートします。ホスト型 MCP サーバーでは、[`HostedMCPTool`][agents.tool.HostedMCPTool] に `tool_config={"require_approval": "always"}` とオプションの `on_approval_request` コールバックを指定することで、承認をサポートします。中断を提示せずに自動承認または自動拒否する場合、Shell および apply_patch ツールは `on_approval` コールバックを受け取ります。

## 承認フローの仕組み

1. モデルがツール呼び出しを出力すると、ランナーはその承認ルール（`needs_approval`、`require_approval`、またはホスト型 MCP における同等の設定）を評価します。
2. そのツール呼び出しに対する承認決定が [`RunContextWrapper`][agents.run_context.RunContextWrapper] にすでに保存されている場合、ランナーは確認を求めずに続行します。呼び出し単位の承認は、特定の呼び出し ID に限定されます。実行の残りの期間、そのツールに対する今後の呼び出しにも同じ決定を適用するには、`always_approve=True` または `always_reject=True` を渡します。
3. それ以外の場合、実行は一時停止し、`RunResult.interruptions`（または `RunResultStreaming.interruptions`）には、`agent.name`、`tool_name`、`arguments` などの詳細を含む [`ToolApprovalItem`][agents.items.ToolApprovalItem] エントリが格納されます。これには、ハンドオフ後またはネストされた `Agent.as_tool()` の実行内で要求された承認も含まれます。
4. `result.to_state()` を使用して実行結果を `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出した後、`Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。ここで `agent` は、その実行における元のトップレベルエージェントです。
5. 再開された実行は中断箇所から続行され、新たな承認が必要になった場合は、このフローに再度入ります。

`always_approve=True` または `always_reject=True` によって固定化された決定は実行状態に保存されるため、同じ一時停止中の実行を後で再開する際、`state.to_string()` / `RunState.from_string(...)` および `state.to_json()` / `RunState.from_json(...)` を使用しても保持されます。

1 回の処理ですべての保留中の承認を解決する必要はありません。`interruptions` には、通常の関数ツール、ホスト型 MCP の承認、ネストされた `Agent.as_tool()` の承認が混在することがあります。一部の項目のみを承認または拒否して再実行すると、解決済みの呼び出しは続行できますが、未解決の項目は `interruptions` に残り、実行は再び一時停止します。

## カスタム拒否メッセージ

デフォルトでは、拒否されたツール呼び出しに対して、SDK の標準拒否テキストが実行に返されます。このメッセージは、次の 2 つのレイヤーでカスタマイズできます。

-   実行全体のフォールバック：[`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter] を設定すると、実行全体における承認拒否について、モデルに表示されるデフォルトメッセージを制御できます。
-   呼び出し単位のオーバーライド：特定の拒否されたツール呼び出しに別のメッセージを返す場合は、`state.reject(...)` に `rejection_message=...` を渡します。

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

両方のレイヤーを組み合わせた完全なコード例については、[`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py) を参照してください。

## 自動承認の決定

手動の `interruptions` は最も一般的なパターンですが、唯一の方法ではありません。

-   ローカルの [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] では、`on_approval` を使用してコード内で即座に承認または拒否できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] では、`tool_config={"require_approval": "always"}` と `on_approval_request` を組み合わせて、同様にプログラムで決定できます。
-   通常の [`function_tool`][agents.tool.function_tool] ツールと [`Agent.as_tool()`][agents.agent.Agent.as_tool] では、このページで説明する手動中断フローを使用します。

これらのコールバックが決定を返すと、人間の応答を待って一時停止することなく実行が続行されます。Realtime および音声セッション API については、[Realtime ガイド](realtime/guide.md)の承認フローを参照してください。

## ストリーミングとセッション

同じ中断フローは、ストリーミング実行でも機能します。ストリーミング実行が一時停止した後、イテレーターが完了するまで [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] を消費し続け、[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] を確認して各項目を解決します。再開後の出力も引き続きストリーミングする場合は、[`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] で再開します。このパターンのストリーミング版については、[ストリーミング](streaming.md)を参照してください。

セッションも使用している場合は、`RunState` から再開するときに同じセッションインスタンスを渡し続けるか、同じバックエンドストアを参照する別のセッションオブジェクトを渡します。これにより、再開後のターンが、保存済みの同じ会話履歴に追加されます。セッションのライフサイクルの詳細については、[セッション](sessions/index.md)を参照してください。

## 一時停止、承認、再開の例

以下のスニペットは、JavaScript の HITL ガイドと同じ流れを示しています。ツールに承認が必要な場合に一時停止し、状態をディスクに永続化して再読み込みし、決定を取得した後に再開します。

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

この例では、`prompt_approval` は `input()` を使用し、`run_in_executor(...)` で実行されるため、同期関数です。承認元がすでに非同期である場合（たとえば、HTTP リクエストや非同期データベースクエリ）、`async def` 関数を使用し、直接 `await` できます。

承認を待機しながら出力をストリーミングするには、`Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を消費した後、上記と同じ `result.to_state()` および再開の手順に従います。

## リポジトリのパターンとコード例

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで消費し、保留中のツール呼び出しを承認してから、`Runner.run_streamed(agent, state)` で再開する方法を示します。
- **カスタム拒否テキスト**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py` は、承認が拒否された場合に、実行レベルの `tool_error_formatter` と呼び出し単位の `rejection_message` オーバーライドを組み合わせる方法を示します。
- **エージェントツールの承認**: `Agent.as_tool(..., needs_approval=...)` は、委任されたエージェントタスクにレビューが必要な場合も、同じ中断フローを適用します。ネストされた中断も外側の実行に提示されるため、ネストされたエージェントではなく、元のトップレベルエージェントを再開してください。
- **ローカルの Shell および apply_patch ツール**: `ShellTool` と `ApplyPatchTool` も `needs_approval` をサポートします。今後の呼び出しに対する決定をキャッシュするには、`state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使用します。自動決定には `on_approval` を指定し（`examples/tools/shell.py` を参照）、手動決定には中断を処理します（`examples/tools/shell_human_in_the_loop.py` を参照）。ホスト型 Shell 環境は `needs_approval` または `on_approval` をサポートしていません。[ツールガイド](tools.md)を参照してください。
- **ローカル MCP サーバー**: `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` の `require_approval` を使用して、MCP ツール呼び出しを承認対象として制御します（`examples/mcp/get_all_mcp_tools_example/main.py` および `examples/mcp/tool_filter_example/main.py` を参照）。
- **ホスト型 MCP サーバー**: HITL を強制するには、`HostedMCPTool` の `require_approval` を `"always"` に設定します。必要に応じて、自動承認または自動拒否のために `on_approval_request` を指定できます（`examples/hosted_mcp/human_in_the_loop.py` および `examples/hosted_mcp/on_approval.py` を参照）。信頼できるサーバーには `"never"` を使用します（`examples/hosted_mcp/simple.py`）。
- **セッションとメモリ**: 承認と会話履歴を複数のターンにわたって保持するには、`Runner.run` にセッションを渡します。SQLite および OpenAI Conversations のセッション版は、`examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: Realtime デモでは、`RealtimeSession` の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開しています（サーバー側のハンドラーについては `examples/realtime/app/server.py`、API の仕様については [Realtime ガイド](realtime/guide.md#tool-approvals)を参照）。

## 長時間にわたる承認

`RunState` は、永続的に使用できるよう設計されています。`state.to_json()` または `state.to_string()` を使用して保留中の作業をデータベースやキューに保存し、後から `RunState.from_json(...)` または `RunState.from_string(...)` で復元できます。

便利なシリアライズオプションは次のとおりです。

-   `context_serializer`: マッピングではないコンテキストオブジェクトのシリアライズ方法をカスタマイズします。
-   `context_deserializer`: `RunState.from_json(...)` または `RunState.from_string(...)` で状態を読み込む際に、マッピングではないコンテキストオブジェクトを再構築します。
- `strict_context=True`: コンテキストがすでにマッピングであるか、適切なシリアライザーまたはデシリアライザーが指定されていない限り、シリアライズまたはデシリアライズを失敗させます。
- `context_override`: 状態の読み込み時に、シリアライズされたコンテキストを置き換えます。元のコンテキストオブジェクトを復元したくない場合に便利ですが、すでにシリアライズ済みのペイロードからそのコンテキストを削除するものではありません。
- `include_tracing_api_key=True`: 再開した作業で同じ認証情報を使用してトレースのエクスポートを継続する必要がある場合、シリアライズされたトレースペイロードにトレーシング API キーを含めます。

シリアライズされた実行状態には、アプリケーションのコンテキストに加え、承認、使用量、シリアライズされた `tool_input`、ネストされたエージェントツール実行の再開情報、トレースメタデータ、サーバー管理の会話設定など、SDK が管理するランタイムメタデータが含まれます。シリアライズされた状態を保存または送信する場合は、`RunContextWrapper.context` を永続化対象データとして扱い、状態とともに意図的に保持または送信したい場合を除き、そこに機密情報を保存しないでください。

## 保留中タスクのバージョン管理

承認が長期間保留される可能性がある場合は、シリアライズされた状態とともに、エージェント定義または SDK のバージョンマーカーを保存してください。これにより、モデル、プロンプト、ツール定義が変更された場合でも、デシリアライズ処理を対応するコードパスに振り分け、非互換性を回避できます。