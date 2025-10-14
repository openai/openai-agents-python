---
search:
  exclude: true
---
# 智能体可视化

智能体可视化允许你使用 **Graphviz** 生成智能体及其关系的结构化图形表示。这有助于理解在应用中智能体、工具和任务转移如何交互。

## 安装

安装可选的 `viz` 依赖组：

```bash
pip install "openai-agents[viz]"
```

## 生成图形

你可以使用 `draw_graph` 函数生成智能体可视化。该函数会创建一个有向图，其中：

- **Agents（智能体）** 显示为黄色方框。
- **MCP Servers** 显示为灰色方框。
- **Tools（工具）** 显示为绿色椭圆。
- **Handoffs（任务转移）** 为从一个智能体指向另一个智能体的有向边。

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

![Agent Graph](../assets/images/graph.png)

这会生成一个图，直观展示了**分诊智能体（triage agent）**的结构，以及它与子智能体和工具的连接关系。

## 可视化解析

生成的图包含：

- 一个表示入口的**起始节点**（`__start__`）。
- 用**矩形**且填充黄色表示的智能体。
- 用**椭圆**且填充绿色表示的工具。
- 用**矩形**且填充灰色表示的 MCP Servers。
- 表示交互的有向边：
  - **实线箭头**：智能体到智能体的任务转移。
  - **点线箭头**：工具调用。
  - **虚线箭头**：MCP Server 调用。
- 一个表示执行终止位置的**结束节点**（`__end__`）。

**注意：**MCP servers 会在 `agents` 包的较新版本中渲染（已在 **v0.2.8** 验证）。如果在你的可视化中看不到 MCP 方框，请升级到最新版本。

## 图形自定义

### 显示图形
默认情况下，`draw_graph` 会内联显示图形。要在单独的窗口中显示，请编写：

```python
draw_graph(triage_agent).view()
```

### 保存图形
默认情况下，`draw_graph` 会内联显示图形。要将其保存为文件，请指定文件名：

```python
draw_graph(triage_agent, filename="agent_graph")
```

这将在工作目录中生成 `agent_graph.png`。