import { Agent, Team } from '../lib/api';
import {
  TreePine,
  MessageSquare,
  Users,
  Zap,
  Check,
} from 'lucide-react';
import clsx from 'clsx';

interface WelcomeViewProps {
  agents: Agent[];
  teams: Record<string, Team>;
  selectedAgents: string[];
  onSelectAgent: (agentId: string) => void;
  onSelectTeam: (agentIds: string[]) => void;
  onNewChat: () => void;
}

export default function WelcomeView({
  agents,
  teams,
  selectedAgents,
  onSelectAgent,
  onSelectTeam,
  onNewChat,
}: WelcomeViewProps) {
  return (
    <div className="flex-1 overflow-y-auto">
      {/* Hero Section */}
      <div className="bg-gradient-to-br from-forest-600 via-forest-700 to-forest-800 text-white">
        <div className="max-w-4xl mx-auto px-6 py-16 text-center">
          <div className="inline-flex items-center justify-center w-20 h-20 bg-white/20 rounded-2xl mb-6">
            <TreePine className="w-12 h-12" />
          </div>
          <h1 className="text-4xl font-bold mb-4">
            Forestry MultiAgent System
          </h1>
          <p className="text-xl text-forest-100 mb-8 max-w-2xl mx-auto">
            AI-powered forestry operations management with 11 specialized agents
            working together to optimize your workflows.
          </p>
          <button
            onClick={onNewChat}
            className="inline-flex items-center gap-2 px-6 py-3 bg-white text-forest-700 font-semibold rounded-xl hover:bg-forest-50 transition-colors shadow-lg"
          >
            <MessageSquare className="w-5 h-5" />
            Start New Chat
          </button>
        </div>
      </div>

      {/* Quick Start Teams */}
      <div className="max-w-6xl mx-auto px-6 py-12">
        <div className="text-center mb-8">
          <h2 className="text-2xl font-bold text-gray-900 mb-2">
            Quick Start Teams
          </h2>
          <p className="text-gray-600">
            Select a pre-configured team or build your own
          </p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-4 mb-12">
          {Object.entries(teams)
            .slice(0, 6)
            .map(([key, team]) => {
              const isSelected =
                JSON.stringify(selectedAgents.sort()) ===
                JSON.stringify(team.agents.sort());

              return (
                <button
                  key={key}
                  onClick={() => onSelectTeam(team.agents)}
                  className={clsx(
                    'p-5 rounded-xl text-left transition-all border-2',
                    isSelected
                      ? 'bg-forest-50 border-forest-500 ring-2 ring-forest-500/20'
                      : 'bg-white border-gray-200 hover:border-forest-300 hover:shadow-md'
                  )}
                >
                  <div className="flex items-center gap-3 mb-3">
                    <div
                      className={clsx(
                        'w-10 h-10 rounded-lg flex items-center justify-center',
                        isSelected ? 'bg-forest-500' : 'bg-gray-100'
                      )}
                    >
                      <Users
                        className={clsx(
                          'w-5 h-5',
                          isSelected ? 'text-white' : 'text-gray-600'
                        )}
                      />
                    </div>
                    <div className="flex-1">
                      <h3 className="font-semibold text-gray-900">
                        {team.name}
                      </h3>
                      <p className="text-sm text-gray-500">
                        {team.agents.length} agents
                      </p>
                    </div>
                    {isSelected && (
                      <Check className="w-5 h-5 text-forest-500" />
                    )}
                  </div>
                  <p className="text-sm text-gray-600 mb-3">
                    {team.description}
                  </p>
                  <div className="flex flex-wrap gap-1">
                    {team.agents.slice(0, 3).map((agentId) => {
                      const agent = agents.find((a) => a.id === agentId);
                      return agent ? (
                        <span
                          key={agentId}
                          className="inline-flex items-center px-2 py-0.5 rounded text-xs"
                          style={{
                            backgroundColor: `${agent.color}20`,
                            color: agent.color,
                          }}
                        >
                          {agent.name}
                        </span>
                      ) : null;
                    })}
                    {team.agents.length > 3 && (
                      <span className="text-xs text-gray-400">
                        +{team.agents.length - 3} more
                      </span>
                    )}
                  </div>
                </button>
              );
            })}
        </div>

        {/* All Agents */}
        <div className="mb-8">
          <div className="text-center mb-8">
            <h2 className="text-2xl font-bold text-gray-900 mb-2">
              All Agents (A-K)
            </h2>
            <p className="text-gray-600">
              Click to select individual agents for your chat
            </p>
          </div>

          <div className="grid sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
            {agents.map((agent) => {
              const isSelected = selectedAgents.includes(agent.id);

              return (
                <button
                  key={agent.id}
                  onClick={() => onSelectAgent(agent.id)}
                  className={clsx(
                    'p-4 rounded-xl text-left transition-all border-2',
                    isSelected
                      ? 'bg-forest-50 border-forest-500'
                      : 'bg-white border-gray-200 hover:border-gray-300 hover:shadow-sm'
                  )}
                >
                  <div className="flex items-start gap-3">
                    <div
                      className="w-12 h-12 rounded-lg flex items-center justify-center text-white font-bold text-lg flex-shrink-0"
                      style={{ backgroundColor: agent.color }}
                    >
                      {agent.name.charAt(0)}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <h3 className="font-semibold text-gray-900 truncate">
                          {agent.name}
                        </h3>
                        {isSelected && (
                          <Check className="w-4 h-4 text-forest-500 flex-shrink-0" />
                        )}
                      </div>
                      <p className="text-xs text-gray-500 mb-2">
                        {agent.category}
                      </p>
                      <p className="text-sm text-gray-600 line-clamp-2">
                        {agent.description}
                      </p>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Selected Agents Action */}
        {selectedAgents.length > 0 && (
          <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50">
            <div className="bg-forest-900 text-white px-6 py-4 rounded-2xl shadow-2xl flex items-center gap-4">
              <div>
                <p className="font-semibold">
                  {selectedAgents.length} agent(s) selected
                </p>
                <p className="text-sm text-forest-200">
                  Ready to start chatting
                </p>
              </div>
              <button
                onClick={onNewChat}
                className="flex items-center gap-2 px-5 py-2 bg-white text-forest-900 font-semibold rounded-lg hover:bg-forest-50 transition-colors"
              >
                <Zap className="w-4 h-4" />
                Start Chat
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Features */}
      <div className="bg-gray-50 py-12">
        <div className="max-w-6xl mx-auto px-6">
          <div className="grid md:grid-cols-3 gap-8">
            <div className="text-center">
              <div className="w-12 h-12 bg-forest-100 rounded-xl flex items-center justify-center mx-auto mb-4">
                <Users className="w-6 h-6 text-forest-600" />
              </div>
              <h3 className="font-semibold text-gray-900 mb-2">
                11 Specialized Agents
              </h3>
              <p className="text-sm text-gray-600">
                From Run Manager to Librarian, each agent handles specific
                aspects of forestry operations
              </p>
            </div>
            <div className="text-center">
              <div className="w-12 h-12 bg-forest-100 rounded-xl flex items-center justify-center mx-auto mb-4">
                <Zap className="w-6 h-6 text-forest-600" />
              </div>
              <h3 className="font-semibold text-gray-900 mb-2">
                Intelligent Routing
              </h3>
              <p className="text-sm text-gray-600">
                Messages are automatically routed to the most relevant agents
                when none are selected
              </p>
            </div>
            <div className="text-center">
              <div className="w-12 h-12 bg-forest-100 rounded-xl flex items-center justify-center mx-auto mb-4">
                <MessageSquare className="w-6 h-6 text-forest-600" />
              </div>
              <h3 className="font-semibold text-gray-900 mb-2">
                Persistent Chats
              </h3>
              <p className="text-sm text-gray-600">
                All conversations are saved to PostgreSQL for reference and
                continuity
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
