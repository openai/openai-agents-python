---
search:
  exclude: true
---
# 智能体可视化

智能体可视化允许你使用**Graphviz**生成智能体及其关系的结构化图形表示。这有助于理解智能体、工具和任务转移在应用程序中的交互方式。

## 安装

安装可选的 `viz` 依赖组：

```bash
pip install "openai-agents[viz]"
```

## 图形生成

你可以使用 `draw_graph` 函数生成智能体可视化。该函数会创建一个有向图，其中：

- **智能体**表示为黄色方框。
- **MCP 服务**表示为灰色方框。
- **工具**表示为绿色椭圆。
- **任务转移**表示为从一个智能体指向另一个智能体的有向边。

### 示例用法

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

![智能体图](../assets/images/graph.png)

这会生成一个图形，用于直观展示**分流智能体**的结构，以及它与子智能体和工具之间的连接。


## 可视化理解

生成的图包括：

- 一个**起始节点**（`__start__`），表示入口点。
- 以黄色填充的**矩形**表示的智能体。
- 以绿色填充的**椭圆**表示的工具。
- 以灰色填充的**矩形**表示的 MCP 服务。
- 表示交互的有向边：
  - **实线箭头**表示智能体之间的任务转移。
  - **点线箭头**表示工具调用。
  - **虚线箭头**表示 MCP 服务调用。
- 一个**结束节点**（`__end__`），表示执行终止的位置。

**注意：** MCP 服务会在近期版本的
`agents` 包中渲染（已在 **v0.2.8** 中验证）。如果你在可视化中没有看到 MCP 方框，
请升级到最新版本。

## 图形自定义

### 图形显示
默认情况下，`draw_graph` 会以内联方式显示图形。若要在单独窗口中显示图形，请编写如下代码：

```python
draw_graph(triage_agent).view()
```

### 图形保存
默认情况下，`draw_graph` 会以内联方式显示图形。若要将其保存为文件，请指定文件名：

```python
draw_graph(triage_agent, filename="agent_graph")
```

这将在工作目录中生成 `agent_graph.png`。