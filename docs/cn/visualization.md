---
search:
  exclude: true
---
# 智能体可视化

智能体可视化允许你使用 **Graphviz** 生成智能体及其关系的结构化图形表示。这有助于理解应用程序中智能体、工具和交接如何相互作用。

## 安装

安装可选的 `viz` 依赖组:

```bash
pip install "openai-agents[viz]"
```

## 生成图表

你可以使用 `draw_graph` 函数生成智能体可视化。此函数创建一个有向图，其中：

- **智能体** 表示为黄色框。
- **MCP 服务器** 表示为灰色框。
- **工具** 表示为绿色椭圆。
- **交接** 表示为智能体之间的有向边。

### 使用示例

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

![智能体图表](../assets/images/graph.png)

这会生成一个可视化图表，表示 **分类智能体** 的结构及其与子智能体和工具的连接。


## 理解可视化

生成的图表包括：

- 一个 **开始节点** (`__start__`) 表示入口点。
- 智能体表示为带有黄色填充的 **矩形**。
- 工具表示为带有绿色填充的 **椭圆**。
- MCP 服务器表示为带有灰色填充的 **矩形**。
- 指示交互的有向边：
  - **实线箭头** 用于智能体到智能体的交接。
  - **虚线箭头** 用于工具调用。
  - **点线箭头** 用于 MCP 服务器调用。
- 一个 **结束节点** (`__end__`) 表示执行终止的位置。

**注意：** MCP 服务器在 `agents` 包的最新版本中渲染（已在 **v0.2.8** 中验证）。如果你的可视化中没有看到 MCP 框，请升级到最新版本。

## 自定义图表

### グラフの表示
默认情况下，`draw_graph` 会内联显示图表。要在单独的窗口中显示图表，请编写以下内容:

```python
draw_graph(triage_agent).view()
```

### 保存图表
默认情况下，`draw_graph` 会内联显示图表。要将其保存为文件，请指定文件名:

```python
draw_graph(triage_agent, filename="agent_graph")
```

这将在工作目录中生成 `agent_graph.png`。