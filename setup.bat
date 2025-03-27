@echo off
setlocal

:: 仮想環境のディレクトリ
set VENV_DIR=temp_venv

:: 仮想環境が既に存在するか確認
if exist %VENV_DIR% (
    echo 既存の仮想環境を削除しています...
    rmdir /s /q %VENV_DIR%
)

:: 仮想環境を作成
echo Pythonの仮想環境を作成しています...
python -m venv %VENV_DIR%

:: 仮想環境をアクティベート
echo 仮想環境をアクティベートしています...
call %VENV_DIR%\Scripts\activate.bat

:: 依存パッケージをインストール
echo 依存パッケージをインストールしています...
pip install -r requirements_bot\requirements.txt

:: Pythonパスを設定
set PYTHONPATH=%PYTHONPATH%;%cd%\python

echo.
echo セットアップが完了しました！
echo 以下のコマンドで要件定義ボットを実行できます：
echo.
echo   call %VENV_DIR%\Scripts\activate.bat
echo   set PYTHONPATH=%%PYTHONPATH%%;%cd%\python
echo   python -m requirements_bot.main
echo.
echo または、ファイルからプロジェクト説明を入力する場合：
echo.
echo   type temp\input.txt | python -m requirements_bot.main
echo.

endlocal 