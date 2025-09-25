---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義語です。気にすべきコンテキストには 2 つの大きなクラスがあります。

1. コードからローカルに利用可能なコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に利用可能なコンテキスト: LLM が応答を生成する際に参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その内部の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては dataclass や Pydantic オブジェクトの使用があります。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。`T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

このとき **最重要** な点は、あるエージェント実行において、すべてのエージェント・ツール関数・ライフサイクルなどが同じ _型_ のコンテキストを使わなければならないことです。

コンテキストは次のような用途に使えます。

-   実行のための文脈データ（例: ユーザー名/uid などの ユーザー に関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM には送信されません。あくまでローカルのオブジェクトであり、読み書きやメソッド呼び出しができます。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーでエラーを検出できるよう、エージェントにジェネリックの `UserInfo` を付与しています（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント/LLM のコンテキスト

LLM が呼び出されたとき、参照できるデータは会話履歴のみです。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できる形にする必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加する。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` の呼び出し時に `input` に追加する。これは `instructions` と似た手法ですが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できます。
3. 関数ツールを通じて公開する。これは _オンデマンド_ のコンテキストに有用です。LLM が必要に応じて判断し、ツールを呼び出してそのデータを取得できます。
4. リトリーバルまたは Web 検索を使う。これらは、ファイルやデータベース（リトリーバル）あるいはウェブ（Web 検索）から関連データを取得できる特別なツールです。関連する文脈データに基づいて応答を「根拠付け」するのに有用です。