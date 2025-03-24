from __future__ import annotations

import re
import json
from typing import Literal

from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypeVar

from ..exceptions import ModelBehaviorError
from ..tracing import SpanError
from ._error_tracing import attach_error_to_current_span

T = TypeVar("T")


def validate_json(json_str: str, type_adapter: TypeAdapter[T], partial: bool) -> T:
    partial_setting: bool | Literal["off", "on", "trailing-strings"] = (
        "trailing-strings" if partial else False
    )
    
    try:
        # 首先尝试直接验证
        validated = type_adapter.validate_json(json_str, experimental_allow_partial=partial_setting)
        return validated
    except ValidationError as e:
        # 如果直接验证失败，尝试从文本中提取JSON
        try:
            # 尝试查找可能的JSON结构
            
            # 1. 查找代码块中的JSON
            json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', json_str)
            if json_block_match:
                extracted = json_block_match.group(1).strip()
                try:
                    validated = type_adapter.validate_json(extracted, experimental_allow_partial=partial_setting)
                    return validated
                except ValidationError:
                    pass  # 继续尝试其他方法
            
            # 2. 查找{...}结构
            json_match = re.search(r'(\{[\s\S]*?\})', json_str)
            if json_match:
                extracted = json_match.group(1).strip()
                try:
                    validated = type_adapter.validate_json(extracted, experimental_allow_partial=partial_setting)
                    return validated
                except ValidationError:
                    pass  # 继续尝试其他方法
            
            # 3. 尝试特殊情况：如果模式很简单（如只有一个number字段）
            if hasattr(type_adapter.core_schema, "schema") and "properties" in type_adapter.core_schema.schema:
                schema = type_adapter.core_schema.schema
                if len(schema["properties"]) == 1 and "number" in schema["properties"]:
                    # 尝试从文本中提取数字
                    number_match = re.search(r'(?:number|值|结果)[^\d]*(\d+)', json_str)
                    if number_match:
                        simple_json = f'{{"number": {number_match.group(1)}}}'
                        try:
                            validated = type_adapter.validate_json(simple_json)
                            return validated
                        except ValidationError:
                            pass  # 继续尝试其他方法
                    
                    # 尝试提取任何数字
                    any_number = re.search(r'\b(\d+)\b', json_str)
                    if any_number:
                        simple_json = f'{{"number": {any_number.group(1)}}}'
                        try:
                            validated = type_adapter.validate_json(simple_json)
                            return validated
                        except ValidationError:
                            pass  # 继续处理原始错误
            
            # 尝试构造所需数据结构失败，抛出原始错误
            attach_error_to_current_span(
                SpanError(
                    message="Invalid JSON provided",
                    data={},
                )
            )
            raise ModelBehaviorError(
                f"Invalid JSON when parsing {json_str} for {type_adapter}; {e}"
            ) from e
        
        except Exception as extraction_error:
            # 如果提取过程出错，仍然抛出原始错误
            attach_error_to_current_span(
                SpanError(
                    message="Invalid JSON provided",
                    data={},
                )
            )
            raise ModelBehaviorError(
                f"Invalid JSON when parsing {json_str} for {type_adapter}; {e}"
            ) from e
