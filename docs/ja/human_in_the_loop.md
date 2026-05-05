---
search:
  exclude: true
---
# Human-in-the-loop

human-in-the-loop ( HITL ) フローを使用すると、機密性の高いツール呼び出しを人が承認または拒否するまで、エージェント実行を一時停止できます。ツールは承認が必要なタイミングを宣言し、実行結果は保留中の承認を中断として表示し、`RunState` によって判断後に実行をシリアライズおよび再開できます。

この承認サーフェスは実行全体に適用され、現在のトップレベルエージェントに限定されません。同じパターンは、ツールが現在のエージェントに属する場合、ハンドオフで到達したエージェントに属する場合、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行に属する場合にも適用されます。ネストされた `Agent.as_tool()` の場合でも、中断は外側の実行に表示されるため、外側の `RunState` で承認または拒否し、元のトップレベル実行を再開します。

`Agent.as_tool()` では、承認は 2 つの異なるレイヤーで発生する可能性があります。エージェントツール自体が `Agent.as_tool(..., needs_approval=...)` によって承認を要求でき、さらにネストされたエージェント内のツールがネスト実行開始後に独自の承認を発生させることもできます。どちらも同じ外側実行の中断フローで処理されます。

このページでは、`interruptions` を介した手動承認フローに焦点を当てます。アプリがコードで判断できる場合、一部のツールタイプはプログラムによる承認コールバックもサポートしており、実行を一時停止せずに継続できます。

## 承認が必要なツールのマーキング

`needs_approval` を `True` に設定すると常に承認が必要になり、呼び出しごとに判断する非同期関数を渡すこともできます。呼び出し可能オブジェクトは、実行コンテキスト、解析済みツールパラメーター、ツール呼び出し ID を受け取ります。

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

`needs_approval` は [`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で利用できます。ローカル MCP サーバーも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を通じて承認をサポートします。ホスト型 MCP サーバーは、[`HostedMCPTool`][agents.tool.HostedMCPTool] の `tool_config={"require_approval": "always"}` と、任意の `on_approval_request` コールバックを介して承認をサポートします。 shell および apply_patch ツールは、割り込みを表示せずに自動承認または自動拒否したい場合に `on_approval` コールバックを受け付けます。

## 承認フローの仕組み

1. モデルがツール呼び出しを出力すると、ランナーはその承認ルール (`needs_approval`、`require_approval`、またはホスト型 MCP の同等機能) を評価します。
2. そのツール呼び出しに対する承認判断がすでに [`RunContextWrapper`][agents.run_context.RunContextWrapper] に保存されている場合、ランナーは確認なしで続行します。呼び出し単位の承認は特定の呼び出し ID にスコープされます。実行の残り期間における同ツールへの今後の呼び出しにも同じ判断を保持するには、`always_approve=True` または `always_reject=True` を渡します。
3. それ以外の場合、実行は一時停止し、`RunResult.interruptions` (または `RunResultStreaming.interruptions`) に `agent.name`、`tool_name`、`arguments` などの詳細を含む [`ToolApprovalItem`][agents.items.ToolApprovalItem] エントリーが入ります。これには、ハンドオフ後またはネストされた `Agent.as_tool()` 実行内で発生した承認も含まれます。
4. `result.to_state()` で結果を `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出した後、`Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。ここで `agent` は、その実行の元のトップレベルエージェントです。
5. 再開された実行は中断地点から継続し、新たな承認が必要であればこのフローに再度入ります。

`always_approve=True` または `always_reject=True` で作成された固定判断は実行状態に保存されるため、同じ一時停止済み実行を後で再開する際に `state.to_string()` / `RunState.from_string(...)` および `state.to_json()` / `RunState.from_json(...)` をまたいで保持されます。

同じパスで保留中の承認をすべて解決する必要はありません。`interruptions` には、通常の関数ツール、ホスト型 MCP 承認、ネストされた `Agent.as_tool()` 承認が混在する可能性があります。一部の項目のみ承認または拒否して再実行した場合、解決済みの呼び出しは継続し、未解決のものは `interruptions` に残って実行を再び一時停止します。

## 拒否メッセージのカスタマイズ

デフォルトでは、拒否されたツール呼び出しは SDK の標準拒否テキストを実行に返します。このメッセージは 2 つのレイヤーでカスタマイズできます。

-   実行全体のフォールバック: [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter] を設定し、実行全体の承認拒否に対するモデル可視のデフォルトメッセージを制御します。
-   呼び出し単位の上書き: 特定の拒否ツール呼び出しだけ別メッセージを表示したい場合、`state.reject(...)` に `rejection_message=...` を渡します。

両方が指定された場合、呼び出し単位の `rejection_message` が実行全体フォーマッターより優先されます。

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

両レイヤーを組み合わせて示す完全な例は [`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py) を参照してください。

## 自動承認判断

手動 `interruptions` は最も汎用的なパターンですが、唯一ではありません。

-   ローカル [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] は `on_approval` を使用してコード内で即時に承認または拒否できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] は、同種のプログラムによる判断のために `tool_config={"require_approval": "always"}` と `on_approval_request` を併用できます。
-   通常の [`function_tool`][agents.tool.function_tool] ツールと [`Agent.as_tool()`][agents.agent.Agent.as_tool] は、このページの手動中断フローを使用します。

これらのコールバックが判断を返すと、実行は人の応答を待って一時停止せずに継続します。 Realtime および音声セッション API については、[Realtime ガイド](realtime/guide.md) の承認フローを参照してください。

## ストリーミングとセッション

同じ中断フローはストリーミング実行でも機能します。ストリーミング実行が一時停止したら、イテレーターが終了するまで [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] を消費し、[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] を確認して解決し、再開後の出力もストリーミングを継続したい場合は [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] で再開します。このパターンのストリーミング版は [ストリーミング](streaming.md) を参照してください。

セッションも使用している場合は、`RunState` から再開する際に同じセッションインスタンスを渡し続けるか、同じバックエンドストアを指す別のセッションオブジェクトを渡してください。再開されたターンは同じ保存済み会話履歴に追加されます。セッションライフサイクルの詳細は [セッション](sessions/index.md) を参照してください。

## 例: 一時停止、承認、再開

以下のスニペットは JavaScript の HITL ガイドを踏襲しています。ツールに承認が必要なときに一時停止し、状態をディスクに保存し、再読み込みして、判断を収集した後に再開します。

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

この例では、`prompt_approval` は `input()` を使用し `run_in_executor(...)` で実行されるため同期的です。承認ソースがすでに非同期 ( 例: HTTP リクエストや非同期データベースクエリ) の場合は、`async def` 関数を使用して直接 `await` できます。

承認待ち中にも出力をストリーミングしたい場合は、`Runner.run_streamed` を呼び出し、完了まで `result.stream_events()` を消費し、その後は上記と同じ `result.to_state()` と再開手順に従ってください。

## リポジトリのパターンと例

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで処理し、保留中ツール呼び出しを承認してから `Runner.run_streamed(agent, state)` で再開する方法を示します。
- **カスタム拒否テキスト**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py` は、承認が拒否されたときに実行レベルの `tool_error_formatter` と呼び出し単位の `rejection_message` 上書きを組み合わせる方法を示します。
- **Agent as tool 承認**: `Agent.as_tool(..., needs_approval=...)` は、委譲されたエージェントタスクにレビューが必要な場合にも同じ中断フローを適用します。ネストされた中断も外側の実行に表示されるため、ネスト側ではなく元のトップレベルエージェントを再開してください。
- **ローカル shell / apply_patch ツール**: `ShellTool` と `ApplyPatchTool` も `needs_approval` をサポートします。将来の呼び出しのために判断をキャッシュするには `state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使用します。自動判断には `on_approval` を指定します ( `examples/tools/shell.py` を参照)。手動判断には中断を処理します ( `examples/tools/shell_human_in_the_loop.py` を参照)。ホスト型 shell 環境は `needs_approval` または `on_approval` をサポートしません。[ツールガイド](tools.md) を参照してください。
- **ローカル MCP サーバー**: `MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` で `require_approval` を使用し、MCP ツール呼び出しを制御します ( `examples/mcp/get_all_mcp_tools_example/main.py` および `examples/mcp/tool_filter_example/main.py` を参照)。
- **ホスト型 MCP サーバー**: HITL を強制するには `HostedMCPTool` で `require_approval` を `"always"` に設定し、必要に応じて `on_approval_request` を指定して自動承認または拒否します ( `examples/hosted_mcp/human_in_the_loop.py` および `examples/hosted_mcp/on_approval.py` を参照)。信頼済みサーバーには `"never"` を使用します (`examples/hosted_mcp/simple.py`)。
- **セッションとメモリ**: 複数ターンにわたり承認と会話履歴を保持するには `Runner.run` にセッションを渡します。 SQLite および OpenAI Conversations セッションのバリアントは `examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: realtime デモは `RealtimeSession` の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開します ( サーバー側ハンドラーは `examples/realtime/app/server.py`、API サーフェスは [Realtime ガイド](realtime/guide.md#tool-approvals) を参照)。

## 長時間実行承認

`RunState` は永続性を考慮して設計されています。保留中作業をデータベースやキューに保存するには `state.to_json()` または `state.to_string()` を使用し、後で `RunState.from_json(...)` または `RunState.from_string(...)` で再作成します。

有用なシリアライズオプション:

-   `context_serializer`: マッピング以外のコンテキストオブジェクトをどのようにシリアライズするかをカスタマイズします。
-   `context_deserializer`: `RunState.from_json(...)` または `RunState.from_string(...)` で状態をロードするときに、マッピング以外のコンテキストオブジェクトを再構築します。
-   `strict_context=True`: コンテキストがすでに
    マッピングであるか、適切な serializer / deserializer を提供しない限り、シリアライズまたはデシリアライズを失敗させます。
-   `context_override`: 状態ロード時にシリアライズ済みコンテキストを置き換えます。これは
    元のコンテキストオブジェクトを復元したくない場合に有用ですが、すでに
    シリアライズ済みペイロードからそのコンテキストを削除するものではありません。
-   `include_tracing_api_key=True`: 再開作業でも同じ認証情報でトレースをエクスポートし続ける必要がある場合に、
    シリアライズされたトレースペイロードに tracing API キーを含めます。

シリアライズされた実行状態には、アプリコンテキストに加えて、承認、
使用量、シリアライズされた `tool_input`、ネストされた agent-as-tool 再開、トレースメタデータ、サーバー管理の
会話設定など、SDK 管理の実行時メタデータが含まれます。シリアライズ状態を保存または転送する予定がある場合は、
`RunContextWrapper.context` を永続化データとして扱い、意図的に
状態と一緒に移動させたい場合を除き、そこに秘密情報を置かないでください。

## 保留タスクのバージョニング

承認がしばらく保留される可能性がある場合は、シリアライズ状態と一緒にエージェント定義または SDK のバージョンマーカーを保存してください。これにより、デシリアライズを対応するコードパスに振り分け、モデル、プロンプト、またはツール定義が変更された際の非互換性を回避できます。