"""
要件定義書をHTML形式にエクスポートし、ブラウザで表示するためのモジュール
"""
import os
import webbrowser
import markdown
from datetime import datetime
from typing import Dict, Any

# 基本的なHTMLテンプレート
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: 'Helvetica Neue', Arial, sans-serif;
            line-height: 1.6;
            margin: 0;
            padding: 0;
            color: #333;
            background-color: #f8f9fa;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
            padding: 20px;
            background-color: white;
            box-shadow: 0 0 10px rgba(0,0,0,0.1);
        }}
        header {{
            background-color: #4285f4;
            color: white;
            padding: 20px;
            margin-bottom: 20px;
        }}
        h1 {{
            margin: 0;
            font-weight: 300;
            font-size: 2.5em;
        }}
        h2 {{
            color: #4285f4;
            border-bottom: 1px solid #eee;
            padding-bottom: 10px;
            margin-top: 30px;
        }}
        h3 {{
            color: #404040;
        }}
        .meta {{
            color: #666;
            font-style: italic;
            margin-top: 10px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
        }}
        table, th, td {{
            border: 1px solid #ddd;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
        }}
        th {{
            background-color: #f2f2f2;
        }}
        .glossary-term {{
            font-weight: bold;
        }}
        .code {{
            background-color: #f5f5f5;
            padding: 15px;
            border-radius: 4px;
            overflow-x: auto;
            font-family: monospace;
        }}
        .architecture {{
            margin: 20px 0;
            padding: 15px;
            background-color: #f9f9f9;
            border-left: 4px solid #4285f4;
        }}
        .footer {{
            margin-top: 50px;
            text-align: center;
            color: #777;
            font-size: 0.9em;
            padding: 20px;
            border-top: 1px solid #eee;
        }}
    </style>
</head>
<body>
    <header>
        <h1>{title}</h1>
        <p class="meta">バージョン: {version} - 生成日時: {date}</p>
    </header>
    <div class="container">
        <h2>エグゼクティブサマリー</h2>
        <div>{executive_summary}</div>
        
        <h2>プロジェクト範囲</h2>
        <div>{scope}</div>
        
        {glossary_section}
        
        <h2>機能要件</h2>
        <div>{functional_requirements}</div>
        
        <h2>非機能要件</h2>
        <div>{non_functional_requirements}</div>
        
        <h2>システムアーキテクチャ概要</h2>
        <div class="architecture">{system_architecture}</div>
        
        <h2>実装ロードマップ</h2>
        <div>{implementation_roadmap}</div>
        
        {appendices_section}
        
        <div class="footer">
            <p>このドキュメントは要件定義ボットによって自動生成されました。</p>
            <p>© {year} Requirements Definition Bot</p>
        </div>
    </div>
</body>
</html>
"""

def convert_to_html(doc_data: Dict[str, Any]) -> str:
    """
    要件定義ドキュメントのデータをHTML形式に変換します。
    
    Args:
        doc_data: 要件定義ドキュメントのデータを含む辞書
        
    Returns:
        HTML形式のドキュメント
    """
    # マークダウンをHTMLに変換
    executive_summary_html = markdown.markdown(doc_data["executive_summary"])
    scope_html = markdown.markdown(doc_data["scope"])
    functional_requirements_html = markdown.markdown(doc_data["functional_requirements"])
    non_functional_requirements_html = markdown.markdown(doc_data["non_functional_requirements"])
    system_architecture_html = markdown.markdown(doc_data["system_architecture_overview"])
    implementation_roadmap_html = markdown.markdown(doc_data["implementation_roadmap"])
    
    # 用語集セクションの生成
    glossary_html = ""
    if doc_data.get("glossary") and len(doc_data["glossary"]) > 0:
        glossary_html = "<h2>用語集</h2>\n<table>\n<tr><th>用語</th><th>説明</th></tr>\n"
        for term, definition in doc_data["glossary"].items():
            glossary_html += f"<tr><td class='glossary-term'>{term}</td><td>{definition}</td></tr>\n"
        glossary_html += "</table>\n"
    
    # 付録セクションの生成
    appendices_html = ""
    if doc_data.get("appendices") and len(doc_data["appendices"]) > 0:
        appendices_html = "<h2>付録</h2>\n"
        for i, appendix in enumerate(doc_data["appendices"]):
            appendices_html += f"<h3>付録 {i+1}</h3>\n<div>{markdown.markdown(appendix)}</div>\n"
    
    # 現在の日時
    now = datetime.now()
    current_date = now.strftime("%Y年%m月%d日 %H:%M")
    current_year = now.strftime("%Y")
    
    # HTMLテンプレートにデータを埋め込む
    html_content = HTML_TEMPLATE.format(
        title=doc_data["title"],
        version=doc_data["version"],
        date=current_date,
        year=current_year,
        executive_summary=executive_summary_html,
        scope=scope_html,
        glossary_section=glossary_html,
        functional_requirements=functional_requirements_html,
        non_functional_requirements=non_functional_requirements_html,
        system_architecture=system_architecture_html,
        implementation_roadmap=implementation_roadmap_html,
        appendices_section=appendices_html
    )
    
    return html_content

def export_to_html(doc_data: Dict[str, Any], output_path: str = None) -> str:
    """
    要件定義ドキュメントをHTMLファイルとしてエクスポートします。
    
    Args:
        doc_data: 要件定義ドキュメントのデータを含む辞書
        output_path: 出力先のファイルパス。指定がない場合は自動生成します。
    
    Returns:
        生成されたHTMLファイルのパス
    """
    html_content = convert_to_html(doc_data)
    
    # 出力先が指定されていない場合は、ドキュメントタイトルと日時から自動生成
    if output_path is None:
        safe_title = doc_data["title"].replace(" ", "_").replace("/", "_").replace("\\", "_")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"requirements_{safe_title}_{timestamp}.html"
    
    # HTMLファイルを書き出す
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    return output_path

def open_in_browser(file_path: str) -> None:
    """
    指定されたHTMLファイルをデフォルトのブラウザで開きます。
    
    Args:
        file_path: HTMLファイルのパス
    """
    # ファイルパスをURI形式に変換
    file_uri = f"file://{os.path.abspath(file_path)}"
    
    # ブラウザで開く
    webbrowser.open(file_uri)

def export_and_open(doc_data: Dict[str, Any], output_path: str = None) -> str:
    """
    要件定義ドキュメントをHTMLファイルとしてエクスポートし、ブラウザで開きます。
    
    Args:
        doc_data: 要件定義ドキュメントのデータを含む辞書
        output_path: 出力先のファイルパス。指定がない場合は自動生成します。
    
    Returns:
        生成されたHTMLファイルのパス
    """
    html_file = export_to_html(doc_data, output_path)
    open_in_browser(html_file)
    return html_file 