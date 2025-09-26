---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK には、ターミナルでエージェントの動作をすばやく対話的にテストできる `run_demo_loop` が用意されています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間の会話履歴を保持します。既定では、生成と同時にモデル出力をストリーミングします。上の例を実行すると、run_demo_loop は対話型のチャットセッションを開始します。入力を継続的に求め、ターン間の会話全体の履歴を記憶し（そのためエージェントは何が話されたかを把握します）、生成と同時にエージェントの応答をリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter キーを押すか、`Ctrl-D` キーボードショートカットを使用します。