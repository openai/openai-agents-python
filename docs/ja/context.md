---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語には複数の意味があります。ここで重要になるコンテキストは大きく 2 つのクラスに分かれます。

1. ローカルにコードから利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に提供されるコンテキスト: 応答を生成する際に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

意識すべき  **最も重要な**  点: 特定のエージェントの実行において、すべてのエージェント、ツール関数、ライフサイクルなどは同一の「コンテキストの型」を使わなければなりません。

コンテキストは次のような用途に使えます。

- 実行のための文脈データ（例: ユーザー名/uid やその他のユーザーに関する情報）
- 依存関係（例: ロガーオブジェクト、データフェッチャーなど）
- ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に送信され  **ません** 。これは純粋にローカルなオブジェクトで、読み書きやメソッド呼び出しが可能です。

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner, function_tool

@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int

@function_tool
async def fetch_user_age(wrapper: RunContextWrapper[UserInfo]) -> str:  # (2)!
    """Fetch the age of the user. Call this function to get user's age information."""
    return f"The user {wrapper.context.name} is 47 years old"

async def main():
    user_info = UserInfo(name="John", uid=123)

    agent = Agent[UserInfo](  # (3)!
        name="Assistant",
        tools=[fetch_user_age],
    )

    result = await Runner.run(  # (4)!
        starting_agent=agent,
        input="What is the age of the user?",
        context=user_info,
    )

    print(result.final_output)  # (5)!
    # The user John is 47 years old.

if __name__ == "__main__":
    asyncio.run(main())
```

1. これはコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使えます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装はコンテキストから読み取ります。
3. エージェントにはジェネリック型 `UserInfo` を指定して、型チェッカーがエラーを検出できるようにします（たとえば、異なるコンテキスト型を受け取るツールを渡してしまった場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴のもの  **のみ** です。つまり、新しいデータを LLM に利用させたい場合は、そのデータが履歴に現れるようにする必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）に適した方法です。
2. `Runner.run` を呼び出すときの `input` に追加します。これは `instructions` の戦略に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)の下位にメッセージを配置できます。
3. 関数ツールで公開します。これは _オンデマンド_ のコンテキストに有用です。LLM が必要に応じて判断し、そのデータを取得するためにツールを呼び出せます。
4. リトリーバルや Web 検索を使います。これらは、ファイルやデータベース（リトリーバル）、または Web（Web 検索）から関連データを取得できる特別なツールです。これは、関連する文脈データで応答を「グラウンディング」するのに役立ちます。