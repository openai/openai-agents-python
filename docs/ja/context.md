---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。ここでは主に次の 2 つのコンテキストについて扱います。

1. コードからローカルに利用可能なコンテキスト: これはツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に利用可能なコンテキスト: これは LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを用います。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフック等には、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

最も重要な注意点: 特定のエージェント実行において、そのエージェント、ツール関数、ライフサイクル等はすべて同じ「型」のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行に関する状況データ（例: ユーザー名や uid などの ユーザー 情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に送信されません。読み書きやメソッド呼び出しが可能な、純粋にローカルのオブジェクトです。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、ツールの実装がコンテキストから読み取っています。
3. エージェントにジェネリック型 `UserInfo` を指定することで、型チェッカーがエラーを検出できます（たとえば、異なるコンテキスト型を取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出されると、参照できるのは会話履歴のみです。つまり、LLM に新しいデータを利用可能にしたい場合は、その履歴に含める形で渡す必要があります。いくつかの方法があります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でもかまいません。常に有用な情報（例: ユーザー名や現在の日付）に適した手法です。
2. `Runner.run` を呼ぶときの `input` に追加します。これは `instructions` の手法に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) においてより下位のメッセージとして与えられます。
3. 関数ツールで公開します。これはオンデマンドのコンテキストに有用で、LLM が必要に応じてツールを呼び出し、そのデータを取得できます。
4. リトリーバル (retrieval) や Web 検索を使用します。これらは、ファイルやデータベースから関連データを取得（リトリーバル）したり、Web（Web 検索）から取得したりできる特別なツールです。関連するコンテキスト データで応答を根拠付けるのに役立ちます。