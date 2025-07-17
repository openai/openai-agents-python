---
search:
  exclude: true
---
# REPL ユーティリティ

SDK には、対話的なテストを素早く行うための `run_demo_loop` が用意されています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループ内でユーザー入力を求め、ターン間で会話履歴を保持します。デフォルトでは、生成されたモデル出力をストリーミング表示します。`quit` または `exit` と入力するか、`Ctrl-D` を押すとループを終了します。