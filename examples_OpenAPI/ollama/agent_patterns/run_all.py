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
    "parallel": "并行执行示例 - 并行生成多个结果并选择最佳",
    "agents_as_tools": "代理作为工具示例 - 使用其他代理作为工具",
    "llm_as_judge": "LLM作为裁判示例 - 使用一个代理评估另一个的输出",
    "input_guardrails": "输入保障示例 - 检测不适当的用户输入",
    "output_guardrails": "输出保障示例 - 确保模型输出没有敏感数据",
    "forcing_tool_use": "强制工具使用示例 - 确保模型使用工具",
}

# forcing_tool_use特殊选项
TOOL_BEHAVIORS = ["default", "first_tool", "custom"]

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

async def run_example(example_name):
    """运行单个示例"""
    print(f"\n\n{'='*60}")
    print(f"    运行示例: {example_name} - {EXAMPLES[example_name]}")
    print(f"{'='*60}")
    
    try:
        # 特殊处理forcing_tool_use示例
        if example_name == "forcing_tool_use":
            # 为forcing_tool_use运行所有三种模式
            for behavior in TOOL_BEHAVIORS:
                print(f"\n>> 使用模式: {behavior}")
                module = importlib.import_module(example_name)
                await module.main(behavior)
        else:
            # 导入并运行示例
            module = importlib.import_module(example_name)
            await module.main()
        return True
    except Exception as e:
        print(f"运行 {example_name} 时出错: {str(e)}")
        return False

async def main():
    # 检查Ollama服务
    if not check_ollama_running():
        return
    
    print("\n===== Ollama代理模式示例 运行工具 =====\n")
    print("选项:")
    print("1. 运行所有示例")
    print("2. 选择单个示例运行")
    
    choice = input("\n请选择操作 (1/2): ")
    
    if choice == "1":
        # 运行所有示例
        print("\n将依次运行所有示例...")
        successes = 0
        failures = 0
        
        for example_name in EXAMPLES.keys():
            success = await run_example(example_name)
            if success:
                successes += 1
            else:
                failures += 1
            
            # 如果不是最后一个示例，暂停一下
            if example_name != list(EXAMPLES.keys())[-1]:
                input("\n按Enter键继续下一个示例...")
        
        print(f"\n\n所有示例运行完成。成功: {successes}, 失败: {failures}")
    
    elif choice == "2":
        # 显示可用示例
        for i, (name, desc) in enumerate(EXAMPLES.items(), 1):
            print(f"{i}. {name}: {desc}")
        
        # 获取用户选择
        while True:
            try:
                ex_choice = input("\n请选择要运行的示例 (数字或名称，q退出): ")
                if ex_choice.lower() == 'q':
                    return
                    
                # 处理数字输入
                if ex_choice.isdigit() and 1 <= int(ex_choice) <= len(EXAMPLES):
                    example_name = list(EXAMPLES.keys())[int(ex_choice) - 1]
                    await run_example(example_name)
                    break
                
                # 处理名称输入
                if ex_choice in EXAMPLES:
                    await run_example(ex_choice)
                    break
                    
                print("无效选择，请重试。")
            except (ValueError, IndexError):
                print("无效选择，请重试。")
    
    else:
        print("无效选择")

if __name__ == "__main__":
    asyncio.run(main())
