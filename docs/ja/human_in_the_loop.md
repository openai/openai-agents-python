---
search:
  exclude: true
---
# ヒューマンインザループ

ヒューマンインザループ (HITL) フローを使用すると、人が慎重な扱いが必要なツール呼び出しを承認または拒否するまで、エージェントの実行を一時停止できます。ツールは承認が必要なタイミングを宣言し、実行結果は保留中の承認を中断として提示し、`RunState` によって判定後に実行をシリアライズして再開できます。

その承認の提示先は実行全体であり、現在のトップレベルのエージェントに限定されません。同じパターンは、ツールが現在のエージェントに属する場合、ハンドオフを通じて到達したエージェントに属する場合、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] 実行に属する場合にも適用されます。ネストされた `Agent.as_tool()` の場合でも、中断は外側の実行に提示されるため、外側の `RunState` で承認または拒否し、元のトップレベルの実行を再開します。

`Agent.as_tool()` では、承認が 2 つの異なるレイヤーで発生する可能性があります。エージェントツール自体が `Agent.as_tool(..., needs_approval=...)` によって承認を要求でき、ネストされたエージェント内のツールも、ネストされた実行が開始した後に独自の承認を要求できます。どちらも同じ外側の実行の中断フローを通じて処理されます。

このページでは、`interruptions` を介した手動承認フローに焦点を当てます。アプリがコード内で判定できる場合、一部のツールタイプはプログラムによる承認コールバックにも対応しているため、実行を一時停止せずに続行できます。

## 承認が必要なツールの指定

常に承認を要求するには `needs_approval` を `True` に設定するか、呼び出しごとに判定する async 関数を指定します。この呼び出し可能オブジェクトは、実行コンテキスト、解析済みのツールパラメーター、ツール呼び出し ID を受け取ります。

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

`needs_approval` は、[`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で利用できます。ローカル MCP サーバーも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を通じて承認に対応しています。ホスト型 MCP サーバーは、`tool_config={"require_approval": "always"}` と任意の `on_approval_request` コールバックを設定した [`HostedMCPTool`][agents.tool.HostedMCPTool] によって承認に対応します。Shell と apply_patch ツールは、中断を提示せずに自動承認または自動拒否したい場合に `on_approval` コールバックを受け付けます。

## 承認フローの仕組み

1. モデルがツール呼び出しを出力すると、ランナーはその承認ルール (`needs_approval`、`require_approval`、またはホスト型 MCP の同等機能) を評価します。
2. そのツール呼び出しの承認判定がすでに [`RunContextWrapper`][agents.run_context.RunContextWrapper] に保存されている場合、ランナーは確認を求めずに処理を続行します。呼び出しごとの承認は特定の呼び出し ID にスコープされます。そのツールに対する今後の呼び出しに、実行の残りの間同じ判定を保持するには、`always_approve=True` または `always_reject=True` を渡します。
3. それ以外の場合、実行は一時停止し、`RunResult.interruptions` (または `RunResultStreaming.interruptions`) に、`agent.name`、`tool_name`、`arguments` などの詳細を含む [`ToolApprovalItem`][agents.items.ToolApprovalItem] エントリが入ります。これには、ハンドオフ後やネストされた `Agent.as_tool()` 実行内で発生した承認も含まれます。
4. 実行結果を `result.to_state()` で `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出してから、`Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。ここで `agent` は、その実行における元のトップレベルのエージェントです。
5. 再開された実行は中断した場所から続行し、新しい承認が必要になった場合はこのフローに再び入ります。

`always_approve=True` または `always_reject=True` で作成された固定判定は実行状態に保存されるため、後で同じ一時停止中の実行を再開するときに `state.to_string()` / `RunState.from_string(...)` および `state.to_json()` / `RunState.from_json(...)` を使っても保持されます。

すべての保留中承認を同じ 1 回の処理で解決する必要はありません。`interruptions` には、通常の関数ツール、ホスト型 MCP の承認、ネストされた `Agent.as_tool()` の承認が混在する場合があります。一部の項目だけを承認または拒否した後に再実行すると、解決済みの呼び出しは続行でき、未解決のものは `interruptions` に残って実行を再び一時停止します。

## カスタム拒否メッセージ

既定では、拒否されたツール呼び出しは SDK 標準の拒否テキストを実行内に返します。このメッセージは 2 つのレイヤーでカスタマイズできます。

- 実行全体のフォールバック: 実行全体で承認拒否に対するモデルに見える既定メッセージを制御するには、[`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter] を設定します。
- 呼び出しごとのオーバーライド: 特定の拒否されたツール呼び出しだけに異なるメッセージを提示したい場合は、`state.reject(...)` に `rejection_message=...` を渡します。

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

両方のレイヤーをまとめて示す完全なコード例については、[`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py) を参照してください。

## 自動承認判定

手動の `interruptions` は最も汎用的なパターンですが、唯一の方法ではありません。

- ローカルの [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] は、`on_approval` を使用してコード内で即座に承認または拒否できます。
- [`HostedMCPTool`][agents.tool.HostedMCPTool] は、`tool_config={"require_approval": "always"}` と `on_approval_request` を組み合わせて、同じ種類のプログラムによる判定を行えます。
- 通常の [`function_tool`][agents.tool.function_tool] ツールと [`Agent.as_tool()`][agents.agent.Agent.as_tool] は、このページの手動中断フローを使用します。

これらのコールバックが判定を返すと、人間の応答を待って一時停止することなく実行が続行されます。Realtime および音声セッション API については、[Realtime ガイド](realtime/guide.md) の承認フローを参照してください。

## ストリーミングとセッション

同じ中断フローはストリーミング実行でも機能します。ストリーミング実行が一時停止した後は、イテレーターが終了するまで [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] を消費し続け、[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] を確認して解決し、再開後の出力もストリーミングし続けたい場合は [`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] で再開します。このパターンのストリーミング版については、[ストリーミング](streaming.md) を参照してください。

セッションも使用している場合は、`RunState` から再開するときに同じセッションインスタンスを渡し続けるか、同じバッキングストアを指す別のセッションオブジェクトを渡します。これにより、再開されたターンは同じ保存済み会話履歴に追加されます。セッションのライフサイクル詳細については、[セッション](sessions/index.md) を参照してください。

## 例: 一時停止・承認・再開

以下のスニペットは JavaScript の HITL ガイドと同じ流れです。ツールに承認が必要な場合に一時停止し、状態をディスクに永続化して再読み込みし、判定を収集した後に再開します。

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

この例では、`prompt_approval` は `input()` を使用し、`run_in_executor(...)` で実行されるため同期的です。承認の取得元がすでに非同期である場合 (たとえば、HTTP リクエストや非同期データベースクエリ)、代わりに `async def` 関数を使用して直接 `await` できます。

承認を待つ間に出力をストリーミングするには、`Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を消費してから、上記と同じ `result.to_state()` と再開手順に従います。

## リポジトリのパターンとコード例

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで読み出し、その後 `Runner.run_streamed(agent, state)` で再開する前に保留中のツール呼び出しを承認する方法を示します。
- **カスタム拒否テキスト**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py` は、承認が拒否された場合に、実行レベルの `tool_error_formatter` と呼び出しごとの `rejection_message` オーバーライドを組み合わせる方法を示します。
- **ツールとしてのエージェントの承認**: `Agent.as_tool(..., needs_approval=...)` は、委譲されたエージェントタスクにレビューが必要な場合に同じ中断フローを適用します。ネストされた中断も外側の実行に提示されるため、ネストされたエージェントではなく元のトップレベルのエージェントを再開してください。
- **ローカル shell と apply_patch ツール**: `ShellTool` と `ApplyPatchTool` も `needs_approval` に対応しています。将来の呼び出しに備えて判定をキャッシュするには、`state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使用します。自動判定には `on_approval` を指定します (`examples/tools/shell.py` を参照)。手動判定には中断を処理します (`examples/tools/shell_human_in_the_loop.py` を参照)。ホスト型 shell 環境は `needs_approval` または `on_approval` に対応していません。[ツールガイド](tools.md) を参照してください。
- **ローカル MCP サーバー**: MCP ツール呼び出しを制御するには、`MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` の `require_approval` を使用します (`examples/mcp/get_all_mcp_tools_example/main.py` と `examples/mcp/tool_filter_example/main.py` を参照)。
- **ホスト型 MCP サーバー**: HITL を強制するには、`HostedMCPTool` で `require_approval` を `"always"` に設定し、必要に応じて自動承認または拒否のために `on_approval_request` を指定します (`examples/hosted_mcp/human_in_the_loop.py` と `examples/hosted_mcp/on_approval.py` を参照)。信頼済みサーバーには `"never"` を使用します (`examples/hosted_mcp/simple.py`)。
- **セッションとメモリ**: セッションを `Runner.run` に渡すと、承認と会話履歴が複数ターンにわたって保持されます。SQLite と OpenAI Conversations のセッション版は、`examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: Realtime デモでは、`RealtimeSession` の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開しています (サーバー側ハンドラーについては `examples/realtime/app/server.py`、API サーフェスについては [Realtime ガイド](realtime/guide.md#tool-approvals) を参照)。

## 長時間にわたる承認

`RunState` は耐久性を持つように設計されています。`state.to_json()` または `state.to_string()` を使用して保留中の作業をデータベースまたはキューに保存し、後で `RunState.from_json(...)` または `RunState.from_string(...)` で再作成します。

便利なシリアライズオプション:

- `context_serializer`: 非マッピングのコンテキストオブジェクトのシリアライズ方法をカスタマイズします。
- `context_deserializer`: `RunState.from_json(...)` または `RunState.from_string(...)` で状態を読み込むときに、非マッピングのコンテキストオブジェクトを再構築します。
- `strict_context=True`: コンテキストがすでにマッピングであるか、適切なシリアライザー / デシリアライザーを指定している場合を除き、シリアライズまたはデシリアライズを失敗させます。
- `context_override`: 状態を読み込むときに、シリアライズされたコンテキストを置き換えます。これは、元のコンテキストオブジェクトを復元したくない場合に便利ですが、すでにシリアライズ済みのペイロードからそのコンテキストを削除するわけではありません。
- `include_tracing_api_key=True`: 再開された作業で同じ認証情報を使ってトレースのエクスポートを継続する必要がある場合、シリアライズされたトレースペイロードにトレーシング API キーを含めます。

シリアライズされた実行状態には、アプリのコンテキストに加えて、承認、使用量、シリアライズ済みの `tool_input`、ネストされた agent-as-tool の再開情報、トレースメタデータ、サーバー管理の会話設定など、SDK 管理のランタイムメタデータが含まれます。シリアライズされた状態を保存または送信する予定がある場合は、`RunContextWrapper.context` を永続化データとして扱い、状態と一緒に移動させる意図がある場合を除き、そこにシークレットを置かないでください。

## 保留中タスクのバージョニング

承認がしばらく保留される可能性がある場合は、エージェント定義または SDK のバージョンマーカーを、シリアライズされた状態と一緒に保存してください。これにより、モデル、プロンプト、ツール定義が変更された場合の非互換性を避けるために、対応するコードパスへデシリアライズ処理を振り分けられます。