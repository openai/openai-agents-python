---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、`run_demo_loop` を提供しており、ターミナル上でエージェントの挙動を素早く対話的にテストできます。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は、ループで ユーザー 入力を促し、ターン間で会話履歴を保持します。既定で、生成と同時にモデル出力を ストリーミング します。上の例を実行すると、run_demo_loop は対話型のチャットセッションを開始します。継続的に入力を求め、ターン間で会話履歴全体を保持します（そのため、エージェントは何が議論されたかを把握できます）、そして生成され次第、エージェントの応答をリアルタイムに自動で ストリーミング します。

このチャットセッションを終了するには、`quit` または `exit` と入力（Enter を押下）するか、`Ctrl-D` のキーボードショートカットを使用してください。