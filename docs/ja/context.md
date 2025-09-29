---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。考慮すべきコンテキストには主に 2 つのクラスがあります。

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に利用できるコンテキスト: これは、応答を生成する際に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` 経由でアクセスできます。

 **最重要** な注意点: 特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは同じコンテキストの型を使用しなければなりません。

コンテキストは次のような用途に使えます:

-   実行時のコンテキストデータ（例: ユーザー名/uid やその他のユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しができます。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使えます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツール実装はコンテキストから読み取ります。
3. エージェントにジェネリック型 `UserInfo` を付けることで、型チェッカーがエラーを検知できるようにします（たとえば、異なるコンテキスト型を取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント/LLM のコンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは対話履歴にあるもの **のみ** です。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照可能な形で提供する必要があります。方法はいくつかあります:

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。これは常に有用な情報（例: ユーザー名や現在の日付）に一般的な戦略です。
2. `Runner.run` 関数を呼び出す際の `input` に追加します。これは `instructions` の戦略に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にあるメッセージを持たせることができます。
3. 関数ツールを通じて公開します。これは _オンデマンド_ のコンテキストに有用で、LLM が必要なときにデータ取得のためにツールを呼び出せます。
4. リトリーバル（retrieval）や Web 検索を使用します。これらは、ファイルやデータベースから関連データを取得する（リトリーバル）、または Web から取得する（Web 検索）特別なツールです。これは、関連するコンテキストデータに基づいて応答を「根拠付け（grounding）」するのに有用です。