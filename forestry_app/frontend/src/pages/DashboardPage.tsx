import { useState } from 'react';
import { Routes, Route, useNavigate } from 'react-router-dom';
import Sidebar from '../components/Sidebar';
import ChatView from '../components/ChatView';
import AgentsPanel from '../components/AgentsPanel';
import PlansView from '../components/PlansView';
import WelcomeView from '../components/WelcomeView';
import { useAuth } from '../hooks/useAuth';
import { useChats } from '../hooks/useChat';
import { useAgents } from '../hooks/useAgents';
import {
  Menu,
  X,
  LogOut,
  TreePine,
} from 'lucide-react';

export default function DashboardPage() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [selectedAgents, setSelectedAgents] = useState<string[]>([]);
  const [showAgentsPanel, setShowAgentsPanel] = useState(false);
  const { user, logout } = useAuth();
  const { chats, createChat, deleteChat, refresh: refreshChats } = useChats();
  const { agents, categories, teams, getAgent } = useAgents();
  const navigate = useNavigate();

  const handleNewChat = async () => {
    const chat = await createChat('New Chat', selectedAgents);
    if (chat) {
      navigate(`/chat/${chat.id}`);
    }
  };

  const handleSelectChat = (chatId: number) => {
    navigate(`/chat/${chatId}`);
  };

  const handleDeleteChat = async (chatId: number) => {
    await deleteChat(chatId);
    navigate('/');
  };

  const handleAgentToggle = (agentId: string) => {
    setSelectedAgents((prev) =>
      prev.includes(agentId)
        ? prev.filter((id) => id !== agentId)
        : [...prev, agentId]
    );
  };

  const handleSelectTeam = (agentIds: string[]) => {
    setSelectedAgents(agentIds);
  };

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  return (
    <div className="h-screen flex bg-gray-50">
      {/* Mobile sidebar toggle */}
      <button
        onClick={() => setSidebarOpen(!sidebarOpen)}
        className="lg:hidden fixed top-4 left-4 z-50 p-2 bg-white rounded-lg shadow-md"
      >
        {sidebarOpen ? <X className="w-6 h-6" /> : <Menu className="w-6 h-6" />}
      </button>

      {/* Sidebar */}
      <div
        className={`
          fixed lg:relative inset-y-0 left-0 z-40
          transform ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
          lg:translate-x-0 transition-transform duration-300 ease-in-out
          w-72 bg-white border-r border-gray-200 flex flex-col
        `}
      >
        {/* Header */}
        <div className="p-4 border-b border-gray-200">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-forest-500 rounded-lg flex items-center justify-center">
              <TreePine className="w-6 h-6 text-white" />
            </div>
            <div>
              <h1 className="font-bold text-gray-900">Forestry Agents</h1>
              <p className="text-xs text-gray-500">{user?.username}</p>
            </div>
          </div>
        </div>

        {/* Sidebar content */}
        <Sidebar
          chats={chats}
          onNewChat={handleNewChat}
          onSelectChat={handleSelectChat}
          onDeleteChat={handleDeleteChat}
          selectedAgents={selectedAgents}
          onToggleAgentsPanel={() => setShowAgentsPanel(!showAgentsPanel)}
        />

        {/* Logout button */}
        <div className="p-4 border-t border-gray-200">
          <button
            onClick={handleLogout}
            className="w-full flex items-center gap-2 px-4 py-2 text-gray-600 hover:text-gray-900 hover:bg-gray-100 rounded-lg transition-colors"
          >
            <LogOut className="w-5 h-5" />
            Sign Out
          </button>
        </div>
      </div>

      {/* Main content */}
      <div className="flex-1 flex flex-col min-w-0">
        <Routes>
          <Route
            path="/"
            element={
              <WelcomeView
                agents={agents}
                teams={teams}
                selectedAgents={selectedAgents}
                onSelectAgent={handleAgentToggle}
                onSelectTeam={handleSelectTeam}
                onNewChat={handleNewChat}
              />
            }
          />
          <Route
            path="/chat/:chatId"
            element={
              <ChatView
                agents={agents}
                getAgent={getAgent}
                selectedAgents={selectedAgents}
                onAgentToggle={handleAgentToggle}
                onRefreshChats={refreshChats}
              />
            }
          />
          <Route
            path="/plans"
            element={<PlansView agents={agents} getAgent={getAgent} />}
          />
        </Routes>
      </div>

      {/* Agents Panel (slide-out) */}
      {showAgentsPanel && (
        <div className="fixed inset-0 z-50 lg:relative lg:inset-auto">
          {/* Backdrop */}
          <div
            className="absolute inset-0 bg-black/50 lg:hidden"
            onClick={() => setShowAgentsPanel(false)}
          />

          {/* Panel */}
          <div className="absolute right-0 top-0 bottom-0 w-80 bg-white shadow-xl lg:shadow-none border-l border-gray-200 overflow-hidden">
            <AgentsPanel
              agents={agents}
              categories={categories}
              teams={teams}
              selectedAgents={selectedAgents}
              onAgentToggle={handleAgentToggle}
              onSelectTeam={handleSelectTeam}
              onClose={() => setShowAgentsPanel(false)}
            />
          </div>
        </div>
      )}
    </div>
  );
}
