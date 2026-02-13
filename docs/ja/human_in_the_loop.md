---
search:
  exclude: true
---
# Human-in-the-loop

human-in-the-loop (HITL) フローを使用すると、人が機微なツール呼び出しを承認または拒否するまで、エージェントの実行を一時停止できます。ツールは承認が必要なタイミングを宣言し、実行結果は保留中の承認を割り込みとして提示し、`RunState` により決定後に実行をシリアライズして再開できます。

## 承認が必要なツールのマーキング

常に承認を要求するには `needs_approval` を `True` に設定するか、呼び出しごとに判定する async 関数を指定します。この callable は実行コンテキスト、解析済みのツール パラメーター、ツール呼び出し ID を受け取ります。

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

`needs_approval` は [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で利用できます。ローカル MCP サーバーも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を通じて承認をサポートします。Hosted MCP サーバーは、`tool_config={"require_approval": "always"}` と任意の `on_approval_request` コールバックを指定した [`HostedMCPTool`][agents.tool.HostedMCPTool] を介して承認をサポートします。Shell および apply_patch ツールは、割り込みを出さずに自動承認または自動拒否したい場合に `on_approval` コールバックを受け付けます。

## 承認フローの仕組み

1. モデルがツール呼び出しを出力すると、runner が `needs_approval` を評価します。
2. そのツール呼び出しに対する承認判断がすでに [`RunContextWrapper`][agents.run_context.RunContextWrapper] に保存されている場合（例: `always_approve=True` によるもの）、runner はプロンプトを出さずに続行します。呼び出しごとの承認は特定の呼び出し ID にスコープされます。将来の呼び出しを自動的に許可するには `always_approve=True` を使用します。
3. それ以外の場合、実行は一時停止し、`RunResult.interruptions`（または `RunResultStreaming.interruptions`）に `agent.name`、`name`、`arguments` などの詳細を含む `ToolApprovalItem` エントリが入ります。
4. `result.to_state()` で結果を `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出し（任意で `always_approve` または `always_reject` を渡します）、その後 `Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。
5. 再開された実行は中断した箇所から継続し、新たに承認が必要になればこのフローに再度入ります。

## 例: 一時停止、承認、再開

以下のスニペットは JavaScript HITL ガイドを踏襲しています。ツールが承認を必要としたときに一時停止し、状態をディスクに永続化し、それを再読み込みして、判断を収集した後に再開します。

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

この例では、`prompt_approval` は `input()` を使用し、`run_in_executor(...)` で実行されるため同期的です。承認元がすでに非同期（例: HTTP リクエストや async データベース クエリ）であれば、`async def` 関数を使い、代わりに直接 `await` できます。

承認待ちの間も出力をストリーミングするには `Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を消費してから、上記と同じ `result.to_state()` と再開手順に従ってください。

## このリポジトリの他のパターン

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで取り出してから、保留中のツール呼び出しを承認し、`Runner.run_streamed(agent, state)` で再開する方法を示します。
- **ツールとしてのエージェントの承認**: `Agent.as_tool(..., needs_approval=...)` は、委譲されたエージェント タスクにレビューが必要な場合に同じ割り込みフローを適用します。
- **Shell および apply_patch ツール**: `ShellTool` と `ApplyPatchTool` も `needs_approval` をサポートします。将来の呼び出しに対して判断をキャッシュするには `state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使用します。自動判断には `on_approval` を指定します（`examples/tools/shell.py` を参照）。手動判断には割り込みを処理します（`examples/tools/shell_human_in_the_loop.py` を参照）。
- **ローカル MCP サーバー**: `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` の `require_approval` を使用して MCP ツール呼び出しをゲートします（`examples/mcp/get_all_mcp_tools_example/main.py` と `examples/mcp/tool_filter_example/main.py` を参照）。
- **Hosted MCP サーバー**: `HostedMCPTool` の `require_approval` を `"always"` に設定して HITL を強制し、必要に応じて `on_approval_request` を指定して自動承認または拒否を行います（`examples/hosted_mcp/human_in_the_loop.py` と `examples/hosted_mcp/on_approval.py` を参照）。信頼できるサーバーには `"never"` を使用します（`examples/hosted_mcp/simple.py`）。
- **セッションとメモリ**: `Runner.run` にセッションを渡すことで、承認と会話履歴が複数ターンにわたって維持されます。SQLite と OpenAI Conversations のセッション バリアントは `examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: realtime デモは、`RealtimeSession` の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開します（サーバー側ハンドラーは `examples/realtime/app/server.py` を参照）。

## 長時間にわたる承認

`RunState` は永続性を意図して設計されています。`state.to_json()` または `state.to_string()` を使用して保留中の作業をデータベースやキューに保存し、後で `RunState.from_json(...)` または `RunState.from_string(...)` で再作成できます。シリアライズされたペイロードに機微なコンテキスト データを永続化したくない場合は `context_override` を渡してください。

## 保留タスクのバージョニング

承認がしばらく滞留する可能性がある場合は、エージェント定義または SDK のバージョン マーカーを、シリアライズされた状態と一緒に保存してください。そうすれば、デシリアライズを一致するコード パスにルーティングでき、モデル、プロンプト、ツール定義が変更された際の非互換を回避できます。