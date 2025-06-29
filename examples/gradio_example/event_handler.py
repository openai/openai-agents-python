# Abstract event handler
from abc import ABC, abstractmethod
from typing import Optional

from gradio.components import ChatMessage
from openai.types.responses import ResponseTextDeltaEvent

from agents import StreamEvent, ToolCallItem, ToolCallOutputItem


class EventHandler(ABC):
    """Abstract event handler"""

    def __init__(self):
        self._next_handler: Optional[EventHandler] = None

    def set_next(self, handler: 'EventHandler') -> 'EventHandler':
        """Set next handler"""
        self._next_handler = handler
        return handler

    def handle(self, event:StreamEvent, messages: list, context: dict) -> bool:
        """
        Process event, return True if handled, False if not handled
        """
        if self.can_handle(event):
            return self._process(event, messages, context)
        elif self._next_handler:
            return self._next_handler.handle(event, messages, context)
        return False

    @abstractmethod
    def can_handle(self, event:StreamEvent) -> bool:
        """Determine if this event can be handled"""
        pass

    @abstractmethod
    def _process(self, event:StreamEvent, messages: list, context: dict) -> bool:
        """Specific processing logic"""
        pass

# Tool call handler
class ToolCallHandler(EventHandler):
    """Handle tool call events"""

    def can_handle(self, event) -> bool:
        return (hasattr(event, 'type') and event.type == "run_item_stream_event"
                and hasattr(event, 'name') and event.name == "tool_called"
                and isinstance(event.item, ToolCallItem))


    def _process(self, event, messages: list, context: dict) -> bool:
        tool_name = getattr(event.item.raw_item, "name", "unknown_tool")
        getattr(event.item.raw_item, "server_label", "unknown_server")
        arguments = getattr(event.item.raw_item, "arguments", {})
        # Regular tool call
        tool_msg = f"🛠️ {tool_name} parameters: {arguments}"
        tool_title = "🛠️ Tool Call"

        # Add to message list in context
        context["current_messages"].append(ChatMessage(
            role="assistant",
            content=tool_msg,
            metadata={"title": tool_title}
        ))
        return True

# Tool output handler
class ToolOutputHandler(EventHandler):
    """Handle tool output events"""

    def can_handle(self, event) -> bool:
        return (hasattr(event, 'type') and event.type == "run_item_stream_event"
                and hasattr(event, 'name') and event.name == "tool_output"
                and isinstance(event.item, ToolCallOutputItem))


    def _process(self, event, messages: list, context: dict) -> bool:
        tool_output = getattr(event.item, "output", "unknown_output")
        # Regular tool output
        tool_msg = f"🛠️ {tool_output}"
        tool_title = "🛠️ Tool Output"

        # Add to message list in context
        context["current_messages"].append(ChatMessage(
            role="assistant",
            content=tool_msg,
            metadata={"title": tool_title}
        ))
        return True

# Agent Update handler
class AgentUpdateHandler(EventHandler):
    """Handle agent update events"""

    def can_handle(self, event) -> bool:
        return (hasattr(event, 'type') and event.type == "agent_updated_stream_event")

    def _process(self, event, messages: list, context: dict) -> bool:
        agent_name = getattr(event.new_agent, "name", "unknown_agent")
        agent_update_msg = f"🤖 Agent {agent_name} updated"

        # Add to message list in context
        context["current_messages"].append(ChatMessage(
            role="assistant",
            content=agent_update_msg,
            metadata={"title": "🤖 Agent Update"}
        ))
        return True

# Handoff handler
class HandoffHandler(EventHandler):
    """Handle agent handoff request events"""

    def can_handle(self, event) -> bool:
        return (hasattr(event, 'type') and event.type == "run_item_stream_event"
                and hasattr(event, 'name') and event.name == "handoff_requested")

    def _process(self, event, messages: list, context: dict) -> bool:
        handoff_from_func = getattr(event.item.raw_item, "name", "unknown")
        handoff_msg = f"🤝 Handoff requested from {handoff_from_func}"

        # Add to message list in context
        context["current_messages"].append(ChatMessage(
            role="assistant",
            content=handoff_msg,
            metadata={"title": "🤝 Handoff Requested"}
        ))
        return True

# Handoff completion handler
class HandoffOccuredHandler(EventHandler):
    """Handle agent handoff completion events"""

    def can_handle(self, event) -> bool:
        return (hasattr(event, 'type') and event.type == "run_item_stream_event"
                and hasattr(event, 'name') and event.name == "handoff_occured")

    def _process(self, event, messages: list, context: dict) -> bool:
        agent_from = getattr(event.item.source_agent, "name", "unknown")
        agent_to = getattr(event.item.target_agent, "name", "unknown")
        handoff_msg = f"🤝 Handoff completed from {agent_from} to {agent_to}"

        # Add to message list in context
        context["current_messages"].append(ChatMessage(
            role="assistant",
            content=handoff_msg,
            metadata={"title": "🤝 Handoff Completed"}
        ))
        return True

# Reasoning handler
class ReasoningHandler(EventHandler):
    """Handle reasoning events"""

    def can_handle(self, event) -> bool:
        return (hasattr(event, 'type') and event.type == "run_item_stream_event"
                and hasattr(event, 'name') and event.name == "reasoning_item_created")

    def _process(self, event, messages: list, context: dict) -> bool:
        reasoning = getattr(event.item.raw_item, "summary", None)
        if reasoning:
            summary_text = "\n".join([s.text for s in reasoning])
        else:
            summary_text = str(event.item.raw_item)
        print(f"🧠 Reasoning: {summary_text}")

        # Add to message list in context
        context["current_messages"].append(ChatMessage(
            role="assistant",
            content=summary_text,
            metadata={"title": "🧠 Reasoning"}
        ))
        return True



# MCP approval handler
class MCPApprovalHandler(EventHandler):
    """Handle MCP approval events"""

    def can_handle(self, event) -> bool:
        return (hasattr(event, 'type') and event.type == "run_item_stream_event"
                and hasattr(event, 'name') and event.name == "mcp_approval_requested")

    def _process(self, event, messages: list, context: dict) -> bool:
        mcp_approval_args = getattr(event.item.raw_item, "arguments", "unknown_arguments")
        mcp_approval_name = getattr(event.item.raw_item, "name", "unknown_name")
        mcp_approval_msg = f"🔧 MCP approval requested: {mcp_approval_args} for {mcp_approval_name}"
        mcp_approval_title = f"🔧 MCP approval requested: {mcp_approval_name}"
        print(mcp_approval_msg)

        # Add to message list in context
        context["current_messages"].append(ChatMessage(
            role="assistant",
            content=mcp_approval_msg,
            metadata={"title": mcp_approval_title}
        ))
        return True

# MCP tool list handler
class MCPListToolsHandler(EventHandler):
    """Handle MCP tool list events"""

    def can_handle(self, event) -> bool:
        return (hasattr(event, 'type') and event.type == "run_item_stream_event"
                and hasattr(event, 'name') and event.name == "mcp_list_tools")

    def _process(self, event, messages: list, context: dict) -> bool:
        mcp_list_tools_name_list = [tool.name for tool in event.item.raw_item.tools]
        [f"tool: {tool.name} with description {tool.description} \n"  for tool in event.item.raw_item.tools]
        mcp_list_tools_msg = f"🔧 MCP Tools: {', '.join(mcp_list_tools_name_list)}"
        mcp_list_tools_title = f"🔧 MCP Tools: {mcp_list_tools_name_list}"
        print(mcp_list_tools_msg)

        # Add to message list in context
        context["current_messages"].append(ChatMessage(
            role="assistant",
            content=mcp_list_tools_msg,
            metadata={"title": mcp_list_tools_title}
        ))
        return True

# Text response handler
class TextResponseHandler(EventHandler):
    """Handle text response events"""

    def can_handle(self, event) -> bool:
        return (hasattr(event, 'type') and event.type == "raw_response_event"
                and isinstance(event.data, ResponseTextDeltaEvent))

    def _process(self, event, messages: list, context: dict) -> bool:
        response_buffer = context.get("response_buffer", "")
        response_buffer += event.data.delta
        context["response_buffer"] = response_buffer

        # Find or create text response message
        text_message_found = False
        for i, msg in enumerate(context["current_messages"]):
            if (hasattr(msg, 'role') and msg.role == "assistant"
                and (not hasattr(msg, 'metadata') or not msg.metadata)):
                # Update existing text message
                context["current_messages"][i] = ChatMessage(
                    role="assistant",
                    content=response_buffer
                )
                text_message_found = True
                break

        # If no text message found, create a new one
        if not text_message_found:
            context["current_messages"].append(ChatMessage(
                role="assistant",
                content=response_buffer
            ))

        return True

# Event handler chain
class EventHandlerChain:
    """Event handler chain manager"""

    def __init__(self):
        self._first_handler: Optional[EventHandler] = None
        self._setup_chain()

    def _setup_chain(self):
        """Setup handler chain"""
        self._first_handler = ToolCallHandler()
        self._first_handler.set_next(ToolOutputHandler()) \
                         .set_next(AgentUpdateHandler()) \
                         .set_next(HandoffHandler()) \
                         .set_next(HandoffOccuredHandler()) \
                         .set_next(MCPApprovalHandler()) \
                         .set_next(MCPListToolsHandler()) \
                         .set_next(ReasoningHandler()) \
                         .set_next(TextResponseHandler())

    def process_event(self, event, messages: list, context: dict) -> bool:
        """Process event"""
        if self._first_handler:
            return self._first_handler.handle(event, messages, context)
        return False
