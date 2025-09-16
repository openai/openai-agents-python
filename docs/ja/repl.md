---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK には、ターミナル上でエージェントの挙動をすばやく対話的にテストできる `run_demo_loop` が用意されています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間で会話履歴を保持します。デフォルトでは、生成と同時にモデルの出力を ストリーミング します。上記の例を実行すると、 run_demo_loop は対話型のチャットセッションを開始します。継続的に入力を求め、ターン間の会話全体の履歴を記憶します（エージェントが何が話されたか把握できるように）。さらに、生成と同時にエージェントの応答をリアルタイムで自動 ストリーミング します。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` のキーボードショートカットを使用します。