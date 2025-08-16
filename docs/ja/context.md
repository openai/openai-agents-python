---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語は多義的です。ここでは主に次の 2 つのコンテキストがあります。

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に利用できるコンテキスト: 応答を生成する際に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作の概要は次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

 **最重要** なポイント: 特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは同じ型のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます:

-   実行のための状況データ（例: ユーザー名 / uid やその他のユーザー情報）
-   依存関係（例: logger オブジェクト、データ取得コンポーネントなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。ローカル専用のオブジェクトであり、読み書きやメソッド呼び出しができます。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、ツールの実装はコンテキストから読み取ります。
3. エージェントにジェネリクス `UserInfo` を付けることで、型チェッカーがエラーを検出できます（たとえば、異なるコンテキスト型を取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出されると、参照できるデータは会話履歴のみです。したがって、新しいデータを LLM に利用させたい場合は、その履歴で利用できる形で提供する必要があります。方法はいくつかあります:

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を返す動的関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）に適した方法です。
2. `Runner.run` を呼び出す際の `input` に追加します。これは `instructions` に追加する方法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にあるメッセージを持たせられます。
3. 関数ツールで公開します。これはオンデマンドのコンテキストに便利で、LLM が必要だと判断したときにツールを呼び出してデータを取得できます。
4. リトリーバルや Web 検索を使用します。これらは、ファイルやデータベースから関連データを取得（リトリーバル）したり、Web から取得（Web 検索）したりできる特別なツールです。関連するコンテキストデータに基づいて応答を「グラウンディング」するのに有用です。