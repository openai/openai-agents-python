from pydantic import BaseModel, Field
import os
import sys

# agentsモジュールを確実にインポートできるようにパス設定
project_root = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
python_dir = os.path.join(project_root, 'python')
if python_dir not in sys.path:
    sys.path.insert(0, python_dir)

try:
    from agents import Agent, WebSearchTool
    from agents.model_settings import ModelSettings
except ImportError as e:
    print(f"エラー: {e}")
    print(f"現在のPYTHONPATH: {sys.path}")
    print("'agents'モジュールをインポートできません。以下のコマンドを実行してください:")
    print("export PYTHONPATH=$PYTHONPATH:$(pwd)/python")
    print("または、pythonディレクトリに移動して実行してください。")
    sys.exit(1)

INSTRUCTIONS = (
    "あなたは要件詳細化の専門家です。初期要件分析の結果を受け取り、各要件をより詳細な技術仕様や設計仕様に展開します。"
    "必要に応じてウェブ検索を行い、最新の技術トレンドや標準、ベストプラクティスを参照してください。"
    "要件間の依存関係を特定し、矛盾や欠落がないか確認します。"
    "すべての要件仕様は実装可能で、明確かつ具体的である必要があります。"
)


class DetailedRequirementSpec(BaseModel):
    requirement_id: str
    """元の要件ID"""

    technical_details: list[str]
    """技術的な詳細仕様"""

    implementation_notes: str
    """実装に関する注意点やガイドライン"""

    dependencies: list[str]
    """この要件が依存する他の要件ID"""

    verification_method: str
    """要件検証の方法（テスト、レビュー、デモなど）"""

    external_references: list[str]
    """関連する外部資料や参考情報（オプション）"""


class RefinedRequirements(BaseModel):
    detailed_specs: list[DetailedRequirementSpec]
    """詳細化された要件仕様のリスト"""

    integration_points: list[str]
    """要件間の主要な統合ポイント"""

    technical_constraints: list[str]
    """詳細化によって明らかになった技術的制約"""

    implementation_risks: list[str]
    """実装に関するリスク要因"""


refiner_agent = Agent(
    name="RefinerAgent",
    instructions=INSTRUCTIONS,
    tools=[WebSearchTool()],
    model="gpt-4o",
    model_settings=ModelSettings(tool_choice="auto"),
    output_type=RefinedRequirements,
) 