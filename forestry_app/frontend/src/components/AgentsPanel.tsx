import { Agent, Team } from '../lib/api';
import {
  X,
  Check,
  Users,
  Calendar,
  Database,
  Sliders,
  Layers,
  CheckCircle,
  Bug,
  Truck,
  MessageSquare,
  TrendingUp,
  Mail,
  BookOpen,
} from 'lucide-react';
import clsx from 'clsx';

const iconMap: Record<string, React.ComponentType<{ className?: string }>> = {
  Calendar,
  Database,
  Sliders,
  Layers,
  CheckCircle,
  Bug,
  Truck,
  MessageSquare,
  TrendingUp,
  Mail,
  BookOpen,
};

interface AgentsPanelProps {
  agents: Agent[];
  categories: string[];
  teams: Record<string, Team>;
  selectedAgents: string[];
  onAgentToggle: (agentId: string) => void;
  onSelectTeam: (agentIds: string[]) => void;
  onClose: () => void;
}

export default function AgentsPanel({
  agents,
  categories,
  teams,
  selectedAgents,
  onAgentToggle,
  onSelectTeam,
  onClose,
}: AgentsPanelProps) {
  const agentsByCategory = categories.reduce(
    (acc, category) => {
      acc[category] = agents.filter((a) => a.category === category);
      return acc;
    },
    {} as Record<string, Agent[]>
  );

  const getIcon = (iconName: string) => {
    return iconMap[iconName] || Database;
  };

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="p-4 border-b border-gray-200 flex items-center justify-between">
        <div>
          <h2 className="font-semibold text-gray-900">Select Agents</h2>
          <p className="text-sm text-gray-500">
            {selectedAgents.length} selected
          </p>
        </div>
        <button
          onClick={onClose}
          className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
        >
          <X className="w-5 h-5" />
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {/* Quick Teams */}
        <div className="p-4 border-b border-gray-200">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">
            Quick Teams
          </h3>
          <div className="grid grid-cols-2 gap-2">
            {Object.entries(teams).map(([key, team]) => (
              <button
                key={key}
                onClick={() => onSelectTeam(team.agents)}
                className={clsx(
                  'p-3 rounded-lg text-left transition-colors border',
                  JSON.stringify(selectedAgents.sort()) ===
                    JSON.stringify(team.agents.sort())
                    ? 'bg-forest-50 border-forest-200 text-forest-700'
                    : 'bg-gray-50 border-gray-200 hover:bg-gray-100'
                )}
              >
                <div className="flex items-center gap-2 mb-1">
                  <Users className="w-4 h-4" />
                  <span className="text-sm font-medium truncate">
                    {team.name}
                  </span>
                </div>
                <p className="text-xs text-gray-500 line-clamp-2">
                  {team.description}
                </p>
              </button>
            ))}
          </div>
        </div>

        {/* Agents by Category */}
        {categories.map((category) => (
          <div key={category} className="p-4 border-b border-gray-200">
            <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">
              {category}
            </h3>
            <div className="space-y-2">
              {agentsByCategory[category]?.map((agent) => {
                const Icon = getIcon(agent.icon);
                const isSelected = selectedAgents.includes(agent.id);

                return (
                  <button
                    key={agent.id}
                    onClick={() => onAgentToggle(agent.id)}
                    className={clsx(
                      'w-full flex items-start gap-3 p-3 rounded-lg transition-all text-left',
                      isSelected
                        ? 'bg-forest-50 ring-2 ring-forest-500'
                        : 'bg-gray-50 hover:bg-gray-100'
                    )}
                  >
                    <div
                      className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0"
                      style={{ backgroundColor: agent.color }}
                    >
                      <Icon className="w-5 h-5 text-white" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-gray-900">
                          {agent.name}
                        </span>
                        {isSelected && (
                          <Check className="w-4 h-4 text-forest-500" />
                        )}
                      </div>
                      <p className="text-xs text-gray-500 line-clamp-2 mt-0.5">
                        {agent.description}
                      </p>
                      <div className="flex flex-wrap gap-1 mt-2">
                        {agent.produces.slice(0, 2).map((item, i) => (
                          <span
                            key={i}
                            className="inline-flex items-center px-2 py-0.5 rounded text-xs bg-gray-200 text-gray-600"
                          >
                            {item}
                          </span>
                        ))}
                        {agent.produces.length > 2 && (
                          <span className="text-xs text-gray-400">
                            +{agent.produces.length - 2} more
                          </span>
                        )}
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {/* Footer */}
      <div className="p-4 border-t border-gray-200 bg-gray-50">
        <div className="flex gap-2">
          <button
            onClick={() => onSelectTeam([])}
            className="flex-1 px-4 py-2 text-gray-600 hover:bg-gray-200 rounded-lg transition-colors text-sm"
          >
            Clear All
          </button>
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 bg-forest-500 hover:bg-forest-600 text-white rounded-lg transition-colors text-sm font-medium"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
