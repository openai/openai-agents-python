---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK には、ターミナル上でエージェントの挙動を素早く対話的にテストできる `run_demo_loop` が用意されています。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間で会話履歴を保持します。デフォルトでは、生成されたモデル出力をそのままストリーミングします。上記の例を実行すると、`run_demo_loop` は対話的なチャットセッションを開始します。継続的に入力を求め、ターン間で会話全体の履歴を記憶し（そのためエージェントは何が話されたかを把握します）、生成と同時にエージェントの応答をリアルタイムで自動的にストリーミングします。

チャットセッションを終了するには、`quit` または `exit` と入力して（そして Enter を押す）、もしくは `Ctrl-D` のキーボードショートカットを使用してください。