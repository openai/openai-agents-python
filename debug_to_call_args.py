#!/usr/bin/env python3
"""
调试脚本：测试 FuncSchema.to_call_args 的行为
"""

import json
from agents.function_schema import function_schema


def test_function(name: str, count: int = 1):
    """测试函数
    
    Args:
        name: 名字参数
        count: 计数参数，默认为1
    """
    return f"name={name}, count={count}"


def main():
    """测试 to_call_args 的行为"""
    print("=== 测试 FuncSchema.to_call_args ===\n")
    
    # 获取函数的 schema
    schema = function_schema(test_function)
    
    print(f"函数名: {schema.name}")
    print(f"函数签名: {schema.signature}")
    print(f"JSON Schema:")
    print(json.dumps(schema.params_json_schema, indent=2, ensure_ascii=False))
    print()
    
    # 测试参数解析
    test_data = {"name": "张三", "count": 3}
    print(f"输入数据: {test_data}")
    
    # 创建 Pydantic 模型实例
    parsed = schema.params_pydantic_model(**test_data)
    print(f"Pydantic 模型实例: {parsed}")
    print(f"模型字段: {parsed.model_fields}")
    print(f"模型数据: {parsed.model_dump()}")
    print()
    
    # 调用 to_call_args
    args, kwargs = schema.to_call_args(parsed)
    print(f"to_call_args 结果:")
    print(f"  args: {args}")
    print(f"  kwargs: {kwargs}")
    print()
    
    # 测试函数调用
    result = test_function(*args, **kwargs)
    print(f"函数调用结果: {result}")
    
    # 测试空参数的情况
    print("\n=== 测试空参数情况 ===")
    empty_data = {}
    try:
        empty_parsed = schema.params_pydantic_model(**empty_data)
        empty_args, empty_kwargs = schema.to_call_args(empty_parsed)
        print(f"空参数 args: {empty_args}")
        print(f"空参数 kwargs: {empty_kwargs}")
    except Exception as e:
        print(f"空参数测试失败: {e}")


if __name__ == "__main__":
    main()
