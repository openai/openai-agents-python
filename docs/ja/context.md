---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。考慮すべきコンテキストには大きく 2 つの種類があります。

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック時、ライフサイクルフックなどで必要になる可能性があるデータや依存関係です。
2. LLM に提供されるコンテキスト: これは、応答生成時に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その内部の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の  Python  オブジェクトを作成します。一般的なパターンとしては、  dataclass  や  Pydantic  オブジェクトを使います。
2. そのオブジェクトを各種実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` は、`wrapper.context` からアクセスできるコンテキストオブジェクトの型を表します。

注意すべき  **最も重要な点**: あるエージェント実行においては、そのエージェント、ツール関数、ライフサイクルなどがすべて同じ種類（_type_）のコンテキストを使用する必要があります。

コンテキストは次のような用途に使用できます。

-   実行のためのコンテキストデータ（例: ユーザー名 / uid などの  ユーザー  に関する情報）
-   依存関係（例:  logger  オブジェクト、データ取得コンポーネントなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しができます。

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

1. これはコンテキストオブジェクトです。ここでは  dataclass  を使っていますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるように（例えば異なるコンテキスト型を受け取るツールを渡そうとした場合など）、エージェントにはジェネリクスの `UserInfo` を付けます。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴からのものだけです。つまり、新しいデータを LLM に利用させたい場合、そのデータを履歴で参照可能になるように取り込む必要があります。これにはいくつかの方法があります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。常に役立つ情報（例: ユーザーの名前や現在の日付）に適した一般的な手法です。
2. `Runner.run` を呼び出すときの `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位に配置するメッセージを持てます。
3. 関数ツールを通じて公開します。これはオンデマンドのコンテキストに有用です。LLM が必要なときにデータ取得のためにツールを呼び出せます。
4. リトリーバルや Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。これは、関連するコンテキストデータに基づいて応答を根拠付け（グラウンディング）するのに役立ちます。