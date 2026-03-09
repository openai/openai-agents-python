---
search:
  exclude: true
---
# Human-in-the-loop

human-in-the-loop ( HITL ) フローを使用すると、機密性の高いツール呼び出しを人が承認または拒否するまで、エージェントの実行を一時停止できます。ツールは承認が必要なタイミングを宣言し、実行結果は保留中の承認を割り込みとして表示し、`RunState` によって決定後に実行をシリアライズして再開できます。

この承認サーフェスは実行全体に適用され、現在のトップレベルエージェントに限定されません。同じパターンは、ツールが現在のエージェントに属する場合、ハンドオフで到達したエージェントに属する場合、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行に属する場合にも適用されます。ネストされた `Agent.as_tool()` の場合でも、割り込みは外側の実行に表示されるため、外側の `RunState` で承認または拒否し、元のトップレベル実行を再開します。

`Agent.as_tool()` では、承認は 2 つの異なるレイヤーで発生する可能性があります。エージェントツール自体が `Agent.as_tool(..., needs_approval=...)` により承認を要求でき、ネストされた実行が開始された後に、ネストされたエージェント内のツールが独自の承認を発生させることもできます。どちらも同じ外側実行の割り込みフローで処理されます。

このページでは、`interruptions` を介した手動承認フローに焦点を当てます。アプリがコードで判断できる場合、一部のツールタイプはプログラムによる承認コールバックもサポートしており、一時停止せずに実行を継続できます。

## 承認が必要なツールの指定

`needs_approval` を `True` に設定すると常に承認が必要になり、または呼び出しごとに判定する非同期関数を指定できます。呼び出し可能オブジェクトは、実行コンテキスト、解析済みツールパラメーター、ツール呼び出し ID を受け取ります。

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

`needs_approval` は [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で利用できます。ローカル MCP サーバーも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を通じて承認をサポートします。ホスト型 MCP サーバーは、[`HostedMCPTool`][agents.tool.HostedMCPTool] で `tool_config={"require_approval": "always"}` を使用し、任意の `on_approval_request` コールバックを指定することで承認をサポートします。Shell および apply_patch ツールは、割り込みを表示せずに自動承認または自動拒否したい場合に `on_approval` コールバックを受け付けます。

## 承認フローの仕組み

1. モデルがツール呼び出しを出力すると、ランナーはその承認ルール（`needs_approval`、`require_approval`、またはホスト型 MCP 相当）を評価します。
2. そのツール呼び出しに対する承認決定がすでに [`RunContextWrapper`][agents.run_context.RunContextWrapper] に保存されている場合、ランナーは確認なしで続行します。呼び出し単位の承認は特定の呼び出し ID にスコープされます。`always_approve=True` または `always_reject=True` を渡すと、実行の残り期間中、そのツールの今後の呼び出しにも同じ決定を保持します。
3. そうでない場合、実行は一時停止し、`RunResult.interruptions`（または `RunResultStreaming.interruptions`）に、`agent.name`、`tool_name`、`arguments` などの詳細を含む [`ToolApprovalItem`][agents.items.ToolApprovalItem] エントリーが入ります。これには、ハンドオフ後またはネストされた `Agent.as_tool()` 実行内で発生した承認も含まれます。
4. 結果を `result.to_state()` で `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出してから、`Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。ここで `agent` はその実行の元のトップレベルエージェントです。
5. 再開された実行は中断地点から続行し、新たな承認が必要になればこのフローに再び入ります。

`always_approve=True` または `always_reject=True` で作成された固定決定は実行状態に保存されるため、同じ一時停止実行を後で再開する際に、`state.to_string()` / `RunState.from_string(...)` および `state.to_json()` / `RunState.from_json(...)` をまたいで維持されます。

同じパスですべての保留中承認を解決する必要はありません。`interruptions` には、通常の関数ツール、ホスト型 MCP 承認、ネストされた `Agent.as_tool()` 承認が混在する可能性があります。一部の項目だけを承認または拒否して再実行すると、解決済み呼び出しは継続でき、未解決のものは `interruptions` に残って再び実行を一時停止します。

## 自動承認決定

手動の `interruptions` は最も汎用的なパターンですが、それだけではありません。

-   ローカルの [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] は、`on_approval` を使ってコード内で即時に承認または拒否できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] は、同種のプログラム的決定のために `tool_config={"require_approval": "always"}` と `on_approval_request` を組み合わせて使えます。
-   通常の [`function_tool`][agents.tool.function_tool] と [`Agent.as_tool()`][agents.agent.Agent.as_tool] は、このページの手動割り込みフローを使用します。

これらのコールバックが決定を返すと、人の応答を待って一時停止することなく実行が継続します。Realtime と音声セッション API については、[Realtime guide](realtime/guide.md) の承認フローを参照してください。

## ストリーミングとセッション

同じ割り込みフローはストリーミング実行でも機能します。ストリーミング実行が一時停止したら、イテレーターが終了するまで [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] の消費を続け、[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] を確認して解決し、再開後の出力もストリーミングしたい場合は [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] で再開します。このパターンのストリーミング版は [Streaming](streaming.md) を参照してください。

セッションも使用している場合は、`RunState` から再開する際に同じセッションインスタンスを渡し続けるか、同じバックエンドストアを指す別のセッションオブジェクトを渡してください。すると再開ターンは同じ保存済み会話履歴に追加されます。セッションライフサイクルの詳細は [Sessions](sessions/index.md) を参照してください。

## 例: 一時停止、承認、再開

以下のスニペットは JavaScript HITL ガイドを反映したものです。ツールが承認を必要とすると一時停止し、状態をディスクに永続化し、再読み込みして、決定を収集した後に再開します。

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

この例では `prompt_approval` は `input()` を使用し、`run_in_executor(...)` で実行されるため同期的です。承認元がすでに非同期（たとえば HTTP リクエストや非同期データベースクエリ）の場合は、`async def` 関数を使用して代わりに直接 `await` できます。

承認待機中も出力をストリーミングするには、`Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を消費してから、上記の `result.to_state()` と再開手順に従ってください。

## リポジトリのパターンとコード例

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで処理してから、保留中のツール呼び出しを承認し、`Runner.run_streamed(agent, state)` で再開する方法を示します。
- **Agent as tool 承認**: `Agent.as_tool(..., needs_approval=...)` は、委譲されたエージェントタスクでレビューが必要な場合に同じ割り込みフローを適用します。ネストされた割り込みも外側実行に表示されるため、ネスト側ではなく元のトップレベルエージェントを再開してください。
- **ローカル shell と apply_patch ツール**: `ShellTool` と `ApplyPatchTool` も `needs_approval` をサポートします。`state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使って、今後の呼び出しの決定をキャッシュできます。自動決定には `on_approval` を指定し（`examples/tools/shell.py` を参照）、手動決定には割り込みを処理します（`examples/tools/shell_human_in_the_loop.py` を参照）。ホスト型 shell 環境は `needs_approval` や `on_approval` をサポートしません。 [tools guide](tools.md) を参照してください。
- **ローカル MCP サーバー**: `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` の `require_approval` を使用して MCP ツール呼び出しをゲートできます（`examples/mcp/get_all_mcp_tools_example/main.py` および `examples/mcp/tool_filter_example/main.py` を参照）。
- **ホスト型 MCP サーバー**: `HostedMCPTool` で `require_approval` を `"always"` に設定して HITL を強制し、必要に応じて `on_approval_request` を指定して自動承認または拒否できます（`examples/hosted_mcp/human_in_the_loop.py` および `examples/hosted_mcp/on_approval.py` を参照）。信頼済みサーバーには `"never"` を使用します（`examples/hosted_mcp/simple.py`）。
- **セッションとメモリ**: 承認と会話履歴を複数ターンにわたって維持するには、`Runner.run` にセッションを渡します。SQLite と OpenAI Conversations のセッションバリアントは `examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: realtime デモは、`RealtimeSession` の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開しています（サーバー側ハンドラーは `examples/realtime/app/server.py`、API サーフェスは [Realtime guide](realtime/guide.md#tool-approvals) を参照）。

## 長時間実行の承認

`RunState` は永続性を重視して設計されています。`state.to_json()` または `state.to_string()` を使用して保留中作業をデータベースやキューに保存し、後で `RunState.from_json(...)` または `RunState.from_string(...)` で再作成できます。

有用なシリアライズオプション:

-   `context_serializer`: 非マッピングのコンテキストオブジェクトをどのようにシリアライズするかをカスタマイズします。
-   `context_deserializer`: `RunState.from_json(...)` または `RunState.from_string(...)` で状態を読み込む際に、非マッピングのコンテキストオブジェクトを再構築します。
-   `strict_context=True`: コンテキストがすでにマッピングであるか、適切な serializer / deserializer を指定していない限り、シリアライズまたはデシリアライズを失敗させます。
-   `context_override`: 状態読み込み時にシリアライズ済みコンテキストを置き換えます。元のコンテキストオブジェクトを復元したくない場合に有用ですが、すでにシリアライズ済みのペイロードからそのコンテキストを削除するものではありません。
-   `include_tracing_api_key=True`: 再開した作業でも同じ資格情報でトレースのエクスポートを継続する必要がある場合に、シリアライズ済みトレースペイロードへトレーシング API キーを含めます。

シリアライズされた実行状態には、アプリコンテキストに加えて、承認、使用量、シリアライズされた `tool_input`、ネストされた agent-as-tool の再開、トレースメタデータ、サーバー管理の会話設定など、SDK 管理のランタイムメタデータが含まれます。シリアライズ済み状態を保存または送信する予定がある場合、`RunContextWrapper.context` は永続化データとして扱い、意図的に状態とともに持ち運びたい場合を除いて秘密情報を置かないでください。

## 保留タスクのバージョニング

承認がしばらく保留される可能性がある場合は、シリアライズ済み状態とともにエージェント定義または SDK のバージョンマーカーを保存してください。これにより、モデル、プロンプト、ツール定義が変更された際の非互換性を避けるために、デシリアライズを一致するコードパスにルーティングできます。