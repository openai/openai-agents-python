---
search:
  exclude: true
---
# ツール

ツールを使用すると、データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータ操作など、エージェントがアクションを実行できます。SDK は 5 つのカテゴリーをサポートしています。

-   OpenAI がホストするツール：OpenAI サーバー上でモデル向けに実行されます。
-   ローカル／ランタイム実行ツール：`ComputerTool` と `ApplyPatchTool` は常にご自身の環境で実行され、`ShellTool` はローカルまたはホスト型コンテナーで実行できます。
-   `FunctionTool` インスタンス：任意の Python 関数をツールとしてラップします。
-   Agents as tools：完全なハンドオフを行わずに、エージェントを呼び出し可能なツールとして公開します。
-   実験的機能：Codex ツール：ツール呼び出しから、ワークスペースにスコープされた Codex タスクを実行します。

## ツールタイプの選択 {#choosing-a-tool-type}

このページをカタログとして使用し、制御するランタイムに該当するセクションに移動してください。

| 目的 | 参照先 |
| --- | --- |
| OpenAI が管理するツール（Web 検索、ファイル検索、Code Interpreter、ホスト型 MCP、画像生成）の使用 | [ホスト型ツール](#hosted-tools) |
| ツール検索を使用して、大規模なツール群の読み込みをランタイムまで延期 | [ホスト型ツール検索](#hosted-tool-search) |
| 生成された JavaScript から複数のツール呼び出しを調整 | [プログラムによるツール呼び出し](#programmatic-tool-calling) |
| 独自のプロセスまたは環境でのツール実行 | [ローカルランタイムツール](#local-runtime-tools) |
| Python 関数のツールとしてのラップ | [関数ツール](#function-tools) |
| ハンドオフせずに、あるエージェントから別のエージェントを呼び出し | [Agents as tools](#agents-as-tools) |
| エージェントからワークスペースにスコープされた Codex タスクを実行 | [実験的機能：Codex ツール](#experimental-codex-tool) |

## ホスト型ツール {#hosted-tools}

OpenAI は、[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する際に、いくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] を使用すると、エージェントが Web を検索できます。
-   [`FileSearchTool`][agents.tool.FileSearchTool] を使用すると、OpenAI のベクトルストアから情報を取得できます。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] を使用すると、LLM がサンドボックス環境でコードを実行できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] は、リモート MCP サーバーのツールをモデルに公開します。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] は、プロンプトから画像を生成します。
-   [`ToolSearchTool`][agents.tool.ToolSearchTool] を使用すると、モデルが遅延されたツール、名前空間、またはホスト型 MCP サーバーをオンデマンドで読み込めます。
-   [`ProgrammaticToolCallingTool`][agents.tool.ProgrammaticToolCallingTool] を使用すると、モデルが生成された JavaScript から対象ツールを調整できます。

ホスト型検索の高度なオプション：

-   `FileSearchTool` は、`vector_store_ids` と `max_num_results` に加えて、`filters`、`ranking_options`、`include_search_results` をサポートします。`max_num_results` には 1 から 50 までの整数を設定します。`None` または 0 を指定すると、プロバイダーのデフォルトが使用されます。
-   `WebSearchTool` は、`filters`、`user_location`、`search_context_size` をサポートします。

```python
from agents import Agent, FileSearchTool, Runner, WebSearchTool

agent = Agent(
    name="Assistant",
    tools=[
        WebSearchTool(),
        FileSearchTool(
            max_num_results=3,
            vector_store_ids=["VECTOR_STORE_ID"],
        ),
    ],
)

async def main():
    result = await Runner.run(agent, "Which coffee shop should I go to, taking into account my preferences and the weather today in SF?")
    print(result.final_output)
```

### ホスト型ツール検索 {#hosted-tool-search}

ツール検索を使用すると、OpenAI Responses モデルは大規模なツール群の読み込みをランタイムまで延期できるため、モデルは現在のターンに必要なサブセットのみを読み込みます。これは、多数の関数ツール、名前空間グループ、またはホスト型 MCP サーバーがあり、すべてのツールを事前に公開せずにツールスキーマのトークン数を削減したい場合に便利です。

エージェントを構築する時点で候補ツールがすでに分かっている場合は、ホスト型ツール検索から始めてください。アプリケーションが読み込む対象を動的に決定する必要がある場合、Responses API はクライアント実行型のツール検索もサポートしていますが、標準の `Runner` はこのモードを自動実行しません。

```python
from typing import Annotated

from agents import Agent, Runner, ToolSearchTool, tool_namespace
from agents.decorators import tool


@tool(defer_loading=True)
def get_customer_profile(
    customer_id: Annotated[str, "The customer ID to look up."],
) -> str:
    """Fetch a CRM customer profile."""
    return f"profile for {customer_id}"


@tool(defer_loading=True)
def list_open_orders(
    customer_id: Annotated[str, "The customer ID to look up."],
) -> str:
    """List open orders for a customer."""
    return f"open orders for {customer_id}"


crm_tools = tool_namespace(
    name="crm",
    description="CRM tools for customer lookups.",
    tools=[get_customer_profile, list_open_orders],
)


agent = Agent(
    name="Operations assistant",
    model="gpt-5.6-sol",
    instructions="Load the crm namespace before using CRM tools.",
    tools=[*crm_tools, ToolSearchTool()],
)

result = await Runner.run(agent, "Look up customer_42 and list their open orders.")
print(result.final_output)
```

留意事項：

-   ホスト型ツール検索は、OpenAI Responses モデルでのみ利用できます。現在の Python SDK のサポートは `openai>=2.25.0` に依存します。
-   エージェントに遅延読み込み対象を設定する場合は、`ToolSearchTool()` をちょうど 1 つ追加します。
-   検索可能な対象には、`@function_tool(defer_loading=True)`、`tool_namespace(name=..., description=..., tools=[...])`、`HostedMCPTool(tool_config={..., "defer_loading": True})` が含まれます。
-   遅延読み込みされる関数ツールは、`ToolSearchTool()` と組み合わせる必要があります。名前空間のみの構成では、モデルが必要なグループをオンデマンドで読み込めるように、`ToolSearchTool()` も使用できます。
-   `tool_namespace()` は、`FunctionTool` インスタンスを共通の名前空間名と説明の下にグループ化します。通常、`crm`、`billing`、`shipping` など、関連するツールが多数ある場合に最適です。
-   OpenAI の公式ベストプラクティスのガイダンスは、[可能な限り名前空間を使用する](https://developers.openai.com/api/docs/guides/tools-tool-search#use-namespaces-where-possible)ことです。
-   可能な場合は、個別に遅延される多数の関数よりも、名前空間またはホスト型 MCP サーバーを優先してください。通常、モデルにとってより優れた高レベルの検索対象となり、トークンもより多く節約できます。
-   名前空間には、即時利用可能なツールと遅延ツールを混在させられます。`defer_loading=True` がないツールは引き続き即時に呼び出せますが、同じ名前空間内の遅延ツールはツール検索を通じて読み込まれます。
-   経験則として、各名前空間は比較的小さく保ち、関数を 10 個未満にするのが理想的です。
-   名前付きの `tool_choice` は、名前空間名だけの対象や遅延専用ツールを指定できません。`auto`、`required`、または実在するトップレベルの呼び出し可能なツール名を優先してください。
-   `ToolSearchTool(execution="client")` は、Responses を手動でオーケストレーションするためのものです。モデルがクライアント実行型の `tool_search_call` を出力した場合、標準の `Runner` は代わりに実行せず、例外を発生させます。
-   ツール検索のアクティビティは、専用のアイテムおよびイベントタイプとして [`RunResult.new_items`](results.md#new-items) と [`RunItemStreamEvent`](streaming.md#run-item-event-names) に表示されます。
-   名前空間を使用した読み込みとトップレベルの遅延ツールの両方を扱う、完全に実行可能なコード例については、`examples/tools/tool_search.py` を参照してください。
-   公式プラットフォームガイド：[ツール検索](https://developers.openai.com/api/docs/guides/tools-tool-search)。

### プログラムによるツール呼び出し {#programmatic-tool-calling}

プログラムによるツール呼び出しを使用すると、サポートされている OpenAI Responses モデルが、対象ツールを呼び出し、それらの出力を組み合わせ、1 つの結果をモデルに返す JavaScript を生成できます。各ツール呼び出しの後にモデルとの往復を行うことなく、ループ、分岐、並列呼び出し、または中間計算を利用できる、範囲の限定されたワークフローに便利です。

生成されたプログラムは、新しいホスト型 V8 環境で実行されます。Node.js API、ファイルシステムやネットワークへのアクセス、永続プロセスは使用できません。プログラムが操作できるのは、明示的に許可したツールのみです。

```python
from pydantic import BaseModel

from agents import (
    Agent,
    ModelSettings,
    ProgrammaticToolCallingTool,
    Runner,
)
from agents.decorators import tool


class InventoryOutput(BaseModel):
    sku: str
    available_units: int


@tool(allowed_callers=["programmatic"])
def get_inventory(sku: str) -> InventoryOutput:
    return InventoryOutput(sku=sku, available_units=42)


agent = Agent(
    name="Inventory planner",
    model="gpt-5.6",
    model_settings=ModelSettings(tool_choice="programmatic_tool_calling"),
    tools=[get_inventory, ProgrammaticToolCallingTool()],
)

result = Runner.run_sync(agent, "Check inventory for desk-lamp and summarize it.")
print(result.final_output)
```

留意事項：

-   プログラムによるツール呼び出しは、サポートされている OpenAI Responses モデルでのみ利用できます。`ProgrammaticToolCallingTool()` と `tool_choice="programmatic_tool_calling"` は、Chat Completions モデルおよび Responses 以外のバックエンドでは拒否されます。
-   エージェントに追加できる `ProgrammaticToolCallingTool()` は最大 1 つです。エージェントは、プログラムから呼び出し可能なツールを少なくとも 1 つ、名前空間、遅延関数、または遅延ホスト型 MCP サーバーを基盤とする `ToolSearchTool()`、もしくは内部構造を公開しないプロンプト管理型ツール群も公開する必要があります。検索可能な対象を持たない単独の `ToolSearchTool()` は拒否されます。
-   `allowed_callers` は、ツールをどのように呼び出せるかを制御します。省略した場合、モデルによる直接呼び出しのみが許可されます。プログラムからのみアクセス可能にするには `["programmatic"]` を使用し、両方を許可するには `["direct", "programmatic"]` を使用します。
-   オプトインできる SDK ツールタイプは、`FunctionTool`、`CustomTool`、`ShellTool`、`ApplyPatchTool`、`HostedMCPTool`、`CodeInterpreterTool` です。関数、カスタム、シェル、apply-patch の各ツールでは、`allowed_callers` が直接公開されます。ホスト型 MCP と Code Interpreter では、`tool_config` 内に `allowed_callers` を設定します。
-   `@function_tool(allowed_callers=[...])` では、Pydantic モデル、TypedDict、dataclass などの構造化された戻り値アノテーションが、自動的に厳密なオブジェクト出力スキーマになります。また、返された値はプログラムに返される前に、そのスキーマに対して検証されます。関数に利用可能なアノテーションがない場合は `output_type=...` を使用し、厳密なオブジェクトスキーマがすでにある場合は、より低レベルのエスケープハッチである `output_json_schema={...}` を使用します。`output_type` と `output_json_schema` は同時に使用できません。`str`、`Any`、`None` の戻り値アノテーションでは、出力スキーマは作成されません。スキーマを基盤とするプログラム所有の呼び出しでは、自由形式のテキストが出力スキーマを満たさないため、デフォルトの失敗フォーマッターは無効になります。そのため、スキーマに準拠した JSON を返すカスタム `failure_error_function` を指定しない限り、ハンドラーの例外は伝播します。
-   プログラム所有の SDK ツールでも、通常の Runner ライフサイクルが使用されます。ツール入出力ガードレール、フック、タイムアウト、同時実行数の制限、承認、セッション、`RunState` の一時停止／再開動作は引き続き適用され、SDK は各子呼び出しとプログラム呼び出し元との関係を保持します。
-   `ProgrammaticToolCallingTool()` が存在する場合、プログラムが実行される前であっても、モデルリクエストの再試行には、より厳格な再実行安全性の境界が使用されます。SDK は、これらのリクエストに対して、プロバイダー管理の再試行と WebSocket のイベント前再試行を無効にします。Runner の再試行ポリシーは、プロバイダーの助言で再実行が安全であると明示された場合にのみ再試行します。`retry_policies.network_error()` だけでは、この境界を上書きしません。
-   承認が重要なツールや影響の大きいツールは、通常、直接呼び出しのままにしておく方が適切です。これにより、大規模なプログラムの一部になる前に、各アクションを人が確認できます。プログラム所有の呼び出しが承認待ちで一時停止した場合は、`RunState` を通じて中断を解決し、通常どおり元の実行を再開します。
-   プログラムによるツール呼び出しは、[ホスト型ツール検索](#hosted-tool-search)と組み合わせられます。生成されたプログラムから遅延ツールを呼び出すには、モデルが事前にそのツールを読み込む必要があります。
-   `program` アイテムと、その通常のプログラム所有の子ツール呼び出しは、[`ToolCallItem`][agents.items.ToolCallItem] エントリとして表示されます。対応する `program_output` は、[`ToolCallOutputItem`][agents.items.ToolCallOutputItem] として表示されます。一方、ホスト型 MCP の承認リクエストとツールカタログでは、専用の MCP アイテムとストリームイベントが使用されます。確認方法の詳細については、[実行結果](results.md#new-items)と[ストリーミング](streaming.md#run-item-event-names)を参照してください。
-   完全な並行在庫計画のコード例については、`examples/tools/programmatic_tool_calling.py` を参照してください。
-   公式プラットフォームガイド：[プログラムによるツール呼び出し](https://developers.openai.com/api/docs/guides/tools-programmatic-tool-calling)。

### ホスト型コンテナーのシェルとスキル {#hosted-container-shell-skills}

`ShellTool` は、OpenAI がホストするコンテナーでの実行もサポートします。ローカルランタイムではなく、管理対象コンテナー内でモデルにシェルコマンドを実行させたい場合は、このモードを使用します。

```python
from agents import Agent, Runner, ShellTool, ShellToolSkillReference

csv_skill: ShellToolSkillReference = {
    "type": "skill_reference",
    "skill_id": "skill_698bbe879adc81918725cbc69dcae7960bc5613dadaed377",
    "version": "1",
}

agent = Agent(
    name="Container shell agent",
    model="gpt-5.6-sol",
    instructions="Use the mounted skill when helpful.",
    tools=[
        ShellTool(
            environment={
                "type": "container_auto",
                "network_policy": {"type": "disabled"},
                "skills": [csv_skill],
            }
        )
    ],
)

result = await Runner.run(
    agent,
    "Use the configured skill to analyze CSV files in /mnt/data and summarize totals by region.",
)
print(result.final_output)
```

後続の実行で既存のコンテナーを再利用するには、`environment={"type": "container_reference", "container_id": "cntr_..."}` を設定します。

留意事項：

-   ホスト型シェルは、Responses API のシェルツールを通じて利用できます。
-   `container_auto` はリクエスト用のコンテナーをプロビジョニングし、`container_reference` は既存のコンテナーを再利用します。
-   `container_auto` には、`file_ids` と `memory_limit` も含められます。
-   `environment.skills` は、スキル参照とインラインスキルバンドルを受け付けます。
-   ホスト型環境では、`ShellTool` に `executor`、`needs_approval`、`on_approval` を設定しないでください。
-   `network_policy` は、`disabled` モードと `allowlist` モードをサポートします。
-   許可リストモードでは、`network_policy.domain_secrets` により、ドメインにスコープされたシークレットを名前で挿入できます。
-   完全なコード例については、`examples/tools/container_shell_skill_reference.py` と `examples/tools/container_shell_inline_skill.py` を参照してください。
-   OpenAI プラットフォームガイド：[シェル](https://platform.openai.com/docs/guides/tools-shell)と[スキル](https://platform.openai.com/docs/guides/tools-skills)。

## ローカルランタイムツール {#local-runtime-tools}

ローカルランタイムツールは、モデルレスポンス自体の外部で実行されます。呼び出すタイミングは引き続きモデルが決定しますが、実際の処理はアプリケーションまたは設定済みの実行環境が行います。

`ComputerTool` と `ApplyPatchTool` には、常にご自身で用意するローカル実装が必要です。`ShellTool` は両方のモードに対応します。管理された実行が必要な場合は上記のホスト型コンテナー設定を使用し、ご自身のプロセスでコマンドを実行する場合は下記のローカルランタイム設定を使用します。

ローカルランタイムツールでは、実装を用意する必要があります。

-   [`ComputerTool`][agents.tool.ComputerTool]：GUI／ブラウザーの自動化を有効にするには、[`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] インターフェースを実装します。
-   [`ShellTool`][agents.tool.ShellTool]：ローカル実行とホスト型コンテナー実行の両方に対応する最新のシェルツールです。
-   [`LocalShellTool`][agents.tool.LocalShellTool]：従来のローカルシェル統合です。
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]：差分をローカルに適用するには、[`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] を実装します。
-   ローカルシェルスキルは、`ShellTool(environment={"type": "local", "skills": [...]})` で利用できます。

シェルアクションのタイムアウトでは、有限のタイムアウトとして正の整数のミリ秒を使用します。0 は実行機能の実装間で共通の意味を持たないため、SDK はローカルの `ShellTool` 実行機能を呼び出す前に、`0` と `None` の両方を明示的なタイムアウトなしとして扱います。それ以外の値は、実行機能の呼び出し前に拒否されます。これはタイムアウトフィールドに固有の動作です。`max_output_length=0` は、空のキャプチャ出力を要求する値として引き続きサポートされます。

### ComputerTool と Responses のコンピューターツール {#computertool-and-the-responses-computer-tool}

`ComputerTool` は、引き続きローカルハーネスです。[`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] の実装を用意すると、SDK はそのハーネスを OpenAI Responses API のコンピューターインターフェースにマッピングします。

明示的な [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) リクエストでは、SDK は GA の組み込みツールペイロード `{"type": "computer"}` を送信します。以前の `computer-use-preview` モデルへのリクエストでは、SDK は引き続きプレビューペイロード `{"type": "computer_use_preview", "environment": ..., "display_width": ..., "display_height": ...}` を送信します。これは、OpenAI の[コンピュータ操作ガイド](https://developers.openai.com/api/docs/guides/tools-computer-use/)に記載されたプラットフォームの移行に対応しています。

-   モデル：`computer-use-preview` -> `gpt-5.5`
-   ツールセレクター：`computer_use_preview` -> `computer`
-   コンピューター呼び出しの形式：`computer_call` ごとに 1 つの `action` -> `computer_call` 上のバッチ化された `actions[]`
-   切り詰め：プレビュー経路では `ModelSettings(truncation="auto")` が必須 -> GA 経路では不要

SDK は、実際の Responses リクエストで有効なモデルに基づいて、そのワイヤー形式を選択します。プロンプトテンプレートを使用し、プロンプト側でモデルを指定しているためリクエストで `model` が省略される場合、`model="gpt-5.5"` を明示的に指定するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制しない限り、SDK はプレビュー互換のコンピューターペイロードを維持します。

[`ComputerTool`][agents.tool.ComputerTool] が存在する場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` はすべて受け付けられ、有効なリクエストモデルに対応する組み込みセレクターに正規化されます。`ComputerTool` がない場合、これらの文字列は引き続き通常の関数名として動作します。

この違いは、`ComputerTool` が [`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーを基盤としている場合に重要です。GA の `computer` ペイロードでは、シリアル化時に `environment` や寸法は不要なため、ファクトリーが `Computer` または `AsyncComputer` インスタンスを生成する前にシリアル化できます。プレビュー互換のシリアル化では、SDK が `environment`、`display_width`、`display_height` を送信できるように、解決済みの `Computer` または `AsyncComputer` インスタンスが引き続き必要です。

ランタイムでは、両方の経路で同じローカルハーネスが引き続き使用されます。プレビューレスポンスは、単一の `action` を持つ `computer_call` アイテムを出力します。`gpt-5.5` はバッチ化された `actions[]` を出力でき、SDK は `computer_call_output` スクリーンショットアイテムを生成する前に、それらを順番に実行します。Playwright を基盤とする実行可能なハーネスについては、`examples/tools/computer_use.py` を参照してください。

```python
from agents import Agent, ApplyPatchTool, ShellTool
from agents.computer import AsyncComputer
from agents.editor import ApplyPatchResult, ApplyPatchOperation, ApplyPatchEditor


class NoopComputer(AsyncComputer):
    environment = "browser"
    dimensions = (1024, 768)
    async def screenshot(self): return ""
    async def click(self, x, y, button): ...
    async def double_click(self, x, y): ...
    async def scroll(self, x, y, scroll_x, scroll_y): ...
    async def type(self, text): ...
    async def wait(self): ...
    async def move(self, x, y): ...
    async def keypress(self, keys): ...
    async def drag(self, path): ...


class NoopEditor(ApplyPatchEditor):
    async def create_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def update_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")
    async def delete_file(self, op: ApplyPatchOperation): return ApplyPatchResult(status="completed")


async def run_shell(request):
    return "shell output"


agent = Agent(
    name="Local tools agent",
    tools=[
        ShellTool(executor=run_shell),
        ApplyPatchTool(editor=NoopEditor()),
        # ComputerTool expects a Computer/AsyncComputer implementation; omitted here for brevity.
    ],
)
```

## 関数ツール {#function-tools}

任意の Python 関数をツールとして使用できます。Agents SDK がツールを自動的に設定します。

-   ツール名には Python 関数の名前が使用されます（名前を指定することもできます）
-   ツールの説明は関数の docstring から取得されます（説明を指定することもできます）
-   関数入力のスキーマは、関数の引数から自動的に作成されます
-   無効にしない限り、各入力の説明は関数の docstring から取得されます

`@tool` によって作成されたツールは、元の Python 呼び出し可能オブジェクトを読み取り専用の `__wrapped__` 属性を通じて公開します。これは検査やテストに便利ですが、直接呼び出すと、スキーマ検証、コンテキスト注入、ガードレール、タイムアウト、失敗処理、トレーシングを含むツールランタイムのパイプラインがバイパスされます。手動で構築した `FunctionTool` インスタンスは、`__wrapped__` を公開しません。

関数シグネチャの抽出には Python の `inspect` モジュールを使用し、docstring の解析には [`griffe`](https://mkdocstrings.github.io/griffe/) を、スキーマの作成には `pydantic` を使用します。

OpenAI Responses モデルを使用している場合、`@function_tool(defer_loading=True)` は、`ToolSearchTool()` によって読み込まれるまで関数ツールを非表示にします。[`tool_namespace()`][agents.tool.tool_namespace] を使用して、関連する関数ツールをグループ化することもできます。完全な設定と制約については、[ホスト型ツール検索](#hosted-tool-search)を参照してください。

```python
import json

from typing_extensions import TypedDict, Any

from agents import Agent, FunctionTool, RunContextWrapper
from agents.decorators import tool


class Location(TypedDict):
    lat: float
    long: float

@tool  # (1)!
async def fetch_weather(location: Location) -> str:
    # (2)!
    """Fetch the weather for a given location.

    Args:
        location: The location to fetch the weather for.
    """
    # In real life, we'd fetch the weather from a weather API
    return "sunny"


@tool(name_override="fetch_data")  # (3)!
def read_file(ctx: RunContextWrapper[Any], path: str, directory: str | None = None) -> str:
    """Read the contents of a file.

    Args:
        path: The path to the file to read.
        directory: The directory to read the file from.
    """
    # In real life, we'd read the file from the file system
    return "<file contents>"


agent = Agent(
    name="Assistant",
    tools=[fetch_weather, read_file],  # (4)!
)

for tool in agent.tools:
    if isinstance(tool, FunctionTool):
        print(tool.name)
        print(tool.description)
        print(json.dumps(tool.params_json_schema, indent=2))
        print()

```

1.  関数の引数には任意の Python 型を使用でき、関数は同期または非同期にできます。
2.  docstring が存在する場合は、説明と引数の説明を取得するために使用されます
3.  関数は、必要に応じて実行コンテキストを第 1 引数として受け取れます。ツール名、説明、使用する docstring スタイルなどのオーバーライドも設定できます。
4.  デコレートされた関数をツールのリストに渡せます。

??? note "出力を表示するには展開"

    ```
    fetch_weather
    Fetch the weather for a given location.
    {
    "$defs": {
      "Location": {
        "properties": {
          "lat": {
            "title": "Lat",
            "type": "number"
          },
          "long": {
            "title": "Long",
            "type": "number"
          }
        },
        "required": [
          "lat",
          "long"
        ],
        "title": "Location",
        "type": "object"
      }
    },
    "properties": {
      "location": {
        "$ref": "#/$defs/Location",
        "description": "The location to fetch the weather for."
      }
    },
    "required": [
      "location"
    ],
    "title": "fetch_weather_args",
    "type": "object"
    }

    fetch_data
    Read the contents of a file.
    {
    "properties": {
      "path": {
        "description": "The path to the file to read.",
        "title": "Path",
        "type": "string"
      },
      "directory": {
        "anyOf": [
          {
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "default": null,
        "description": "The directory to read the file from.",
        "title": "Directory"
      }
    },
    "required": [
      "path"
    ],
    "title": "fetch_data_args",
    "type": "object"
    }
    ```

### 関数ツールからの画像またはファイルの返却 {#returning-images-or-files-from-function-tools}

テキスト出力に加えて、関数ツールの出力として 1 つまたは複数の画像やファイルを返せます。そのためには、次のいずれかを返します。

-   画像：[`ToolOutputImage`][agents.tool.ToolOutputImage]（または TypedDict 版の [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
-   ファイル：[`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（または TypedDict 版の [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
-   テキスト：文字列、文字列化可能なオブジェクト、または [`ToolOutputText`][agents.tool.ToolOutputText]（もしくは TypedDict 版の [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### カスタム関数ツール {#custom-function-tools}

Python 関数をツールとして使用したくない場合もあります。その場合は、必要に応じて [`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。次の項目を指定する必要があります。

-   `name`
-   `description`
-   引数の JSON スキーマである `params_json_schema`
-   [`ToolContext`][agents.tool_context.ToolContext] と JSON 文字列形式の引数を受け取り、ツール出力（テキスト、構造化されたツール出力オブジェクト、出力のリストなど）を返す非同期関数 `on_invoke_tool`

```python
from typing import Any

from pydantic import BaseModel

from agents import RunContextWrapper, FunctionTool



def do_some_work(data: str) -> str:
    return "done"


class FunctionArgs(BaseModel):
    username: str
    age: int


async def run_function(ctx: RunContextWrapper[Any], args: str) -> str:
    parsed = FunctionArgs.model_validate_json(args)
    return do_some_work(data=f"{parsed.username} is {parsed.age} years old")


tool = FunctionTool(
    name="process_user",
    description="Processes extracted user data",
    params_json_schema=FunctionArgs.model_json_schema(),
    on_invoke_tool=run_function,
)
```

### 引数と docstring の自動解析 {#automatic-argument-and-docstring-parsing}

前述のとおり、関数シグネチャを自動的に解析してツールのスキーマを抽出し、docstring を解析してツールおよび個々の引数の説明を抽出します。留意点は次のとおりです。

1. シグネチャの解析は、`inspect` モジュールを通じて行われます。型アノテーションを使用して引数の型を把握し、スキーマ全体を表す Pydantic モデルを動的に構築します。Python の基本型、Pydantic モデル、TypedDict など、ほとんどの型をサポートしています。
2. docstring の解析には `griffe` を使用します。サポートされている docstring 形式は、`google`、`sphinx`、`numpy` です。docstring 形式の自動検出を試みますが、これはベストエフォートであり、`function_tool` を呼び出す際に明示的に設定できます。`use_docstring_info` を `False` に設定すると、docstring の解析を無効にすることもできます。Google スタイルの docstring では、要約テキストの直後に空行を挟まずに配置された `Args:`、`Arguments:`、`Params:`、`Parameters:` セクションもパーサーが受け付けます。

スキーマ抽出のコードは、[`agents.function_schema`][] にあります。

### Pydantic Field による引数の制約と説明 {#constraining-and-describing-arguments-with-pydantic-field}

Pydantic の [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) を使用して、ツール引数に制約（数値の最小値／最大値、文字列の長さやパターンなど）と説明を追加できます。Pydantic と同様に、デフォルト値を使用する形式（`arg: int = Field(..., ge=1)`）と `Annotated`（`arg: Annotated[int, Field(..., ge=1)]`）の両方がサポートされています。生成される JSON スキーマと検証には、これらの制約が含まれます。

```python
from typing import Annotated
from pydantic import Field
from agents.decorators import tool

# Default-based form
@tool
def score_a(score: int = Field(..., ge=0, le=100, description="Score from 0 to 100")) -> str:
    return f"Score recorded: {score}"

# Annotated form
@tool
def score_b(score: Annotated[int, Field(..., ge=0, le=100, description="Score from 0 to 100")]) -> str:
    return f"Score recorded: {score}"
```

### 関数ツールのタイムアウト {#function-tool-timeouts}

`@function_tool(timeout=...)` を使用すると、非同期関数ツールに呼び出しごとのタイムアウトを設定できます。

```python
import asyncio
from agents import Agent
from agents.decorators import tool


@tool(timeout=2.0)
async def slow_lookup(query: str) -> str:
    await asyncio.sleep(10)
    return f"Result for {query}"


agent = Agent(
    name="Timeout demo",
    instructions="Use tools when helpful.",
    tools=[slow_lookup],
)
```

タイムアウトに達した場合、デフォルトの動作は `timeout_behavior="error_as_result"` で、モデルから認識可能なタイムアウトメッセージ（たとえば `Tool 'slow_lookup' timed out after 2 seconds.`）を送信します。

タイムアウト処理は次のように制御できます。

-   `timeout_behavior="error_as_result"`（デフォルト）：モデルが復旧できるように、タイムアウトメッセージをモデルへ返します。
-   `timeout_behavior="raise_exception"`：[`ToolTimeoutError`][agents.exceptions.ToolTimeoutError] を発生させ、実行を失敗させます。
-   `error_as_result` を使用する場合、`timeout_error_function=...` でタイムアウトメッセージをカスタマイズします。

```python
import asyncio
from agents import Agent, Runner, ToolTimeoutError
from agents.decorators import tool


@tool(timeout=1.5, timeout_behavior="raise_exception")
async def slow_tool() -> str:
    await asyncio.sleep(5)
    return "done"


agent = Agent(name="Timeout hard-fail", tools=[slow_tool])

try:
    await Runner.run(agent, "Run the tool")
except ToolTimeoutError as e:
    print(f"{e.tool_name} timed out in {e.timeout_seconds} seconds")
```

!!! note

    タイムアウト設定は、非同期の `@function_tool` ハンドラーでのみサポートされています。

### 関数ツールのエラー処理 {#handling-errors-in-function-tools}

`@function_tool` を使用して関数ツールを作成する際に、`failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュした場合に LLM へエラーレスポンスを提供する関数です。

-   デフォルトでは（何も渡さない場合）、エラーが発生したことを LLM に通知する `default_tool_error_function` が実行されます。
-   独自のエラー関数を渡すと、代わりにその関数が実行され、レスポンスが LLM に送信されます。
-   `None` を明示的に渡すと、ツール呼び出しのエラーが再度発生し、ご自身で処理できます。たとえば、モデルが無効な JSON を生成した場合は `ModelBehaviorError`、コードがクラッシュした場合は `UserError` などが発生する可能性があります。

```python
from agents import RunContextWrapper
from agents.decorators import tool
from typing import Any

def my_custom_error_function(context: RunContextWrapper[Any], error: Exception) -> str:
    """A custom function to provide a user-friendly error message."""
    print(f"A tool call failed with the following error: {error}")
    return "An internal server error occurred. Please try again later."

@tool(failure_error_function=my_custom_error_function)
def get_user_profile(user_id: str) -> str:
    """Fetches a user profile from a mock API.
     This function demonstrates a 'flaky' or failing API call.
    """
    if user_id == "user_123":
        return "User profile for user_123 successfully retrieved."
    else:
        raise ValueError(f"Could not retrieve profile for user_id: {user_id}. API returned an error.")

```

`FunctionTool` オブジェクトを手動で作成する場合は、`on_invoke_tool` 関数内でエラーを処理する必要があります。

## Agents as tools {#agents-as-tools}

一部のワークフローでは、制御をハンドオフする代わりに、中央のエージェントで専門エージェントのネットワークをオーケストレーションしたい場合があります。これは、エージェントをツールとしてモデル化することで実現できます。

```python
import asyncio

from agents import Agent, Runner

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You translate the user's message to Spanish",
)

french_agent = Agent(
    name="French agent",
    instructions="You translate the user's message to French",
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    instructions=(
        "You are a translation agent. You use the tools given to you to translate. "
        "If asked for multiple translations, you call the relevant tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="translate_to_spanish",
            tool_description="Translate the user's message to Spanish",
        ),
        french_agent.as_tool(
            tool_name="translate_to_french",
            tool_description="Translate the user's message to French",
        ),
    ],
)

async def main():
    result = await Runner.run(orchestrator_agent, input="Say 'Hello, how are you?' in Spanish.")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

### ツールエージェントのカスタマイズ {#customizing-tool-agents}

`agent.as_tool` は、エージェントをツールに変換するための便利なメソッドです。`max_turns`、`run_config`、`hooks`、`previous_response_id`、`conversation_id`、`session`、`needs_approval` など、一般的なランタイムオプションをサポートします。また、`parameters`、`input_builder`、`include_input_schema` による構造化入力もサポートします。

状態オプションは、ツール呼び出しによって開始されるネストされたエージェント実行を設定します。親実行の会話状態は、自動的には継承されません。親実行とネストされた実行の間でクライアント管理の履歴を共有するには、両方に同じ `session` を明示的に渡します。`Runner.run` と同様に、ネストされた実行には 1 つの状態戦略を選択します。クライアント管理の `session`、または `previous_response_id` もしくは `conversation_id` によるサーバー管理の継続です。

```python
from agents.decorators import tool


@tool
async def run_my_agent() -> str:
    """A tool that runs the agent with custom configs"""

    agent = Agent(name="My agent", instructions="...")

    result = await Runner.run(
        agent,
        input="...",
        max_turns=5,
        run_config=...
    )

    return str(result.final_output)
```

### ツールエージェントの構造化入力 {#structured-input-for-tool-agents}

デフォルトでは、`Agent.as_tool()` は、1 つの文字列フィールド `input`（`{"input": "..."}`）を持つオブジェクトを想定します。ただし、`parameters`（Pydantic モデル型または dataclass 型）を渡すことで、構造化されたスキーマを公開できます。

追加オプション：

- `include_input_schema=True` は、生成されるネストされた入力に完全な JSON Schema を含めます。
- `input_builder=...` を使用すると、構造化されたツール引数をネストされたエージェント入力へ変換する方法を完全にカスタマイズできます。
- `RunContextWrapper.tool_input` には、ネストされた実行コンテキスト内で解析済みの構造化ペイロードが格納されます。

```python
from pydantic import BaseModel, Field


class TranslationInput(BaseModel):
    text: str = Field(description="Text to translate.")
    source: str = Field(description="Source language.")
    target: str = Field(description="Target language.")


translator_tool = translator_agent.as_tool(
    tool_name="translate_text",
    tool_description="Translate text between languages.",
    parameters=TranslationInput,
    include_input_schema=True,
)
```

完全に実行可能なコード例については、`examples/agent_patterns/agents_as_tools_structured.py` を参照してください。

### ツールエージェントの承認ゲート {#approval-gates-for-tool-agents}

`Agent.as_tool(..., needs_approval=...)` は、`function_tool` と同じ承認フローを使用します。承認が必要な場合、実行は一時停止し、保留中のアイテムが `result.interruptions` に表示されます。その後、`result.to_state()` を使用し、`state.approve(...)` または `state.reject(...)` を呼び出した後に再開します。一時停止／再開の完全なパターンについては、[Human-in-the-loop ガイド](human_in_the_loop.md)を参照してください。

### カスタム出力抽出 {#custom-output-extraction}

場合によっては、ツールエージェントの出力を中央のエージェントへ返す前に変更したいことがあります。これは、次のような場合に便利です。

-   サブエージェントのチャット履歴から特定の情報（JSON ペイロードなど）を抽出する。
-   エージェントの最終回答を変換または再フォーマットする（Markdown をプレーンテキストや CSV に変換するなど）。
-   出力を検証するか、エージェントのレスポンスが欠落している場合や形式が不正な場合にフォールバック値を提供する。

これを行うには、`as_tool` メソッドに `custom_output_extractor` 引数を指定します。

```python
async def extract_json_payload(run_result: RunResult) -> str:
    # Scan the agent’s outputs in reverse order until we find a JSON-like message from a tool call.
    for item in reversed(run_result.new_items):
        if isinstance(item, ToolCallOutputItem) and item.output.strip().startswith("{"):
            return item.output.strip()
    # Fallback to an empty JSON object if nothing was found
    return "{}"


json_tool = data_agent.as_tool(
    tool_name="get_data_json",
    tool_description="Run the data agent and return only its JSON payload",
    custom_output_extractor=extract_json_payload,
)
```

カスタム抽出機能内では、ネストされた [`RunResult`][agents.result.RunResult] によって [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] も公開されます。これは、ネストされた実行結果を後処理する際に、外側のツール名、呼び出し ID、または未加工の引数が必要な場合に便利です。[実行結果ガイド](results.md#agent-as-tool-metadata)を参照してください。

### ネストされたエージェント実行のストリーミング {#streaming-nested-agent-runs}

`as_tool` に `on_stream` コールバックを渡すと、ネストされたエージェントが出力するストリーミングイベントを受信しながら、ストリーム完了後に最終出力を返せます。

```python
from agents import AgentToolStreamEvent


async def handle_stream(event: AgentToolStreamEvent) -> None:
    # Inspect the underlying StreamEvent along with agent metadata.
    print(f"[stream] {event['agent'].name} :: {event['event'].type}")


billing_agent_tool = billing_agent.as_tool(
    tool_name="billing_helper",
    tool_description="Answer billing questions.",
    on_stream=handle_stream,  # Can be sync or async.
)
```

想定される動作：

- イベントタイプは `StreamEvent["type"]` と同じです：`raw_response_event`、`run_item_stream_event`、`agent_updated_stream_event`。
- `on_stream` を指定すると、ネストされたエージェントが自動的にストリーミングモードで実行され、最終出力を返す前にストリームが最後まで処理されます。
- ハンドラーは同期または非同期にできます。各イベントは到着順に配信されます。
- モデルのツール呼び出しを介してツールが呼び出された場合、`tool_call` が存在します。直接呼び出しでは `None` のままになる場合があります。
- 完全に実行可能なサンプルについては、`examples/agent_patterns/agents_as_tools_streaming.py` を参照してください。

### 条件付きツール有効化 {#conditional-tool-enabling}

`is_enabled` パラメーターを使用すると、ランタイムでエージェントツールを条件付きで有効または無効にできます。これにより、コンテキスト、ユーザー設定、またはランタイム条件に基づいて、LLM が利用できるツールを動的に絞り込めます。

```python
import asyncio
from agents import Agent, AgentBase, Runner, RunContextWrapper
from pydantic import BaseModel

class LanguageContext(BaseModel):
    language_preference: str = "french_spanish"

def french_enabled(ctx: RunContextWrapper[LanguageContext], agent: AgentBase) -> bool:
    """Enable French for French+Spanish preference."""
    return ctx.context.language_preference == "french_spanish"

# Create specialized agents
spanish_agent = Agent(
    name="spanish_agent",
    instructions="You respond in Spanish. Always reply to the user's question in Spanish.",
)

french_agent = Agent(
    name="french_agent",
    instructions="You respond in French. Always reply to the user's question in French.",
)

# Create orchestrator with conditional tools
orchestrator = Agent(
    name="orchestrator",
    instructions=(
        "You are a multilingual assistant. You use the tools given to you to respond to users. "
        "You must call ALL available tools to provide responses in different languages. "
        "You never respond in languages yourself, you always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="respond_spanish",
            tool_description="Respond to the user's question in Spanish",
            is_enabled=True,  # Always enabled
        ),
        french_agent.as_tool(
            tool_name="respond_french",
            tool_description="Respond to the user's question in French",
            is_enabled=french_enabled,
        ),
    ],
)

async def main():
    context = LanguageContext(language_preference="french_spanish")
    result = await Runner.run(orchestrator, "How are you?", context=context)
    print(result.final_output)

asyncio.run(main())
```

`is_enabled` パラメーターは、次の値を受け付けます。

-   **ブール値**：`True`（常に有効）または `False`（常に無効）
-   **呼び出し可能な関数**：`(context, agent)` を受け取り、ブール値を返す関数
-   **非同期関数**：複雑な条件付きロジックのための非同期関数

無効なツールはランタイムで LLM から完全に隠されるため、次の用途に便利です。

-   リクエストにスコープされた機能の可視性
-   環境固有のツール可用性（開発環境と本番環境）
-   異なるツール設定の A/B テスト
-   ランタイム状態に基づく動的なツールフィルタリング

ローカルで設定された関数ツールの場合、Runner は呼び出し前にも `is_enabled` を再評価します。ただし、`is_enabled` は可視性とディスパッチを制御するものであり、ツール引数やアクセス対象のリソースに依存する認可の代わりにはなりません。これらのチェックはツール実装内で適用するか、必要に応じて[ツール入力ガードレール](guardrails.md#tool-guardrails)と[承認](human_in_the_loop.md)を使用してください。MCP サーバーは、保護された操作を自身で認可する必要があります。

関数ツール、MCP ツール、ハンドオフ全体に 1 つのアプリケーションポリシーを適用するパターンについては、[コンテキスト管理](context.md#use-local-context-for-capability-visibility)を参照してください。

## 実験的機能：Codex ツール {#experimental-codex-tool}

`codex_tool` は Codex CLI をラップし、ツール呼び出し中にエージェントがワークスペースにスコープされたタスク（シェル、ファイル編集、MCP ツール）を実行できるようにします。このインターフェースは実験的機能であり、変更される可能性があります。

メインエージェントから、現在の実行を離れることなく、範囲の限定されたワークスペースタスクを Codex に委任したい場合に使用します。デフォルトのツール名は `codex` です。カスタム名を設定する場合は、`codex` であるか、`codex_` で始まる必要があります。エージェントに複数の Codex ツールを含める場合、それぞれに一意の名前を使用する必要があります。

```python
from agents import Agent
from agents.extensions.experimental.codex import ThreadOptions, TurnOptions, codex_tool

agent = Agent(
    name="Codex Agent",
    instructions="Use the codex tool to inspect the workspace and answer the question.",
    tools=[
        codex_tool(
            sandbox_mode="workspace-write",
            working_directory="/path/to/repo",
            default_thread_options=ThreadOptions(
                model="gpt-5.5",
                model_reasoning_effort="low",
                network_access_enabled=True,
                web_search_mode="disabled",
                approval_policy="never",
            ),
            default_turn_options=TurnOptions(
                idle_timeout_seconds=60,
            ),
            persist_session=True,
        )
    ],
)
```

まず、次のオプショングループを使用します。

-   実行対象：`sandbox_mode` と `working_directory` は、Codex が操作できる場所を定義します。これらは一緒に指定し、作業ディレクトリが Git リポジトリ内にない場合は `skip_git_repo_check=True` を設定します。
-   スレッドのデフォルト：`default_thread_options=ThreadOptions(...)` は、モデル、推論の労力、承認ポリシー、追加ディレクトリ、ネットワークアクセス、Web 検索モードを設定します。従来の `web_search_enabled` よりも `web_search_mode` を優先してください。
-   ターンのデフォルト：`default_turn_options=TurnOptions(...)` は、`idle_timeout_seconds` や任意指定のキャンセル用 `signal` など、ターンごとの動作を設定します。
-   ツール I/O：ツール呼び出しには、`{ "type": "text", "text": ... }` または `{ "type": "local_image", "path": ... }` を持つ `inputs` アイテムを少なくとも 1 つ含める必要があります。`output_schema` を使用すると、構造化された Codex レスポンスを必須にできます。

スレッドの再利用と永続化は別々に制御されます。

-   `persist_session=True` は、同じツールインスタンスへの繰り返し呼び出しで 1 つの Codex スレッドを再利用します。
-   `use_run_context_thread_id=True` は、同じ変更可能なコンテキストオブジェクトを共有する複数の実行にわたって、スレッド ID を実行コンテキストに保存して再利用します。
-   スレッド ID の優先順位は、呼び出しごとの `thread_id`、有効な場合は実行コンテキストのスレッド ID、設定済みの `thread_id` オプションの順です。
-   デフォルトの実行コンテキストキーは、`name="codex"` では `codex_thread_id`、`name="codex_<suffix>"` では `codex_thread_id_<suffix>` です。`run_context_thread_id_key` で上書きできます。

ランタイム設定：

-   認証：`CODEX_API_KEY`（推奨）または `OPENAI_API_KEY` を設定するか、`codex_options={"api_key": "..."}` を渡します。
-   ランタイム：`codex_options.base_url` は CLI のベース URL を上書きします。
-   バイナリ解決：CLI のパスを固定するには、`codex_options.codex_path_override`（または `CODEX_PATH`）を設定します。設定しない場合、SDK は `PATH` から `codex` を解決し、見つからなければバンドルされたベンダーバイナリを使用します。
-   環境：`codex_options.env` は、サブプロセス環境を完全に制御します。これを指定すると、サブプロセスは `os.environ` を継承しません。
-   ストリーム制限：`codex_options.codex_subprocess_stream_limit_bytes`（または `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`）は、stdout／stderr リーダーの制限を制御します。有効な範囲は `65536` から `67108864` で、デフォルトは `8388608` です。
-   ストリーミング：`on_stream` は、スレッド／ターンのライフサイクルイベントとアイテムイベント（`reasoning`、`command_execution`、`mcp_tool_call`、`file_change`、`web_search`、`todo_list`、`error` アイテムの更新）を受信します。
-   出力：実行結果には `response`、`usage`、`thread_id` が含まれ、使用量は `RunContextWrapper.usage` に追加されます。

リファレンス：

-   [Codex ツール API リファレンス](ref/extensions/experimental/codex/codex_tool.md)
-   [ThreadOptions リファレンス](ref/extensions/experimental/codex/thread_options.md)
-   [TurnOptions リファレンス](ref/extensions/experimental/codex/turn_options.md)
-   完全に実行可能なサンプルについては、`examples/tools/codex.py` と `examples/tools/codex_same_thread.py` を参照してください。