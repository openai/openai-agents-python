---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナルで直接、エージェントの動作を素早く対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間で会話履歴を保持します。デフォルトでは、生成と同時にモデル出力をストリーミングします。上記の例を実行すると、`run_demo_loop` は対話型のチャットセッションを開始します。継続的に入力を求め、ターン間の会話履歴全体を記憶し（そのためエージェントが何について話したかを把握できます）、生成され次第、エージェントの応答を自動的にリアルタイムでストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して（ Enter キーを押す）、または `Ctrl-D` のキーボードショートカットを使用します。