---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。重要なのは次の 2 つのクラスです。

1. コードからローカルで利用可能なコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に提供されるコンテキスト: 応答を生成する際に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

**最も重要** な注意点: 特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは同じコンテキストの型を使用する必要があります。

コンテキストは次のような用途に使えます:

-   実行時のコンテキストデータ（例: ユーザー名/uid やその他のユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取っているのが分かります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーでエラーを検出できるように（例えば、異なるコンテキスト型を受け取るツールを渡した場合など）、エージェントにジェネリクス `UserInfo` を付けています。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント/LLM のコンテキスト

LLM が呼び出されるとき、LLM が参照できるのは会話履歴からのデータのみです。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できる形で提供しなければなりません。方法はいくつかあります:

1. エージェントの `instructions` に追加します。これは「system prompt」や「developer message」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` 関数を呼ぶときの `input` に追加します。これは `instructions` を使う方法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)でより下位のメッセージを持たせられます。
3. 関数ツール経由で公開します。これは _オンデマンド_ のコンテキストに有用です。LLM は必要なときにデータが必要だと判断し、ツールを呼び出してそのデータを取得できます。
4. リトリーバルまたは Web 検索を使います。これらは、ファイルやデータベース（リトリーバル）、あるいは Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに応答を「グラウンディング」するのに有用です。