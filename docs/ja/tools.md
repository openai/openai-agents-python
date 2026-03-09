---
search:
  exclude: true
---
# ツール

ツールを使うと、エージェントはアクションを実行できます。たとえば、データ取得、コード実行、外部 API 呼び出し、さらにはコンピュータ操作などです。 SDK は 5 つのカテゴリーをサポートしています。

-   OpenAI がホストするツール: OpenAI サーバー上でモデルと並行して実行されます。
-   ローカル / ランタイム実行ツール: `ComputerTool` と `ApplyPatchTool` は常にあなたの環境で実行され、`ShellTool` はローカルまたはホストコンテナで実行できます。
-   Function Calling: 任意の Python 関数をツールとしてラップします。
-   Agents as tools: 完全なハンドオフなしで、エージェントを呼び出し可能なツールとして公開します。
-   Experimental: Codex tool: ツール呼び出しから、ワークスペーススコープの Codex タスクを実行します。

## ツールタイプの選択

このページをカタログとして使い、次に自分が制御するランタイムに合うセクションへ進んでください。

| 次をしたい場合... | ここから開始 |
| --- | --- |
| OpenAI 管理ツールを使う ( Web 検索、ファイル検索、Code Interpreter、ホスト型 MCP、画像生成 ) | [Hosted tools](#hosted-tools) |
| ツール検索で、実行時まで大規模なツール面を遅延させる | [Hosted tool search](#hosted-tool-search) |
| 自分のプロセスまたは環境でツールを実行する | [Local runtime tools](#local-runtime-tools) |
| Python 関数をツールとしてラップする | [Function tools](#function-tools) |
| ハンドオフなしで、あるエージェントから別のエージェントを呼ぶ | [Agents as tools](#agents-as-tools) |
| エージェントからワークスペーススコープの Codex タスクを実行する | [Experimental: Codex tool](#experimental-codex-tool) |

## Hosted tools

[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する場合、 OpenAI はいくつかの組み込みツールを提供しています。

-   [`WebSearchTool`][agents.tool.WebSearchTool] は、エージェントが Web 検索を行えるようにします。
-   [`FileSearchTool`][agents.tool.FileSearchTool] は、 OpenAI ベクトルストアから情報を取得できるようにします。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] は、 LLM がサンドボックス環境でコードを実行できるようにします。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] は、リモート MCP サーバーのツールをモデルに公開します。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] は、プロンプトから画像を生成します。
-   [`ToolSearchTool`][agents.tool.ToolSearchTool] は、モデルが必要に応じて遅延ツール、名前空間、またはホスト MCP サーバーを読み込めるようにします。

高度なホスト検索オプション:

-   `FileSearchTool` は、`vector_store_ids` と `max_num_results` に加えて、`filters`、`ranking_options`、`include_search_results` をサポートします。
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

### Hosted tool search

ツール検索により、 OpenAI Responses モデルは大規模なツール面を実行時まで遅延できるため、モデルは現在のターンに必要なサブセットだけを読み込みます。これは、多数の関数ツール、名前空間グループ、またはホスト MCP サーバーがあり、すべてのツールを事前公開せずにツールスキーマのトークンを削減したい場合に有用です。

候補ツールがエージェント構築時に既知である場合は、 hosted tool search から開始してください。アプリケーションが動的に読み込む対象を判断する必要がある場合、 Responses API はクライアント実行のツール検索もサポートしますが、標準の `Runner` はそのモードを自動実行しません。

```python
from typing import Annotated

from agents import Agent, Runner, ToolSearchTool, function_tool, tool_namespace


@function_tool(defer_loading=True)
def get_customer_profile(
    customer_id: Annotated[str, "The customer ID to look up."],
) -> str:
    """Fetch a CRM customer profile."""
    return f"profile for {customer_id}"


@function_tool(defer_loading=True)
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
    model="gpt-5.4",
    instructions="Load the crm namespace before using CRM tools.",
    tools=[*crm_tools, ToolSearchTool()],
)

result = await Runner.run(agent, "Look up customer_42 and list their open orders.")
print(result.final_output)
```

知っておくべき点:

-   Hosted tool search は OpenAI Responses モデルでのみ利用可能です。現在の Python SDK サポートは `openai>=2.25.0` に依存します。
-   エージェントで遅延読み込み面を設定する場合は、`ToolSearchTool()` を正確に 1 つ追加してください。
-   検索可能な面には、`@function_tool(defer_loading=True)`、`tool_namespace(name=..., description=..., tools=[...])`、`HostedMCPTool(tool_config={..., "defer_loading": True})` が含まれます。
-   遅延読み込み関数ツールは `ToolSearchTool()` と組み合わせる必要があります。名前空間のみの構成でも、モデルが必要時に適切なグループを読み込めるよう `ToolSearchTool()` を使用できます。
-   `tool_namespace()` は、`FunctionTool` インスタンスを共有の名前空間名と説明の下にグループ化します。これは通常、`crm`、`billing`、`shipping` のように関連ツールが多い場合に最適です。
-   OpenAI の公式ベストプラクティスガイドは [Use namespaces where possible](https://developers.openai.com/api/docs/guides/tools-tool-search#use-namespaces-where-possible) です。
-   可能な場合は、多数の個別遅延関数よりも名前空間またはホスト MCP サーバーを優先してください。通常、モデルにとってより良い高レベル検索面と、より高いトークン削減効果が得られます。
-   名前空間には即時ツールと遅延ツールを混在できます。`defer_loading=True` がないツールは即時呼び出し可能なままで、同じ名前空間内の遅延ツールはツール検索経由で読み込まれます。
-   目安として、各名前空間は比較的小さく保ち、理想的には 10 関数未満にしてください。
-   名前付き `tool_choice` は、裸の名前空間名や遅延専用ツールを対象にできません。`auto`、`required`、または実在するトップレベル呼び出し可能ツール名を優先してください。
-   `ToolSearchTool(execution="client")` は手動 Responses オーケストレーション用です。モデルがクライアント実行の `tool_search_call` を出力した場合、標準 `Runner` はあなたの代わりに実行せずエラーにします。
-   ツール検索アクティビティは [`RunResult.new_items`](results.md#new-items) と、専用のアイテム / イベント型を持つ [`RunItemStreamEvent`](streaming.md#run-item-event-names) に表示されます。
-   名前空間読み込みとトップレベル遅延ツールの両方を網羅した実行可能な完全例は `examples/tools/tool_search.py` を参照してください。
-   公式プラットフォームガイド: [Tool search](https://developers.openai.com/api/docs/guides/tools-tool-search)。

### ホストコンテナ shell + skills

`ShellTool` は OpenAI ホストコンテナ実行もサポートします。モデルにローカルランタイムではなく管理コンテナで shell コマンドを実行させたい場合は、このモードを使用してください。

```python
from agents import Agent, Runner, ShellTool, ShellToolSkillReference

csv_skill: ShellToolSkillReference = {
    "type": "skill_reference",
    "skill_id": "skill_698bbe879adc81918725cbc69dcae7960bc5613dadaed377",
    "version": "1",
}

agent = Agent(
    name="Container shell agent",
    model="gpt-5.4",
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

後続の run で既存コンテナを再利用するには、`environment={"type": "container_reference", "container_id": "cntr_..."}` を設定します。

知っておくべき点:

-   ホスト shell は Responses API の shell ツール経由で利用可能です。
-   `container_auto` はリクエスト用にコンテナをプロビジョニングし、`container_reference` は既存コンテナを再利用します。
-   `container_auto` には `file_ids` と `memory_limit` も含められます。
-   `environment.skills` は skill 参照とインライン skill バンドルを受け付けます。
-   ホスト環境では、`ShellTool` に `executor`、`needs_approval`、`on_approval` を設定しないでください。
-   `network_policy` は `disabled` と `allowlist` モードをサポートします。
-   allowlist モードでは、`network_policy.domain_secrets` でドメインスコープのシークレットを名前で注入できます。
-   完全な例は `examples/tools/container_shell_skill_reference.py` と `examples/tools/container_shell_inline_skill.py` を参照してください。
-   OpenAI プラットフォームガイド: [Shell](https://platform.openai.com/docs/guides/tools-shell) と [Skills](https://platform.openai.com/docs/guides/tools-skills)。

## ローカルランタイムツール

ローカルランタイムツールは、モデル応答自体の外側で実行されます。モデルはいつ呼び出すかを決定しますが、実際の処理はアプリケーションまたは設定済み実行環境が行います。

`ComputerTool` と `ApplyPatchTool` は常に、あなたが提供するローカル実装を必要とします。`ShellTool` は両モードにまたがります。管理実行が必要なら上記ホストコンテナ構成を使い、自分のプロセスでコマンドを実行したいなら以下のローカルランタイム構成を使ってください。

ローカルランタイムツールでは実装の提供が必要です:

-   [`ComputerTool`][agents.tool.ComputerTool]: GUI / ブラウザ自動化を有効にするには [`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] インターフェースを実装します。
-   [`ShellTool`][agents.tool.ShellTool]: ローカル実行とホストコンテナ実行の両方に対応する最新 shell ツールです。
-   [`LocalShellTool`][agents.tool.LocalShellTool]: レガシーのローカル shell 統合です。
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]: 差分をローカル適用するには [`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] を実装します。
-   ローカル shell skills は `ShellTool(environment={"type": "local", "skills": [...]})` で利用できます。

### ComputerTool と Responses computer tool

`ComputerTool` は依然としてローカルハーネスです。あなたが [`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] 実装を提供し、 SDK がそのハーネスを OpenAI Responses API の computer 面にマッピングします。

明示的な [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) リクエストでは、 SDK は GA 組み込みツールペイロード `{"type": "computer"}` を送信します。古い `computer-use-preview` モデルでは、プレビュー用ペイロード `{"type": "computer_use_preview", "environment": ..., "display_width": ..., "display_height": ...}` を維持します。これは OpenAI の [Computer use guide](https://developers.openai.com/api/docs/guides/tools-computer-use/) で説明されているプラットフォーム移行を反映しています。

-   モデル: `computer-use-preview` -> `gpt-5.4`
-   ツールセレクター: `computer_use_preview` -> `computer`
-   Computer 呼び出し形状: `computer_call` あたり 1 つの `action` -> `computer_call` 上のバッチ `actions[]`
-   Truncation: プレビューパスでは `ModelSettings(truncation="auto")` が必須 -> GA パスでは不要

SDK は、実際の Responses リクエスト上の有効モデルから wire 形状を選択します。プロンプトテンプレートを使い、プロンプト側が `model` を所有するためリクエストに `model` がない場合、`model="gpt-5.4"` を明示するか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制しない限り、 SDK はプレビュー互換 computer ペイロードを維持します。

[`ComputerTool`][agents.tool.ComputerTool] が存在する場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` はすべて受け入れられ、有効リクエストモデルに一致する組み込みセレクターへ正規化されます。`ComputerTool` がない場合、これらの文字列は通常の関数名として動作します。

この違いは、`ComputerTool` が [`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーに支えられている場合に重要です。GA の `computer` ペイロードはシリアライズ時に `environment` や寸法を必要としないため、未解決ファクトリーでも問題ありません。プレビュー互換シリアライズでは、 SDK が `environment`、`display_width`、`display_height` を送るため、解決済みの `Computer` または `AsyncComputer` インスタンスが依然必要です。

実行時は、どちらのパスも同じローカルハーネスを使います。プレビュー応答は単一 `action` の `computer_call` アイテムを出力し、`gpt-5.4` はバッチ `actions[]` を出力でき、 SDK は `computer_call_output` スクリーンショットアイテムを生成する前に順番に実行します。実行可能な Playwright ベースのハーネスは `examples/tools/computer_use.py` を参照してください。

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

## 関数ツール

任意の Python 関数をツールとして使えます。 Agents SDK が自動的にツールを設定します。

-   ツール名は Python 関数名になります (または名前を提供できます)
-   ツール説明は関数の docstring から取得されます (または説明を提供できます)
-   関数入力のスキーマは、関数引数から自動生成されます
-   各入力の説明は、無効化しない限り関数の docstring から取得されます

関数シグネチャ抽出には Python の `inspect` モジュールを使用し、docstring 解析には [`griffe`](https://mkdocstrings.github.io/griffe/)、スキーマ作成には `pydantic` を使用します。

OpenAI Responses モデルを使用している場合、`@function_tool(defer_loading=True)` は `ToolSearchTool()` が読み込むまで関数ツールを非表示にします。[`tool_namespace()`][agents.tool.tool_namespace] で関連関数ツールをグループ化することもできます。完全な設定と制約は [Hosted tool search](#hosted-tool-search) を参照してください。

```python
import json

from typing_extensions import TypedDict, Any

from agents import Agent, FunctionTool, RunContextWrapper, function_tool


class Location(TypedDict):
    lat: float
    long: float

@function_tool  # (1)!
async def fetch_weather(location: Location) -> str:
    # (2)!
    """Fetch the weather for a given location.

    Args:
        location: The location to fetch the weather for.
    """
    # In real life, we'd fetch the weather from a weather API
    return "sunny"


@function_tool(name_override="fetch_data")  # (3)!
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

1.  関数引数には任意の Python 型を使用でき、関数は sync / async どちらでも構いません。
2.  docstring がある場合、説明と引数説明の取得に使用されます。
3.  関数は任意で `context` を受け取れます (最初の引数である必要があります)。ツール名、説明、使用する docstring スタイルなどのオーバーライドも設定できます。
4.  デコレートした関数をツールリストに渡せます。

??? note "出力を表示"

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

### 関数ツールからの画像またはファイルの返却

テキスト出力の返却に加えて、関数ツールの出力として 1 つ以上の画像またはファイルを返せます。そのためには、次のいずれかを返します。

-   画像: [`ToolOutputImage`][agents.tool.ToolOutputImage] (または TypedDict 版の [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict])
-   ファイル: [`ToolOutputFileContent`][agents.tool.ToolOutputFileContent] (または TypedDict 版の [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict])
-   テキスト: 文字列、文字列化可能オブジェクト、または [`ToolOutputText`][agents.tool.ToolOutputText] (または TypedDict 版の [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict])

### カスタム関数ツール

場合によっては、 Python 関数をツールとして使いたくないことがあります。その場合は、必要に応じて [`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。必要なものは次のとおりです。

-   `name`
-   `description`
-   `params_json_schema` (引数の JSON スキーマ)
-   `on_invoke_tool` ( [`ToolContext`][agents.tool_context.ToolContext] と JSON 文字列としての引数を受け取り、ツール出力 (たとえばテキスト、構造化ツール出力オブジェクト、または出力リスト) を返す async 関数)

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

### 引数と docstring の自動解析

前述のとおり、ツール用スキーマ抽出のために関数シグネチャを自動解析し、ツール説明と個別引数説明抽出のために docstring を解析します。注意点は次のとおりです。

1. シグネチャ解析は `inspect` モジュールで行います。引数型の理解には型アノテーションを使い、全体スキーマを表す Pydantic モデルを動的に構築します。 Python プリミティブ、Pydantic モデル、TypedDict などを含む、ほとんどの型をサポートします。
2. docstring 解析には `griffe` を使用します。サポートされる docstring 形式は `google`、`sphinx`、`numpy` です。docstring 形式は自動検出を試みますが、これはベストエフォートであり、`function_tool` 呼び出し時に明示設定できます。`use_docstring_info` を `False` に設定して docstring 解析を無効化することもできます。

スキーマ抽出コードは [`agents.function_schema`][] にあります。

### Pydantic Field による引数制約と説明

Pydantic の [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) を使うと、ツール引数に制約 (例: 数値の最小 / 最大、文字列の長さやパターン) と説明を追加できます。Pydantic と同様に、デフォルトベース (`arg: int = Field(..., ge=1)`) と `Annotated` (`arg: Annotated[int, Field(..., ge=1)]`) の両形式をサポートします。生成される JSON スキーマとバリデーションには、これらの制約が含まれます。

```python
from typing import Annotated
from pydantic import Field
from agents import function_tool

# Default-based form
@function_tool
def score_a(score: int = Field(..., ge=0, le=100, description="Score from 0 to 100")) -> str:
    return f"Score recorded: {score}"

# Annotated form
@function_tool
def score_b(score: Annotated[int, Field(..., ge=0, le=100, description="Score from 0 to 100")]) -> str:
    return f"Score recorded: {score}"
```

### 関数ツールのタイムアウト

async 関数ツールには、`@function_tool(timeout=...)` で呼び出しごとのタイムアウトを設定できます。

```python
import asyncio
from agents import Agent, Runner, function_tool


@function_tool(timeout=2.0)
async def slow_lookup(query: str) -> str:
    await asyncio.sleep(10)
    return f"Result for {query}"


agent = Agent(
    name="Timeout demo",
    instructions="Use tools when helpful.",
    tools=[slow_lookup],
)
```

タイムアウトに達した場合、デフォルト動作は `timeout_behavior="error_as_result"` で、モデル可視のタイムアウトメッセージ (例: `Tool 'slow_lookup' timed out after 2 seconds.`) を送信します。

タイムアウト処理は次のように制御できます。

-   `timeout_behavior="error_as_result"` (デフォルト): タイムアウトメッセージをモデルに返し、復旧できるようにします。
-   `timeout_behavior="raise_exception"`: [`ToolTimeoutError`][agents.exceptions.ToolTimeoutError] を発生させ、 run を失敗させます。
-   `timeout_error_function=...`: `error_as_result` 使用時のタイムアウトメッセージをカスタマイズします。

```python
import asyncio
from agents import Agent, Runner, ToolTimeoutError, function_tool


@function_tool(timeout=1.5, timeout_behavior="raise_exception")
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

    タイムアウト設定は async `@function_tool` ハンドラーでのみサポートされます。

### 関数ツールでのエラー処理

`@function_tool` で関数ツールを作成する際、`failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュしたときに LLM へ返すエラー応答を提供する関数です。

-   デフォルト (何も渡さない場合) では、エラー発生を LLM に伝える `default_tool_error_function` が実行されます。
-   独自のエラー関数を渡すと、代わりにそれが実行され、その応答が LLM に送られます。
-   明示的に `None` を渡すと、ツール呼び出しエラーはあなたが処理できるよう再送出されます。これはモデルが無効 JSON を生成した場合の `ModelBehaviorError` や、コードがクラッシュした場合の `UserError` などです。

```python
from agents import function_tool, RunContextWrapper
from typing import Any

def my_custom_error_function(context: RunContextWrapper[Any], error: Exception) -> str:
    """A custom function to provide a user-friendly error message."""
    print(f"A tool call failed with the following error: {error}")
    return "An internal server error occurred. Please try again later."

@function_tool(failure_error_function=my_custom_error_function)
def get_user_profile(user_id: str) -> str:
    """Fetches a user profile from a mock API.
     This function demonstrates a 'flaky' or failing API call.
    """
    if user_id == "user_123":
        return "User profile for user_123 successfully retrieved."
    else:
        raise ValueError(f"Could not retrieve profile for user_id: {user_id}. API returned an error.")

```

`FunctionTool` オブジェクトを手動作成する場合は、`on_invoke_tool` 関数内でエラーを処理する必要があります。

## Agents as tools

一部のワークフローでは、制御をハンドオフする代わりに、中央エージェントで専門エージェントのネットワークをエージェントオーケストレーションしたい場合があります。これは、エージェントをツールとしてモデル化することで実現できます。

```python
from agents import Agent, Runner
import asyncio

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
        "You are a translation agent. You use the tools given to you to translate."
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
```

### ツールエージェントのカスタマイズ

`agent.as_tool` 関数は、エージェントをツールに変換しやすくするための便利メソッドです。`max_turns`、`run_config`、`hooks`、`previous_response_id`、`conversation_id`、`session`、`needs_approval` などの一般的なランタイムオプションをサポートします。さらに、`parameters`、`input_builder`、`include_input_schema` による構造化入力もサポートします。高度なオーケストレーション (例: 条件付きリトライ、フォールバック動作、複数エージェント呼び出しの連鎖) では、ツール実装内で `Runner.run` を直接使用してください。

```python
@function_tool
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

### ツールエージェントの構造化入力

デフォルトでは、`Agent.as_tool()` は単一文字列入力 (`{"input": "..."}`) を想定しますが、`parameters` (Pydantic モデルまたは dataclass 型) を渡すことで構造化スキーマを公開できます。

追加オプション:

- `include_input_schema=True` は、生成されるネスト入力に完全な JSON Schema を含めます。
- `input_builder=...` は、構造化ツール引数をネストエージェント入力に変換する方法を完全にカスタマイズできます。
- `RunContextWrapper.tool_input` は、ネスト run コンテキスト内に解析済み構造化ペイロードを保持します。

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

完全に実行可能な例は `examples/agent_patterns/agents_as_tools_structured.py` を参照してください。

### ツールエージェントの承認ゲート

`Agent.as_tool(..., needs_approval=...)` は `function_tool` と同じ承認フローを使用します。承認が必要な場合、 run は一時停止し、保留中アイテムは `result.interruptions` に表示されます。次に `result.to_state()` を使用し、`state.approve(...)` または `state.reject(...)` 呼び出し後に再開します。完全な一時停止 / 再開パターンは [Human-in-the-loop guide](human_in_the_loop.md) を参照してください。

### カスタム出力抽出

特定のケースでは、中央エージェントに返す前にツールエージェントの出力を変更したいことがあります。これは次のような場合に有用です。

-   サブエージェントのチャット履歴から特定情報 (例: JSON ペイロード) を抽出する。
-   エージェントの最終回答を変換または再整形する (例: Markdown をプレーンテキストや CSV に変換)。
-   出力を検証する、またはエージェント応答が欠落 / 不正形式の場合にフォールバック値を提供する。

これは、`as_tool` メソッドに `custom_output_extractor` 引数を渡すことで実現できます。

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

カスタム抽出器内では、ネストされた [`RunResult`][agents.result.RunResult] は
[`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] も公開します。これは
ネスト結果の後処理中に、外側ツール名、呼び出し ID、または raw 引数が必要な場合に有用です。
[Results guide](results.md#agent-as-tool-metadata) も参照してください。

### ネストされたエージェント run のストリーミング

`as_tool` に `on_stream` コールバックを渡すと、ストリーム完了後に最終出力を返しつつ、ネストエージェントが出力するストリーミングイベントを監視できます。

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

想定される挙動:

- イベント型は `StreamEvent["type"]` を反映します: `raw_response_event`、`run_item_stream_event`、`agent_updated_stream_event`。
- `on_stream` を提供すると、ネストエージェントは自動的にストリーミングモードで実行され、最終出力返却前にストリームがドレインされます。
- ハンドラーは同期または非同期にでき、各イベントは到着順で配信されます。
- `tool_call` は、モデルのツール呼び出し経由でツールが呼ばれた場合に存在します。直接呼び出しでは `None` のままの場合があります。
- 完全に実行可能なサンプルは `examples/agent_patterns/agents_as_tools_streaming.py` を参照してください。

### 条件付きツール有効化

`is_enabled` パラメーターを使うと、実行時にエージェントツールを条件付きで有効 / 無効にできます。これにより、コンテキスト、ユーザー設定、またはランタイム条件に基づいて、 LLM が利用可能なツールを動的にフィルタリングできます。

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
    context = RunContextWrapper(LanguageContext(language_preference="french_spanish"))
    result = await Runner.run(orchestrator, "How are you?", context=context.context)
    print(result.final_output)

asyncio.run(main())
```

`is_enabled` パラメーターは次を受け付けます。

-   **ブール値**: `True` (常に有効) または `False` (常に無効)
-   **呼び出し可能関数**: `(context, agent)` を受け取りブール値を返す関数
-   **非同期関数**: 複雑な条件ロジック向けの async 関数

無効化されたツールは実行時に LLM から完全に隠されるため、次の用途に有効です。

-   ユーザー権限に基づく機能ゲート
-   環境別ツール可用性 ( dev vs prod )
-   異なるツール構成の A/B テスト
-   ランタイム状態に基づく動的ツールフィルタリング

## Experimental: Codex tool

`codex_tool` は Codex CLI をラップし、エージェントがツール呼び出し中にワークスペーススコープのタスク ( shell、ファイル編集、 MCP ツール ) を実行できるようにします。この面は実験的であり、変更される可能性があります。

現在の run を離れずに、メインエージェントから Codex に境界付きワークスペースタスクを委譲したい場合に使用します。デフォルトのツール名は `codex` です。カスタム名を設定する場合、それは `codex` であるか `codex_` で始まる必要があります。エージェントに複数の Codex ツールがある場合、それぞれが一意名である必要があります。

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
                model="gpt-5.4",
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

まず次のオプショングループから始めてください。

-   実行面: `sandbox_mode` と `working_directory` は Codex が操作できる場所を定義します。これらは組み合わせて設定し、作業ディレクトリが Git リポジトリ内にない場合は `skip_git_repo_check=True` を設定してください。
-   スレッドデフォルト: `default_thread_options=ThreadOptions(...)` は、モデル、推論努力、承認ポリシー、追加ディレクトリ、ネットワークアクセス、 Web 検索モードを設定します。レガシーの `web_search_enabled` より `web_search_mode` を優先してください。
-   ターンデフォルト: `default_turn_options=TurnOptions(...)` は、`idle_timeout_seconds` や任意のキャンセル `signal` など、ターンごとの動作を設定します。
-   ツール I/O: ツール呼び出しには、`{ "type": "text", "text": ... }` または `{ "type": "local_image", "path": ... }` を持つ `inputs` アイテムを少なくとも 1 つ含める必要があります。`output_schema` により構造化 Codex 応答を必須にできます。

スレッド再利用と永続化は別々の制御です。

-   `persist_session=True` は、同一ツールインスタンスへの繰り返し呼び出しで 1 つの Codex スレッドを再利用します。
-   `use_run_context_thread_id=True` は、同じ可変コンテキストオブジェクトを共有する run 間で、 run コンテキスト内にスレッド ID を保存して再利用します。
-   スレッド ID の優先順位は、呼び出しごとの `thread_id`、次に ( 有効時 ) run-context スレッド ID、次に設定済み `thread_id` オプションです。
-   デフォルト run-context キーは、`name="codex"` では `codex_thread_id`、`name="codex_<suffix>"` では `codex_thread_id_<suffix>` です。`run_context_thread_id_key` で上書きできます。

ランタイム設定:

-   認証: `CODEX_API_KEY` (推奨) または `OPENAI_API_KEY` を設定するか、`codex_options={"api_key": "..."}` を渡します。
-   ランタイム: `codex_options.base_url` は CLI の base URL を上書きします。
-   バイナリ解決: CLI パスを固定するには `codex_options.codex_path_override` (または `CODEX_PATH`) を設定します。設定しない場合、 SDK は `PATH` から `codex` を解決し、その後バンドル済み vendor バイナリへフォールバックします。
-   環境: `codex_options.env` はサブプロセス環境を完全に制御します。これを指定すると、サブプロセスは `os.environ` を継承しません。
-   ストリーム制限: `codex_options.codex_subprocess_stream_limit_bytes` (または `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`) は stdout / stderr リーダー制限を制御します。有効範囲は `65536` から `67108864`、デフォルトは `8388608` です。
-   ストリーミング: `on_stream` はスレッド / ターンのライフサイクルイベントとアイテムイベント (`reasoning`、`command_execution`、`mcp_tool_call`、`file_change`、`web_search`、`todo_list`、`error` のアイテム更新) を受け取ります。
-   出力: 結果には `response`、`usage`、`thread_id` が含まれます。usage は `RunContextWrapper.usage` に追加されます。

参照:

-   [Codex tool API reference](ref/extensions/experimental/codex/codex_tool.md)
-   [ThreadOptions reference](ref/extensions/experimental/codex/thread_options.md)
-   [TurnOptions reference](ref/extensions/experimental/codex/turn_options.md)
-   完全に実行可能なサンプルは `examples/tools/codex.py` と `examples/tools/codex_same_thread.py` を参照してください。