from __future__ import annotations

import asyncio
import os
import sys
from typing import Callable, Optional

# agentsモジュールを確実にインポートできるようにパス設定
project_root = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
python_dir = os.path.join(project_root, 'python')
if python_dir not in sys.path:
    sys.path.insert(0, python_dir)
print(f"現在の作業ディレクトリ: {os.getcwd()}")
print(f"PYTHONPATHに追加: {python_dir}")
print(f"現在のPYTHONPATH: {sys.path}")

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
except ImportError as e:
    print(f"エラー: {e}")
    print(f"現在のPYTHONPATH: {sys.path}")
    print("'agents'モジュールをインポートできません。以下のコマンドを実行してください:")
    print("export PYTHONPATH=$PYTHONPATH:$(pwd)/python")
    print("または、pythonディレクトリに移動して実行してください。")
    
    # 回避策を試みる
    try:
        print("回避策を試みています...")
        # ファイルシステムを確認
        import glob
        print("pythonディレクトリの内容:")
        for f in glob.glob(os.path.join(python_dir, "*")):
            print(f" - {f}")
            
        # インストールされているパッケージを確認
        import subprocess
        result = subprocess.run(["python3", "-m", "pip", "list"], capture_output=True, text=True)
        print(f"インストールされているパッケージ:\n{result.stdout}")
    except Exception as ex:
        print(f"回避策の実行中にエラーが発生しました: {ex}")
        
    sys.exit(1)

try:
    from rich.console import Console
except ImportError:
    print("richライブラリがインストールされていません。'pip3 install rich'を実行してください。")
    sys.exit(1)

from .agents.analyzer_agent import RequirementsAnalysis, analyzer_agent
from .agents.document_agent import RequirementsDocument, document_agent
from .agents.refiner_agent import RefinedRequirements, refiner_agent
from .printer import Printer


class RequirementsManager:
    def __init__(self):
        self.trace_id = None
        self.console = Console()
        self.printer = Printer(self.console)
        
    async def run(
        self, 
        project_description: str, 
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> RequirementsDocument:
        """
        要件定義プロセスを実行します。
        
        Args:
            project_description: プロジェクトの説明
            progress_callback: 進捗状況を通知するためのコールバック関数
            
        Returns:
            生成された要件定義ドキュメント
        """
        self.trace_id = gen_trace_id()
        with trace("Requirements Definition trace", trace_id=self.trace_id):
            self.printer.update_item(
                "trace_id",
                f"トレース表示: https://platform.openai.com/traces/{self.trace_id}",
                is_done=True,
                hide_checkmark=True,
            )
            
            self.printer.add_divider("start_divider", "要件定義プロセス開始")
            
            # 進捗状況をコールバックに通知
            if progress_callback:
                progress_callback("要件定義プロセスを開始します...")
            
            # 要件分析
            self.printer.update_item("analyzing", "プロジェクト要件を分析中...")
            if progress_callback:
                progress_callback("プロジェクト要件を分析中...")
            
            requirements_analysis = await self._analyze_requirements(
                project_description, 
                progress_callback
            )
            
            # 要件詳細化
            self.printer.update_item("refining", "要件の詳細化中...")
            if progress_callback:
                progress_callback("要件の詳細化中...")
                
            refined_requirements = await self._refine_requirements(
                requirements_analysis,
                progress_callback
            )
            
            # ドキュメント生成
            self.printer.update_item("documenting", "要件定義書を作成中...")
            if progress_callback:
                progress_callback("要件定義書を作成中...")
                
            requirements_document = await self._create_document(
                requirements_analysis, 
                refined_requirements,
                progress_callback
            )

            self.printer.add_divider("end_divider", "要件定義プロセス完了")
            
            final_summary = f"要件定義完了：{requirements_document.title} (v{requirements_document.version})"
            self.printer.update_item("final_summary", final_summary, is_done=True)

            self.printer.end()

            print("\n\n=====要件定義書=====\n\n")
            print(f"タイトル: {requirements_document.title}")
            print(f"バージョン: {requirements_document.version}")
            print("\n===エグゼクティブサマリー===\n")
            print(requirements_document.executive_summary)
            print("\n===機能要件===\n")
            print(requirements_document.functional_requirements)
            print("\n===非機能要件===\n")
            print(requirements_document.non_functional_requirements)
            print("\n===実装ロードマップ===\n")
            print(requirements_document.implementation_roadmap)

            # 最終ドキュメントを返す
            return requirements_document

    async def _analyze_requirements(
        self, 
        project_description: str, 
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> RequirementsAnalysis:
        result = await Runner.run(
            analyzer_agent,
            f"プロジェクト説明: {project_description}",
        )
        
        count = len(result.final_output_as(RequirementsAnalysis).requirements)
        
        self.printer.update_item(
            "analyzing",
            f"要件分析完了：{count}件の要件を特定しました",
            is_done=True,
        )
            
        if progress_callback:
            progress_callback(f"要件分析完了：{count}件の要件を特定しました")
            
        return result.final_output_as(RequirementsAnalysis)

    async def _refine_requirements(
        self, 
        requirements_analysis: RequirementsAnalysis,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> RefinedRequirements:
        # 要件分析結果を文字列にフォーマット
        requirements_str = "\n".join([
            f"ID: {req.id}, カテゴリ: {req.category}, 説明: {req.description}, 優先度: {req.priority}"
            for req in requirements_analysis.requirements
        ])
        
        input_text = (
            f"プロジェクト概要: {requirements_analysis.project_summary}\n"
            f"ステークホルダー: {', '.join(requirements_analysis.stakeholders)}\n"
            f"要件リスト:\n{requirements_str}\n"
            f"前提条件: {', '.join(requirements_analysis.assumptions)}\n"
            f"制約事項: {', '.join(requirements_analysis.constraints)}"
        )
        
        result = await Runner.run(
            refiner_agent,
            input_text,
        )
        
        detailed_specs_count = len(result.final_output_as(RefinedRequirements).detailed_specs)
        
        self.printer.update_item(
            "refining",
            f"要件詳細化完了：{detailed_specs_count}件の詳細仕様を作成しました",
            is_done=True,
        )
            
        if progress_callback:
            progress_callback(f"要件詳細化完了：{detailed_specs_count}件の詳細仕様を作成しました")
            
        return result.final_output_as(RefinedRequirements)

    async def _create_document(
        self, 
        requirements_analysis: RequirementsAnalysis, 
        refined_requirements: RefinedRequirements,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> RequirementsDocument:
        # 分析結果と詳細化結果を文字列に変換
        analysis_str = (
            f"プロジェクト概要: {requirements_analysis.project_summary}\n"
            f"ステークホルダー: {', '.join(requirements_analysis.stakeholders)}\n"
            f"前提条件: {', '.join(requirements_analysis.assumptions)}\n"
            f"制約事項: {', '.join(requirements_analysis.constraints)}"
        )
        
        # 要件リストを整形
        req_list_str = ""
        for req in requirements_analysis.requirements:
            req_list_str += f"要件ID: {req.id}\n"
            req_list_str += f"カテゴリ: {req.category}\n"
            req_list_str += f"説明: {req.description}\n"
            req_list_str += f"優先度: {req.priority}\n"
            req_list_str += "受け入れ基準:\n"
            for criteria in req.acceptance_criteria:
                req_list_str += f"- {criteria}\n"
            req_list_str += "\n"
            
        # 詳細仕様リストを整形
        detail_spec_str = ""
        for spec in refined_requirements.detailed_specs:
            detail_spec_str += f"要件ID: {spec.requirement_id}\n"
            detail_spec_str += "技術的詳細:\n"
            for detail in spec.technical_details:
                detail_spec_str += f"- {detail}\n"
            detail_spec_str += f"実装ノート: {spec.implementation_notes}\n"
            detail_spec_str += f"依存関係: {', '.join(spec.dependencies)}\n"
            detail_spec_str += f"検証方法: {spec.verification_method}\n"
            if spec.external_references:
                detail_spec_str += "外部参照:\n"
                for ref in spec.external_references:
                    detail_spec_str += f"- {ref}\n"
            detail_spec_str += "\n"
            
        # 統合情報
        integration_str = "\n".join([f"- {point}" for point in refined_requirements.integration_points])
        constraints_str = "\n".join([f"- {constraint}" for constraint in refined_requirements.technical_constraints])
        risks_str = "\n".join([f"- {risk}" for risk in refined_requirements.implementation_risks])
        
        # すべての情報を結合
        input_text = (
            f"プロジェクト分析サマリー:\n{analysis_str}\n\n"
            f"要件リスト:\n{req_list_str}\n\n"
            f"詳細仕様:\n{detail_spec_str}\n\n"
            f"統合ポイント:\n{integration_str}\n\n"
            f"技術的制約:\n{constraints_str}\n\n"
            f"実装リスク:\n{risks_str}"
        )
        
        result = await Runner.run(
            document_agent,
            input_text,
        )
        
        self.printer.update_item(
            "documenting",
            "要件定義書の作成完了",
            is_done=True,
        )
            
        if progress_callback:
            progress_callback("要件定義書の作成完了")
            
        return result.final_output_as(RequirementsDocument) 