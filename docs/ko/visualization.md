---
search:
  exclude: true
---
# 에이전트 시각화

에이전트 시각화는 **Graphviz** 를 사용해 에이전트와 그 관계를 구조적으로 표현한 그래프를 생성할 수 있게 해줍니다. 이는 애플리케이션 내에서 에이전트, 도구, 핸드오프가 어떻게 상호작용하는지 이해하는 데 유용합니다.

## 설치

선택적 `viz` 의존성 그룹을 설치하세요:

```bash
pip install "openai-agents[viz]"
```

## 그래프 생성

`draw_graph` 함수를 사용해 에이전트 시각화를 생성할 수 있습니다. 이 함수는 다음과 같은 구성의 방향 그래프를 만듭니다:

- **에이전트** 는 노란색 상자로 표시됩니다
- **MCP 서버** 는 회색 상자로 표시됩니다
- **도구** 는 녹색 타원으로 표시됩니다
- **핸드오프** 는 한 에이전트에서 다른 에이전트로 향하는 방향 간선입니다

### 사용 예

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

이는 **triage 에이전트** 와 하위 에이전트 및 도구와의 연결 구조를 시각적으로 나타내는 그래프를 생성합니다.


## 시각화 이해

생성된 그래프에는 다음이 포함됩니다:

- 진입점을 나타내는 **시작 노드** (`__start__`)
- 노란색 채우기의 **직사각형** 으로 표시된 에이전트
- 녹색 채우기의 **타원** 으로 표시된 도구
- 회색 채우기의 **직사각형** 으로 표시된 MCP 서버
- 상호작용을 나타내는 방향 간선:
  - 에이전트 간 핸드오프는 **실선 화살표**
  - 도구 호출은 **점선 화살표**
  - MCP 서버 호출은 **파선 화살표**
- 실행이 종료되는 위치를 나타내는 **종료 노드** (`__end__`)

## 그래프 커스터마이즈

### 그래프 표시
기본적으로 `draw_graph` 는 그래프를 인라인으로 표시합니다. 별도의 창에서 표시하려면 다음을 작성하세요:

```python
draw_graph(triage_agent).view()
```

### 그래프 저장
기본적으로 `draw_graph` 는 그래프를 인라인으로 표시합니다. 파일로 저장하려면 파일명을 지정하세요:

```python
draw_graph(triage_agent, filename="agent_graph")
```

이 경우 작업 디렉터리에 `agent_graph.png` 가 생성됩니다.