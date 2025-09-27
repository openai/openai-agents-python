---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、ターミナルで直接エージェントの動作を素早く対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は会話の履歴をターン間で保持しながら、ループでユーザー入力を促します。デフォルトでは、生成され次第モデルの出力をストリーミングします。上記の例を実行すると、`run_demo_loop` が対話型チャットセッションを開始します。入力を継続的に求め、ターン間の会話全体の履歴を記憶するため（エージェントが何について話したかを把握できます）、生成と同時にエージェントの応答をリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter キーを押すか、`Ctrl-D` のキーボードショートカットを使用します。