---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は `run_demo_loop` を提供し、ターミナル上でエージェントの挙動を素早くインタラクティブにテストできます。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間で会話履歴を保持します。デフォルトでは、生成と同時にモデル出力をストリーミングします。上記の例を実行すると、`run_demo_loop` はインタラクティブなチャットセッションを開始します。入力を継続的に求め、ターン間で会話の全履歴を記憶します（そのためエージェントは何が話されたかを把握します）。また、生成と同時にエージェントの応答を自動でリアルタイムにストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して（Enter キーを押す）、または `Ctrl-D` のキーボードショートカットを使用します。