---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK には、ターミナルでエージェントの挙動を迅速かつ対話的にテストできる `run_demo_loop` が用意されています。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成中のモデル出力をそのままストリーミングします。上の例を実行すると、`run_demo_loop` が対話的なチャットセッションを開始します。以後、入力を継続的に尋ね、各ターン間で会話全体の履歴を記憶するため（エージェントは何が話されたかを把握できます）、生成されるのと同時にエージェントの応答をリアルタイムで自動ストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、キーボードショートカットの Ctrl-D を使用してください。