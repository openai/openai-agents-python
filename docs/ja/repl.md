---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、ターミナルでエージェントの動作をすばやく対話形式でテストするための `run_demo_loop` を提供します。


```python
import asyncio
from agents import Agent, run_demo_loop


async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)


if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループ内でユーザー入力を求め、ターン間の会話履歴を保持します。デフォルトでは、モデルの出力を生成と同時にストリーミングします。上記の例を実行すると、run_demo_loop が対話型のチャットセッションを開始します。継続的に入力を求め、ターン間の会話履歴全体を記憶し（そのため、エージェントはこれまでに話し合った内容を把握できます）、生成されるエージェントの応答をリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter キーを押すか、`Ctrl-D` キーボードショートカットを使用します。