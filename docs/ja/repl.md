---
search:
  exclude: true
---
# REPL ユーティリティ

`run_demo_loop` を使うと、ターミナルから手軽にエージェントを試せます。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="あなたは親切なアシスタントです")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は入力を繰り返し受け取り、会話履歴を保持したままエージェントを実行します。既定ではストリーミング出力を表示します。
`quit` または `exit` と入力するか `Ctrl-D` を押すと終了します。
