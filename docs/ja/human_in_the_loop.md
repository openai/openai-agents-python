---
search:
  exclude: true
---
# Human-in-the-loop

human-in-the-loop (HITL) フローを使用して、機密性の高いツール呼び出しを人が承認または拒否するまで、エージェントの実行を一時停止します。ツールは承認が必要なタイミングを宣言し、実行結果は保留中の承認を割り込みとして表面化し、`RunState` により判断後に実行をシリアライズして再開できます。

## 承認が必要なツールのマーキング

`needs_approval` を `True` に設定すると常に承認が必要になります。あるいは、呼び出しごとに判断する async 関数を指定できます。呼び出し可能オブジェクトは、実行コンテキスト、パース済みのツール パラメーター、ツール呼び出し ID を受け取ります。

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

`needs_approval` は [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で利用できます。ローカル MCP サーバーも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を通じて承認をサポートします。ホスト型 MCP サーバーは、`tool_config={"require_approval": "always"}` と任意の `on_approval_request` コールバックを備えた [`HostedMCPTool`][agents.tool.HostedMCPTool] により承認をサポートします。Shell と apply_patch のツールは、割り込みを表面化せずに自動承認または自動拒否したい場合に `on_approval` コールバックを受け付けます。

## 承認フローの仕組み

1. モデルがツール呼び出しを出力すると、runner は `needs_approval` を評価します。
2. そのツール呼び出しに対する承認判断がすでに [`RunContextWrapper`][agents.run_context.RunContextWrapper] に保存されている場合（例: `always_approve=True` による）、runner はプロンプト表示なしで処理を続行します。呼び出しごとの承認は特定の呼び出し ID にスコープされます。今後の呼び出しを自動的に許可するには `always_approve=True` を使用します。
3. それ以外の場合、実行は一時停止し、`RunResult.interruptions`（または `RunResultStreaming.interruptions`）に `ToolApprovalItem` エントリが含まれます。そこには `agent.name`、`name`、`arguments` などの詳細が入ります。
4. `result.to_state()` で結果を `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出し（任意で `always_approve` または `always_reject` を渡します）、その後 `Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。
5. 再開された実行は中断地点から続行し、新たな承認が必要になればこのフローに再び入ります。

## 例: 一時停止、承認、再開

以下のスニペットは JavaScript の HITL ガイドと同様に、ツールが承認を必要としたら一時停止し、状態をディスクに永続化して再読み込みし、判断を収集した後に再開します。

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

この例では、`prompt_approval` は `input()` を使用し、`run_in_executor(...)` で実行されるため同期的です。承認ソースがすでに非同期（たとえば HTTP リクエストや async なデータベース クエリ）の場合は、`async def` 関数を使って直接 `await` できます。

承認待ちの間も出力をストリーミングするには、`Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を消費してから、上で示したのと同じ `result.to_state()` と再開手順に従います。

## リポジトリのパターンと examples

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで処理してから、保留中のツール呼び出しを承認し、`Runner.run_streamed(agent, state)` で再開する方法を示します。
- **ツールとしてのエージェント承認**: `Agent.as_tool(..., needs_approval=...)` は、委譲されたエージェント タスクにレビューが必要な場合に同じ割り込みフローを適用します。
- **Shell と apply_patch のツール**: `ShellTool` と `ApplyPatchTool` も `needs_approval` をサポートします。将来の呼び出しのために判断をキャッシュするには、`state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使用します。自動判断には `on_approval` を指定します（`examples/tools/shell.py` を参照）。手動判断には割り込みを処理します（`examples/tools/shell_human_in_the_loop.py` を参照）。
- **ローカル MCP サーバー**: `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` の `require_approval` を使用して MCP ツール呼び出しをゲートします（`examples/mcp/get_all_mcp_tools_example/main.py` と `examples/mcp/tool_filter_example/main.py` を参照）。
- **ホスト型 MCP サーバー**: `HostedMCPTool` で `require_approval` を `"always"` に設定して HITL を強制し、任意で `on_approval_request` を指定して自動承認または拒否を行います（`examples/hosted_mcp/human_in_the_loop.py` と `examples/hosted_mcp/on_approval.py` を参照）。信頼できるサーバーには `"never"` を使用します（`examples/hosted_mcp/simple.py`）。
- **セッションとメモリ**: `Runner.run` にセッションを渡すことで、承認と会話履歴が複数ターンにわたって保持されます。SQLite と OpenAI Conversations のセッション バリアントは `examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: realtime デモは、`RealtimeSession` の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開します（サーバー側ハンドラーは `examples/realtime/app/server.py` を参照）。

## 長時間の承認待ち

`RunState` は永続性を意図して設計されています。`state.to_json()` または `state.to_string()` を使用して保留中の作業をデータベースやキューに保存し、後で `RunState.from_json(...)` または `RunState.from_string(...)` で再生成します。シリアライズされたペイロードに機密性の高いコンテキスト データを永続化したくない場合は、`context_override` を渡します。

## 保留タスクのバージョニング

承認がしばらく滞留する可能性がある場合は、エージェント定義または SDK のバージョン マーカーをシリアライズ済み状態と一緒に保存してください。これにより、デシリアライズを一致するコード パスにルーティングでき、モデル、プロンプト、ツール定義が変更された際の非互換性を回避できます。