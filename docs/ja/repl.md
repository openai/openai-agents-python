---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK には、ターミナル上でエージェントの挙動を素早くインタラクティブにテストできる `run_demo_loop` が用意されています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間で会話履歴を保持します。既定では、生成され次第モデル出力をストリーミングします。上の例を実行すると、`run_demo_loop` がインタラクティブなチャットセッションを開始します。継続的に入力を求め、ターン間で会話全体の履歴を記憶するため（エージェントが何について話したかを把握できます）、生成されると同時にエージェントの応答を自動でリアルタイムにストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` のキーボードショートカットを使用してください。