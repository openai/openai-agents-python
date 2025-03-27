"""
要件定義ボットの設定ファイル
環境変数から設定を読み込みます。
"""
import os
from typing import Optional
from pathlib import Path

# python-dotenvライブラリを使用して.envファイルから環境変数を読み込む
try:
    from dotenv import load_dotenv
    # .envファイルのパスを設定
    env_path = Path(__file__).resolve().parent.parent / '.env'
    # .envファイルが存在する場合は読み込む
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        print(f".envファイルを読み込みました: {env_path}")
    else:
        print(f".envファイルが見つかりません: {env_path}")
except ImportError:
    print("警告: python-dotenvがインストールされていません。環境変数から直接読み込みます。")

# 環境変数から設定を読み込む
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
DEFAULT_MODEL: str = os.getenv("DEFAULT_MODEL", "gpt-4o")
DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

# 設定値の検証
if not OPENAI_API_KEY:
    print("警告: OPENAI_API_KEYが設定されていません。APIの呼び出しが失敗する可能性があります。")

def get_setting(key: str, default: Optional[str] = None) -> str:
    """
    指定されたキーの設定値を取得します。
    環境変数に設定されていない場合はデフォルト値を返します。
    
    Args:
        key: 設定キー
        default: デフォルト値
        
    Returns:
        設定値
    """
    return os.getenv(key, default) 