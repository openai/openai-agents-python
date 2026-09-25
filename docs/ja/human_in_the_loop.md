---
search:
  exclude: true
---
# ヒューマンインザループ

ヒューマンインザループ（HITL）フローを使用すると、機密性の高いツール呼び出しを人が承認または拒否するまで、エージェントの実行を一時停止できます。ツールは承認が必要なタイミングを宣言し、実行結果では保留中の承認が割り込みとして提示されます。また、`RunState` を使用すると、一時停止した実行をシリアライズし、判断後に再開できます。

この承認の適用範囲は実行全体であり、現在のトップレベルエージェントに限定されません。同じパターンは、ツールが現在のエージェントに属する場合、ハンドオフによって到達したエージェントに属する場合、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行に属する場合にも適用されます。ネストされた `Agent.as_tool()` の場合でも、割り込みは外側の実行に提示されるため、外側の `RunState` で承認または拒否し、元のトップレベル実行を再開します。

`Agent.as_tool()` では、承認は 2 つの異なるレイヤーで発生する可能性があります。エージェントツール自体が `Agent.as_tool(..., needs_approval=...)` による承認を必要とする場合と、ネストされた実行の開始後に、そのエージェント内のツールが独自の承認を要求する場合です。どちらも、同じ外側の実行の割り込みフローを通じて処理されます。

このページでは、`interruptions` を使用する手動承認フローを中心に説明します。アプリがコード内で判断できる場合、一部のツールタイプではプログラムによる承認コールバックもサポートされているため、実行を一時停止せずに続行できます。

## 承認が必要なツールの指定 {#marking-tools-that-need-approval}

常に承認を必要とするには `needs_approval` を `True` に設定します。または、呼び出しごとに判断する非同期関数を指定します。この呼び出し可能オブジェクトは、実行コンテキスト、解析済みのツールパラメーター、ツール呼び出し ID を受け取ります。

SDK が引数を安全に検査できない場合、呼び出し可能な承認ルールは安全側に倒して拒否します。引数が欠落している、空である、空白のみを含む、不正な JSON である、有効な JSON であってもオブジェクトではない（たとえば、`null` やリスト）、または `NaN`、`Infinity`、`-Infinity` などの非標準定数を含む場合、その呼び出し可能オブジェクトは呼び出されず、ツール呼び出しには手動承認が必要になります。この動作は、Runner と Realtime のツール呼び出しで同じです。

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

`needs_approval` は、[`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で利用できます。ローカル MCP サーバーも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を通じて承認をサポートします。ホスト型 MCP サーバーでは、`tool_config={"require_approval": "always"}` とオプションの `on_approval_request` コールバックを指定した [`HostedMCPTool`][agents.tool.HostedMCPTool] を通じて承認をサポートします。Shell ツールと apply_patch ツールは、割り込みを提示せずに自動承認または自動拒否する場合、`on_approval` コールバックを受け取ります。

## 承認フローの仕組み {#how-the-approval-flow-works}

1. モデルがツール呼び出しを生成すると、Runner はその承認ルール（`needs_approval`、`require_approval`、またはホスト型 MCP の同等機能）を評価します。
2. そのツール呼び出しに対する承認判断がすでに [`RunContextWrapper`][agents.run_context.RunContextWrapper] に保存されている場合、Runner は確認を求めずに続行します。呼び出しごとの承認は特定の呼び出し ID に限定されます。実行の残りの期間中、同じツール ID に対する今後の呼び出しにも同じ判断を保持するには、`always_approve=True` または `always_reject=True` を渡します。
3. 承認ルールで承認が必要とされ、そのツール呼び出しに対する判断が保存されていない場合、実行は一時停止し、`RunResult.interruptions`（または `RunResultStreaming.interruptions`）に、`agent.name`、`tool_name`、`arguments` などの詳細を持つ [`ToolApprovalItem`][agents.items.ToolApprovalItem] エントリが含まれます。これには、ハンドオフ後またはネストされた `Agent.as_tool()` の実行内で発生した承認も含まれます。
4. `result.to_state()` を使用して実行結果を `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出してから、`Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。ここで、`agent` はその実行の元のトップレベルエージェントです。
5. 再開された実行は中断した箇所から続行し、新たな承認が必要になった場合は、このフローに再び入ります。

`always_approve=True` または `always_reject=True` で作成された固定判断は実行状態に保存されるため、後で同じ一時停止済み実行を再開する際も、`state.to_string()` / `RunState.from_string(...)` および `state.to_json()` / `RunState.from_json(...)` を経ても保持されます。

[`HostedMCPTool`][agents.tool.HostedMCPTool] からの承認リクエストでは、Agents SDK は `server_label` とツール名の組み合わせによって、固定されたツール判断を識別します。あるホスト型 MCP サーバー上の `lookup_account` に対する常時承認の判断によって、別のサーバー上にある同名のツールが承認されることはありません。Agents SDK が常時承認または常時拒否の判断を保持するのは、ホスト型 MCP の承認リクエストに空でない両方の ID フィールドが含まれる場合のみです。

保留中のすべての承認を同じ処理内で解決する必要はありません。`interruptions` には、通常の関数ツール、ホスト型 MCP の承認、ネストされた `Agent.as_tool()` の承認が混在する場合があります。一部の項目のみを承認または拒否して再実行すると、解決済みの呼び出しは続行できますが、未解決の呼び出しは `interruptions` に残り、実行は再び一時停止します。

## カスタム拒否メッセージ {#custom-rejection-messages}

デフォルトでは、拒否されたツール呼び出しに対して、SDK の標準的な拒否テキストが実行に返されます。このメッセージは 2 つのレイヤーでカスタマイズできます。

-   実行全体のフォールバック: [`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter] を設定すると、実行全体にわたる承認拒否について、モデルに表示されるデフォルトメッセージを制御できます。
-   呼び出しごとのオーバーライド: 特定の拒否済みツール呼び出しに別のメッセージを提示する場合は、`state.reject(...)` に `rejection_message=...` を渡します。

両方が指定されている場合は、呼び出しごとの `rejection_message` が実行全体のフォーマッターより優先されます。

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

## 自動承認判断 {#automatic-approval-decisions}

手動の `interruptions` は最も汎用的なパターンですが、唯一の方法ではありません。

-   ローカルの [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] は、`on_approval` を使用してコード内で即座に承認または拒否できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] は、`on_approval_request` とともに `tool_config={"require_approval": "always"}` を使用して、同様のプログラムによる判断を行えます。
-   通常の [`function_tool`][agents.tool.function_tool] ツールと [`Agent.as_tool()`][agents.agent.Agent.as_tool] は、このページで説明する手動割り込みフローを使用します。

これらのコールバックが判断を返すと、実行は人の応答を待って一時停止することなく続行します。Realtime および音声セッション API については、[Realtime ガイド](realtime/guide.md)の承認フローを参照してください。

## ストリーミングとセッション {#streaming-and-sessions}

同じ割り込みフローは、ストリーミング実行でも機能します。ストリーミング実行が一時停止した後、イテレーターが終了するまで [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] の取得を続け、[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] を確認して解決します。再開後の出力でもストリーミングを継続する場合は、[`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] で再開します。このパターンのストリーミング版については、[ストリーミング](streaming.md)を参照してください。

セッションも使用している場合は、`RunState` から再開するときに同じセッションインスタンスを引き続き渡すか、同じセッション ID とバッキングストア用に構成された別のセッションオブジェクトを渡します。再開されたターンは、保存済みの同じ会話履歴に追加されます。セッションのライフサイクルの詳細については、[セッション](sessions/index.md)を参照してください。

## 例: 一時停止、承認、再開 {#example-pause-approve-resume}

以下のスニペットは JavaScript の HITL ガイドと同じ流れです。ツールに承認が必要な場合に一時停止し、状態をディスクに保存して再読み込みし、判断を収集した後に再開します。

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

この例では、`prompt_approval` は `input()` を使用し、`run_in_executor(...)` で実行されるため、同期処理です。承認元がすでに非同期である場合（たとえば、HTTP リクエストや非同期データベースクエリ）は、`async def` 関数を使用し、直接 `await` できます。

承認のために一時停止する可能性がある実行でストリーミングを使用するには、`Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を取得してから、上記と同じ `result.to_state()` および再開手順に従います。

## リポジトリのパターンとコード例 {#repository-patterns-and-examples}

- **ストリーミング承認**: `examples/agent_patterns/human_in_the_loop_stream.py` は、`stream_events()` を最後まで取得してから、保留中のツール呼び出しを承認し、`Runner.run_streamed(agent, state)` で再開する方法を示します。
- **カスタム拒否テキスト**: `examples/agent_patterns/human_in_the_loop_custom_rejection.py` は、承認が拒否された場合に、実行レベルの `tool_error_formatter` と呼び出しごとの `rejection_message` オーバーライドを組み合わせる方法を示します。
- **エージェントをツールとして使用する場合の承認**: `Agent.as_tool(..., needs_approval=...)` は、委任されたエージェントタスクにレビューが必要な場合に、同じ割り込みフローを適用します。ネストされた割り込みも外側の実行に提示されるため、ネストされたエージェントではなく、元のトップレベルエージェントを再開します。
- **ローカルの Shell ツールと apply_patch ツール**: `ShellTool` と `ApplyPatchTool` も `needs_approval` をサポートします。実行の残りの期間中、そのツールに対する今後の呼び出しで判断をキャッシュするには、`state.approve(interruption, always_approve=True)` または `state.reject(..., always_reject=True)` を使用します。コールバック内で承認を解決するには、`on_approval` を指定します。`examples/tools/shell.py` は、デフォルトでオペレーターに確認を求める対話型コールバックを示します。すべての Shell 呼び出しを拒否する自動ポリシーでは、`ShellTool` に `needs_approval=True` と `on_approval=lambda _context, _item: {"approve": False, "reason": "Disabled by policy"}` を設定します。代わりにアプリケーションが一時停止した実行をレビューできるようにするには、割り込みを処理します（`examples/tools/shell_human_in_the_loop.py` を参照）。ホスト型 Shell 環境は、`needs_approval` または `on_approval` をサポートしていません。[ツールガイド](tools.md)を参照してください。
- **ローカル MCP サーバー**: MCP ツール呼び出しを制御するには、`MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` で `require_approval` を使用します（`examples/mcp/get_all_mcp_tools_example/main.py` と `examples/mcp/tool_filter_example/main.py` を参照）。
- **ホスト型 MCP サーバー**: HITL を必須にするには、`HostedMCPTool` に `tool_config={"require_approval": "always"}` を設定します。必要に応じて、自動承認または自動拒否用の `on_approval_request` を指定できます（`examples/hosted_mcp/human_in_the_loop.py` と `examples/hosted_mcp/on_approval.py` を参照）。信頼済みサーバーには `"never"` を使用します（`examples/hosted_mcp/simple.py`）。
- **セッションとメモリ**: 承認と会話履歴を複数のターンにわたって保持するには、`Runner.run` にセッションを渡します。SQLite と OpenAI Conversations のセッションバリアントは、`examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**: Realtime デモでは、`RealtimeSession` 上の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開しています（サーバー側ハンドラーについては `examples/realtime/app/server.py`、API サーフェスについては [Realtime ガイド](realtime/guide.md#tool-approvals)を参照）。

## 長時間にわたる承認 {#long-running-approvals}

`RunState` は、永続性を持つように設計されています。保留中の処理をデータベースまたはキューに保存するには `state.to_json()` または `state.to_string()` を使用し、後で再作成するには `RunState.from_json(...)` または `RunState.from_string(...)` を使用します。

### サーバー上での承認状態の保持 {#keep-approval-state-on-the-server}

シリアライズされた `RunState` には、承認判断、保留中のツール呼び出し、ツール引数などの実行状態が含まれます。SDK はこの状態を復元しますが、`RunState.from_json()` と `RunState.from_string()` は、スナップショットやそれを送信した人物を認証しません。信頼できるストレージに保存されたスナップショット、またはアプリケーションが完全性と所有権をすべて検証したスナップショットのみをデシリアライズしてください。スキーマチェックやツール呼び出しのフィンガープリントは、スナップショットを認証するものではありません。

ブラウザーまたはモバイルの承認インターフェースでは、完全なスナップショットをアプリケーションが管理するサーバーストレージに保持してください。レビュアーには、閲覧が許可されたツールの詳細と、保留中の判断に対応する不透明な ID のみを送信します。ツール名と引数は信頼できない表示コンテンツとして扱い、HTML としてレンダリングする際にはエスケープしてください。

判断を受信した場合、サーバーは次の処理を行う必要があります。

1. アプリケーションのセッションまたは認証ミドルウェアを使用して、レビュアーを認証します。承認リクエスト本文からレビュアーの ID を取得しないでください。
2. 保存された実行と選択された保留中の呼び出しに対して、そのレビュアーが操作する権限を持つことを確認します。実行 ID または判断 ID を保持しているだけでは、認可されたことにはなりません。
3. 送信された判断 ID と真偽値の判断を、サーバーに保存されている保留中のリクエストと照合して検証します。サーバーが所有するスナップショットを読み込み、`state.get_interruptions()` で保留中の項目を取得してください。クライアントから代替のツール呼び出し、引数、承認レコード、またはシリアライズ済み状態を受け入れないでください。
4. サーバーが所有するこれらの項目に `state.approve(...)` または `state.reject(...)` を適用してから、実行を再開します。同時送信または再送されたリクエストによって同じスナップショットが 2 回再開されないように、保留中の各リクエストの消費をストレージと連携させてください。共有ストレージでは、再開後の実行を開始する前に、所有者を確認するアトミックな遷移を使用します。

[サーバー側承認のコード例](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/human_in_the_loop_server.py)では、CLI クライアントのシミュレーションと、単一プロセス内の 1 つのイベントループに限定されたストアを使用して、このパターンを示しています。このコード例では、バッチ内の保留中の呼び出しごとに 1 つの判断が必要です。また、デシリアライズと再開後の実行より前にリクエストを消費するため、失敗やキャンセルが発生した場合もリクエストは消費されます。本番アプリケーションでは、再試行前にツールの副作用との整合性を取るための認証、リクエスト保護、ストレージ保持、復旧機能を用意する必要があります。このコード例は、デプロイ可能な HTTP サービスではありません。

`context` を `context_override` に置き換えたり、`strict_context=True` を設定したり、シリアライズ済みの承認レコードのみを削除したりしても、信頼できないスナップショットが安全になるわけではありません。他のフィールドも再開後の実行を制御します。アプリケーションが完全なスナップショットをクライアント経由で転送する場合は、デシリアライズの前に、その完全性を検証し、認可されたユーザーと実行に関連付け、リプレイを防止する必要があります。このような検証によってスナップショットが暗号化されたり、その内容がクライアントから隠されたりすることはありません。

### シリアライズオプション {#serialization-options}

有用なシリアライズオプションは次のとおりです。

-   `context_serializer`: マッピングではないコンテキストオブジェクトのシリアライズ方法をカスタマイズします。
-   `context_deserializer`: `RunState.from_json(...)` または `RunState.from_string(...)` で状態を読み込む際に、マッピングではないコンテキストオブジェクトを再構築します。
- `strict_context=True`: コンテキストがすでにマッピングであるか、`context_serializer` が指定されている場合を除き、シリアライズを失敗させます。また、コンテキストがすでにマッピングであるか、`context_deserializer` が指定されている場合を除き、デシリアライズを失敗させます。
- `context_override`: 状態の読み込み時に、シリアライズされたコンテキストを置き換えます。元のコンテキストオブジェクトを復元したくない場合に便利ですが、すでにシリアライズ済みのペイロードからそのコンテキストを削除するものではありません。
- `include_tracing_api_key=True`: 再開後の処理でも同じ認証情報を使用してトレースを引き続きエクスポートする必要がある場合、シリアライズされたトレースペイロードにトレーシング API キーを含めます。

シリアライズされた実行状態には、アプリのコンテキストに加えて、承認、使用量、シリアライズされた `tool_input`、ネストされたエージェントツールの再開、トレースメタデータ、サーバー管理の会話設定など、SDK が管理するランタイムメタデータが含まれます。シリアライズされた状態を保存または転送する予定がある場合は、`RunContextWrapper.context` を永続化データとして扱い、意図的に状態とともに移動させる場合を除き、そこにシークレットを配置しないでください。

## 保留中タスクのバージョン管理 {#versioning-pending-tasks}

承認が長期間保留される可能性がある場合は、シリアライズされた状態とともに、エージェント定義または SDK のバージョンマーカーを保存してください。これにより、モデル、プロンプト、またはツール定義が変更された場合でも、対応するコードパスにデシリアライズをルーティングし、非互換性を回避できます。