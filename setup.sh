#!/bin/bash

# 仮想環境のディレクトリ
VENV_DIR="temp_venv"

# 仮想環境が既に存在するか確認
if [ -d "$VENV_DIR" ]; then
    echo "既存の仮想環境を削除しています..."
    rm -rf "$VENV_DIR"
fi

# 仮想環境を作成
echo "Pythonの仮想環境を作成しています..."
python3 -m venv "$VENV_DIR"

# 仮想環境をアクティベート
echo "仮想環境をアクティベートしています..."
source "$VENV_DIR/bin/activate"

# 依存パッケージをインストール
echo "依存パッケージをインストールしています..."
pip install -r requirements_bot/requirements.txt

# Pythonパスを設定
export PYTHONPATH="$PYTHONPATH:$(pwd)/python"

echo ""
echo "セットアップが完了しました！"
echo "以下のコマンドで要件定義ボットを実行できます："
echo ""
echo "  source $VENV_DIR/bin/activate"
echo "  export PYTHONPATH=\"\$PYTHONPATH:$(pwd)/python\""
echo "  python -m requirements_bot.main"
echo ""
echo "または、ファイルからプロジェクト説明を入力する場合："
echo ""
echo "  cat temp/input.txt | python -m requirements_bot.main"
echo "" 