---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナルでエージェントの動作を素早く対話的にテストできる `run_demo_loop` を提供します。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間で会話履歴を保持します。デフォルトでは、生成と同時にモデル出力をストリーミングします。上記の例を実行すると、run_demo_loop は対話型のチャット セッションを開始します。あなたの入力を継続的に求め、ターン間の会話全体を記憶し（エージェントが何について話したかを把握できるように）、生成と同時にエージェントの応答をリアルタイムで自動ストリーミングします。

チャット セッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` のキーボード ショートカットを使用します。