---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は `run_demo_loop` を提供しており、ターミナルでエージェントの挙動を素早く対話的にテストできます。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループで ユーザー 入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成されると同時にモデルの出力を ストリーミング 配信します。上記の例を実行すると、 run_demo_loop が対話型チャット セッションを開始します。これにより継続的に入力を求め、ターン間の会話履歴を記憶して（エージェントが何を話したか把握できるようにし）、生成されたレスポンスをリアルタイムで自動 ストリーミング します。

チャット セッションを終了するには、`quit` または `exit` と入力して Enter キーを押すか、`Ctrl-D` のキーボードショートカットを使用してください。