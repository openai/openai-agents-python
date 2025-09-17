---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、ターミナル上でエージェントの挙動を手早く対話的にテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` は、対話のターン間で履歴を保持しつつ、ループでユーザー入力を促します。デフォルトでは、生成され次第モデルの出力をストリーミングします。上の例を実行すると、 run_demo_loop が対話型チャットセッションを開始します。これは継続的に入力を求め、ターン間の会話全体の履歴を記憶します（そのためエージェントは何が議論されたかを把握できます）。また、応答が生成されると同時に、それらをリアルタイムで自動的にストリーミングして表示します。

このチャットセッションを終了するには、`quit` または `exit` と入力して Enter を押すか、`Ctrl-D` キーボードショートカットを使用します。