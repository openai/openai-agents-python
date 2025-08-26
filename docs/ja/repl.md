---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナル上でエージェントの挙動をすばやく対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間の会話履歴を保持します。既定では、生成され次第モデルの出力をストリーミングします。上の例を実行すると、 run_demo_loop が対話型のチャットセッションを開始します。継続的に入力を尋ね、ターン間の会話履歴全体を記憶するため（エージェントがこれまでの議論内容を把握できます）、生成と同時にエージェントの応答をリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、 `quit` または `exit` と入力して Enter を押すか、 `Ctrl-D` のキーボードショートカットを使用します。