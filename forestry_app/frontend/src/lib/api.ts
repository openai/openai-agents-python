const API_BASE = '/api';

interface ApiResponse<T> {
  data?: T;
  error?: string;
}

async function fetchApi<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<ApiResponse<T>> {
  const token = localStorage.getItem('token');

  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    ...options.headers,
  };

  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  try {
    const response = await fetch(`${API_BASE}${endpoint}`, {
      ...options,
      headers,
    });

    if (!response.ok) {
      if (response.status === 401) {
        localStorage.removeItem('token');
        window.location.href = '/login';
        return { error: 'Unauthorized' };
      }
      const errorData = await response.json().catch(() => ({}));
      return { error: errorData.detail || `Error: ${response.status}` };
    }

    const data = await response.json();
    return { data };
  } catch (err) {
    return { error: err instanceof Error ? err.message : 'Network error' };
  }
}

// Auth API
export const auth = {
  login: async (username: string, password: string) => {
    const formData = new URLSearchParams();
    formData.append('username', username);
    formData.append('password', password);

    const response = await fetch(`${API_BASE}/auth/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: formData,
    });

    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || 'Login failed');
    }

    const data = await response.json();
    localStorage.setItem('token', data.access_token);
    return data;
  },

  logout: () => {
    localStorage.removeItem('token');
  },

  getUser: () => fetchApi<{ username: string; id: number }>('/auth/me'),

  isAuthenticated: () => !!localStorage.getItem('token'),
};

// Agents API
export interface Agent {
  id: string;
  name: string;
  description: string;
  category: string;
  produces: string[];
  icon: string;
  color: string;
}

export interface AgentListResponse {
  agents: Agent[];
  categories: string[];
}

export interface Team {
  name: string;
  description: string;
  agents: string[];
}

export const agents = {
  list: () => fetchApi<AgentListResponse>('/agents/'),
  get: (id: string) => fetchApi<Agent>(`/agents/${id}`),
  getCategories: () => fetchApi<{ categories: string[] }>('/agents/categories'),
  getByCategory: (category: string) =>
    fetchApi<{ category: string; agents: Agent[] }>(`/agents/category/${encodeURIComponent(category)}`),
  getTeams: () => fetchApi<{ teams: Record<string, Team> }>('/agents/teams/default'),
  route: (message: string, context?: string) =>
    fetchApi<{ recommended_agents: string[]; reasoning: string; confidence: number }>(
      '/agents/route',
      {
        method: 'POST',
        body: JSON.stringify({ message, context }),
      }
    ),
};

// Chats API
export interface Message {
  id: number;
  chat_id: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  agent_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface Chat {
  id: number;
  user_id: number;
  title: string;
  agent_ids: string[];
  created_at: string;
  updated_at: string;
  is_archived: boolean;
  messages: Message[];
}

export interface ChatListItem {
  id: number;
  title: string;
  agent_ids: string[];
  created_at: string;
  updated_at: string;
  message_count: number;
}

export const chats = {
  list: () => fetchApi<ChatListItem[]>('/chats/'),
  get: (id: number) => fetchApi<Chat>(`/chats/${id}`),
  create: (title?: string, agent_ids?: string[]) =>
    fetchApi<Chat>('/chats/', {
      method: 'POST',
      body: JSON.stringify({ title, agent_ids }),
    }),
  update: (id: number, title?: string, agent_ids?: string[]) =>
    fetchApi<{ message: string }>(`/chats/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ title, agent_ids }),
    }),
  delete: (id: number) =>
    fetchApi<{ message: string }>(`/chats/${id}`, { method: 'DELETE' }),
  sendMessage: (chatId: number, content: string, agent_ids?: string[]) =>
    fetchApi<Message>(`/chats/${chatId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content, agent_ids }),
    }),
};

// Plans API
export interface Plan {
  id: number;
  user_id: number;
  title: string;
  description: string | null;
  agent_ids: string[];
  status: string;
  content: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface PlanListItem {
  id: number;
  title: string;
  description: string | null;
  status: string;
  agent_ids: string[];
  created_at: string;
  updated_at: string;
}

export const plans = {
  list: (status?: string) =>
    fetchApi<PlanListItem[]>(`/plans/${status ? `?status=${status}` : ''}`),
  get: (id: number) => fetchApi<Plan>(`/plans/${id}`),
  create: (
    title: string,
    description?: string,
    agent_ids?: string[],
    content?: Record<string, unknown>
  ) =>
    fetchApi<Plan>('/plans/', {
      method: 'POST',
      body: JSON.stringify({ title, description, agent_ids, content }),
    }),
  update: (
    id: number,
    updates: {
      title?: string;
      description?: string;
      agent_ids?: string[];
      status?: string;
      content?: Record<string, unknown>;
    }
  ) =>
    fetchApi<Plan>(`/plans/${id}`, {
      method: 'PUT',
      body: JSON.stringify(updates),
    }),
  delete: (id: number) =>
    fetchApi<{ message: string }>(`/plans/${id}`, { method: 'DELETE' }),
  execute: (id: number) =>
    fetchApi<{
      plan_id: number;
      status: string;
      task_results: Array<{
        task_id: number;
        status: string;
        output?: string;
        error?: string;
      }>;
    }>(`/plans/${id}/execute`, { method: 'POST' }),
};

// WebSocket helper
export function createChatWebSocket(
  chatId: number,
  onMessage: (data: {
    type: string;
    content?: string;
    agents?: string[];
    message_id?: number;
    error?: string;
  }) => void,
  onClose?: () => void
): WebSocket | null {
  const token = localStorage.getItem('token');
  if (!token) return null;

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  const ws = new WebSocket(
    `${protocol}//${host}/api/chats/ws/${chatId}?token=${encodeURIComponent(token)}`
  );

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      onMessage(data);
    } catch (e) {
      console.error('WebSocket message parse error:', e);
    }
  };

  ws.onclose = () => {
    onClose?.();
  };

  ws.onerror = (error) => {
    console.error('WebSocket error:', error);
  };

  return ws;
}

export default { auth, agents, chats, plans };
