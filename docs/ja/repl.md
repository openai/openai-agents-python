---
search:
  exclude: true
---
# REPL ユーティリティ

SDK では、クイックなインタラクティブテストを行うために `run_demo_loop` を提供しています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループ内でユーザー入力を促し、ターン間で会話履歴を保持します。デフォルトでは、生成され次第モデルの出力を ストリーミング します。ループを終了するには `quit` または `exit` と入力するか（または Ctrl-D を押してください）。