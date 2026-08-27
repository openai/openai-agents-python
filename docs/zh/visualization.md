---
search:
  exclude: true
---
# 智能体可视化

智能体可视化允许你使用 **Graphviz** 生成智能体及其与其他智能体、工具和 MCP 服务器之间连接关系的结构化图形表示。这有助于理解智能体、工具和任务转移在应用程序中如何交互。

## 安装 {#installation}

安装可选的 `viz` 依赖组：

```bash
pip install "openai-agents[viz]"
```

## 图形生成 {#generating-a-graph}

你可以使用 `draw_graph` 函数生成智能体可视化图。此函数会创建一个有向图，其中：

- **智能体**以黄色方框表示。
- **MCP 服务器**以灰色方框表示。
- **工具**以绿色椭圆表示。
- **任务转移**以从一个智能体指向另一个智能体的有向边表示。

### 用法示例 {#example-usage}

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

![智能体图](../assets/images/graph.png)

这会生成一张图，以可视化方式展示**分诊智能体**的结构及其与子智能体和工具之间的连接。

`draw_graph()` 会递归展开直接在 `handoffs` 中提供或通过 `handoff(agent)` 注册的目标智能体。无论采用哪种方式，图中都会包含每个目标的工具、MCP 服务器和下游任务转移。如果自定义 `Handoff` 没有可用的目标 `Agent`，则只会将其渲染为具名目标，因此图中无法展开该目标背后的资源。


## 可视化说明 {#understanding-the-visualization}

生成的图包括：

- 一个表示入口点的**起始节点**（`__start__`）。
- 以黄色填充的**矩形**表示的智能体。
- 以绿色填充的**椭圆**表示的工具。
- 以灰色填充的**矩形**表示的 MCP 服务器。
- 表示交互的有向边：
  - **实线箭头**表示智能体之间的任务转移。
  - **点线箭头**表示工具调用。
  - **虚线箭头**表示 MCP 服务器调用。
- 一个表示执行终止位置的**结束节点**（`__end__`）。

**注意：**在较新版本的 `agents` 包中会渲染 MCP 服务器，包括已验证此行为的 **v0.2.8**。如果在可视化图中看不到 MCP 方框，请升级到最新版本。

## 图形自定义 {#customizing-the-graph}

### 图形显示 {#showing-the-graph}
默认情况下，`draw_graph` 会内联显示图形。若要在单独的窗口中显示图形，请编写以下代码：

```python
draw_graph(triage_agent).view()
```

### 图形保存 {#saving-the-graph}
默认情况下，`draw_graph` 会内联显示图形。若要将其保存为文件，请指定文件名：

```python
draw_graph(triage_agent, filename="agent_graph")
```

这将在工作目录中生成 `agent_graph.png`。