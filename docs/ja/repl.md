---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK には、`run_demo_loop` が用意されており、端末上でエージェントの動作を素早く対話的にテストできます。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は、ループでユーザー入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成され次第モデル出力をストリーミングします。上記の例を実行すると、`run_demo_loop` が対話型チャットセッションを開始します。継続的に入力を求め、ターン間の会話履歴全体を記憶し（これによりエージェントは何が議論されたかを把握できます）、生成と同時にエージェントの応答をリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` のキーボードショートカットを使用します。