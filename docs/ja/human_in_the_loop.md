---
search:
  exclude: true
---
# ヒューマンインザループ

human-in-the-loop (HITL) フローを使用すると、機密性の高いツール呼び出しが人によって承認または拒否されるまで、エージェントの実行を一時停止できます。ツールは承認が必要なタイミングを宣言し、実行結果は保留中の承認を中断として表面化し、`RunState` によって判断後の実行をシリアライズして再開できます。

この承認が表面化する範囲は実行全体であり、現在のトップレベルエージェントに限定されません。同じパターンは、ツールが現在のエージェントに属する場合、ハンドオフで到達したエージェントに属する場合、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行に属する場合にも適用されます。ネストされた `Agent.as_tool()` の場合でも、中断は外側の実行に表面化するため、外側の `RunState` で承認または拒否し、元のトップレベル実行を再開します。

`Agent.as_tool()` では、承認は 2 つの異なる層で発生することがあります。エージェントツール自体が `Agent.as_tool(..., needs_approval=...)` によって承認を必要とする場合があり、ネストされたエージェント内のツールが、ネストされた実行の開始後に独自の承認を要求する場合もあります。どちらも同じ外側の実行の中断フローで処理されます。

このページでは、`interruptions` による手動承認フローに焦点を当てます。アプリがコード内で判断できる場合、一部のツールタイプはプログラムによる承認コールバックもサポートしているため、一時停止せずに実行を継続できます。

## 承認が必要なツールのマーク付け

常に承認を必須にするには `needs_approval` を `True` に設定し、呼び出しごとに判断するには async 関数を指定します。この呼び出し可能オブジェクトは、実行コンテキスト、解析済みのツールパラメーター、ツール呼び出し ID を受け取ります。

```python
from agents import Agent, Runner, function_tool


@function_tool(needs_approval=True)
async def cancel_order(order_id: int) -> str:
    return f"Cancelled order {order_id}"


async def requires_review(_ctx, params, _call_id) -> bool:
    return "refund" in params.get("subject", "").lower()


@function_tool(needs_approval=requires_review)
async def send_email(subject: str, body: str) -> str:
    return f"Sent '{subject}'"


agent = Agent(
    name="Support agent",
    instructions="Handle tickets and ask for approval when needed.",
    tools=[cancel_order, send_email],
)
```

`needs_approval` は [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で利用できます。ローカル MCP サーバーも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` によって承認をサポートします。ホスト型 MCP サーバーは、`tool_config={"require_approval": "always"}` と任意の `on_approval_request` コールバックを指定した [`HostedMCPTool`][agents.tool.HostedMCPTool] によって承認をサポートします。シェルツールと apply_patch ツールは、中断を表面化させずに自動承認または自動拒否したい場合に、`on_approval` コールバックを受け付けます。

## 承認フローの仕組み

1. モデルがツール呼び出しを生成すると、ランナーはその承認ルール（`needs_approval`、`require_approval`、またはホスト型 MCP の同等設定）を評価します。
2. そのツール呼び出しに対する承認判断がすでに [`RunContextWrapper`][agents.run_context.RunContextWrapper] に保存されている場合、ランナーはプロンプトを表示せずに続行します。呼び出しごとの承認は特定の呼び出し ID にスコープされます。実行の残りの期間におけるそのツールへの今後の呼び出しにも同じ判断を保持するには、`always_approve=True` または `always_reject=True` を渡します。
3. それ以外の場合、実行は一時停止し、`RunResult.interruptions`（または `RunResultStreaming.interruptions`）に、`agent.name`、`tool_name`、`arguments` などの詳細を含む [`ToolApprovalItem`][agents.items.ToolApprovalItem] エントリが含まれます。これには、ハンドオフ後、またはネストされた `Agent.as_tool()` 実行内で要求された承認も含まれます。
4. `result.to_state()` で実行結果を `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出してから、`Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。ここで `agent` は、その実行の元のトップレベルエージェントです。
5. 再開された実行は中断した箇所から続行し、新しい承認が必要になった場合はこのフローに再び入ります。

`always_approve=True` または `always_reject=True` で作成された固定的な判断は実行状態に保存されるため、後で同じ一時停止中の実行を再開する際に、`state.to_string()` / `RunState.from_string(...)` や `state.to_json()` / `RunState.from_json(...)` を経ても保持されます。

同じパスで保留中の承認をすべて解決する必要はありません。`interruptions` には、通常の関数ツール、ホスト型 MCP 承認、ネストされた `Agent.as_tool()` 承認が混在する場合があります。一部の項目だけを承認または拒否してから再実行すると、解決済みの呼び出しは続行できますが、未解決のものは `interruptions` に残り、実行を再び一時停止します。

## カスタム拒否メッセージ

デフォルトでは、拒否されたツール呼び出しは SDK の標準拒否テキストを実行内に返します。このメッセージは 2 つの層でカスタマイズできます。

-   実行全体のフォールバック: 実行全体にわたる承認拒否について、モデルから見えるデフォルトメッセージを制御するには、[`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter] を設定します。
-   呼び出しごとの上書き: 特定の拒否されたツール呼び出しで別のメッセージを表面化させたい場合は、`state.reject(...)` に `rejection_message=...` を渡します。

両方が指定された場合は、呼び出しごとの `rejection_message` が実行全体のフォーマッターより優先されます。

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

両方の層を組み合わせて示す完全な例については、[`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py) を参照してください。

## 自動承認判断

手動の `interruptions` は最も一般的なパターンですが、それだけではありません。

-   ローカルの [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] は、`on_approval` を使用してコード内で即座に承認または拒否できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] は、`tool_config={"require_approval": "always"}` と `on_approval_request` を組み合わせて使用することで、同じ種類のプログラムによる判断を行えます。
-   通常の [`function_tool`][agents.tool.function_tool] ツールと [`Agent.as_tool()`][agents.agent.Agent.as_tool] は、このページの手動中断フローを使用します。

これらのコールバックが判断を返すと、実行は人間の応答を待って一時停止せずに続行します。Realtime と音声セッション API については、[Realtime ガイド](realtime/guide.md)の承認フローを参照してください。

## ストリーミングとセッション

同じ中断フローはストリーミング実行でも機能します。ストリーミング実行が一時停止した後は、イテレーターが終了するまで [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] を消費し続け、[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] を確認し、それらを解決して、再開後の出力もストリーミングし続けたい場合は [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] で再開します。このパターンのストリーミング版については、[ストリーミング](streaming.md)を参照してください。

セッションも使用している場合は、`RunState` から再開するときに同じセッションインスタンスを渡し続けるか、同じバックエンドストアを指す別のセッションオブジェクトを渡します。これにより、再開されたターンは同じ保存済み会話履歴に追加されます。セッションのライフサイクルの詳細については、[セッション](sessions/index.md)を参照してください。

## 例: 一時停止、承認、再開

以下のスニペットは JavaScript HITL ガイドに対応するものです。ツールが承認を必要とすると一時停止し、状態をディスクに永続化し、再読み込みして、判断を収集した後に再開します。

```python
import asyncio
import json
from pathlib import Path

from agents import Agent, Runner, RunState, function_tool


async def needs_oakland_approval(_ctx, params, _call_id) -> bool:
    return "Oakland" in params.get("city", "")


@function_tool(needs_approval=needs_oakland_approval)
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

この例では、`prompt_approval` は `input()` を使用し、`run_in_executor(...)` で実行されるため同期的です。承認元がすでに非同期である場合（たとえば、HTTP リクエストや async データベースクエリ）、代わりに `async def` 関数を使用して直接 `await` できます。

承認待ちの間に出力をストリーミングするには、`Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を消費してから、上記と同じ `result.to_state()` と再開手順に従います。

## リポジトリのパターンとコード例

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで消費し、その後に保留中のツール呼び出しを承認してから `Runner.run_streamed(agent, state)` で再開する方法を示しています。
- **カスタム拒否テキスト**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py` は、承認が拒否されたときに、実行レベルの `tool_error_formatter` と呼び出しごとの `rejection_message` 上書きを組み合わせる方法を示しています。
- **ツールとしてのエージェントの承認**: `Agent.as_tool(..., needs_approval=...)` は、委任されたエージェントタスクにレビューが必要な場合に、同じ中断フローを適用します。ネストされた中断も外側の実行に表面化するため、ネストされたエージェントではなく、元のトップレベルエージェントを再開します。
- **ローカルシェルと apply_patch ツール**: `ShellTool` と `ApplyPatchTool` も `needs_approval` をサポートします。今後の呼び出しに対して判断をキャッシュするには、`state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使用します。自動判断には `on_approval` を指定します（`examples/tools/shell.py` を参照）。手動判断には中断を処理します（`examples/tools/shell_human_in_the_loop.py` を参照）。ホスト型シェル環境は `needs_approval` または `on_approval` をサポートしていません。[ツールガイド](tools.md)を参照してください。
- **ローカル MCP サーバー**: MCP ツール呼び出しを制御するには、`MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` で `require_approval` を使用します（`examples/mcp/get_all_mcp_tools_example/main.py` と `examples/mcp/tool_filter_example/main.py` を参照）。
- **ホスト型 MCP サーバー**: HITL を強制するには、`HostedMCPTool` で `require_approval` を `"always"` に設定し、必要に応じて自動承認または拒否のために `on_approval_request` を指定します（`examples/hosted_mcp/human_in_the_loop.py` と `examples/hosted_mcp/on_approval.py` を参照）。信頼できるサーバーには `"never"` を使用します（`examples/hosted_mcp/simple.py`）。
- **セッションとメモリ**: 承認と会話履歴が複数ターンにわたって保持されるように、`Runner.run` にセッションを渡します。SQLite と OpenAI Conversations セッションのバリエーションは、`examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: Realtime デモは、`RealtimeSession` 上の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開しています（サーバー側ハンドラーについては `examples/realtime/app/server.py`、API サーフェスについては [Realtime ガイド](realtime/guide.md#tool-approvals)を参照）。

## 長時間にわたる承認

`RunState` は耐久性を持つように設計されています。保留中の作業をデータベースまたはキューに保存するには `state.to_json()` または `state.to_string()` を使用し、後で `RunState.from_json(...)` または `RunState.from_string(...)` で再作成します。

有用なシリアライズオプション:

-   `context_serializer`: 非マッピングのコンテキストオブジェクトをシリアライズする方法をカスタマイズします。
-   `context_deserializer`: `RunState.from_json(...)` または `RunState.from_string(...)` で状態を読み込むときに、非マッピングのコンテキストオブジェクトを再構築します。
-   `strict_context=True`: コンテキストがすでに
    マッピングであるか、適切なシリアライザー/デシリアライザーを指定している場合を除き、シリアライズまたはデシリアライズを失敗させます。
-   `context_override`: 状態を読み込むときに、シリアライズされたコンテキストを置き換えます。これは、元のコンテキストオブジェクトを復元したくない場合に便利ですが、
    すでにシリアライズされたペイロードからそのコンテキストを削除するわけではありません。
-   `include_tracing_api_key=True`: 同じ認証情報でトレースのエクスポートを継続するために再開後の作業で必要な場合、
    シリアライズされたトレースペイロードにトレーシング API キーを含めます。

シリアライズされた実行状態には、アプリのコンテキストに加えて、承認、
使用量、シリアライズされた `tool_input`、ネストされた agent-as-tool の再開、トレースメタデータ、サーバー管理の
会話設定など、SDK が管理するランタイムメタデータが含まれます。シリアライズされた状態を保存または送信する予定がある場合は、
`RunContextWrapper.context` を永続化データとして扱い、状態と一緒に移動することを意図している場合を除き、
そこにシークレットを置かないでください。

## 保留中タスクのバージョニング

承認がしばらく保留状態のままになる可能性がある場合は、シリアライズされた状態と一緒に、エージェント定義または SDK のバージョンマーカーを保存してください。そうすれば、モデル、プロンプト、ツール定義が変更されたときの非互換性を避けるために、デシリアライズを対応するコードパスにルーティングできます。