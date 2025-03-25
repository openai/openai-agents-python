import sys
import os
import importlib
import asyncio

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

# 可用的示例列表
EXAMPLES = {
    "routing": "路由/分流示例 - 根据语言自动选择专门的代理",
    "deterministic": "确定性流程示例 - 按顺序执行多个步骤",
    "parallelization": "并行执行示例 - 并行生成多个结果并选择最佳",
    "agents_as_tools": "代理作为工具示例 - 使用其他代理作为工具",
    "llm_as_judge": "LLM作为裁判示例 - 使用一个代理评估另一个的输出",
    "input_guardrails": "输入保障示例 - 检测不适当的用户输入",
    "output_guardrails": "输出保障示例 - 确保模型输出没有敏感数据",
    "forcing_tool_use": "强制工具使用示例 - 确保模型使用工具",
}

def check_ollama_running():
    """检查Ollama服务是否运行"""
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("错误: Ollama服务返回非200状态码。请确保Ollama服务正在运行。")
            return False
    except Exception as e:
        print(f"错误: 无法连接到Ollama服务。请确保Ollama服务正在运行。\n{str(e)}")
        print("\n如果您尚未安装Ollama，请从https://ollama.ai下载并安装，然后运行'ollama serve'启动服务")
        return False
    return True

async def main():
    # 检查Ollama服务
    if not check_ollama_running():
        return
    
    # 显示可用示例
    print("\n===== Ollama代理模式示例 =====\n")
    for i, (name, desc) in enumerate(EXAMPLES.items(), 1):
        print(f"{i}. {name}: {desc}")
    
    # 获取用户选择
    while True:
        try:
            choice = input("\n请选择要运行的示例 (数字或名称，q退出): ")
            if choice.lower() == 'q':
                return
                
            # 处理数字输入
            if choice.isdigit() and 1 <= int(choice) <= len(EXAMPLES):
                example_name = list(EXAMPLES.keys())[int(choice) - 1]
                break
            
            # 处理名称输入
            if choice in EXAMPLES:
                example_name = choice
                break
                
            print("无效选择，请重试。")
        except (ValueError, IndexError):
            print("无效选择，请重试。")
    
    print(f"\n启动示例: {example_name}")
    print("=" * 50)
    
    # 根据强制工具使用的特殊性进行处理
    if example_name == "forcing_tool_use":
        # 导入模块
        module = importlib.import_module(example_name)
        # 获取可用的工具使用模式
        tool_behaviors = ["default", "first_tool", "custom"]
        print("强制工具使用示例有以下模式:")
        for i, behavior in enumerate(tool_behaviors, 1):
            print(f"{i}. {behavior}")
        
        # 获取用户选择的模式
        while True:
            try:
                behavior_choice = input("\n请选择模式 (数字或名称): ")
                if behavior_choice.isdigit() and 1 <= int(behavior_choice) <= len(tool_behaviors):
                    behavior = tool_behaviors[int(behavior_choice) - 1]
                    break
                if behavior_choice in tool_behaviors:
                    behavior = behavior_choice
                    break
                print("无效选择，请重试。")
            except (ValueError, IndexError):
                print("无效选择，请重试。")
                
        # 运行示例
        await module.main(behavior)
    else:
        # 导入并运行示例
        module = importlib.import_module(example_name)
        await module.main()
    
    print("\n示例执行完毕。")

if __name__ == "__main__":
    asyncio.run(main())
