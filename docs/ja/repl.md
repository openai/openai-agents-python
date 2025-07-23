---
search:
  exclude: true
---
# REPL ユーティリティ

SDK では、素早く対話的にテストできる `run_demo_loop` を提供しています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は、ループ内でユーザー入力を促し、各ターン間で会話履歴を保持します。デフォルトでは、生成されたとおりにモデル出力をストリーミング表示します。ループを終了するには `quit` または `exit` と入力するか、`Ctrl-D` を押してください。