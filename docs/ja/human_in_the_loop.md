---
search:
  exclude: true
---
# ヒューマンインザループ

ヒューマンインザループ（HITL）フローを使用すると、機密性の高いツール呼び出しを人が承認または拒否するまで、エージェントの実行を一時停止できます。ツールは承認が必要なタイミングを宣言し、実行結果は保留中の承認を中断として公開します。また、`RunState` を使用すると、一時停止した実行をシリアライズし、判断後に再開できます。

この承認の仕組みは実行全体に適用され、現在のトップレベルエージェントだけに限定されません。同じパターンは、ツールが現在のエージェント、ハンドオフ先のエージェント、またはネストされた [`Agent.as_tool()`][agents.agent.Agent.as_tool] の実行に属する場合にも適用されます。ネストされた `Agent.as_tool()` の場合でも、中断は外側の実行に公開されるため、外側の `RunState` で承認または拒否し、元のトップレベル実行を再開します。

`Agent.as_tool()` では、承認が 2 つの異なるレイヤーで発生する可能性があります。エージェントツール自体が `Agent.as_tool(..., needs_approval=...)` による承認を必要とする場合と、ネストされた実行の開始後に、ネストされたエージェント内のツールが独自の承認を要求する場合です。どちらも、同じ外側の実行の中断フローで処理されます。

このページでは、`interruptions` を使用する手動承認フローに焦点を当てます。アプリがコード内で判断できる場合、一部のツールタイプではプログラムによる承認コールバックもサポートされており、実行を一時停止せずに続行できます。

## 承認が必要なツールの指定 {#marking-tools-that-need-approval}

常に承認を必要とするには `needs_approval` を `True` に設定し、呼び出しごとに判断するには非同期関数を指定します。この callable は、実行コンテキスト、解析済みのツールパラメーター、ツール呼び出し ID を受け取ります。

callable の承認ルールでは、SDK が引数を安全に検査できない場合、デフォルトで拒否されます。引数が欠落している、空である、空白文字のみを含む、不正な JSON である、有効な JSON ではあるもののオブジェクトではない（たとえば、`null` やリスト）、または `NaN`、`Infinity`、`-Infinity` などの非標準定数を含む場合、callable は呼び出されず、その呼び出しには手動承認が必要になります。この動作は Runner と Realtime のツール呼び出しで同じです。

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

`needs_approval` は、[`function_tool`][agents.tool.function_tool]、[`Agent.as_tool`][agents.agent.Agent.as_tool]、[`ShellTool`][agents.tool.ShellTool]、[`ApplyPatchTool`][agents.tool.ApplyPatchTool] で利用できます。ローカル MCP サーバーでも、[`MCPServerStdio`][agents.mcp.server.MCPServerStdio]、[`MCPServerSse`][agents.mcp.server.MCPServerSse]、[`MCPServerStreamableHttp`][agents.mcp.server.MCPServerStreamableHttp] の `require_approval` を通じて承認がサポートされます。ホスト型 MCP サーバーでは、`tool_config={"require_approval": "always"}` とオプションの `on_approval_request` コールバックを指定した [`HostedMCPTool`][agents.tool.HostedMCPTool] を通じて承認がサポートされます。中断を公開せずに自動承認または自動拒否したい場合、シェルツールと apply_patch ツールでは `on_approval` コールバックを使用できます。

ローカルの `ShellTool` と `ApplyPatchTool` では、承認はオプトインです。`needs_approval` のデフォルトは `False` です。`on_approval` コールバックを指定するだけでは、承認は有効になりません。`needs_approval=True` または callable の承認ポリシーも設定してください。SDK は、承認が必要であり、既存の承認判断がない呼び出しに対してのみコールバックを呼び出します。実行の責任範囲と構成例については、[ローカルシェルとファイル編集の承認](tools.md#approval-for-local-shell-and-file-edits)を参照してください。

`ComputerTool` では、Responses API が返す安全性チェックを受け入れるか拒否するかを `on_safety_check` で指定します。このコールバックは、保留中のチェックがあるコンピュータ呼び出しに対してのみ実行され、このページで説明する一時停止と再開の承認フローは使用しません。詳細と例については、[コンピュータの安全性チェック](tools.md#pending-computer-safety-checks)を参照してください。

## 承認フローの仕組み {#how-the-approval-flow-works}

1. モデルがツール呼び出しを生成すると、Runner はその承認ルール（`needs_approval`、`require_approval`、またはホスト型 MCP の同等機能）を評価します。
2. そのツール呼び出しに対する承認判断がすでに [`RunContextWrapper`][agents.run_context.RunContextWrapper] に保存されている場合、Runner は確認を求めずに続行します。呼び出しごとの承認は特定の呼び出し ID に限定されます。同じツール ID に対する今後の呼び出しでも同じ判断を維持するには、`always_approve=True` または `always_reject=True` を渡します。
3. 承認ルールで承認が必要とされ、そのツール呼び出しに対する判断が保存されていない場合、実行が一時停止し、`RunResult.interruptions`（または `RunResultStreaming.interruptions`）に、`agent.name`、`tool_name`、`arguments` などの詳細を含む [`ToolApprovalItem`][agents.items.ToolApprovalItem] エントリが格納されます。これには、ハンドオフ後またはネストされた `Agent.as_tool()` の実行内で要求された承認も含まれます。
4. `result.to_state()` を使用して実行結果を `RunState` に変換し、`state.approve(...)` または `state.reject(...)` を呼び出してから、`Runner.run(agent, state)` または `Runner.run_streamed(agent, state)` で再開します。ここで、`agent` はその実行の元のトップレベルエージェントです。
5. 再開された実行は中断した箇所から続行され、新たな承認が必要になった場合は、このフローに再び入ります。

`always_approve=True` または `always_reject=True` で作成された永続的な判断は、承認を保持する `RunContextWrapper` が再利用されている間、個別の `Runner.run(...)` または `Runner.run_streamed(...)` の呼び出しをまたいでも有効です。プレーンなアプリケーションコンテキストオブジェクトを新しい実行に渡すと、新しいラッパーが作成されます。そのオブジェクトを再利用するだけでは、承認は引き継がれません。`RunState` にも永続的な判断が保存されるため、後で一時停止した実行を再開する際に、`state.to_string()` / `RunState.from_string(...)` および `state.to_json()` / `RunState.from_json(...)` を行っても判断は維持されます。

関数ツールの場合、永続的な判断のスコープは承認を要求したエージェントに限定されます。別のエージェントの関数ツールは、ツール名が同じというだけでは判断を継承しません。

!!! warning "永続的なシェルおよびファイル編集の判断はエージェント単位ではありません"

    ローカルの [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] では、永続的な判断は所有するエージェント、executor、editor ではなく、ツールのタイプと名前に基づいて照合されます。エージェントが承認状態を共有している場合、ハンドオフ後に executor または editor の権限が異なっていても、`always_approve=True` の判断が同じタイプと名前を持つ別のエージェントのツールに適用される可能性があります。デフォルト名はそれぞれ `shell` と `apply_patch` です。その永続的な承認が適用される場合、SDK は再度確認を求めず、移行先ツールの `needs_approval` ポリシーまたは `on_approval` コールバックも再評価しません。`always_reject=True` の判断も同様に共有されます。

    executor または editor の権限が異なる場合は、呼び出しごとの判断を使用します。`state.approve(interruption)` または `state.reject(interruption)` を呼び出し、`always_approve` と `always_reject` はデフォルト値の `False` のままにします。新しいシェルまたはファイル編集の呼び出しごとに人による判断を必須にするには、`needs_approval=True` も設定し、`on_approval` は指定しないでください。呼び出しごとの判断によって、既存の永続的な判断が消去されることはありません。

    異なる実行権限で新しい実行を開始する場合は、承認を保持しているラッパーを再利用せず、新しい `RunContextWrapper` を使用してください。既存の `RunState` を再開すると、新しいコンテキストオブジェクトまたはラッパーを渡した場合でも、その承認は維持されます。コンテキストのオーバーライドでは承認状態はリセットされません。

[`HostedMCPTool`][agents.tool.HostedMCPTool] からの承認リクエストでは、Agents SDK は `server_label` とツール名の組み合わせによって、永続的なツール判断を識別します。あるホスト型 MCP サーバー上の `lookup_account` に対する常時承認の判断によって、別のサーバー上の同名ツールが承認されることはありません。Agents SDK が常時承認または常時拒否の判断を永続化するのは、ホスト型 MCP の承認リクエストに空でない両方の ID フィールドが含まれている場合のみです。

保留中のすべての承認を同じ処理内で解決する必要はありません。`interruptions` には、通常の関数ツール、ホスト型 MCP の承認、ネストされた `Agent.as_tool()` の承認が混在する場合があります。一部の項目だけを承認または拒否した後に再実行すると、解決済みの呼び出しは続行できますが、未解決の呼び出しは `interruptions` に残り、実行が再び一時停止します。

## カスタム拒否メッセージ {#custom-rejection-messages}

デフォルトでは、拒否されたツール呼び出しについて、SDK の標準的な拒否テキストが実行に返されます。このメッセージは、次の 2 つのレイヤーでカスタマイズできます。

-   実行全体のフォールバック：[`RunConfig.tool_error_formatter`][agents.run.RunConfig.tool_error_formatter] を設定すると、実行全体にわたる承認拒否について、モデルに表示されるデフォルトメッセージを制御できます。
-   呼び出しごとのオーバーライド：特定の拒否されたツール呼び出しで別のメッセージを表示したい場合は、`state.reject(...)` に `rejection_message=...` を渡します。

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

両方のレイヤーを組み合わせた完全な例については、[`examples/agent_patterns/human_in_the_loop_custom_rejection.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/human_in_the_loop_custom_rejection.py)を参照してください。

## 自動承認の判断 {#automatic-approval-decisions}

手動の `interruptions` は最も汎用的なパターンですが、唯一の方法ではありません。

-   ローカルの [`ShellTool`][agents.tool.ShellTool] と [`ApplyPatchTool`][agents.tool.ApplyPatchTool] では、`on_approval` を使用して、コード内ですぐに承認または拒否できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] では、`tool_config={"require_approval": "always"}` と `on_approval_request` を組み合わせて、同様のプログラムによる判断を行えます。
-   通常の [`function_tool`][agents.tool.function_tool] ツールと [`Agent.as_tool()`][agents.agent.Agent.as_tool] では、このページの手動中断フローを使用します。

これらのコールバックが判断を返すと、人の応答を待つために一時停止することなく、実行が続行されます。Realtime および音声セッション API については、[Realtime ガイド](realtime/guide.md)の承認フローを参照してください。

## ストリーミングとセッション {#streaming-and-sessions}

同じ中断フローは、ストリーミング実行でも機能します。ストリーミング実行が一時停止した後、イテレーターが終了するまで [`RunResultStreaming.stream_events()`][agents.result.RunResultStreaming.stream_events] を消費し続け、[`RunResultStreaming.interruptions`][agents.result.RunResultStreaming.interruptions] を確認して解決します。再開後の出力でもストリーミングを継続したい場合は、[`Runner.run_streamed(...)`][agents.run.Runner.run_streamed] で再開します。このパターンのストリーミング版については、[ストリーミング](streaming.md)を参照してください。

セッションも使用している場合は、`RunState` から再開するときに同じセッションインスタンスを引き続き渡すか、同じセッション ID とバッキングストア向けに構成された別のセッションオブジェクトを渡します。再開されたターンは、同じ保存済み会話履歴に追加されます。セッションのライフサイクルの詳細については、[セッション](sessions/index.md)を参照してください。

## 例：一時停止、承認、再開 {#example-pause-approve-resume}

以下のスニペットは JavaScript の HITL ガイドに対応しています。ツールに承認が必要になると一時停止し、状態をディスクに永続化し、再読み込みして、判断の収集後に再開します。

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

この例では、`prompt_approval` は `input()` を使用し、`run_in_executor(...)` で実行されるため同期的です。承認元がすでに非同期である場合（たとえば、HTTP リクエストや非同期データベースクエリ）、`async def` 関数を使用し、直接 `await` できます。

承認のために一時停止する可能性がある実行でストリーミングを使用するには、`Runner.run_streamed` を呼び出し、完了するまで `result.stream_events()` を消費してから、上記と同じ `result.to_state()` および再開の手順に従います。

## リポジトリのパターンとコード例 {#repository-patterns-and-examples}

- **ストリーミング承認**：`examples/agent_patterns/human_in_the_loop_stream.py` では、`stream_events()` を最後まで処理し、保留中のツール呼び出しを承認してから `Runner.run_streamed(agent, state)` で再開する方法を示します。
- **カスタム拒否テキスト**：`examples/agent_patterns/human_in_the_loop_custom_rejection.py` では、承認が拒否されたときに、実行レベルの `tool_error_formatter` と呼び出しごとの `rejection_message` オーバーライドを組み合わせる方法を示します。
- **ツールとしてのエージェントの承認**：`Agent.as_tool(..., needs_approval=...)` では、委任されたエージェントタスクにレビューが必要な場合に、同じ中断フローを適用します。ネストされた中断も外側の実行に公開されるため、ネストされたエージェントではなく、元のトップレベルエージェントを再開します。
- **ローカルシェルおよび apply_patch ツール**：`ShellTool` と `ApplyPatchTool` でも `needs_approval` がサポートされています。呼び出しごとの判断には、`state.approve(interruption)` または `state.reject(interruption)` を使用します。`always_approve=True` と `always_reject=True` のオプションは、判断を `RunContextWrapper` と `RunState` に永続化します。これらのオプションを使用する前に、[承認スコープに関する警告](#how-the-approval-flow-works)を確認してください。コールバック内で承認を解決するには、`on_approval` を指定します。`examples/tools/shell.py` は、デフォルトで Operator に確認を求める対話型コールバックの例です。既存の承認判断がないシェル呼び出しを自動的に拒否するには、`ShellTool` に `needs_approval=True` と `on_approval=lambda _context, _item: {"approve": False, "reason": "Disabled by policy"}` を設定します。代わりに、アプリケーションで一時停止した実行をレビューするには、中断を処理します（`examples/tools/shell_human_in_the_loop.py` を参照）。ホスト型シェル環境では、`needs_approval` または `on_approval` はサポートされていません。[ツールガイド](tools.md)を参照してください。
- **ローカル MCP サーバー**：MCP ツール呼び出しを制御するには、`MCPServerStdio` / `MCPServerSse` / `MCPServerStreamableHttp` で `require_approval` を使用します（`examples/mcp/get_all_mcp_tools_example/main.py` と `examples/mcp/tool_filter_example/main.py` を参照）。
- **ホスト型 MCP サーバー**：HITL を強制するには、`HostedMCPTool` に `tool_config={"require_approval": "always"}` を設定します。必要に応じて、自動承認または自動拒否を行う `on_approval_request` を指定できます（`examples/hosted_mcp/human_in_the_loop.py` と `examples/hosted_mcp/on_approval.py` を参照）。信頼できるサーバーには `"never"` を使用します（`examples/hosted_mcp/simple.py`）。
- **セッションとメモリ**：ターン間で会話履歴を維持するには `Runner.run` にセッションを渡し、一時停止した実行を再開するときに承認判断を維持するには `RunState` を使用します。SQLite および OpenAI Conversations のセッションバリアントは、`examples/memory/memory_session_hitl_example.py` と `examples/memory/openai_session_hitl_example.py` にあります。
- **Realtime エージェント**：Realtime のデモでは、`RealtimeSession` 上の `approve_tool_call` / `reject_tool_call` を介してツール呼び出しを承認または拒否する WebSocket メッセージを公開しています（サーバー側のハンドラーについては `examples/realtime/app/server.py`、API の機能については [Realtime ガイド](realtime/guide.md#tool-approvals)を参照）。

## 長時間にわたる承認 {#long-running-approvals}

`RunState` は永続性を考慮して設計されています。`state.to_json()` または `state.to_string()` を使用して、保留中の作業をデータベースまたはキューに保存し、後から `RunState.from_json(...)` または `RunState.from_string(...)` で再作成します。

### サーバー上での承認状態の保持 {#keep-approval-state-on-the-server}

シリアライズされた `RunState` には、承認判断、保留中のツール呼び出し、ツール引数などの実行状態が含まれます。SDK はこの状態を復元しますが、`RunState.from_json()` と `RunState.from_string()` はスナップショットや、それを送信した人物を認証しません。信頼できるストレージから取得したスナップショット、またはアプリケーションが完全性と所有権をすべて検証したスナップショットだけをデシリアライズしてください。スキーマチェックやツール呼び出しのフィンガープリントだけでは、スナップショットを認証できません。

ブラウザーまたはモバイルの承認インターフェースでは、完全なスナップショットをアプリケーションが管理するサーバーストレージに保持してください。レビュアーには、そのレビュアーに閲覧権限があるツールの詳細と、保留中の判断を表す不透明な識別子だけを送信します。ツール名と引数は信頼できない表示コンテンツとして扱い、HTML としてレンダリングする際にはエスケープしてください。

判断を受信したら、サーバーは次の処理を行う必要があります。

1. アプリケーションのセッションまたは認証ミドルウェアを使用して、レビュアーを認証します。承認リクエストの本文からレビュアーの ID を取得しないでください。
2. 保存された実行と選択された保留中の呼び出しに対して操作する権限を、そのレビュアーに付与します。実行 ID または判断 ID を所有しているだけでは、権限があることにはなりません。
3. 送信された判断 ID とブール値の判断を、サーバーに保存された保留中のリクエストと照合して検証します。サーバーが所有するスナップショットを読み込み、`state.get_interruptions()` で保留中の項目を取得してください。置換されたツール呼び出し、引数、承認レコード、またはシリアライズされた状態をクライアントから受け入れないでください。
4. サーバーが所有する項目に `state.approve(...)` または `state.reject(...)` を適用してから、実行を再開します。同時送信または再送信によって同じスナップショットが 2 回再開されないように、保留中の各リクエストの消費をストレージと連携させてください。共有ストレージでは、再開された実行を開始する前に、所有者を確認するアトミックな遷移を使用します。

[サーバー側の承認例](https://github.com/openai/openai-agents-python/blob/main/examples/agent_patterns/human_in_the_loop_server.py)では、CLI クライアントのシミュレーションと、単一プロセス内の 1 つのイベントループに限定されたストアを使用して、このパターンを示しています。この例では、バッチ内の保留中の呼び出しごとに 1 つの判断が必要です。この例は、デシリアライズと再開された実行の前にリクエストを消費するため、失敗やキャンセルが発生した場合でもリクエストは消費されます。本番アプリケーションでは、認証、リクエスト保護、ストレージの保持、および再試行前にツールの副作用を整合させるリカバリー機能を用意する必要があります。この例は、デプロイ可能な HTTP サービスではありません。

`context` を `context_override` に置き換えたり、`strict_context=True` を設定したり、シリアライズされた承認レコードだけを削除したりしても、信頼できないスナップショットが安全になるわけではありません。他のフィールドも再開後の実行を制御します。アプリケーションが完全なスナップショットをクライアント経由で転送する場合は、デシリアライズする前に、その完全性を検証し、承認されたユーザーと実行に関連付け、リプレイを防止する必要があります。この検証ではスナップショットは暗号化されず、その内容もクライアントから隠されません。

### シリアライズのオプション {#serialization-options}

便利なシリアライズオプションは次のとおりです。

-   `context_serializer`：マッピングではないコンテキストオブジェクトのシリアライズ方法をカスタマイズします。
-   `context_deserializer`：`RunState.from_json(...)` または `RunState.from_string(...)` で状態を読み込む際に、マッピングではないコンテキストオブジェクトを再構築します。
- `strict_context=True`：コンテキストがすでにマッピングであるか、`context_serializer` を指定していない限り、シリアライズを失敗させます。また、コンテキストがすでにマッピングであるか、`context_deserializer` を指定していない限り、デシリアライズを失敗させます。
- `context_override`：状態を読み込む際に、シリアライズされたコンテキストを置き換えます。元のコンテキストオブジェクトを復元したくない場合に便利ですが、すでにシリアライズされたペイロードからそのコンテキストが削除されるわけではありません。
- `include_tracing_api_key=True`：再開した作業でも同じ認証情報を使用してトレースのエクスポートを継続する必要がある場合に、シリアライズされたトレースペイロードへトレーシング API キーを含めます。

シリアライズされた実行状態には、アプリのコンテキストに加えて、承認、使用量、シリアライズされた `tool_input`、ネストされたツールとしてのエージェントの再開、トレースメタデータ、サーバー管理の会話設定など、SDK が管理するランタイムメタデータが含まれます。シリアライズされた状態を保存または送信する予定がある場合、`RunContextWrapper.context` を永続化されるデータとして扱い、意図的に状態と一緒に移動させる場合を除いて、機密情報を格納しないでください。

## 保留中タスクのバージョン管理 {#versioning-pending-tasks}

承認が長期間保留される可能性がある場合は、シリアライズされた状態とともに、エージェント定義または SDK のバージョンマーカーを保存してください。これにより、モデル、プロンプト、ツール定義が変更されたときの非互換性を避けるために、対応するコードパスへデシリアライズ処理を振り分けられます。