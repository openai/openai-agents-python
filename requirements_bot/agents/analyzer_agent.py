from pydantic import BaseModel
import os
import sys

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
    "あなたは要件分析の専門家です。与えられたプロジェクトの説明から、機能要件と非機能要件を抽出し分析してください。"
    "要件を明確に定義し、プロジェクトの目的、ユーザーの期待、技術的制約などを考慮してください。"
    "すべての要件は優先順位付けされ、一貫性のある形式で提供されるべきです。"
    "曖昧な表現は避け、具体的で測定可能な要件を作成してください。"
)


class RequirementItem(BaseModel):
    id: str
    "要件のID（例：REQ-001）"

    category: str
    "要件のカテゴリ（機能要件/非機能要件）と詳細区分（性能/セキュリティ/ユーザビリティなど）"

    description: str
    "要件の詳細な説明"

    priority: str
    "要件の優先度（高/中/低）"

    acceptance_criteria: list[str]
    "要件が満たされたと判断するための受け入れ基準"


class RequirementsAnalysis(BaseModel):
    project_summary: str
    """プロジェクトの概要"""

    stakeholders: list[str]
    """特定された主要なステークホルダー"""

    requirements: list[RequirementItem]
    """抽出された要件のリスト"""

    assumptions: list[str]
    """プロジェクトに関する前提条件や仮定"""

    constraints: list[str]
    """プロジェクトの制約事項"""


analyzer_agent = Agent(
    name="AnalyzerAgent",
    instructions=PROMPT,
    model="gpt-4o",
    output_type=RequirementsAnalysis,
) 