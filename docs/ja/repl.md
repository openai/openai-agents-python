---
search:
  exclude: true
---
# REPL ユーティリティ

 SDK には、ターミナルでエージェントの挙動を素早くインタラクティブにテストできる `run_demo_loop` が用意されています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

 `run_demo_loop` はループ内で ユーザー からの入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成されるそばからモデルの出力を ストリーミング します。上記の例を実行すると、 run_demo_loop はインタラクティブなチャットセッションを開始します。入力を継続的に受け取り、ターンごとに会話履歴を保持することで（エージェントがこれまでのやり取りを把握できます）、生成された応答をリアルタイムで自動的に ストリーミング します。

 このチャットセッションを終了するには、`quit` または `exit` と入力して Enter キーを押すか、 `Ctrl-D` キーボードショートカットを使用してください。