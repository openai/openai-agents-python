# 要件定義ボット - 仮想環境セットアップ

このドキュメントでは、Dockerを使用せずにPython仮想環境で要件定義ボットを実行する方法を説明します。

## 前提条件

- Python 3.8以上がインストールされていること
- pipがインストールされていること

## セットアップ手順

### macOS/Linux環境

1. 環境変数ファイルの準備

`.env` ファイルをプロジェクトのルートディレクトリに作成し、以下のように設定します（既に存在する場合は確認してください）：

```
# OpenAI API設定
OPENAI_API_KEY=あなたのOpenAI APIキー

# モデル設定
DEFAULT_MODEL=gpt-4o

# アプリケーション設定
DEBUG=false
```

2. セットアップスクリプトを実行

```bash
chmod +x setup.sh
./setup.sh
```

これにより、必要な仮想環境と依存パッケージがインストールされます。

### Windows環境

1. 環境変数ファイルの準備

`.env` ファイルをプロジェクトのルートディレクトリに作成し、以下のように設定します（既に存在する場合は確認してください）：

```
# OpenAI API設定
OPENAI_API_KEY=あなたのOpenAI APIキー

# モデル設定
DEFAULT_MODEL=gpt-4o

# アプリケーション設定
DEBUG=false
```

2. セットアップスクリプトを実行

```
setup.bat
```

これにより、必要な仮想環境と依存パッケージがインストールされます。

## 実行方法

### macOS/Linux環境

仮想環境をアクティベートして実行：

```bash
source temp_venv/bin/activate
export PYTHONPATH="$PYTHONPATH:$(pwd)/python"
python -m requirements_bot.main
```

### Windows環境

仮想環境をアクティベートして実行：

```
call temp_venv\Scripts\activate.bat
set PYTHONPATH=%PYTHONPATH%;%cd%\python
python -m requirements_bot.main
```

### ファイルからプロジェクト説明を入力する場合

1. プロジェクト説明を `temp/input.txt` に記述します
2. 以下のコマンドを実行します：

macOS/Linux:
```bash
cat temp/input.txt | python -m requirements_bot.main
```

Windows:
```
type temp\input.txt | python -m requirements_bot.main
```

## 出力ファイル

生成された要件定義ドキュメントは `temp` ディレクトリに保存されます。

## トラブルシューティング

- APIキーエラーが発生する場合は、`.env` ファイルが正しく設定されているか確認してください
- `No module named 'agents'`エラーが発生する場合は、PYTHONPATHが正しく設定されているか確認してください
- Windowsでエンコーディングエラーが発生する場合は、コマンドプロンプトで `chcp 65001` を実行してUTF-8エンコーディングを有効にしてください

## 仮想環境のクリーンアップ

不要になった場合は、以下のコマンドで仮想環境を削除できます：

macOS/Linux:
```bash
rm -rf temp_venv
```

Windows:
```
rmdir /s /q temp_venv
```

## 注意事項

- `.env` ファイルには機密情報が含まれているため、Gitリポジトリにコミットしないでください
- APIキーの料金プランに応じて使用料金が発生します 