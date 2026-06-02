---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、ターミナルでエージェントの動作を直接すばやく対話的にテストするための `run_demo_loop` を提供します。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループ内でユーザー入力を求め、ターン間の会話履歴を保持します。デフォルトでは、生成されるモデル出力をストリーミングします。上記の例を実行すると、run_demo_loop は対話型チャットセッションを開始します。入力を継続的に求め、ターン間の会話履歴全体を記憶し（そのためエージェントはこれまでに話し合われた内容を把握できます）、エージェントの応答が生成されるとリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、単に `quit` または `exit` と入力して Enter キーを押すか、`Ctrl-D` キーボードショートカットを使用します。