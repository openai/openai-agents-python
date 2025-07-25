---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、迅速な対話テスト用に `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、各ターン間の会話履歴を保持します。デフォルトでは、生成されると同時にモデル出力をストリーミングします。ループを終了するには `quit` または `exit` と入力するか、`Ctrl-D` を押してください。