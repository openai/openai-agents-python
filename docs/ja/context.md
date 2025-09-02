---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語は多義的です。考慮すべきコンテキストには大きく 2 つの種類があります。

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に提供されるコンテキスト: これは、応答を生成する際に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティを通じて表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。`T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

 **最重要** な点: 特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは、同じコンテキストの型を使用する必要があります。

コンテキストは次のような用途に使えます:

-   実行のためのコンテキストデータ（例: ユーザー名 / uid など、ユーザーに関するその他の情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM に **送信されません**。これは純粋にローカルなオブジェクトであり、読み取り・書き込み・メソッド呼び出しが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるように、エージェントにジェネリクス `UserInfo` を付与しています（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴のもの **のみ** です。つまり、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できるようにする必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは固定文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` を呼び出すときの `input` に追加します。これは `instructions` の手法に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) においてより下位のメッセージを持たせることができます。
3. 関数ツールを通じて公開します。これはオンデマンドのコンテキストに有用で、LLM が必要に応じてそのデータを取得するためにツールを呼び出せます。
4. ファイル検索 または Web 検索 を使用します。これらは、ファイルやデータベース（ファイル検索）または Web（Web 検索）から関連データを取得できる特別なツールです。これは、応答を関連するコンテキストデータに「グラウンディング」するのに有用です。