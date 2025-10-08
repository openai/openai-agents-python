---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、`run_demo_loop` を提供しており、ターミナルでエージェントの動作を素早く対話的にテストできます。

  
```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は、ループ内でユーザー入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成されたとおりにモデルの出力をストリーミングします。上の例を実行すると、run_demo_loop はインタラクティブなチャットセッションを開始します。あなたの入力を継続的に求め、ターン間で会話の全履歴を記憶します（そのため、エージェントは何が議論されたかを把握できます）。また、生成されるそばからエージェントの応答を自動的にリアルタイムであなたにストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して（Enter を押す）、もしくは Ctrl-D のキーボードショートカットを使用してください。