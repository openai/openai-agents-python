import { useState, useEffect, useCallback, useRef } from 'react';
import { chats, Chat, ChatListItem, Message, createChatWebSocket } from '../lib/api';

export function useChats() {
  const [chatList, setChatList] = useState<ChatListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchChats = useCallback(async () => {
    setLoading(true);
    const response = await chats.list();
    if (response.data) {
      setChatList(response.data);
    } else if (response.error) {
      setError(response.error);
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    fetchChats();
  }, [fetchChats]);

  const createChat = async (title?: string, agentIds?: string[]) => {
    const response = await chats.create(title, agentIds);
    if (response.data) {
      await fetchChats();
      return response.data;
    }
    return null;
  };

  const deleteChat = async (id: number) => {
    const response = await chats.delete(id);
    if (!response.error) {
      await fetchChats();
      return true;
    }
    return false;
  };

  return {
    chats: chatList,
    loading,
    error,
    createChat,
    deleteChat,
    refresh: fetchChats,
  };
}

export function useActiveChat(chatId: number | null) {
  const [chat, setChat] = useState<Chat | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [streamContent, setStreamContent] = useState('');
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const fetchChat = useCallback(async () => {
    if (!chatId) {
      setChat(null);
      setMessages([]);
      return;
    }

    setLoading(true);
    const response = await chats.get(chatId);
    if (response.data) {
      setChat(response.data);
      setMessages(response.data.messages);
    } else if (response.error) {
      setError(response.error);
    }
    setLoading(false);
  }, [chatId]);

  useEffect(() => {
    fetchChat();
  }, [fetchChat]);

  // Cleanup WebSocket on unmount or chat change
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [chatId]);

  const sendMessage = async (content: string, agentIds?: string[]) => {
    if (!chatId) return null;

    setSending(true);
    setError(null);

    // Add user message optimistically
    const tempUserMessage: Message = {
      id: Date.now(),
      chat_id: chatId,
      role: 'user',
      content,
      agent_id: null,
      metadata: {},
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMessage]);

    const response = await chats.sendMessage(chatId, content, agentIds);
    setSending(false);

    if (response.data) {
      // Replace temp message with real one and add assistant response
      setMessages((prev) => {
        const filtered = prev.filter((m) => m.id !== tempUserMessage.id);
        return [
          ...filtered,
          { ...tempUserMessage, id: response.data!.id - 1 },
          response.data!,
        ];
      });
      return response.data;
    } else if (response.error) {
      setError(response.error);
      // Remove optimistic message on error
      setMessages((prev) => prev.filter((m) => m.id !== tempUserMessage.id));
    }
    return null;
  };

  const sendMessageStreaming = async (content: string, agentIds?: string[]) => {
    if (!chatId) return;

    setSending(true);
    setStreaming(true);
    setStreamContent('');
    setError(null);

    // Add user message optimistically
    const tempUserMessage: Message = {
      id: Date.now(),
      chat_id: chatId,
      role: 'user',
      content,
      agent_id: null,
      metadata: {},
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, tempUserMessage]);

    // Close existing WebSocket if any
    if (wsRef.current) {
      wsRef.current.close();
    }

    let fullContent = '';

    wsRef.current = createChatWebSocket(
      chatId,
      (data) => {
        switch (data.type) {
          case 'stream_start':
            setStreamContent('');
            fullContent = '';
            break;
          case 'stream':
            if (data.content) {
              fullContent += data.content;
              setStreamContent(fullContent);
            }
            break;
          case 'stream_end':
            setStreaming(false);
            break;
          case 'message_complete':
            setSending(false);
            setStreaming(false);
            if (data.content && data.message_id) {
              const assistantMessage: Message = {
                id: data.message_id,
                chat_id: chatId,
                role: 'assistant',
                content: data.content,
                agent_id: data.agents?.join(',') || null,
                metadata: { agents_used: data.agents },
                created_at: new Date().toISOString(),
              };
              setMessages((prev) => [...prev, assistantMessage]);
              setStreamContent('');
            }
            break;
          case 'error':
            setSending(false);
            setStreaming(false);
            setError(data.error || 'Unknown error');
            break;
        }
      },
      () => {
        setSending(false);
        setStreaming(false);
      }
    );

    if (wsRef.current) {
      wsRef.current.onopen = () => {
        wsRef.current?.send(JSON.stringify({ content, agent_ids: agentIds }));
      };
    }
  };

  const updateAgents = async (agentIds: string[]) => {
    if (!chatId) return false;
    const response = await chats.update(chatId, undefined, agentIds);
    if (!response.error) {
      setChat((prev) => (prev ? { ...prev, agent_ids: agentIds } : null));
      return true;
    }
    return false;
  };

  return {
    chat,
    messages,
    loading,
    sending,
    streaming,
    streamContent,
    error,
    sendMessage,
    sendMessageStreaming,
    updateAgents,
    refresh: fetchChat,
  };
}
