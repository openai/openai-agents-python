---
search:
  exclude: true
---
# REPL ユーティリティ

SDK は、お使いのターミナルでエージェントの動作を手早くインタラクティブにテストできる `run_demo_loop` を提供します。

```python
import asyncio
from agents import Agent, run_demo_loop

async def main() -> None:
    agent = Agent(name="Assistant", instructions="You are a helpful assistant.")
    await run_demo_loop(agent)

if __name__ == "__main__":
    asyncio.run(main())
```

`run_demo_loop` はループでユーザー入力を促し、ターン間の会話履歴を保持します。デフォルトでは、生成され次第、モデルの出力をストリーミングします。上の例を実行すると、 run_demo_loop がインタラクティブなチャットセッションを開始します。継続的に入力を求め、ターン間で会話の全履歴を保持します（そのため、エージェントは何が話されたかを把握できます）。そして、生成されるそばからエージェントの応答をリアルタイムで自動的にストリーミングします。

このチャットセッションを終了するには、 `quit` または `exit` と入力して（ Enter キーを押す）か、 `Ctrl-D` のキーボードショートカットを使用してください。