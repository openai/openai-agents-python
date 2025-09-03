---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。関心を持つべきコンテキストには、主に次の 2 つのクラスがあります。

1. コードでローカルに利用可能なコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック時、ライフサイクルフック内などで必要になる可能性があるデータや依存関係です。
2. LLM に利用可能なコンテキスト: これは、LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとして、dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も重要なポイント: あるエージェントの実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ型のコンテキストを使用する必要があります。

コンテキストは次のような用途に使えます:

-   実行のためのコンテキストデータ（例: ユーザー名 / uid やその他のユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトであり、読み書きしたり、そのメソッドを呼び出したりできます。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツール実装はコンテキストから読み取ります。
3. エージェントにジェネリクス `UserInfo` を指定し、型チェッカーがエラーを検出できるようにします（例えば、異なるコンテキスト型を取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、参照できるデータは会話履歴に含まれるもののみです。つまり、LLM に新しいデータを利用可能にしたい場合は、その履歴で参照できる形で提供しなければなりません。これにはいくつかの方法があります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。これは常に有用な情報（例: ユーザー名や現在日付）の提供に一般的な戦術です。
2. `Runner.run` 関数を呼び出すときに `input` に追加します。これは `instructions` の戦術に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) においてより下位のメッセージを持つことができます。
3. function tools を介して公開します。これはオンデマンドのコンテキストに有用です。LLM は必要なデータがあると判断したときにツールを呼び出してそのデータを取得できます。
4. 検索（retrieval）や Web 検索を使用します。これらは、ファイルやデータベース（retrieval）もしくは Web（Web 検索）から関連データを取得できる特別なツールです。これは、関連するコンテキストデータに基づいて応答をグラウンディングするのに有用です。