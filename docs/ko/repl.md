---
search:
  exclude: true
---
# REPL 유틸리티

SDK는 터미널에서 직접 에이전트의 동작을 빠르게 대화형으로 테스트할 수 있도록 `run_demo_loop`를 제공합니다.


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop`는 루프 안에서 사용자 입력을 요청하며, 턴 사이의 대화 기록을 유지합니다. 기본적으로 모델 출력이 생성되는 대로 스트리밍합니다. 위 예제를 실행하면 run_demo_loop가 대화형 채팅 세션을 시작합니다. 계속해서 입력을 요청하고, 턴 사이의 전체 대화 기록을 기억하며(따라서 에이전트는 지금까지 논의된 내용을 알 수 있습니다), 에이전트의 응답이 생성되는 즉시 실시간으로 자동 스트리밍해 제공합니다.

이 채팅 세션을 종료하려면 `quit` 또는 `exit`를 입력한 뒤 Enter를 누르거나 `Ctrl-D` 키보드 단축키를 사용하면 됩니다.