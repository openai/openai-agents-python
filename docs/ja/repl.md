---
search:
  exclude: true
---
# REPL ユーティリティ

この SDK は、ターミナルでエージェントの動作を素早くインタラクティブにテストできる `run_demo_loop` を提供します。


```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループ内でユーザー入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成されたモデル出力をそのままストリーミングします。上の例を実行すると、`run_demo_loop` はインタラクティブなチャットセッションを開始します。入力を継続的に求め、ターン間の全会話履歴を記憶するため（エージェントが何について話したかを把握できます）、エージェントの応答を生成と同時にリアルタイムで自動的にあなたへストリーミングします。

このチャットセッションを終了するには、`quit` または `exit` と入力して（Enter を押す）か、`Ctrl-D` キーボードショートカットを使用します。