from pydantic import BaseModel, Field
import os
import sys
from typing import Dict, List, Optional

# agentsモジュールを確実にインポートできるようにパス設定
project_root = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
python_dir = os.path.join(project_root, 'python')
if python_dir not in sys.path:
    sys.path.insert(0, python_dir)

try:
    from agents import Agent
except ImportError as e:
    print(f"エラー: {e}")
    print(f"現在のPYTHONPATH: {sys.path}")
    print("'agents'モジュールをインポートできません。以下のコマンドを実行してください:")
    print("export PYTHONPATH=$PYTHONPATH:$(pwd)/python")
    print("または、pythonディレクトリに移動して実行してください。")
    sys.exit(1)

PROMPT = (
    "あなたは要件ドキュメント作成の専門家です。分析された要件と詳細仕様を整理して、公式な要件定義ドキュメントを作成してください。"
    "ドキュメントは、経営陣、開発者、テスターなど、異なるステークホルダーが理解できるように構成されるべきです。"
    "明確な構造、一貫した用語、プロフェッショナルな表現を使用してください。"
    "視覚的要素（図表、フローチャートなど）を文字表現で提案し、重要な関係性を示してください。"
    "最終的なドキュメントは、プロジェクトの全体像と詳細の両方を網羅する必要があります。"
)


class RequirementsDocument(BaseModel):
    title: str
    """ドキュメントのタイトル"""

    version: str
    """ドキュメントのバージョン"""

    executive_summary: str
    """経営陣向けの要約（プロジェクト概要、主要機能、ビジネス価値）"""

    scope: str
    """プロジェクトの範囲と境界の定義"""

    functional_requirements: str
    """機能要件の詳細なマークダウンセクション"""

    non_functional_requirements: str
    """非機能要件の詳細なマークダウンセクション"""

    system_architecture_overview: str
    """システムアーキテクチャの概要（テキストベースの図表説明含む）"""

    implementation_roadmap: str
    """実装ロードマップと優先順位付け"""

    # オプションフィールド
    glossary: Optional[Dict[str, str]] = Field(default_factory=dict)
    """専門用語の説明（オプション）"""

    appendices: Optional[List[str]] = Field(default_factory=list)
    """追加情報や参考資料（オプション）"""


document_agent = Agent(
    name="DocumentAgent",
    instructions=PROMPT,
    model="gpt-4o",
    output_type=RequirementsDocument,
) 