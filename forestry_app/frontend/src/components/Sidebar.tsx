import { ChatListItem } from '../lib/api';
import {
  MessageSquare,
  Plus,
  Trash2,
  Users,
  FileText,
} from 'lucide-react';
import { useLocation, Link } from 'react-router-dom';
import clsx from 'clsx';

interface SidebarProps {
  chats: ChatListItem[];
  onNewChat: () => void;
  onSelectChat: (chatId: number) => void;
  onDeleteChat: (chatId: number) => void;
  selectedAgents: string[];
  onToggleAgentsPanel: () => void;
}

export default function Sidebar({
  chats,
  onNewChat,
  onSelectChat,
  onDeleteChat,
  selectedAgents,
  onToggleAgentsPanel,
}: SidebarProps) {
  const location = useLocation();
  const currentChatId = location.pathname.startsWith('/chat/')
    ? parseInt(location.pathname.split('/')[2])
    : null;

  const formatDate = (dateStr: string) => {
    const date = new Date(dateStr);
    const now = new Date();
    const diffDays = Math.floor(
      (now.getTime() - date.getTime()) / (1000 * 60 * 60 * 24)
    );

    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    if (diffDays < 7) return `${diffDays} days ago`;
    return date.toLocaleDateString();
  };

  return (
    <div className="flex-1 flex flex-col overflow-hidden">
      {/* Actions */}
      <div className="p-4 space-y-2">
        <button
          onClick={onNewChat}
          className="w-full flex items-center gap-2 px-4 py-2.5 bg-forest-500 hover:bg-forest-600 text-white rounded-lg transition-colors font-medium"
        >
          <Plus className="w-5 h-5" />
          New Chat
        </button>

        <button
          onClick={onToggleAgentsPanel}
          className="w-full flex items-center justify-between px-4 py-2.5 bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors"
        >
          <span className="flex items-center gap-2">
            <Users className="w-5 h-5" />
            Select Agents
          </span>
          {selectedAgents.length > 0 && (
            <span className="px-2 py-0.5 bg-forest-500 text-white text-xs rounded-full">
              {selectedAgents.length}
            </span>
          )}
        </button>

        <Link
          to="/plans"
          className={clsx(
            'w-full flex items-center gap-2 px-4 py-2.5 rounded-lg transition-colors',
            location.pathname === '/plans'
              ? 'bg-forest-100 text-forest-700'
              : 'bg-gray-100 hover:bg-gray-200 text-gray-700'
          )}
        >
          <FileText className="w-5 h-5" />
          Plans
        </Link>
      </div>

      {/* Chat list */}
      <div className="flex-1 overflow-y-auto px-4 pb-4">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
          Recent Chats
        </h3>

        {chats.length === 0 ? (
          <div className="text-center py-8 text-gray-400">
            <MessageSquare className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">No chats yet</p>
          </div>
        ) : (
          <div className="space-y-1">
            {chats.map((chat) => (
              <div
                key={chat.id}
                className={clsx(
                  'group flex items-center gap-2 p-3 rounded-lg cursor-pointer transition-colors',
                  currentChatId === chat.id
                    ? 'bg-forest-100 text-forest-800'
                    : 'hover:bg-gray-100 text-gray-700'
                )}
                onClick={() => onSelectChat(chat.id)}
              >
                <MessageSquare className="w-4 h-4 flex-shrink-0 opacity-60" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium truncate">{chat.title}</p>
                  <p className="text-xs text-gray-400">
                    {formatDate(chat.updated_at)} · {chat.message_count} msgs
                  </p>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onDeleteChat(chat.id);
                  }}
                  className="opacity-0 group-hover:opacity-100 p-1 hover:bg-red-100 rounded text-gray-400 hover:text-red-500 transition-all"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
