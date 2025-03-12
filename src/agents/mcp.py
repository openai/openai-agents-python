from __future__ import annotations

from typing import Any, Dict, List, Set

from mcp.types import EmbeddedResource, ImageContent, TextContent, Tool as MCPTool
from mcp_agent.config import MCPSettings, Settings, get_settings
from mcp_agent.context import Context
from mcp_agent.mcp.mcp_aggregator import MCPAggregator
from mcp_agent.mcp_server_registry import ServerRegistry

from . import _utils
from .logger import logger
from .run_context import RunContextWrapper, TContext
from .tool import FunctionTool, Tool

MCPContent = TextContent | ImageContent | EmbeddedResource

# Define a method to automatically load MCP server registry if using MCP servers
def load_mcp_server_registry(
    config: MCPSettings | None = None,
    config_path: str | None = None
) -> ServerRegistry:
    """
    Load MCP server registry from config object or config file path.

    Args:
        config: The MCPSettings object containing the server configurations.
            If unspecified, it will be loaded from the config_path.
        config_path: The file path to load the MCP server configurations from.
            if config is unspecified, this is required.
    """
    try:
        settings: Settings = None
        if config:
            # Use provided settings object
            logger.debug("Loading MCP server registry from provided MCPSettings object.")
            settings = Settings(mcp=config)
        else:
            # Load settings from config file
            logger.debug("Loading MCP server registry from config file: %s", config_path)
            settings = get_settings(config_path)

        # Create the ServerRegistry instance
        server_registry = ServerRegistry(config=settings)
        return server_registry
    except Exception as e:
        logger.error(
            "Error loading MCP server registry. config=%s, config_path=%s, Error: %s",
            config.model_dump_json() if config else "None",
            config_path,
            e
        )
        raise

def ensure_mcp_server_registry_in_context(
    run_context: RunContextWrapper[TContext],
    force: bool = False
) -> ServerRegistry:
    """
    Load the MCP server registry and attach it to the context object.
    If the server registry is already loaded, it will b

    Args:
        run_context: Run context wrapper which will have the server registry attached
        force: Whether to force reload the server registry
    """
    # Check if server registry is already loaded
    server_registry = getattr(run_context.context, 'server_registry', None)
    if not force and server_registry:
        logger.debug("MCP server registry already loaded in context. Skipping reload.")
        return server_registry

    # Load the server registry
    config = getattr(run_context.context, 'mcp_config', None)
    config_path = getattr(run_context.context, 'mcp_config_path', None)
    server_registry = load_mcp_server_registry(config=config, config_path=config_path)

    # Attach the server registry to the context
    run_context.context.mcp_server_registry = server_registry

def create_mcp_aggregator(
    run_context: RunContextWrapper[TContext],
    name: str,
    servers: list[str],
    server_registry: ServerRegistry | None = None,
    connection_persistence: bool = True
) -> MCPAggregator:
    """
    Create the MCP aggregator with the MCP servers from server registry.
    This doesn't initialize the aggregator. For initialization, use `initialize_mcp_aggregator`.

    Args:
        run_context: Run context wrapper
        name: Name of the agent using the aggregator
        servers: List of MCP server names
        server_registry: Server registry instance (if not provided, it will be retrieved from context)
        connection_persistence: Whether to keep the server connections alive, or restart per call
    """
    if not servers:
        raise RuntimeError("No MCP servers specified. No MCP aggregator created.")

    # Get or create the server registry from the context
    context: Context = None
    if server_registry:
        context = Context(server_registry=server_registry)
    else:
        server_registry = getattr(run_context.context, 'mcp_server_registry', None)
        if not server_registry:
            raise RuntimeError(
                "No server registry found in run context. Either specify it or set in context."
            )
        context = Context(server_registry=server_registry)

    # Create the aggregator
    aggregator = MCPAggregator(
        server_names=servers,
        connection_persistence=connection_persistence,
        name=name,
        context=context
    )

    return aggregator

async def initialize_mcp_aggregator(
    run_context: RunContextWrapper[TContext],
    name: str,
    servers: list[str],
    server_registry: ServerRegistry | None = None,
    connection_persistence: bool = True) -> MCPAggregator:
    """Initialize the MCP aggregator, which initializes all the server connections."""
    # Create the aggregator
    aggregator = create_mcp_aggregator(
        run_context=run_context,
        name=name,
        servers=servers,
        server_registry=server_registry,
        connection_persistence=connection_persistence
    )

    # Initialize the aggregator
    try:
        logger.info("Initializing MCPAggregator for %s with servers %s.", name, servers)
        await aggregator.__aenter__()
        logger.debug("MCPAggregator created and initialized for %s.", name)
        return aggregator
    except Exception as e:
        logger.error("Error creating MCPAggregator: %s", e)
        await aggregator.__aexit__(None, None, None)
        raise

def mcp_content_to_text(content: MCPContent | list[MCPContent]) -> str:
    """
    Convert CallToolResult MCP content to text.

    Args:
        content: MCP content object(s)

    Returns:
        String representation of the content
    """
    # Handle list of content items
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if hasattr(item, 'type') and item.type == 'text' and hasattr(item, 'text'):
                # Text content
                text_parts.append(item.text)
            elif hasattr(item, 'type') and item.type == 'image' and hasattr(item, 'data'):
                # Image content - convert to text description
                mime_type = getattr(item, 'mimeType', 'unknown type')
                text_parts.append(f"[Image: {mime_type}]")
            elif hasattr(item, 'resource'):
                # Embedded resource
                resource = item.resource
                if hasattr(resource, 'text'):
                    text_parts.append(resource.text)
                elif hasattr(resource, 'blob'):
                    mime_type = getattr(resource, 'mimeType', 'unknown type')
                    text_parts.append(f"[Resource: {mime_type}]")
            else:
                # Unknown content type
                text_parts.append(str(item))

        if text_parts:
            return "\n".join(text_parts)
        return ""

    # Single content item
    if hasattr(content, 'type') and content.type == 'text' and hasattr(content, 'text'):
        return content.text
    elif hasattr(content, 'type') and content.type == 'image' and hasattr(content, 'data'):
        mime_type = getattr(content, 'mimeType', 'unknown type')
        return f"[Image: {mime_type}]"
    elif hasattr(content, 'resource'):
        resource = content.resource
        if hasattr(resource, 'text'):
            return resource.text
        elif hasattr(resource, 'blob'):
            mime_type = getattr(resource, 'mimeType', 'unknown type')
            return f"[Resource: {mime_type}]"

    # Fallback to string representation
    return str(content)

async def mcp_list_tools(server_aggregator: MCPAggregator) -> list[Tool]:
    """
    List all available tools from MCP servers that are part of the provided server aggregator.

    Args:
        server_aggregator: MCP server aggregator instance (must be initialized already)

    Returns:
        List of available tools
    """

    if not server_aggregator or server_aggregator.initialized is False:
        raise RuntimeError("MCP server aggregator not initialized when calling list_tools")

    # Get tools list from the aggregator
    tools_result = await server_aggregator.list_tools()

    # Convert MCP tools to OpenAI Agent SDK tools
    mcp_tools: list[Tool] = []
    for mcp_tool in tools_result.tools:
        tool = mcp_tool_to_function_tool(mcp_tool, server_aggregator)
        mcp_tools.append(tool)

    return mcp_tools


# JSON Schema properties not supported by OpenAI functions
UNSUPPORTED_SCHEMA_PROPERTIES = {
    "minimum", "minLength", "maxLength", "pattern", "format", "minItems", "maxItems", 
    "uniqueItems", "minProperties", "maxProperties", "multipleOf", 
    "exclusiveMinimum", "exclusiveMaximum", "$schema", "examples", "default"
}

def sanitize_json_schema_for_openai(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitize a JSON Schema to make it compatible with OpenAI function calling.
    Removes properties not supported by OpenAI's function schema validation.

    Args:
        schema: The original JSON schema

    Returns:
        A sanitized schema compatible with OpenAI
    """
    if not isinstance(schema, dict):
        return schema

    result = {}

    # Process each key in the schema
    for key, value in schema.items():
        # Skip unsupported properties
        if key in UNSUPPORTED_SCHEMA_PROPERTIES:
            continue

        # Handle nested objects recursively
        if isinstance(value, dict):
            result[key] = sanitize_json_schema_for_openai(value)
        # Handle arrays of objects
        elif isinstance(value, list):
            result[key] = [
                sanitize_json_schema_for_openai(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    
    # Special handling for the properties/required issue
    # OpenAI requires all properties to be in the required array
    if "type" in result and result["type"] == "object" and "properties" in result:
        # Get all property names
        property_names = list(result.get("properties", {}).keys())
        
        # Set required field to include all properties
        if property_names:
            result["required"] = property_names

    return result

def mcp_tool_to_function_tool(
    mcp_tool: MCPTool, server_aggregator: MCPAggregator
) -> FunctionTool:
    """
    Convert an MCP tool to an OpenAI Agent SDK function tool.
    """

    # Create a properly named wrapper function
    function_name = _utils.transform_string_function_style(mcp_tool.name)

    # Create a wrapper factory to ensure each tool gets its own closure
    def create_wrapper(current_tool_name: str, current_tool_desc: str):
        async def wrapper_fn(ctx: RunContextWrapper[TContext], **kwargs: Any) -> Any:
            """MCP Tool wrapper function."""
            if not server_aggregator or server_aggregator.initialized is False:
                raise RuntimeError(
                    f"MCP aggregator not initialized for agent {server_aggregator.agent_name}"
                )

            # Call the tool through the aggregator
            result = await server_aggregator.call_tool(
                name=current_tool_name,
                arguments=kwargs
            )

            # Handle errors
            if getattr(result, 'isError', False):
                error_message = "Unknown error"
                # Try to extract error from content if available
                if hasattr(result, 'content'):
                    error_message = mcp_content_to_text(result.content)
                raise RuntimeError(
                    f"Error calling MCP tool '{current_tool_name}': {error_message}"
                )

            # Convert MCP content to string using helper method
            if hasattr(result, 'content'):
                return mcp_content_to_text(result.content)

            # Fallback for unexpected formats
            return str(result)

        # Set proper name and docstring for the function
        wrapper_fn.__name__ = function_name
        wrapper_fn.__doc__ = current_tool_desc or f"MCP tool: {current_tool_name}"
        return wrapper_fn

    # Create a wrapper for this specific tool
    tool_desc = mcp_tool.description or f"MCP tool: {mcp_tool.name}"
    wrapper_fn = create_wrapper(mcp_tool.name, tool_desc)

    # Create JSON schema for parameters - MCP uses inputSchema 
    params_schema = getattr(mcp_tool, 'inputSchema', {
        "type": "object",
        "properties": {},
        "required": [],
    })

    # OpenAI requires additionalProperties to be false for tool schemas
    params_schema["additionalProperties"] = False


    # Sanitize schema to remove properties not supported by OpenAI
    # OpenAI doesn't support minLength, maxLength, pattern, format, etc.
    params_schema = sanitize_json_schema_for_openai(params_schema)

    # Create a invoke tool factory to ensure each tool gets its own closure
    def create_invoke_tool(current_tool_name: str, current_wrapper_fn):
        async def invoke_tool(run_context: RunContextWrapper[Any], arguments_json: str) -> str:
            try:
                # Parse arguments from JSON
                import json
                args = json.loads(arguments_json)

                # Call the wrapper function with the arguments
                result = await current_wrapper_fn(run_context, **args)

                # Since wrapper_fn now returns string content (via _mcp_content_to_text),
                # we should already have a properly formatted string result
                return result
            except Exception as e:
                # Log the error
                logger.error(f"Error invoking MCP tool {current_tool_name}: {e}")

                # Format error message
                error_type = type(e).__name__
                error_message = str(e)

                # Return error message that's helpful to the model
                return f"Error ({error_type}): {error_message}"
        return invoke_tool

    # Create the invoke tool function specific to this tool
    invoke_tool = create_invoke_tool(mcp_tool.name, wrapper_fn)

    # Create a function tool
    tool = FunctionTool(
        name=mcp_tool.name,
        description=mcp_tool.description or f"MCP tool: {mcp_tool.name}",
        params_json_schema=params_schema,
        on_invoke_tool=invoke_tool,
    )

    return tool
