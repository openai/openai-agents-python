---
search:
  exclude: true
---
# エージェントの可視化

エージェント の可視化では、 **Graphviz** を使用して、エージェント とその関係の構造化されたグラフィカル表現を生成できます。これは、アプリケーション内でエージェント、ツール、ハンドオフ がどのように相互作用するかを理解するのに役立ちます。

## インストール

オプションの `viz` 依存関係グループをインストールします:

```bash
pip install "openai-agents[viz]"
```

## グラフの生成

`draw_graph` 関数を使用して、エージェント の可視化を生成できます。この関数は、次のような有向グラフを作成します:

- **エージェント** は黄色のボックスで表されます。
- **MCP サーバー** は灰色のボックスで表されます。
- **ツール** は緑の楕円で表されます。
- **ハンドオフ** は、あるエージェント から別のエージェント への有向エッジです。

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

![エージェント グラフ](../assets/images/graph.png)

これにより、トリアージ エージェント の構造と、サブエージェント やツール との接続を視覚的に表すグラフが生成されます。


## 可視化の理解

生成されたグラフには次が含まれます:

- **開始ノード** ( `__start__` ) はエントリーポイントを示します。
- エージェント は黄色の塗りの **長方形** で表されます。
- ツール は緑の塗りの **楕円** で表されます。
- MCP サーバー は灰色の塗りの **長方形** で表されます。
- 相互作用を示す有向エッジ:
  - **実線の矢印** はエージェント 間のハンドオフを表します。
  - **点線の矢印** はツール の呼び出しを表します。
  - **破線の矢印** は MCP サーバー の呼び出しを表します。
- **終了ノード** ( `__end__` ) は実行の終了位置を示します。

## グラフのカスタマイズ

### グラフの表示
デフォルトでは、 `draw_graph` はグラフをインライン表示します。グラフを別ウィンドウで表示するには、次のようにします:

```python
draw_graph(triage_agent).view()
```

### グラフの保存
デフォルトでは、 `draw_graph` はグラフをインライン表示します。ファイルとして保存するには、ファイル名を指定します:

```python
draw_graph(triage_agent, filename="agent_graph")
```

これにより、作業ディレクトリに `agent_graph.png` が生成されます。