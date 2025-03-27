"""
要件定義ボットのAPIハンドラー
Next.jsからの呼び出しに対応するためのスクリプト
"""
import sys
import os
import json
import asyncio
import argparse
import traceback
from typing import Dict, Any
from datetime import datetime

# 環境変数からAPIキーを確認
if not os.environ.get("OPENAI_API_KEY"):
    try:
        from .config import OPENAI_API_KEY
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
        print(f"APIキーを設定ファイルから読み込みました")
    except ImportError:
        print("警告: config.pyファイルが見つかりません。環境変数からAPIキーを使用します。")
        if not os.environ.get("OPENAI_API_KEY"):
            print("エラー: OPENAI_API_KEYが環境変数にも設定されていません。")
            sys.exit(1)
else:
    print("APIキーを環境変数から読み込みました")

# デバッグ情報を出力
print(f"現在の作業ディレクトリ: {os.getcwd()}")
print(f"Pythonバージョン: {sys.version}")
print(f"現在のPYTHONPATH: {sys.path}")

# agentsモジュールを確実にインポートできるようにパス設定
project_root = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
python_dir = os.path.join(project_root, 'python')
if python_dir not in sys.path:
    sys.path.insert(0, python_dir)
    print(f"パスに追加しました: {python_dir}")

# 追加のモジュール検索のため、現在のディレクトリも追加
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)
    print(f"現在のディレクトリをパスに追加しました: {current_dir}")

try:
    # まずimport文を試みる
    try:
        from agents import Runner, gen_trace_id, trace
        print("agentsモジュールのインポートに成功しました")
    except ImportError as e:
        print(f"agentsモジュールのインポートに失敗しました: {e}")
        # サードパーティのパッケージのインストール状況を確認
        try:
            import pkg_resources
            print("インストール済みパッケージ:")
            for d in pkg_resources.working_set:
                print(f" - {d.project_name} {d.version}")
        except ImportError:
            print("pkg_resourcesのインポートに失敗しました")
        
        # 絶対インポートを試みる
        print("絶対インポートを試みます...")
        import agents
        print(f"agentsモジュールの場所: {agents.__file__}")
        from agents import Runner, gen_trace_id, trace
    
    from .manager import RequirementsManager
    from .html_exporter import export_to_html
    print("必要なモジュールをインポートしました")
except ImportError as e:
    print(f"インポートエラー: {e}")
    print(f"現在のPYTHONPATH: {sys.path}")
    print("スタックトレース:")
    traceback.print_exc()
    
    # 以下のような回避策を試みる
    try:
        print("回避策を試みています...")
        # 外部コマンドを実行してagentsパッケージの場所を探す
        import subprocess
        result = subprocess.run(["python3", "-c", "import sys; print(sys.path)"], 
                               capture_output=True, text=True)
        print(f"システムのPythonパス: {result.stdout}")
        
        # ファイルシステムを確認
        import glob
        print("pythonディレクトリの内容:")
        for f in glob.glob(os.path.join(python_dir, "*")):
            print(f" - {f}")
    except Exception as ex:
        print(f"回避策の実行中にエラーが発生しました: {ex}")
    
    sys.exit(1)

async def generate_requirements(
    project_description: str,
    project_name: str,
    output_dir: str
) -> Dict[str, Any]:
    """
    要件定義書を生成し、結果をJSONとHTMLで出力します。
    
    Args:
        project_description: プロジェクトの説明テキスト
        project_name: プロジェクト名
        output_dir: 出力ディレクトリのパス
        
    Returns:
        生成結果の情報を含む辞書
    """
    try:
        print(f"要件定義の生成を開始します: プロジェクト '{project_name}'")
        
        # 要件定義を実行
        manager = RequirementsManager()
        print("RequirementsManagerを初期化しました")
        
        requirements_document = await manager.run(project_description)
        print("要件定義の生成が完了しました")
        
        # 出力用の安全なプロジェクト名を生成
        safe_project_name = project_name.replace(" ", "_").replace("/", "_").replace("\\", "_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # HTMLファイルを生成
        html_file_name = f"requirements_{safe_project_name}_{timestamp}.html"
        html_file_path = os.path.join(output_dir, html_file_name)
        print(f"HTMLファイルを生成します: {html_file_path}")
        
        # ドキュメントデータをシリアライズ可能な辞書に変換
        try:
            # 新しいPydanticではmodel_dump()を使用
            doc_dict = requirements_document.model_dump()
            print("model_dump()メソッドを使用してドキュメントを変換しました")
        except AttributeError:
            try:
                # 古いPydanticではdict()を使用
                doc_dict = requirements_document.dict()
                print("dict()メソッドを使用してドキュメントを変換しました")
            except AttributeError:
                # どちらもない場合は__dict__を使用
                doc_dict = vars(requirements_document)
                print("vars()関数を使用してドキュメントを変換しました")
        
        # HTMLを生成
        html_file = export_to_html(doc_dict, html_file_path)
        print(f"HTMLファイルの生成が完了しました: {html_file}")
        
        # 結果ファイルを生成
        result_file_name = f"result_{timestamp}.json"
        result_file_path = os.path.join(output_dir, result_file_name)
        
        # 結果データ
        result_data = {
            "success": True,
            "project_name": project_name,
            "timestamp": timestamp,
            "html_file": html_file,
            "document": doc_dict
        }
        
        # 結果をJSONファイルとして保存
        with open(result_file_path, 'w', encoding='utf-8') as f:
            json.dump(result_data, f, ensure_ascii=False, indent=2)
        print(f"結果ファイルの生成が完了しました: {result_file_path}")
        
        return result_data
    except Exception as e:
        print(f"要件定義生成中にエラーが発生しました: {e}")
        print("スタックトレース:")
        traceback.print_exc()
        raise

def main():
    """
    コマンドラインからの実行用エントリーポイント
    """
    try:
        parser = argparse.ArgumentParser(description='要件定義ボットAPIハンドラー')
        parser.add_argument('input_file', help='プロジェクト説明を含む入力ファイルのパス')
        parser.add_argument('project_name', help='プロジェクト名')
        parser.add_argument('output_dir', help='出力ディレクトリのパス')
        
        args = parser.parse_args()
        print(f"引数: input_file={args.input_file}, project_name={args.project_name}, output_dir={args.output_dir}")
        
        # ディレクトリの存在確認
        if not os.path.exists(args.output_dir):
            os.makedirs(args.output_dir, exist_ok=True)
            print(f"出力ディレクトリを作成しました: {args.output_dir}")
        
        # 入力ファイルからプロジェクト説明を読み込む
        if not os.path.exists(args.input_file):
            print(f"エラー: 入力ファイルが見つかりません: {args.input_file}")
            sys.exit(1)
            
        with open(args.input_file, 'r', encoding='utf-8') as f:
            project_description = f.read()
        print(f"プロジェクト説明を読み込みました ({len(project_description)} 文字)")
        
        # 非同期関数を実行
        result = asyncio.run(generate_requirements(
            project_description=project_description,
            project_name=args.project_name,
            output_dir=args.output_dir
        ))
        
        print(json.dumps({"success": True, "message": "要件定義書の生成が完了しました"}, ensure_ascii=False))
    except Exception as e:
        print(f"エラーが発生しました: {e}")
        print("スタックトレース:")
        traceback.print_exc()
        print(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)

if __name__ == "__main__":
    main() 