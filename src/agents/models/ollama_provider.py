from __future__ import annotations

import json
import time
import uuid
import re
from typing import Any, AsyncIterator, Dict, List, Literal, Union, cast, overload

import httpx
from openai import AsyncOpenAI, AsyncStream, NOT_GIVEN, NotGiven
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage

from .. import _debug
from ..agent_output import AgentOutputSchema
from ..exceptions import AgentsException
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..logger import logger
from ..model_settings import ModelSettings
from ..tool import Tool
from ..tracing.span_data import GenerationSpanData
from ..tracing.spans import Span
from ..usage import Usage
from .interface import Model, ModelProvider, ModelTracing
from .openai_chatcompletions import OpenAIChatCompletionsModel

DEFAULT_MODEL = "llama3"

class OllamaAdapterException(AgentsException):
    """Exception raised when Ollama adapter encounters issues."""
    pass

class OllamaAsyncClient:
    """Adapter class to make Ollama API behave like OpenAI's AsyncOpenAI client."""
    
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url
        self.http_client = httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(60.0))
        # 修正结构，匹配 OpenAI 客户端的多层结构
        self.chat = self.Chat(http_client=self.http_client, base_url=base_url)
    
    class Chat:
        """模拟 OpenAI 客户端的 chat 层级"""
        def __init__(self, http_client: httpx.AsyncClient, base_url: str):
            self.http_client = http_client
            self.base_url = base_url
            # 添加 completions 层级
            self.completions = self.Completions(http_client=self.http_client, base_url=base_url)
        
        class Completions:
            """模拟 OpenAI 客户端的 chat.completions 层级"""
            def __init__(self, http_client: httpx.AsyncClient, base_url: str):
                self.http_client = http_client
                self.base_url = base_url
                
            def _clean_not_given(self, obj: Any) -> Any:
                """递归清理对象中的 NotGiven 值"""
                if obj is NOT_GIVEN or isinstance(obj, NotGiven):
                    return None
                elif isinstance(obj, dict):
                    return {k: self._clean_not_given(v) for k, v in obj.items() 
                           if v is not NOT_GIVEN and not isinstance(v, NotGiven)}
                elif isinstance(obj, list):
                    return [self._clean_not_given(item) for item in obj 
                           if item is not NOT_GIVEN and not isinstance(item, NotGiven)]
                else:
                    return obj

            def _extract_json_from_text(self, text: str, schema: dict = None) -> str:
                """从文本响应中提取JSON内容"""
                # 尝试多种常见的JSON提取模式
                
                # 模式1: 查找```json...```块
                json_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
                if json_block_match:
                    json_str = json_block_match.group(1).strip()
                    try:
                        # 确保是有效JSON
                        json.loads(json_str)
                        return json_str
                    except json.JSONDecodeError:
                        pass  # 尝试下一种方式
                
                # 模式2: 查找{...}块
                json_match = re.search(r'(\{[\s\S]*?\})', text)
                if json_match:
                    json_str = json_match.group(1).strip()
                    try:
                        # 确保是有效JSON
                        json.loads(json_str)
                        return json_str
                    except json.JSONDecodeError:
                        pass  # 尝试下一种方式
                
                # 模式3: 如果有"number: 数字"这样的格式，尝试构造JSON
                number_match = re.search(r'(?:number|值|结果)[^\d]*(\d+)', text)
                if number_match:
                    return f'{{"number": {number_match.group(1)}}}'
                
                # 模式4: 如果有明确的键值对描述，尝试构建JSON
                if schema and "properties" in schema:
                    # 提取schema中的所有属性
                    properties = schema.get("properties", {})
                    json_obj = {}
                    
                    # 对每个属性尝试从文本中提取值
                    for prop_name, prop_info in properties.items():
                        # 根据属性类型构造不同的正则表达式
                        prop_type = prop_info.get("type")
                        
                        if prop_type == "integer" or prop_type == "number":
                            # 查找形如 "属性名: 123" 或 "属性名是123" 的模式
                            number_pattern = fr'(?:{prop_name}|{prop_name.title()})[^\d]*(\d+)'
                            match = re.search(number_pattern, text)
                            if match:
                                try:
                                    value = int(match.group(1)) if prop_type == "integer" else float(match.group(1))
                                    json_obj[prop_name] = value
                                except ValueError:
                                    pass  # 转换失败，跳过
                                    
                        elif prop_type == "boolean":
                            # 查找布尔值，如 "属性名: true" 或 "属性名是true/是的/正确"
                            true_pattern = fr'(?:{prop_name}|{prop_name.title()}).*?(?:true|True|是的|正确|yes|Yes)'
                            false_pattern = fr'(?:{prop_name}|{prop_name.title()}).*?(?:false|False|不是|错误|no|No)'
                            
                            if re.search(true_pattern, text):
                                json_obj[prop_name] = True
                            elif re.search(false_pattern, text):
                                json_obj[prop_name] = False
                                
                        elif prop_type == "string":
                            # 对于字符串，尝试提取引号内的内容
                            string_pattern = fr'(?:{prop_name}|{prop_name.title()})[^\"\']*([\"\'])(.*?)\1'
                            match = re.search(string_pattern, text)
                            if match:
                                json_obj[prop_name] = match.group(2)
                    
                    # 如果至少提取到一个属性，则构建JSON
                    if json_obj:
                        return json.dumps(json_obj)
                
                # 如果上述模式都不匹配
                if schema and "properties" in schema and "number" in schema["properties"]:
                    # 如果schema中有number字段但没有提取到，尝试从文本中找到任意数字
                    any_number_match = re.search(r'\b(\d+)\b', text)
                    if any_number_match:
                        return f'{{"number": {any_number_match.group(1)}}}'
                
                # 如果所有尝试都失败，但需要一个有效的JSON且schema中有第一个属性
                if schema and "properties" in schema:
                    first_prop = next(iter(schema["properties"]), "result")
                    # 使用文本内容作为该属性的值
                    return f'{{"{first_prop}": {json.dumps(text.strip())}}}'
                
                # 最终回退：返回原始文本
                return text
                
            def _inject_json_format_instructions(self, messages: list, schema: dict) -> list:
                """向消息中注入JSON格式指令"""
                # 创建深拷贝避免修改原始消息
                new_messages = messages.copy()
                
                # 准备JSON格式指令 - 使用更详细的指导
                properties_desc = []
                required_props = []
                
                # 生成每个属性的描述
                if "properties" in schema:
                    for prop_name, prop_info in schema["properties"].items():
                        prop_type = prop_info.get("type", "any")
                        prop_desc = prop_info.get("description", f"{prop_name} 的值")
                        properties_desc.append(f"- {prop_name} ({prop_type}): {prop_desc}")
                        
                        # 如果属性是必须的
                        if "required" in schema and prop_name in schema["required"]:
                            required_props.append(prop_name)
                
                properties_text = "\n".join(properties_desc) if properties_desc else "无特定属性"
                required_text = "、".join(required_props) if required_props else "无"
                
                json_instruction = (
                    "请严格按照以下JSON格式要求作答：\n"
                    "1. 必须返回有效的JSON对象\n"
                    "2. JSON对象应包含以下属性：\n"
                    f"{properties_text}\n"
                    f"3. 必须提供的属性：{required_text}\n"
                    "4. 不要在JSON前后添加额外的文字说明\n"
                    "5. 不要使用Markdown格式或代码块，直接返回原始JSON\n\n"
                    "JSON模式：\n"
                    f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
                )
                
                # 检查是否已有系统消息
                has_system = False
                for i, msg in enumerate(new_messages):
                    if msg.get("role") == "system":
                        has_system = True
                        # 向现有系统消息添加JSON指令
                        new_messages[i]["content"] = f"{msg['content']}\n\n{json_instruction}"
                        break
                
                # 如果没有系统消息，添加一个
                if not has_system:
                    new_messages.insert(0, {"role": "system", "content": json_instruction})
                
                return new_messages
                
            async def create(self, **kwargs):
                """Mimic OpenAI's chat.completions.create method but call Ollama API."""
                # 清理参数中的 NOT_GIVEN 值
                cleaned_kwargs = self._clean_not_given(kwargs)
                
                model = cleaned_kwargs.get("model", DEFAULT_MODEL)
                messages = cleaned_kwargs.get("messages", [])
                temperature = cleaned_kwargs.get("temperature", 0.7)
                max_tokens = cleaned_kwargs.get("max_tokens", 2048)
                stream = cleaned_kwargs.get("stream", False)
                response_format = cleaned_kwargs.get("response_format")
                
                # 检查是否需要JSON格式输出
                needs_json = False
                json_schema = None
                if response_format and response_format.get("type") == "json_schema":
                    needs_json = True
                    json_schema = response_format.get("json_schema", {}).get("schema")
                    # 注入JSON格式指令
                    if json_schema:
                        messages = self._inject_json_format_instructions(messages, json_schema)
                
                # 构建Ollama API请求
                payload = {
                    "model": model,
                    "messages": messages,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    }
                }
                
                # 如果模型支持 json 格式输出，启用它
                # 注意：有些Ollama版本/模型支持format=json选项
                if needs_json:
                    payload["options"]["format"] = "json"
                
                # 检测是使用标准API还是兼容API
                use_compatible_api = True
                try:
                    if stream:
                        # 流式接口总是使用API/chat
                        return await self._create_stream(payload, needs_json=needs_json, json_schema=json_schema)
                    else:
                        # 非流式接口 - 首先尝试v1 API (OpenAI兼容格式)
                        url = f"{self.base_url}/v1/chat/completions"
                        logger.debug(f"Sending request to Ollama compatible API: {url}")
                        
                        response = await self.http_client.post(url, json=payload)
                        if response.status_code == 404:
                            # 如果v1 API不可用，回退到标准API
                            use_compatible_api = False
                        else:
                            response.raise_for_status()
                            data = response.json()  # 这应该是标准OpenAI格式响应
                            logger.debug(f"Received data from Ollama compatible API")
                            
                            # 如果需要JSON格式但没有得到，尝试处理响应文本
                            if needs_json and "choices" in data and data["choices"] and "message" in data["choices"][0]:
                                message = data["choices"][0]["message"]
                                if "content" in message:
                                    content = message["content"]
                                    # 尝试从文本中提取JSON
                                    json_content = self._extract_json_from_text(content, json_schema)
                                    try:
                                        # 验证是否为有效JSON
                                        parsed = json.loads(json_content)
                                        # 替换原始内容
                                        data["choices"][0]["message"]["content"] = json.dumps(parsed)
                                    except json.JSONDecodeError:
                                        # 如果不是有效JSON，保留原样
                                        pass
                                        
                            return ChatCompletion.model_validate(data)  # 直接返回兼容格式
                            
                        if not use_compatible_api:
                            # 使用标准API
                            url = f"{self.base_url}/api/chat"
                            logger.debug(f"Sending request to Ollama native API: {url}")
                            
                            response = await self.http_client.post(url, json=payload)
                            response.raise_for_status()
                            
                            # 处理流式格式返回的多行JSON
                            buffer = ""
                            last_message = None
                            
                            for line in response.text.strip().split('\n'):
                                if not line.strip():
                                    continue
                                    
                                try:
                                    data = json.loads(line)
                                    if "message" in data and "content" in data["message"]:
                                        # 累积内容
                                        buffer += data["message"]["content"]
                                        last_message = data
                                except json.JSONDecodeError:
                                    logger.warning(f"Failed to parse line: {line[:100]}...")
                            
                            # 构建最终响应 - 使用最后一个消息和累积的内容
                            if not last_message:
                                raise OllamaAdapterException("No valid message found in Ollama response")
                                
                            # 使用最后一个消息加上累积的内容
                            final_data = last_message.copy()
                            if "message" in final_data:
                                # 如果需要JSON格式，进行处理
                                content = buffer
                                if needs_json:
                                    # 尝试从文本中提取JSON
                                    content = self._extract_json_from_text(buffer, json_schema)
                                    try:
                                        # 验证是否为有效JSON
                                        json.loads(content)
                                    except json.JSONDecodeError:
                                        # 如果无法解析为JSON，再次尝试简单的包装
                                        if json_schema:
                                            logger.debug(f"Failed to parse JSON from response, using fallback")
                                            # 创建简单JSON，使用更智能的提取
                                            content = self._extract_json_from_text(buffer, json_schema)
                                
                                final_data["message"]["content"] = content
                            
                            # 将Ollama响应转换为类似OpenAI的响应格式
                            return self._convert_to_chat_completion(final_data)
                except httpx.HTTPError as e:
                    raise OllamaAdapterException(f"Ollama API error: {str(e)}") from e
                except Exception as e:
                    raise OllamaAdapterException(f"Unexpected error with Ollama API: {str(e)}") from e
            
            def _convert_to_chat_completion(self, ollama_response: Dict[str, Any]) -> ChatCompletion:
                """将Ollama的响应转换为ChatCompletion格式"""
                # 处理不同版本的Ollama可能有不同的响应结构
                message = ollama_response.get("message", {})
                if not message and "response" in ollama_response:
                    # 某些版本的Ollama可能使用response字段
                    message = {"content": ollama_response.get("response", "")}
                
                response_text = message.get("content", "")
                if not response_text and "response" in ollama_response:
                    # 备选方案：直接使用response字段
                    response_text = ollama_response.get("response", "")
                
                # 估算token使用情况 (Ollama不提供这个数据)
                prompt_text = str(ollama_response.get("prompt", ""))
                prompt_tokens = len(prompt_text.split())
                completion_tokens = len(response_text.split())
                
                chat_message = ChatCompletionMessage(
                    content=response_text,
                    role="assistant",
                    function_call=None,
                    tool_calls=None,
                )
                
                return ChatCompletion(
                    id=str(uuid.uuid4()),
                    choices=[{
                        "finish_reason": "stop",
                        "index": 0,
                        "message": chat_message,
                        "logprobs": None,
                    }],
                    created=int(time.time()),
                    model=ollama_response.get("model", ""),
                    object="chat.completion",
                    system_fingerprint=None,
                    usage=CompletionUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=prompt_tokens + completion_tokens,
                    ),
                )
            
            async def _create_stream(self, payload, needs_json=False, json_schema=None) -> AsyncStream[ChatCompletionChunk]:
                """创建流式响应，类似OpenAI的流式接口"""
                # 先尝试兼容API
                url = f"{self.base_url}/v1/chat/completions"
                stream_payload = payload.copy()
                stream_payload["stream"] = True
                
                try:
                    # 尝试使用兼容API
                    response = await self.http_client.post(url, json=stream_payload, timeout=1.0)
                    # 如果404则回退到标准API
                    if response.status_code == 404:
                        # 标准API不使用stream参数
                        url = f"{self.base_url}/api/chat"
                    else:
                        # 兼容API成功，这里需要特殊处理
                        response.raise_for_status()
                        # 直接返回OpenAI兼容流，不再需要额外处理
                        return AsyncStream.from_response(response)
                except (httpx.HTTPError, httpx.TimeoutException):
                    # 回退到标准API
                    url = f"{self.base_url}/api/chat"
                
                async def stream_generator():
                    try:
                        # 使用标准API
                        logger.debug(f"Streaming from Ollama API: {url}")
                        async with self.http_client.stream("POST", url, json=payload, timeout=60.0) as http_response:
                            http_response.raise_for_status()
                            buffer = ""
                            last_json_sent = False
                            
                            async for chunk in http_response.aiter_text():
                                if not chunk.strip():
                                    continue
                                
                                # 处理可能的多行JSON
                                for line in chunk.strip().split('\n'):
                                    if not line.strip():
                                        continue
                                        
                                    try:
                                        data = json.loads(line)
                                    except json.JSONDecodeError:
                                        logger.warning(f"Failed to parse streaming chunk: {line[:100]}...")
                                        continue
                                    
                                    # 提取内容 - 处理不同响应格式
                                    content = ""
                                    if "message" in data and "content" in data["message"]:
                                        content = data["message"]["content"]
                                    elif "response" in data:
                                        content = data["response"]
                                        
                                    if content:
                                        # 累积全文以便最后处理JSON格式
                                        buffer += content
                                        
                                        # 创建类似的ChatCompletionChunk
                                        yield ChatCompletionChunk(
                                            id=str(uuid.uuid4()),
                                            choices=[{
                                                "delta": {"content": content, "role": "assistant"},
                                                "finish_reason": None,
                                                "index": 0,
                                                "logprobs": None,
                                            }],
                                            created=int(time.time()),
                                            model=data.get("model", ""),
                                            object="chat.completion.chunk",
                                            system_fingerprint=None,
                                            usage=None,
                                        )
                                        
                                    # 处理最后一个块
                                    if data.get("done", False):
                                        # 尝试从累积的缓冲区中提取JSON
                                        if needs_json and buffer and not last_json_sent:
                                            # 使用更强大的JSON提取
                                            json_content = self._extract_json_from_text(buffer, json_schema)
                                            
                                            try:
                                                # 验证JSON有效性
                                                parsed_json = json.loads(json_content)
                                                formatted_json = json.dumps(parsed_json, indent=2)
                                                
                                                # 发送一个单独的delta包含格式化的JSON
                                                yield ChatCompletionChunk(
                                                    id=str(uuid.uuid4()),
                                                    choices=[{
                                                        "delta": {"content": f"\n\n```json\n{formatted_json}\n```", "role": "assistant"},
                                                        "finish_reason": None,
                                                        "index": 0,
                                                        "logprobs": None,
                                                    }],
                                                    created=int(time.time()),
                                                    model=data.get("model", ""),
                                                    object="chat.completion.chunk",
                                                    system_fingerprint=None,
                                                    usage=None,
                                                )
                                                last_json_sent = True
                                            except json.JSONDecodeError:
                                                # 如果提取的内容不是有效JSON，尝试构造一个
                                                if json_schema and "properties" in json_schema:
                                                    first_prop = next(iter(json_schema["properties"].keys()), "result")
                                                    simple_json = {first_prop: buffer.strip()}
                                                    formatted_json = json.dumps(simple_json, indent=2)
                                                    
                                                    # 发送构造的JSON
                                                    yield ChatCompletionChunk(
                                                        id=str(uuid.uuid4()),
                                                        choices=[{
                                                            "delta": {"content": f"\n\n```json\n{formatted_json}\n```", "role": "assistant"},
                                                            "finish_reason": None,
                                                            "index": 0,
                                                            "logprobs": None,
                                                        }],
                                                        created=int(time.time()),
                                                        model=data.get("model", ""),
                                                        object="chat.completion.chunk",
                                                        system_fingerprint=None,
                                                        usage=None,
                                                    )
                                                    last_json_sent = True
                                        
                                        # 估算token
                                        prompt_text = str(data.get("prompt", ""))
                                        prompt_tokens = len(prompt_text.split())
                                        completion_tokens = len(buffer.split())
                                        
                                        # 发送结束标记
                                        yield ChatCompletionChunk(
                                            id=str(uuid.uuid4()),
                                            choices=[{
                                                "delta": {},
                                                "finish_reason": "stop",
                                                "index": 0,
                                                "logprobs": None,
                                            }],
                                            created=int(time.time()),
                                            model=data.get("model", ""),
                                            object="chat.completion.chunk",
                                            system_fingerprint=None,
                                            usage=CompletionUsage(
                                                prompt_tokens=prompt_tokens,
                                                completion_tokens=completion_tokens,
                                                total_tokens=prompt_tokens + completion_tokens,
                                            ),
                                        )
                                    
                    except httpx.HTTPError as e:
                        raise OllamaAdapterException(f"Ollama API stream error: {str(e)}") from e
                    except Exception as e:
                        raise OllamaAdapterException(f"Unexpected error in Ollama stream: {str(e)}") from e
                        
                # 返回AsyncStream适配器
                return AsyncStream(stream_generator())

class OllamaProvider(ModelProvider):
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        default_model: str = DEFAULT_MODEL,
    ) -> None:
        """创建Ollama提供程序
        
        Args:
            base_url: Ollama API基础URL
            default_model: 默认使用的模型名称
        """
        self.base_url = base_url
        self.default_model = default_model
        
    def get_model(self, model: str | Model) -> Model:
        """获取指定名称的模型实例
        
        Args:
            model: 模型名称或实例
            
        Returns:
            Model: 模型实例
        """
        if isinstance(model, Model):
            return model
        
        # 创建适配后的客户端
        ollama_client = OllamaAsyncClient(base_url=self.base_url)
        
        # 使用OpenAIChatCompletionsModel，但传入Ollama的适配器客户端
        return OpenAIChatCompletionsModel(
            model=model or self.default_model,
            openai_client=ollama_client,
        )
        
    async def check_health(self) -> bool:
        """检查Ollama服务是否可用
        
        Returns:
            bool: True表示服务正常，False表示服务异常
        """
        try:
            client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
            response = await client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            return bool(response.json().get("models"))
        except Exception:
            return False
