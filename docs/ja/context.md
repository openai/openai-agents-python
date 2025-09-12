---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。考慮すべき主なコンテキストには次の 2 種類があります。

1. コードでローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック中、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に利用できるコンテキスト: これは、応答を生成する際に LLM が参照できるデータです。

## ローカルコンテキスト

これは、[`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出し、ライフサイクルフックなどには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

**最も重要** な注意点: 特定のエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ _型_ のコンテキストを使用する必要があります。

コンテキストは次のような用途に使えます:

-   実行のための文脈データ（例: ユーザー名 / uid などユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に送信されるわけではありません。これは純粋にローカルなオブジェクトであり、そこから読み取り、書き込み、メソッド呼び出しができます。

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
3. 型チェッカーでエラーを検出できるよう、エージェントにジェネリック `UserInfo` を付与します（例えば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されるとき、LLM が参照できる **唯一の** データは会話履歴からのものです。したがって、新しいデータを LLM に利用可能にしたい場合、その履歴で参照できる形で提供する必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でもかまいません。これは常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` を呼び出すときの `input` に追加します。これは `instructions` の手法に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) においてより下位のメッセージとして追加できます。
3. 関数ツールを通じて公開します。これは _オンデマンド_ のコンテキストに有用です。LLM が必要に応じてデータを要求し、ツールを呼び出してそのデータを取得できます。
4. リトリーバルまたは Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。関連する文脈データに基づいて応答を「グラウンディング」するのに有用です。