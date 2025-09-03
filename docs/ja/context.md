---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。主に次の 2 つのクラスのコンテキストがあります。

1. コードでローカルに利用可能なコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック内、ライフサイクルフック内などで必要となるデータや依存関係です。
2. LLM に対して利用可能なコンテキスト: これは、応答を生成するときに LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティを通じて表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては、dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトをさまざまな実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出し、ライフサイクルフックなどに、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` 経由でアクセスできます。

最も **重要** な注意点: あるエージェント実行においては、そのエージェント、ツール関数、ライフサイクルなどのすべてで、同じ型のコンテキストを使う必要があります。

コンテキストは次のような用途に使えます:

- 実行のためのコンテキストデータ（例: ユーザー名/uid など、ユーザーに関する情報）
- 依存関係（例: ロガーオブジェクト、データフェッチャーなど）
- ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM には送信されません。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使えます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることが分かります。ツールの実装はコンテキストから読み取ります。
3. ジェネリクスの `UserInfo` をエージェントに付与し、型チェッカーがエラーを検出できるようにします（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント/ LLM のコンテキスト

LLM が呼び出されると、参照できるデータは会話履歴にあるものだけです。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できるようにする必要があります。これには次の方法があります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。これは常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` 関数を呼び出すときに `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できます。
3. 関数ツールを通じて公開します。これはオンデマンドなコンテキストに有用です。LLM が必要になったときにデータを取得するためにツールを呼び出せます。
4. リトリーバルまたは Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。これは、関連するコンテキストデータに応答を「グラウンディング」するのに有用です。