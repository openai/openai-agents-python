# 要件定義ボット - Docker環境

このドキュメントでは、Docker環境で要件定義ボットを実行する方法を説明します。

## 前提条件

- Docker がインストールされていること
- Docker Compose がインストールされていること

## セットアップ手順

1. 環境変数ファイルの準備

`.env` ファイルを作成し、以下のように設定します（既に存在する場合は確認してください）：

```
# OpenAI API設定
OPENAI_API_KEY=あなたのOpenAI APIキー

# モデル設定
DEFAULT_MODEL=gpt-4o

# アプリケーション設定
DEBUG=false
```

2. Dockerイメージのビルド

```bash
docker-compose build
```

## 実行方法

### インタラクティブモードで実行する

```bash
docker-compose run --rm requirements-bot
```

これを実行すると、プロジェクトの説明を入力するよう求められます。

### ファイルからプロジェクト説明を入力する場合

1. プロジェクト説明を `temp/input.txt` に記述します
2. 以下のコマンドを実行します：

```bash
docker-compose run --rm requirements-bot bash -c "cat /app/temp/input.txt | python -m requirements_bot.main"
```

## 出力ファイル

生成された要件定義ドキュメントは `temp` ディレクトリに保存されます。

## トラブルシューティング

- APIキーエラーが発生する場合は、`.env` ファイルが正しく設定されているか確認してください
- コンテナ内で問題が発生した場合、以下のコマンドでシェルに入ることができます：

```bash
docker-compose run --rm requirements-bot bash
```

- ファイル権限の問題がある場合は、以下のコマンドを実行してください：

```bash
sudo chown -R $(id -u):$(id -g) temp/
```

## 注意事項

- `.env` ファイルには機密情報が含まれているため、Gitリポジトリにコミットしないでください
- 本番環境で使用する場合は、適切なセキュリティ対策を講じてください
- APIキーの料金プランに応じて使用料金が発生します 