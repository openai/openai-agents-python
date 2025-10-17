---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、ターミナルでエージェントの挙動を迅速に対話的にテストするための `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間で会話履歴を保持します。既定では、生成と同時にモデルの出力をストリーミングします。上の例を実行すると、 run_demo_loop が対話型のチャットセッションを開始します。あなたの入力を継続的に求め、ターン間で会話全体の履歴を記憶し（そのためエージェントは何が話されたかを把握できます）、生成と同時にエージェントの応答をリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` キーボードショートカットを使用します。