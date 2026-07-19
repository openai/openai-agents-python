---
search:
  exclude: true
---
# ツール

ツールを使用すると、データの取得、コードの実行、外部 API の呼び出し、さらにはコンピュータ操作など、エージェントがさまざまなアクションを実行できます。SDK は、次の 5 つのカテゴリーをサポートしています。

-   OpenAI がホストするツール：OpenAI のサーバー上でモデルとともに実行されます。
-   ローカル／ランタイム実行ツール：`ComputerTool` と `ApplyPatchTool` は常にユーザーの環境で実行され、`ShellTool` はローカルまたはホスト型コンテナで実行できます。
-   Function Calling：任意の Python 関数をツールとしてラップします。
-   Agents as tools：完全なハンドオフを行わずに、エージェントを呼び出し可能なツールとして公開します。
-   実験的機能：Codex ツール：ツール呼び出しから、ワークスペースにスコープされた Codex タスクを実行します。

## ツールタイプの選択

このページをカタログとして使用し、制御するランタイムに該当するセクションへ移動してください。

| 目的 | 参照先 |
| --- | --- |
| OpenAI が管理するツール（Web 検索、ファイル検索、Code Interpreter、ホスト型 MCP、画像生成）を使用する | [ホスト型ツール](#hosted-tools) |
| ツール検索を使用して、大規模なツール群の読み込みをランタイムまで延期する | [ホスト型ツール検索](#hosted-tool-search) |
| 独自のプロセスまたは環境でツールを実行する | [ローカルランタイムツール](#local-runtime-tools) |
| Python 関数をツールとしてラップする | [関数ツール](#function-tools) |
| ハンドオフを行わず、あるエージェントから別のエージェントを呼び出す | [Agents as tools](#agents-as-tools) |
| エージェントから、ワークスペースにスコープされた Codex タスクを実行する | [実験的機能：Codex ツール](#experimental-codex-tool) |

## ホスト型ツール

[`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] を使用する場合、OpenAI はいくつかの組み込みツールを提供します。

-   [`WebSearchTool`][agents.tool.WebSearchTool] を使用すると、エージェントは Web を検索できます。
-   [`FileSearchTool`][agents.tool.FileSearchTool] を使用すると、OpenAI のベクトルストアから情報を取得できます。
-   [`CodeInterpreterTool`][agents.tool.CodeInterpreterTool] を使用すると、LLM はサンドボックス環境でコードを実行できます。
-   [`HostedMCPTool`][agents.tool.HostedMCPTool] は、リモート MCP サーバーのツールをモデルに公開します。
-   [`ImageGenerationTool`][agents.tool.ImageGenerationTool] は、プロンプトから画像を生成します。
-   [`ToolSearchTool`][agents.tool.ToolSearchTool] を使用すると、モデルは遅延読み込みされるツール、名前空間、またはホスト型 MCP サーバーをオンデマンドで読み込めます。

ホスト型検索の高度なオプション：

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

### ホスト型ツール検索

ツール検索を使用すると、OpenAI Responses モデルは大規模なツール群の読み込みをランタイムまで延期できるため、現在のターンに必要なサブセットのみをモデルが読み込みます。多数の関数ツール、名前空間グループ、またはホスト型 MCP サーバーがあり、すべてのツールを事前に公開せずにツールスキーマのトークンを削減したい場合に便利です。

エージェントを構築する時点で候補ツールがすでに判明している場合は、ホスト型ツール検索から始めてください。アプリケーション側で読み込む内容を動的に決定する必要がある場合、Responses API はクライアント実行型のツール検索もサポートしていますが、標準の `Runner` はそのモードを自動実行しません。

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
    model="gpt-5.6-sol",
    instructions="Load the crm namespace before using CRM tools.",
    tools=[*crm_tools, ToolSearchTool()],
)

result = await Runner.run(agent, "Look up customer_42 and list their open orders.")
print(result.final_output)
```

注意事項：

-   ホスト型ツール検索は、OpenAI Responses モデルでのみ利用できます。現在の Python SDK のサポートは `openai>=2.25.0` に依存します。
-   エージェントに遅延読み込み対象を設定する場合は、`ToolSearchTool()` を 1 つだけ追加してください。
-   検索可能な対象には、`@function_tool(defer_loading=True)`、`tool_namespace(name=..., description=..., tools=[...])`、`HostedMCPTool(tool_config={..., "defer_loading": True})` が含まれます。
-   遅延読み込みされる関数ツールは、`ToolSearchTool()` と組み合わせる必要があります。名前空間のみの構成でも `ToolSearchTool()` を使用すると、モデルが適切なグループをオンデマンドで読み込めます。
-   `tool_namespace()` は、共有の名前空間名と説明の下に `FunctionTool` インスタンスをまとめます。通常、`crm`、`billing`、`shipping` など、関連するツールが多数ある場合に最適です。
-   OpenAI の公式ベストプラクティスガイダンスは、[可能な限り名前空間を使用する](https://developers.openai.com/api/docs/guides/tools-tool-search#use-namespaces-where-possible)ことです。
-   可能であれば、個別に遅延読み込みされる関数を多数使用するのではなく、名前空間またはホスト型 MCP サーバーを優先してください。通常、その方がモデルにとって優れた高レベルの検索対象となり、トークンをより効果的に節約できます。
-   名前空間では、即時利用可能なツールと遅延読み込みされるツールを混在させられます。`defer_loading=True` が指定されていないツールは即座に呼び出し可能なままであり、同じ名前空間内の遅延ツールはツール検索を通じて読み込まれます。
-   目安として、各名前空間は比較的小さく保ち、関数を 10 個未満にすることが理想的です。
-   名前付きの `tool_choice` では、単独の名前空間名や遅延読み込み専用ツールを対象にできません。`auto`、`required`、または実際にトップレベルで呼び出し可能なツール名を使用してください。
-   `ToolSearchTool(execution="client")` は、Responses を手動でオーケストレーションするためのものです。モデルがクライアント実行型の `tool_search_call` を出力した場合、標準の `Runner` はそれを実行せずに例外を発生させます。
-   ツール検索のアクティビティは、専用の項目型およびイベント型として [`RunResult.new_items`](results.md#new-items) と [`RunItemStreamEvent`](streaming.md#run-item-event-names) に表示されます。
-   名前空間による読み込みとトップレベルの遅延ツールの両方を扱う、実行可能な完全なコード例については、`examples/tools/tool_search.py` を参照してください。
-   公式プラットフォームガイド：[ツール検索](https://developers.openai.com/api/docs/guides/tools-tool-search)。

### ホスト型コンテナシェルとスキル

`ShellTool` は、OpenAI がホストするコンテナでの実行もサポートしています。ローカルランタイムではなく、管理されたコンテナ内でモデルにシェルコマンドを実行させたい場合は、このモードを使用してください。

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

後続の実行で既存のコンテナを再利用するには、`environment={"type": "container_reference", "container_id": "cntr_..."}` を設定します。

注意事項：

-   ホスト型シェルは、Responses API のシェルツールを通じて利用できます。
-   `container_auto` はリクエスト用のコンテナをプロビジョニングし、`container_reference` は既存のコンテナを再利用します。
-   `container_auto` には、`file_ids` と `memory_limit` も指定できます。
-   `environment.skills` は、スキル参照およびインラインスキルバンドルを受け付けます。
-   ホスト型環境では、`ShellTool` に `executor`、`needs_approval`、`on_approval` を設定しないでください。
-   `network_policy` は、`disabled` モードと `allowlist` モードをサポートします。
-   許可リストモードでは、`network_policy.domain_secrets` を使用して、ドメインにスコープされたシークレットを名前で注入できます。
-   完全なコード例については、`examples/tools/container_shell_skill_reference.py` と `examples/tools/container_shell_inline_skill.py` を参照してください。
-   OpenAI プラットフォームガイド：[シェル](https://platform.openai.com/docs/guides/tools-shell)および[スキル](https://platform.openai.com/docs/guides/tools-skills)。

## ローカルランタイムツール

ローカルランタイムツールは、モデルのレスポンス自体の外部で実行されます。モデルは引き続き呼び出すタイミングを決定しますが、実際の処理はアプリケーションまたは設定された実行環境が行います。

`ComputerTool` と `ApplyPatchTool` には、ユーザーが提供するローカル実装が常に必要です。`ShellTool` は両方のモードに対応しています。管理された実行が必要な場合は上記のホスト型コンテナ設定を使用し、独自のプロセスでコマンドを実行する場合は以下のローカルランタイム設定を使用してください。

ローカルランタイムツールを使用するには、実装を提供する必要があります。

-   [`ComputerTool`][agents.tool.ComputerTool]：GUI／ブラウザーの自動化を有効にするには、[`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] インターフェースを実装します。
-   [`ShellTool`][agents.tool.ShellTool]：ローカル実行とホスト型コンテナ実行の両方に対応する最新のシェルツールです。
-   [`LocalShellTool`][agents.tool.LocalShellTool]：従来のローカルシェル統合です。
-   [`ApplyPatchTool`][agents.tool.ApplyPatchTool]：差分をローカルに適用するには、[`ApplyPatchEditor`][agents.editor.ApplyPatchEditor] を実装します。
-   `ShellTool(environment={"type": "local", "skills": [...]})` を使用すると、ローカルシェルスキルを利用できます。

### ComputerTool と Responses のコンピュータツール

`ComputerTool` は引き続きローカルハーネスです。[`Computer`][agents.computer.Computer] または [`AsyncComputer`][agents.computer.AsyncComputer] の実装を提供すると、SDK がそのハーネスを OpenAI Responses API のコンピュータ機能にマッピングします。

明示的な [`gpt-5.5`](https://developers.openai.com/api/docs/models/gpt-5.5) リクエストでは、SDK は GA の組み込みツールペイロード `{"type": "computer"}` を送信します。以前の `computer-use-preview` モデルでは、プレビューペイロード `{"type": "computer_use_preview", "environment": ..., "display_width": ..., "display_height": ...}` が引き続き使用されます。これは、OpenAI の[コンピュータ操作ガイド](https://developers.openai.com/api/docs/guides/tools-computer-use/)で説明されているプラットフォーム移行に対応しています。

-   モデル：`computer-use-preview` -> `gpt-5.5`
-   ツールセレクター：`computer_use_preview` -> `computer`
-   コンピュータ呼び出しの形式：`computer_call` ごとに 1 つの `action` -> `computer_call` 上の一括 `actions[]`
-   切り詰め：プレビューパスでは `ModelSettings(truncation="auto")` が必須 -> GA パスでは不要

SDK は、実際の Responses リクエストで有効なモデルに基づいて、そのワイヤ形式を選択します。プロンプトテンプレートを使用しており、モデルがプロンプト側で指定されているためリクエストで `model` が省略される場合、`model="gpt-5.5"` を明示したままにするか、`ModelSettings(tool_choice="computer")` または `ModelSettings(tool_choice="computer_use")` で GA セレクターを強制しない限り、SDK はプレビュー互換のコンピュータペイロードを維持します。

[`ComputerTool`][agents.tool.ComputerTool] が存在する場合、`tool_choice="computer"`、`"computer_use"`、`"computer_use_preview"` はすべて受け付けられ、有効なリクエストモデルに一致する組み込みセレクターへ正規化されます。`ComputerTool` がない場合、これらの文字列は通常の関数名として動作します。

`ComputerTool` が [`ComputerProvider`][agents.tool.ComputerProvider] ファクトリーによって提供される場合、この違いは重要です。GA の `computer` ペイロードでは、シリアライズ時に `environment` や表示サイズが不要なため、未解決のファクトリーでも問題ありません。一方、プレビュー互換のシリアライズでは、SDK が `environment`、`display_width`、`display_height` を送信できるよう、解決済みの `Computer` または `AsyncComputer` インスタンスが必要です。

ランタイムでは、どちらのパスも同じローカルハーネスを使用します。プレビューのレスポンスは、単一の `action` を持つ `computer_call` 項目を出力します。`gpt-5.5` は一括の `actions[]` を出力でき、SDK は `computer_call_output` スクリーンショット項目を生成する前に、それらを順番に実行します。Playwright ベースの実行可能なハーネスについては、`examples/tools/computer_use.py` を参照してください。

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

任意の Python 関数をツールとして使用できます。Agents SDK がツールを自動的にセットアップします。

-   ツール名には Python 関数の名前が使用されます（または名前を指定できます）
-   ツールの説明には関数の docstring が使用されます（または説明を指定できます）
-   関数入力のスキーマは、関数の引数から自動的に作成されます
-   無効にしない限り、各入力の説明は関数の docstring から取得されます

Python の `inspect` モジュールを使用して関数シグネチャを抽出し、[`griffe`](https://mkdocstrings.github.io/griffe/) で docstring を解析し、`pydantic` でスキーマを作成します。

OpenAI Responses モデルを使用している場合、`@function_tool(defer_loading=True)` は、`ToolSearchTool()` が読み込むまで関数ツールを非表示にします。[`tool_namespace()`][agents.tool.tool_namespace] を使用して、関連する関数ツールをグループ化することもできます。完全な設定と制約については、[ホスト型ツール検索](#hosted-tool-search)を参照してください。

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

1.  関数の引数には任意の Python 型を使用でき、関数は同期または非同期にできます。
2.  docstring が存在する場合、説明および引数の説明を取得するために使用されます
3.  関数は必要に応じて `context` を受け取れます（最初の引数である必要があります）。ツール名、説明、使用する docstring スタイルなどをオーバーライドすることもできます。
4.  デコレートした関数をツールのリストに渡せます。

??? note "出力の展開表示"

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

テキスト出力に加えて、1 つまたは複数の画像やファイルを関数ツールの出力として返せます。そのためには、次のいずれかを返します。

-   画像：[`ToolOutputImage`][agents.tool.ToolOutputImage]（または TypedDict 版の [`ToolOutputImageDict`][agents.tool.ToolOutputImageDict]）
-   ファイル：[`ToolOutputFileContent`][agents.tool.ToolOutputFileContent]（または TypedDict 版の [`ToolOutputFileContentDict`][agents.tool.ToolOutputFileContentDict]）
-   テキスト：文字列、文字列化可能なオブジェクト、または [`ToolOutputText`][agents.tool.ToolOutputText]（または TypedDict 版の [`ToolOutputTextDict`][agents.tool.ToolOutputTextDict]）

### カスタム関数ツール

Python 関数をツールとして使用したくない場合もあります。必要に応じて、[`FunctionTool`][agents.tool.FunctionTool] を直接作成できます。次の項目を指定する必要があります。

-   `name`
-   `description`
-   `params_json_schema`：引数の JSON スキーマ
-   `on_invoke_tool`：[ツールコンテキスト][agents.tool_context.ToolContext]と JSON 文字列形式の引数を受け取り、ツール出力（テキスト、構造化されたツール出力オブジェクト、出力のリストなど）を返す非同期関数

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

前述のように、関数シグネチャを自動的に解析してツールのスキーマを抽出し、docstring を解析してツールおよび個々の引数の説明を抽出します。これに関する注意事項は次のとおりです。

1. シグネチャの解析は `inspect` モジュールを使用して行われます。型アノテーションを使用して引数の型を把握し、スキーマ全体を表す Pydantic モデルを動的に構築します。Python の基本型、Pydantic モデル、TypedDict など、ほとんどの型をサポートしています。
2. docstring の解析には `griffe` を使用します。サポートされる docstring 形式は `google`、`sphinx`、`numpy` です。docstring 形式の自動検出を試みますが、これはベストエフォートであり、`function_tool` の呼び出し時に明示的に設定できます。`use_docstring_info` を `False` に設定して、docstring の解析を無効にすることもできます。

スキーマ抽出のコードは [`agents.function_schema`][] にあります。

### Pydantic Field による引数の制約と説明

Pydantic の [`Field`](https://docs.pydantic.dev/latest/concepts/fields/) を使用して、ツール引数に制約（数値の最小値／最大値、文字列の長さやパターンなど）と説明を追加できます。Pydantic と同様に、デフォルト値ベースの形式（`arg: int = Field(..., ge=1)`）と `Annotated` 形式（`arg: Annotated[int, Field(..., ge=1)]`）の両方がサポートされます。生成される JSON スキーマとバリデーションには、これらの制約が含まれます。

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

`@function_tool(timeout=...)` を使用して、非同期関数ツールの呼び出しごとにタイムアウトを設定できます。

```python
import asyncio
from agents import Agent, function_tool


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

タイムアウトに達した場合、デフォルトの動作は `timeout_behavior="error_as_result"` で、モデルから確認できるタイムアウトメッセージ（例：`Tool 'slow_lookup' timed out after 2 seconds.`）を送信します。

タイムアウト処理は次のように制御できます。

-   `timeout_behavior="error_as_result"`（デフォルト）：モデルが復旧できるよう、タイムアウトメッセージをモデルに返します。
-   `timeout_behavior="raise_exception"`：[`ToolTimeoutError`][agents.exceptions.ToolTimeoutError] を発生させ、実行を失敗させます。
-   `timeout_error_function=...`：`error_as_result` を使用する場合のタイムアウトメッセージをカスタマイズします。

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

    タイムアウト設定は、非同期の `@function_tool` ハンドラーでのみサポートされます。

### 関数ツールでのエラー処理

`@function_tool` を使用して関数ツールを作成する場合、`failure_error_function` を渡せます。これは、ツール呼び出しがクラッシュした場合に LLM へエラーレスポンスを提供する関数です。

-   デフォルトでは（何も渡さない場合）、エラーが発生したことを LLM に通知する `default_tool_error_function` が実行されます。
-   独自のエラー関数を渡した場合は、代わりにその関数が実行され、レスポンスが LLM に送信されます。
-   明示的に `None` を渡した場合、ツール呼び出しのエラーは再度発生し、ユーザー側で処理できます。モデルが無効な JSON を生成した場合は `ModelBehaviorError`、コードがクラッシュした場合は `UserError` になる可能性があります。

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

`FunctionTool` オブジェクトを手動で作成する場合、`on_invoke_tool` 関数内でエラーを処理する必要があります。

## Agents as tools

ワークフローによっては、制御をハンドオフする代わりに、中央のエージェントで特化型エージェントのネットワークをオーケストレーションしたい場合があります。これは、エージェントをツールとしてモデル化することで実現できます。

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

### ツールエージェントのカスタマイズ

`agent.as_tool` 関数は、エージェントを簡単にツールへ変換するための便利なメソッドです。`max_turns`、`run_config`、`hooks`、`previous_response_id`、`conversation_id`、`session`、`needs_approval` など、一般的なランタイムオプションをサポートします。また、`parameters`、`input_builder`、`include_input_schema` による構造化入力もサポートします。

状態オプションは、ツール呼び出しによって開始されるネストされたエージェント実行を設定します。親実行の会話状態は自動的には継承されません。クライアント管理の履歴を親実行とネストされた実行の間で共有するには、同じ `session` を両方に明示的に渡してください。`Runner.run` と同様に、ネストされた実行には 1 つの状態戦略を選択してください。クライアント管理の `session`、または `previous_response_id` や `conversation_id` を使用したサーバー管理の継続のいずれかです。

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

デフォルトでは、`Agent.as_tool()` は単一の文字列入力（`{"input": "..."}`）を想定しますが、`parameters`（Pydantic モデルまたは dataclass 型）を渡すことで構造化スキーマを公開できます。

追加オプション：

- `include_input_schema=True` を指定すると、生成されるネストされた入力に完全な JSON Schema が含まれます。
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

実行可能な完全なコード例については、`examples/agent_patterns/agents_as_tools_structured.py` を参照してください。

### ツールエージェントの承認ゲート

`Agent.as_tool(..., needs_approval=...)` は、`function_tool` と同じ承認フローを使用します。承認が必要な場合、実行は一時停止し、保留中の項目が `result.interruptions` に表示されます。次に `result.to_state()` を使用し、`state.approve(...)` または `state.reject(...)` を呼び出した後に再開します。完全な一時停止／再開パターンについては、[Human-in-the-loop ガイド](human_in_the_loop.md)を参照してください。

### カスタム出力の抽出

場合によっては、中央のエージェントに返す前に、ツールエージェントの出力を変更したいことがあります。これは、次のような場合に役立ちます。

-   サブエージェントのチャット履歴から特定の情報（JSON ペイロードなど）を抽出する。
-   エージェントの最終回答を変換または再フォーマットする（Markdown をプレーンテキストや CSV に変換するなど）。
-   出力を検証する。または、エージェントのレスポンスが欠落しているか不正な形式の場合にフォールバック値を提供する。

これは、`as_tool` メソッドに `custom_output_extractor` 引数を指定することで実現できます。

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

カスタム抽出関数内では、ネストされた [`RunResult`][agents.result.RunResult] から [`agent_tool_invocation`][agents.result.RunResultBase.agent_tool_invocation] にもアクセスできます。これは、ネストされた実行結果を後処理する際に、外側のツール名、呼び出し ID、または raw 引数が必要な場合に便利です。[実行結果ガイド](results.md#agent-as-tool-metadata)を参照してください。

### ネストされたエージェント実行のストリーミング

`as_tool` に `on_stream` コールバックを渡すと、ネストされたエージェントが出力するストリーミングイベントを受け取りながら、ストリームの完了後に最終出力を返せます。

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

- イベント型は `StreamEvent["type"]` と同様です：`raw_response_event`、`run_item_stream_event`、`agent_updated_stream_event`。
- `on_stream` を指定すると、ネストされたエージェントは自動的にストリーミングモードで実行され、最終出力を返す前にストリームが最後まで処理されます。
- ハンドラーは同期または非同期にできます。各イベントは到着した順に渡されます。
- モデルのツール呼び出し経由でツールが呼び出された場合、`tool_call` が存在します。直接呼び出した場合は `None` になることがあります。
- 実行可能な完全なサンプルについては、`examples/agent_patterns/agents_as_tools_streaming.py` を参照してください。

### 条件付きツール有効化

`is_enabled` パラメーターを使用して、ランタイムでエージェントツールを条件付きで有効または無効にできます。これにより、コンテキスト、ユーザー設定、ランタイム条件に基づいて、LLM が利用できるツールを動的にフィルタリングできます。

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

`is_enabled` パラメーターは、次の値を受け付けます。

-   **ブール値**：`True`（常に有効）または `False`（常に無効）
-   **呼び出し可能な関数**：`(context, agent)` を受け取り、ブール値を返す関数
-   **非同期関数**：複雑な条件ロジックのための非同期関数

無効なツールはランタイムで LLM から完全に非表示になるため、次の用途に役立ちます。

-   ユーザー権限に基づく機能制御
-   環境固有のツール可用性（開発環境と本番環境）
-   異なるツール設定の A/B テスト
-   ランタイム状態に基づく動的なツールフィルタリング

## 実験的機能：Codex ツール

`codex_tool` は Codex CLI をラップし、エージェントがツール呼び出し中にワークスペースにスコープされたタスク（シェル、ファイル編集、MCP ツール）を実行できるようにします。この機能は実験的であり、変更される可能性があります。

メインエージェントが現在の実行を離れることなく、範囲が限定されたワークスペースタスクを Codex に委任する場合に使用します。デフォルトのツール名は `codex` です。カスタム名を設定する場合、その名前は `codex` であるか、`codex_` で始まる必要があります。エージェントに複数の Codex ツールを含める場合、それぞれに一意の名前を使用する必要があります。

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

まず、次のオプショングループから設定してください。

-   実行対象：`sandbox_mode` と `working_directory` は、Codex が操作できる場所を定義します。これらは組み合わせて使用し、作業ディレクトリが Git リポジトリ内にない場合は `skip_git_repo_check=True` を設定してください。
-   スレッドのデフォルト：`default_thread_options=ThreadOptions(...)` は、モデル、推論強度、承認ポリシー、追加ディレクトリ、ネットワークアクセス、Web 検索モードを設定します。従来の `web_search_enabled` よりも `web_search_mode` を優先してください。
-   ターンのデフォルト：`default_turn_options=TurnOptions(...)` は、`idle_timeout_seconds` やオプションのキャンセル用 `signal` など、ターンごとの動作を設定します。
-   ツール I/O：ツール呼び出しには、`{ "type": "text", "text": ... }` または `{ "type": "local_image", "path": ... }` を持つ `inputs` 項目を少なくとも 1 つ含める必要があります。`output_schema` を使用すると、構造化された Codex レスポンスを必須にできます。

スレッドの再利用と永続化は、別々に制御されます。

-   `persist_session=True` は、同じツールインスタンスへの繰り返し呼び出しで 1 つの Codex スレッドを再利用します。
-   `use_run_context_thread_id=True` は、同じ変更可能なコンテキストオブジェクトを共有する複数の実行にわたり、実行コンテキスト内にスレッド ID を保存して再利用します。
-   スレッド ID の優先順位は、呼び出しごとの `thread_id`、実行コンテキストのスレッド ID（有効な場合）、設定済みの `thread_id` オプションの順です。
-   デフォルトの実行コンテキストキーは、`name="codex"` の場合は `codex_thread_id`、`name="codex_<suffix>"` の場合は `codex_thread_id_<suffix>` です。`run_context_thread_id_key` でオーバーライドできます。

ランタイム設定：

-   認証：`CODEX_API_KEY`（推奨）または `OPENAI_API_KEY` を設定するか、`codex_options={"api_key": "..."}` を渡します。
-   ランタイム：`codex_options.base_url` は CLI のベース URL をオーバーライドします。
-   バイナリ解決：CLI のパスを固定するには、`codex_options.codex_path_override`（または `CODEX_PATH`）を設定します。それ以外の場合、SDK は `PATH` から `codex` を解決し、見つからなければ同梱のベンダーバイナリを使用します。
-   環境：`codex_options.env` は、サブプロセス環境を完全に制御します。指定した場合、サブプロセスは `os.environ` を継承しません。
-   ストリーム制限：`codex_options.codex_subprocess_stream_limit_bytes`（または `OPENAI_AGENTS_CODEX_SUBPROCESS_STREAM_LIMIT_BYTES`）は、stdout／stderr リーダーの制限を制御します。有効範囲は `65536` から `67108864` で、デフォルトは `8388608` です。
-   ストリーミング：`on_stream` は、スレッド／ターンのライフサイクルイベントと項目イベント（`reasoning`、`command_execution`、`mcp_tool_call`、`file_change`、`web_search`、`todo_list`、`error` の項目更新）を受け取ります。
-   出力：実行結果には `response`、`usage`、`thread_id` が含まれます。使用量は `RunContextWrapper.usage` に追加されます。

リファレンス：

-   [Codex ツール API リファレンス](ref/extensions/experimental/codex/codex_tool.md)
-   [ThreadOptions リファレンス](ref/extensions/experimental/codex/thread_options.md)
-   [TurnOptions リファレンス](ref/extensions/experimental/codex/turn_options.md)
-   実行可能な完全なサンプルについては、`examples/tools/codex.py` と `examples/tools/codex_same_thread.py` を参照してください。