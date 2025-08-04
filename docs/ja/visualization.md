---
search:
  exclude: true
---
# エージェント可視化

エージェント可視化では、 **Graphviz** を使用してエージェントおよびその関係を構造化されたグラフィカル表現として生成できます。これにより、アプリケーション内でエージェント、ツール、ハンドオフがどのように相互作用するかを理解しやすくなります。

## インストール

オプションの `viz` 依存関係グループをインストールします:

```bash
pip install "openai-agents[viz]"
```

## グラフの生成

`draw_graph` 関数を使用してエージェントの可視化を生成できます。この関数は有向グラフを作成し、以下のように表現します:

- **エージェント** は黄色のボックス。  
- **MCP サーバー** は灰色のボックス。  
- **ツール** は緑色の楕円。  
- **ハンドオフ** はあるエージェントから別のエージェントへの向き付きエッジ。

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

これにより、 **triage エージェント** の構造とサブエージェントおよびツールとの接続を視覚的に表現したグラフが生成されます。

## 可視化の理解

生成されたグラフには以下が含まれます:

- エントリーポイントを示す **start ノード** (`__start__`)。  
- 黄色で塗りつぶされた **長方形** で表されるエージェント。  
- 緑で塗りつぶされた **楕円** で表されるツール。  
- 灰色で塗りつぶされた **長方形** で表される MCP サーバー。  
- 相互作用を示す向き付きエッジ:  
  - エージェント間ハンドオフを示す **実線矢印**。  
  - ツール呼び出しを示す **点線矢印**。  
  - MCP サーバー呼び出しを示す **破線矢印**。  
- 実行が終了する場所を示す **end ノード** (`__end__`)。

## グラフのカスタマイズ

### グラフの表示
デフォルトでは `draw_graph` はグラフをインラインで表示します。別ウィンドウでグラフを表示するには、次のように記述します:

```python
draw_graph(triage_agent).view()
```

### グラフの保存
デフォルトでは `draw_graph` はグラフをインラインで表示します。ファイルとして保存するには、ファイル名を指定します:

```python
draw_graph(triage_agent, filename="agent_graph")
```

これにより、作業ディレクトリに `agent_graph.png` が生成されます。