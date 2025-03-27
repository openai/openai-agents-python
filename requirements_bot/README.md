# 要件定義ボット (Requirements Bot)

要件定義ボットは、プロジェクトの説明からプロフェッショナルな要件定義ドキュメントを自動生成するAIエージェントシステムです。このツールは、OpenAI Agents SDKを使用して構築されており、複数のAIエージェントが協調して要件定義プロセスを実行します。

## 主な機能

1. **要件分析（Analyzer Agent）**: プロジェクト説明から機能要件と非機能要件を抽出・分析
2. **要件詳細化（Refiner Agent）**: 抽出された要件を技術的な詳細仕様に展開
3. **ドキュメント生成（Document Agent）**: 分析結果と詳細仕様から公式な要件定義ドキュメントを作成

## インストール方法

1. Python環境のセットアップ

```bash
python3 -m venv env
source env/bin/activate  # Windows: env\Scripts\activate
```

2. 依存パッケージのインストール

```bash
# 必要なパッケージをすべてインストール
pip3 install openai-agents rich
```

3. OpenAI APIキーの設定

APIキーを設定するには2つの方法があります：

**方法1: 設定ファイルを使用（推奨）**

`requirements_bot/config.py`ファイルを作成し、以下のように記述します：

```python
# OpenAI APIキー
OPENAI_API_KEY = "あなたのAPIキーをここに記述"

# その他の設定（必要に応じて）
DEFAULT_MODEL = "gpt-4o"
```

**方法2: 環境変数を使用**

```bash
export OPENAI_API_KEY=あなたのAPIキーをここに記述  # Windows: set OPENAI_API_KEY=your_api_key_here
```

## 使用方法

### コマンドラインインターフェース

```bash
python3 -m requirements_bot.main
```

実行すると、プロジェクトの説明を入力するよう求められます。詳細な説明を入力することで、より正確な要件定義が可能になります。入力を終了するには、新しい行でCtrl+D（Unix/Mac）またはCtrl+Z（Windows）を押してください。

入力がない場合は、サンプルプロジェクト（オンライン予約システム）の説明が使用されます。

### 開発環境での実行

リポジトリをクローンして開発する場合は、以下の手順でセットアップしてください：

```bash
# リポジトリのルートディレクトリで
export PYTHONPATH=$PYTHONPATH:$(pwd)/python
python3 -m requirements_bot.main
```

または、pythonディレクトリに移動して実行することもできます：

```bash
cd python
python3 -m requirements_bot.main
```

### Windowsでの実行（WSL推奨）

Windowsでは、WSL（Windows Subsystem for Linux）を使用して実行することを推奨します。

```bash
# WSL環境内で
export PYTHONPATH=$PYTHONPATH:$(pwd)/python
python3 -m requirements_bot.main
```

PowerShellでの実行：

```powershell
# PowerShellで
$env:PYTHONPATH = "$env:PYTHONPATH;$(pwd)\python"
python3 -m requirements_bot.main
```

## エージェントの詳細

### 1. 要件分析エージェント（Analyzer Agent）
- プロジェクト説明を分析し、要件を抽出
- 各要件にIDと優先度を割り当て
- ステークホルダーや前提条件を特定

### 2. 要件詳細化エージェント（Refiner Agent）
- 各要件を技術的な詳細に展開
- 要件間の依存関係を特定
- 実装に関するリスクを評価

### 3. ドキュメント作成エージェント（Document Agent）
- 分析結果と詳細仕様から公式ドキュメントを作成
- 異なるステークホルダー向けの情報を適切に整理
- システムアーキテクチャの概要を提供

## トレーシング

要件定義プロセスの全ステップはOpenAIのトレーシングシステムで記録され、URLが表示されます。これにより、エージェントの動作を詳細に確認することができます。

## 出力例

要件定義プロセスが完了すると、以下の情報が表示されます：

- 要件定義書のタイトルとバージョン
- エグゼクティブサマリー
- 機能要件の詳細
- 非機能要件の詳細
- 実装ロードマップ

## トラブルシューティング

- `No module named 'agents'`エラー：「開発環境での実行」セクションの手順に従って、PYTHONPATHを設定してください
- `No module named 'rich'`エラー：依存パッケージ（rich）が不足しています。`pip3 install rich`を実行してください
- APIキーエラー：`config.py`ファイルが正しく設定されているか確認するか、環境変数を設定してください
- Windowsで`UnicodeEncodeError`が発生する場合：コマンドプロンプトで`chcp 65001`を実行してUTF-8エンコーディングを有効にするか、WSLを使用してください
- プログレスバーが正しく表示されない場合：ターミナルの設定を確認し、適切なエンコーディングと文字セットを設定してください

## 注意事項

- 生成される要件定義書はプロジェクトの出発点として活用し、専門家によるレビューを推奨します
- より高品質な結果を得るには、プロジェクト説明に具体的な情報を含めてください
- APIキーの料金プランに応じて使用料金が発生します
- `config.py`ファイルは公開リポジトリにコミットしないでください（機密情報保護のため）
- Windowsでの実行時は、WSLまたはPowerShellの使用を推奨します 