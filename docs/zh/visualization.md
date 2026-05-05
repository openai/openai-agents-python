---
search:
  exclude: true
---
# 智能体可视化

智能体可视化允许你使用 **Graphviz** 生成智能体及其关系的结构化图形表示。这有助于理解智能体、工具调用和任务转移在应用中的交互方式。

## 安装

安装可选的 `viz` 依赖组：

```bash
pip install "openai-agents[viz]"
```

## 生成图

你可以使用 `draw_graph` 函数生成智能体可视化。该函数会创建一个有向图，其中：

- **智能体** 以黄色方框表示。
- **MCP 服务** 以灰色方框表示。
- **工具调用** 以绿色椭圆表示。
- **任务转移** 是从一个智能体指向另一个智能体的有向边。

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

这会生成一张图，直观展示**分诊智能体**及其与子智能体和工具的连接关系。


## 理解可视化

生成的图包括：

- 一个 **起始节点**（`__start__`），表示入口点。
- 以黄色填充的**矩形**表示的智能体。
- 以绿色填充的**椭圆**表示的工具。
- 以灰色填充的**矩形**表示的 MCP 服务。
- 表示交互的有向边：
  - 智能体到智能体任务转移使用**实线箭头**。
  - 工具调用使用**点线箭头**。
  - MCP 服务调用使用**虚线箭头**。
- 一个 **结束节点**（`__end__`），表示执行终止的位置。

**注意：** MCP 服务会在较新版本的
`agents` 包中渲染（已在 **v0.2.8** 验证）。如果你在可视化中看不到 MCP 方框，
请升级到最新版本。

## 自定义图

### 显示图
默认情况下，`draw_graph` 会以内联方式显示图。若要在单独窗口中显示图，请写入以下内容：

```python
draw_graph(triage_agent).view()
```

### 保存图
默认情况下，`draw_graph` 会以内联方式显示图。若要将其保存为文件，请指定文件名：

```python
draw_graph(triage_agent, filename="agent_graph")
```

这会在工作目录中生成 `agent_graph.png`。