---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は対話型テストをすばやく行うために `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループ内でユーザー入力を促し、各ターン間で会話履歴を保持します。デフォルトでは生成されたモデル出力をストリーミングします。ループを終了するには `quit` または `exit` と入力するか、`Ctrl-D` を押してください。