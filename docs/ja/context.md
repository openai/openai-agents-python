---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。考慮すべきコンテキストには主に 2 つの種類があります。

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になる可能性のあるデータや依存関係です。
2. LLM に利用可能なコンテキスト: これは、応答を生成する際に LLM が目にするデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンは dataclass や Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**))`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

 **最重要** な注意点: 特定のエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じコンテキストの _型_ を使用する必要があります。

コンテキストは次のような用途に使えます。

-   実行のためのコンテキストデータ（例: ユーザー名/uid や、ユーザー に関するその他の情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注記"

    コンテキストオブジェクトは LLM には送信されません。これは純粋にローカルのオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるよう、エージェントにジェネリクス `UserInfo` を付けています（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴に含まれるものだけです。つまり、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できる形で提供する必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。これは常に有用な情報（例: ユーザー の名前や現在の日付）に一般的な手法です。
2. `Runner.run` 関数を呼び出すときに `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位に配置されるメッセージにできます。
3. 関数ツール を通じて公開します。これは _オンデマンド_ のコンテキストに有用です。LLM が必要に応じてデータを取得するタイミングを判断し、ツールを呼び出してそのデータを取得できます。
4. リトリーバル や Web 検索 を使用します。これらは、ファイルやデータベース（リトリーバル）またはウェブ（Web 検索）から関連データを取得できる特別なツールです。これは、応答を関連するコンテキストデータで「グラウンディング」するのに有用です。