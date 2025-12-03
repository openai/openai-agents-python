---
search:
  exclude: true
---
# エージェントの可視化

エージェントの可視化では、**Graphviz** を使って エージェント とその関係の構造化されたグラフィカル表現を生成できます。これは、アプリケーション内で エージェント、ツール、ハンドオフ がどのように相互作用するかを理解するのに有用です。

## インストール

オプションの `viz` 依存関係グループをインストールします:

```bash
pip install "openai-agents[viz]"
```

## グラフの生成

`draw_graph` 関数を使って エージェントの可視化 を生成できます。この関数は次のような有向グラフを作成します:

- **エージェント** は黄色のボックスで表されます。
- **MCP サーバー** は灰色のボックスで表されます。
- **ツール** は緑色の楕円で表されます。
- **ハンドオフ** は、あるエージェントから別のエージェントへの有向エッジです。

### 使用例

```python
import os

from agents import Agent, function_tool
from agents.mcp.server import MCPServerStdio
from agents.extensions.visualization import draw_graph

@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny."

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
)

current_dir = os.path.dirname(os.path.abspath(__file__))
samples_dir = os.path.join(current_dir, "sample_files")
mcp_server = MCPServerStdio(
    name="Filesystem Server, via npx",
    params={
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", samples_dir],
    },
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    tools=[get_weather],
    mcp_servers=[mcp_server],
)

draw_graph(triage_agent)
```

![Agent Graph](../assets/images/graph.png)

これは、**トリアージ エージェント** とサブエージェントやツールへの接続の構造を視覚的に表すグラフを生成します。


## 可視化の理解

生成されるグラフには次が含まれます:

- エントリポイントを示す **開始ノード**（`__start__`）。
- 黄色で塗りつぶされた **長方形** のエージェント。
- 緑色で塗りつぶされた **楕円** のツール。
- 灰色で塗りつぶされた **長方形** の MCP サーバー。
- 相互作用を示す有向エッジ:
  - エージェント間のハンドオフには **実線の矢印**。
  - ツール呼び出しには **点線の矢印**。
  - MCP サーバー呼び出しには **破線の矢印**。
- 実行の終了箇所を示す **終了ノード**（`__end__`）。

**注:** MCP サーバーは最近の `agents` パッケージのバージョンで描画されます（**v0.2.8** で確認済み）。可視化に MCP ボックスが表示されない場合は、最新リリースにアップグレードしてください。

## グラフのカスタマイズ

### グラフの表示
デフォルトでは、`draw_graph` はグラフをインラインで表示します。別ウィンドウで表示するには、次のように記述します:

```python
draw_graph(triage_agent).view()
```

### グラフの保存
デフォルトでは、`draw_graph` はグラフをインラインで表示します。ファイルとして保存するには、ファイル名を指定します:

```python
draw_graph(triage_agent, filename="agent_graph")
```

これにより、作業ディレクトリに `agent_graph.png` が生成されます。