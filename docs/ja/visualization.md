---
search:
  exclude: true
---
# エージェントの可視化

エージェントの可視化では、 **Graphviz** を使用して、エージェントと、他のエージェント、ツール、MCP サーバーとの接続を構造化されたグラフとして生成できます。これは、アプリケーション内でエージェント、ツール、ハンドオフがどのように連携するかを理解するのに役立ちます。

## インストール {#installation}

オプションの `viz` 依存関係グループをインストールします。

```bash
pip install "openai-agents[viz]"
```

## グラフの生成 {#generating-a-graph}

`draw_graph` 関数を使用して、エージェントの可視化を生成できます。この関数は、次のような有向グラフを作成します。

- **エージェント** は黄色のボックスで表されます。
- **MCP サーバー** は灰色のボックスで表されます。
- **ツール** は緑色の楕円で表されます。
- **ハンドオフ** は、あるエージェントから別のエージェントへの有向エッジで表されます。

### 使用例 {#example-usage}

```python
import os

from agents import Agent, handoff
from agents.decorators import tool
from agents.mcp.server import MCPServerStdio
from agents.extensions.visualization import draw_graph

@tool
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
    handoffs=[handoff(spanish_agent), handoff(english_agent)],
    tools=[get_weather],
    mcp_servers=[mcp_server],
)

draw_graph(triage_agent)
```

![エージェントグラフ](../assets/images/graph.png)

これにより、 **トリアージエージェント** の構造と、サブエージェントおよびツールとの接続を視覚的に表すグラフが生成されます。

`draw_graph()` は、`handoffs` で直接指定された対象エージェント、または `handoff(agent)` を通じて登録された対象エージェントを再帰的に展開します。どちらの形式でも、グラフには各対象のツール、MCP サーバー、およびその先のハンドオフが含まれます。利用可能な対象 `Agent` がないカスタム `Handoff` は、名前付きの接続先としてのみ描画されるため、グラフではその接続先の背後にあるリソースを展開できません。


## 可視化の構成 {#understanding-the-visualization}

生成されるグラフには、次の要素が含まれます。

- エントリーポイントを示す **開始ノード**（`__start__`）。
- 黄色で塗りつぶされた **長方形** で表されるエージェント。
- 緑色で塗りつぶされた **楕円** で表されるツール。
- 灰色で塗りつぶされた **長方形** で表される MCP サーバー。
- インタラクションを示す有向エッジ：
  - エージェント間のハンドオフを示す **実線の矢印**。
  - ツール呼び出しを示す **点線の矢印**。
  - MCP サーバー呼び出しを示す **破線の矢印**。
- 実行が終了する場所を示す **終了ノード**（`__end__`）。

**注：** MCP サーバーは、`agents` パッケージの最近のバージョンで描画されます。この動作が確認されている **v0.2.8** も含まれます。可視化に MCP のボックスが表示されない場合は、最新リリースにアップグレードしてください。

## グラフのカスタマイズ {#customizing-the-graph}

### グラフの表示 {#showing-the-graph}
デフォルトでは、`draw_graph` はグラフをインラインで表示します。グラフを別ウィンドウに表示するには、次のように記述します。

```python
draw_graph(triage_agent).view()
```

### グラフの保存 {#saving-the-graph}
デフォルトでは、`draw_graph` はグラフをインラインで表示します。ファイルとして保存するには、ファイル名を指定します。

```python
draw_graph(triage_agent, filename="agent_graph")
```

これにより、作業ディレクトリに `agent_graph.png` が生成されます。