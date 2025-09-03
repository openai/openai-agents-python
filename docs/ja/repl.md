---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナル上でエージェントの挙動をすばやく対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成されたモデル出力をそのままストリーミングします。上記の例を実行すると、`run_demo_loop` は対話的なチャットセッションを開始します。入力を継続的に求め、ターン間の会話全体の履歴を記憶するため（エージェントは何が議論されたかを把握できます）、エージェントの応答は生成され次第リアルタイムに自動であなたへストリーミングされます。

このチャットセッションを終了するには、`quit` または `exit` と入力して（Enter キーを押す）か、`Ctrl-D` キーボードショートカットを使用します。