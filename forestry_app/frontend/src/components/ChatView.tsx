import { useState, useRef, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { useActiveChat } from '../hooks/useChat';
import { Agent } from '../lib/api';
import ReactMarkdown from 'react-markdown';
import {
  Send,
  Loader2,
  Bot,
  User,
  ChevronDown,
  Check,
} from 'lucide-react';
import clsx from 'clsx';

interface ChatViewProps {
  agents: Agent[];
  getAgent: (id: string) => Agent | undefined;
  selectedAgents: string[];
  onAgentToggle: (agentId: string) => void;
  onRefreshChats: () => void;
}

export default function ChatView({
  agents,
  getAgent,
  selectedAgents,
  onAgentToggle,
  onRefreshChats,
}: ChatViewProps) {
  const { chatId } = useParams<{ chatId: string }>();
  const chatIdNum = chatId ? parseInt(chatId) : null;

  const {
    chat,
    messages,
    loading,
    sending,
    streaming,
    streamContent,
    error,
    sendMessage,
    updateAgents,
  } = useActiveChat(chatIdNum);

  const [input, setInput] = useState('');
  const [showAgentDropdown, setShowAgentDropdown] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamContent]);

  // Focus input on mount
  useEffect(() => {
    inputRef.current?.focus();
  }, [chatIdNum]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || sending) return;

    const message = input.trim();
    setInput('');

    const agentsToUse =
      selectedAgents.length > 0 ? selectedAgents : chat?.agent_ids;
    await sendMessage(message, agentsToUse);
    onRefreshChats();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const currentAgents = chat?.agent_ids || selectedAgents;

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="w-8 h-8 text-forest-500 animate-spin" />
          <p className="text-gray-500">Loading chat...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col h-full">
      {/* Header */}
      <div className="px-6 py-4 border-b border-gray-200 bg-white flex items-center justify-between">
        <div>
          <h2 className="font-semibold text-gray-900">
            {chat?.title || 'New Chat'}
          </h2>
          <p className="text-sm text-gray-500">
            {currentAgents.length > 0
              ? `${currentAgents.length} agent(s) active`
              : 'No agents selected - will auto-route'}
          </p>
        </div>

        {/* Agent selector dropdown */}
        <div className="relative">
          <button
            onClick={() => setShowAgentDropdown(!showAgentDropdown)}
            className="flex items-center gap-2 px-4 py-2 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
          >
            <Bot className="w-4 h-4" />
            <span className="text-sm">
              {currentAgents.length > 0
                ? `${currentAgents.length} Selected`
                : 'Select Agents'}
            </span>
            <ChevronDown className="w-4 h-4" />
          </button>

          {showAgentDropdown && (
            <div className="absolute right-0 top-full mt-2 w-72 bg-white rounded-lg shadow-xl border border-gray-200 z-20 max-h-96 overflow-y-auto">
              <div className="p-2">
                {agents.map((agent) => (
                  <button
                    key={agent.id}
                    onClick={() => {
                      onAgentToggle(agent.id);
                      updateAgents(
                        selectedAgents.includes(agent.id)
                          ? selectedAgents.filter((id) => id !== agent.id)
                          : [...selectedAgents, agent.id]
                      );
                    }}
                    className={clsx(
                      'w-full flex items-center gap-3 p-2 rounded-lg transition-colors text-left',
                      currentAgents.includes(agent.id)
                        ? 'bg-forest-50 text-forest-700'
                        : 'hover:bg-gray-50'
                    )}
                  >
                    <div
                      className="w-8 h-8 rounded-lg flex items-center justify-center text-white text-xs font-bold"
                      style={{ backgroundColor: agent.color }}
                    >
                      {agent.name.charAt(0)}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium truncate">
                        {agent.name}
                      </p>
                      <p className="text-xs text-gray-500 truncate">
                        {agent.category}
                      </p>
                    </div>
                    {currentAgents.includes(agent.id) && (
                      <Check className="w-4 h-4 text-forest-500" />
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-6 space-y-4">
        {messages.length === 0 && !streaming && (
          <div className="text-center py-12">
            <Bot className="w-12 h-12 mx-auto text-gray-300 mb-4" />
            <h3 className="text-lg font-medium text-gray-600 mb-2">
              Start a conversation
            </h3>
            <p className="text-gray-400 max-w-md mx-auto">
              {currentAgents.length > 0
                ? `Chat with ${currentAgents.map((id) => getAgent(id)?.name || id).join(', ')}`
                : 'Ask a question and the system will route it to the appropriate agents'}
            </p>
          </div>
        )}

        {messages.map((message) => (
          <div
            key={message.id}
            className={clsx(
              'flex gap-4 animate-fade-in',
              message.role === 'user' ? 'justify-end' : 'justify-start'
            )}
          >
            {message.role === 'assistant' && (
              <div className="flex-shrink-0 w-8 h-8 bg-forest-500 rounded-lg flex items-center justify-center">
                <Bot className="w-5 h-5 text-white" />
              </div>
            )}

            <div
              className={clsx(
                'max-w-[75%] rounded-2xl px-4 py-3',
                message.role === 'user'
                  ? 'bg-forest-500 text-white'
                  : 'bg-white border border-gray-200 shadow-sm'
              )}
            >
              {message.role === 'assistant' && message.agent_id && (
                <div className="flex flex-wrap gap-1 mb-2">
                  {message.agent_id.split(',').map((agentId) => {
                    const agent = getAgent(agentId.trim());
                    return agent ? (
                      <span
                        key={agentId}
                        className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium"
                        style={{
                          backgroundColor: `${agent.color}20`,
                          color: agent.color,
                        }}
                      >
                        {agent.name}
                      </span>
                    ) : null;
                  })}
                </div>
              )}

              <div
                className={clsx(
                  'prose prose-sm max-w-none',
                  message.role === 'user' && 'prose-invert'
                )}
              >
                <ReactMarkdown>{message.content}</ReactMarkdown>
              </div>
            </div>

            {message.role === 'user' && (
              <div className="flex-shrink-0 w-8 h-8 bg-gray-200 rounded-lg flex items-center justify-center">
                <User className="w-5 h-5 text-gray-600" />
              </div>
            )}
          </div>
        ))}

        {/* Streaming response */}
        {streaming && streamContent && (
          <div className="flex gap-4 animate-fade-in">
            <div className="flex-shrink-0 w-8 h-8 bg-forest-500 rounded-lg flex items-center justify-center">
              <Bot className="w-5 h-5 text-white" />
            </div>
            <div className="max-w-[75%] rounded-2xl px-4 py-3 bg-white border border-gray-200 shadow-sm">
              <div className="prose prose-sm max-w-none">
                <ReactMarkdown>{streamContent}</ReactMarkdown>
              </div>
            </div>
          </div>
        )}

        {/* Typing indicator */}
        {sending && !streamContent && (
          <div className="flex gap-4">
            <div className="flex-shrink-0 w-8 h-8 bg-forest-500 rounded-lg flex items-center justify-center">
              <Bot className="w-5 h-5 text-white" />
            </div>
            <div className="bg-white border border-gray-200 rounded-2xl px-4 py-3 shadow-sm">
              <div className="flex gap-1">
                <div className="typing-dot" />
                <div className="typing-dot" />
                <div className="typing-dot" />
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Error message */}
      {error && (
        <div className="px-6 py-2 bg-red-50 border-t border-red-100">
          <p className="text-sm text-red-600">{error}</p>
        </div>
      )}

      {/* Input */}
      <div className="p-4 bg-white border-t border-gray-200">
        <form onSubmit={handleSubmit} className="flex gap-3">
          <div className="flex-1 relative">
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type your message... (Shift+Enter for new line)"
              rows={1}
              className="w-full px-4 py-3 bg-gray-100 rounded-xl resize-none focus:outline-none focus:ring-2 focus:ring-forest-500 focus:bg-white transition-all"
              disabled={sending}
            />
          </div>
          <button
            type="submit"
            disabled={!input.trim() || sending}
            className="px-4 py-3 bg-forest-500 hover:bg-forest-600 disabled:bg-gray-300 disabled:cursor-not-allowed text-white rounded-xl transition-colors"
          >
            {sending ? (
              <Loader2 className="w-5 h-5 animate-spin" />
            ) : (
              <Send className="w-5 h-5" />
            )}
          </button>
        </form>
      </div>
    </div>
  );
}
