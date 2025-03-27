import asyncio
import os
from .manager import RequirementsManager
from .config import OPENAI_API_KEY  # 設定ファイルからAPIキーをインポート


async def main() -> None:
    # APIキーを設定
    os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
    
    print("==== 要件定義ボット ====")
    print("プロジェクトの説明を入力してください。詳細であるほど良い要件定義が可能です。")
    print("入力を終了するには、新しい行で Ctrl+D（Unix/Mac）または Ctrl+Z（Windows）を入力してください。")
    
    lines = []
    try:
        while True:
            line = input()
            lines.append(line)
    except EOFError:
        pass
    
    project_description = "\n".join(lines)
    
    if not project_description.strip():
        print("プロジェクト説明が入力されていません。サンプル説明を使用します。")
        project_description = """
        オンライン予約システム開発プロジェクト
        
        目的：
        小規模なレストランやサービス業向けのクラウドベースの予約管理システムを開発する。
        顧客はウェブサイトから簡単に予約でき、店舗側は管理画面から予約状況を確認・管理できるようにする。
        
        主な利用者：
        1. 一般顧客（予約者）
        2. 店舗スタッフ（予約管理者）
        3. 店舗オーナー（システム管理者）
        
        必要な機能：
        - 顧客向け予約インターフェース
        - 店舗管理者向け予約管理画面
        - 予約の自動確認メール送信
        - キャンセル・変更機能
        - 顧客情報管理
        - 予約状況分析・レポート
        
        技術要件：
        - レスポンシブWebデザイン
        - クラウドホスティング
        - セキュアなデータ保存
        - 他システムとの連携API
        
        制約条件：
        - 開発期間は3ヶ月
        - 小規模店舗の予算に適した価格設定が必要
        - 技術スタッフがいない店舗でも運用可能な簡易さ
        """
    
    await RequirementsManager().run(project_description)


if __name__ == "__main__":
    asyncio.run(main()) 