---
search:
  exclude: true
---
# 에이전트 시각화

에이전트 시각화를 사용하면 **Graphviz**를 통해 에이전트와 다른 에이전트, 도구 및 MCP 서버 간 연결을 구조화된 그래프로 생성할 수 있습니다. 이는 애플리케이션 내에서 에이전트, 도구 및 핸드오프가 상호작용하는 방식을 이해하는 데 유용합니다.

## 설치 {#installation}

선택적 `viz` 의존성 그룹을 설치합니다.

```bash
pip install "openai-agents[viz]"
```

## 그래프 생성 {#generating-a-graph}

`draw_graph` 함수를 사용하여 에이전트 시각화를 생성할 수 있습니다. 이 함수는 다음과 같은 방향 그래프를 생성합니다.

- **에이전트**는 노란색 상자로 표시됩니다.
- **MCP 서버**는 회색 상자로 표시됩니다.
- **도구**는 녹색 타원으로 표시됩니다.
- **핸드오프**는 한 에이전트에서 다른 에이전트로 향하는 방향 간선으로 표시됩니다.

### 사용 예시 {#example-usage}

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

![에이전트 그래프](../assets/images/graph.png)

이 코드는 **트리아지 에이전트**의 구조와 하위 에이전트 및 도구와의 연결을 시각적으로 나타내는 그래프를 생성합니다.

`draw_graph()`는 `handoffs`에 직접 제공되거나 `handoff(agent)`를 통해 등록된 대상 에이전트를 재귀적으로 확장합니다. 두 방식 모두 그래프에 각 대상의 도구, MCP 서버 및 후속 핸드오프가 포함됩니다. 사용 가능한 대상 `Agent`가 없는 사용자 지정 `Handoff`는 이름이 지정된 목적지로만 렌더링되므로, 그래프가 해당 목적지 이면의 리소스를 확장할 수 없습니다.


## 시각화 이해 {#understanding-the-visualization}

생성된 그래프에는 다음이 포함됩니다.

- 진입점을 나타내는 **시작 노드**(`__start__`)
- 노란색으로 채워진 **직사각형**으로 표시되는 에이전트
- 녹색으로 채워진 **타원**으로 표시되는 도구
- 회색으로 채워진 **직사각형**으로 표시되는 MCP 서버
- 상호작용을 나타내는 방향 간선
  - 에이전트 간 핸드오프를 나타내는 **실선 화살표**
  - 도구 호출을 나타내는 **점선 화살표**
  - MCP 서버 호출을 나타내는 **파선 화살표**
- 실행이 종료되는 위치를 나타내는 **종료 노드**(`__end__`)

**참고:** MCP 서버는 이 동작이 확인된 **v0.2.8**을 포함하여 최신 버전의 `agents` 패키지에서 렌더링됩니다. 시각화에 MCP 상자가 표시되지 않으면 최신 릴리스로 업그레이드하세요.

## 그래프 사용자 지정 {#customizing-the-graph}

### 그래프 표시 {#showing-the-graph}
기본적으로 `draw_graph`은 그래프를 인라인으로 표시합니다. 별도의 창에 그래프를 표시하려면 다음과 같이 작성합니다.

```python
draw_graph(triage_agent).view()
```

### 그래프 저장 {#saving-the-graph}
기본적으로 `draw_graph`은 그래프를 인라인으로 표시합니다. 파일로 저장하려면 파일 이름을 지정합니다.

```python
draw_graph(triage_agent, filename="agent_graph")
```

그러면 작업 디렉터리에 `agent_graph.png`가 생성됩니다.