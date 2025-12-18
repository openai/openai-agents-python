import { useState, useEffect } from 'react';
import { plans, PlanListItem, Plan, Agent } from '../lib/api';
import {
  Plus,
  FileText,
  Play,
  Trash2,
  ChevronRight,
  Clock,
  CheckCircle,
  AlertCircle,
  Loader2,
} from 'lucide-react';
import clsx from 'clsx';

interface PlansViewProps {
  agents: Agent[];
  getAgent: (id: string) => Agent | undefined;
}

export default function PlansView({ agents, getAgent }: PlansViewProps) {
  const [plansList, setPlansList] = useState<PlanListItem[]>([]);
  const [selectedPlan, setSelectedPlan] = useState<Plan | null>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [executing, setExecuting] = useState(false);

  const [newPlanTitle, setNewPlanTitle] = useState('');
  const [newPlanDescription, setNewPlanDescription] = useState('');
  const [showNewPlanForm, setShowNewPlanForm] = useState(false);

  useEffect(() => {
    fetchPlans();
  }, []);

  const fetchPlans = async () => {
    setLoading(true);
    const response = await plans.list();
    if (response.data) {
      setPlansList(response.data);
    }
    setLoading(false);
  };

  const handleCreatePlan = async () => {
    if (!newPlanTitle.trim()) return;

    setCreating(true);
    const response = await plans.create(
      newPlanTitle,
      newPlanDescription || undefined,
      []
    );

    if (response.data) {
      await fetchPlans();
      setNewPlanTitle('');
      setNewPlanDescription('');
      setShowNewPlanForm(false);
      setSelectedPlan(response.data);
    }
    setCreating(false);
  };

  const handleSelectPlan = async (planId: number) => {
    const response = await plans.get(planId);
    if (response.data) {
      setSelectedPlan(response.data);
    }
  };

  const handleDeletePlan = async (planId: number) => {
    await plans.delete(planId);
    await fetchPlans();
    if (selectedPlan?.id === planId) {
      setSelectedPlan(null);
    }
  };

  const handleExecutePlan = async (planId: number) => {
    setExecuting(true);
    await plans.execute(planId);
    await handleSelectPlan(planId);
    await fetchPlans();
    setExecuting(false);
  };

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'completed':
        return <CheckCircle className="w-4 h-4 text-green-500" />;
      case 'active':
        return <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />;
      case 'draft':
        return <Clock className="w-4 h-4 text-gray-400" />;
      default:
        return <AlertCircle className="w-4 h-4 text-yellow-500" />;
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case 'completed':
        return 'bg-green-100 text-green-700';
      case 'active':
        return 'bg-blue-100 text-blue-700';
      case 'draft':
        return 'bg-gray-100 text-gray-700';
      default:
        return 'bg-yellow-100 text-yellow-700';
    }
  };

  return (
    <div className="flex-1 flex">
      {/* Plans List */}
      <div className="w-80 border-r border-gray-200 bg-white flex flex-col">
        <div className="p-4 border-b border-gray-200">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-gray-900">Plans</h2>
            <button
              onClick={() => setShowNewPlanForm(true)}
              className="p-2 hover:bg-gray-100 rounded-lg transition-colors text-forest-600"
            >
              <Plus className="w-5 h-5" />
            </button>
          </div>

          {showNewPlanForm && (
            <div className="space-y-3 p-3 bg-gray-50 rounded-lg">
              <input
                type="text"
                value={newPlanTitle}
                onChange={(e) => setNewPlanTitle(e.target.value)}
                placeholder="Plan title"
                className="w-full px-3 py-2 border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-forest-500"
              />
              <textarea
                value={newPlanDescription}
                onChange={(e) => setNewPlanDescription(e.target.value)}
                placeholder="Description (optional)"
                rows={2}
                className="w-full px-3 py-2 border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-forest-500 resize-none"
              />
              <div className="flex gap-2">
                <button
                  onClick={() => setShowNewPlanForm(false)}
                  className="flex-1 px-3 py-2 text-gray-600 hover:bg-gray-200 rounded-lg transition-colors text-sm"
                >
                  Cancel
                </button>
                <button
                  onClick={handleCreatePlan}
                  disabled={!newPlanTitle.trim() || creating}
                  className="flex-1 px-3 py-2 bg-forest-500 hover:bg-forest-600 disabled:bg-gray-300 text-white rounded-lg transition-colors text-sm font-medium"
                >
                  {creating ? 'Creating...' : 'Create'}
                </button>
              </div>
            </div>
          )}
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="w-6 h-6 text-forest-500 animate-spin" />
            </div>
          ) : plansList.length === 0 ? (
            <div className="text-center py-8">
              <FileText className="w-8 h-8 mx-auto text-gray-300 mb-2" />
              <p className="text-sm text-gray-400">No plans yet</p>
            </div>
          ) : (
            <div className="space-y-2">
              {plansList.map((plan) => (
                <button
                  key={plan.id}
                  onClick={() => handleSelectPlan(plan.id)}
                  className={clsx(
                    'w-full p-3 rounded-lg text-left transition-colors group',
                    selectedPlan?.id === plan.id
                      ? 'bg-forest-50 ring-2 ring-forest-500'
                      : 'hover:bg-gray-50'
                  )}
                >
                  <div className="flex items-start gap-3">
                    <div className="mt-0.5">{getStatusIcon(plan.status)}</div>
                    <div className="flex-1 min-w-0">
                      <p className="font-medium text-gray-900 truncate">
                        {plan.title}
                      </p>
                      {plan.description && (
                        <p className="text-xs text-gray-500 truncate mt-0.5">
                          {plan.description}
                        </p>
                      )}
                      <div className="flex items-center gap-2 mt-2">
                        <span
                          className={clsx(
                            'text-xs px-2 py-0.5 rounded',
                            getStatusColor(plan.status)
                          )}
                        >
                          {plan.status}
                        </span>
                        <span className="text-xs text-gray-400">
                          {new Date(plan.updated_at).toLocaleDateString()}
                        </span>
                      </div>
                    </div>
                    <ChevronRight className="w-4 h-4 text-gray-300 group-hover:text-gray-500 mt-1" />
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Plan Detail */}
      <div className="flex-1 flex flex-col">
        {selectedPlan ? (
          <>
            {/* Header */}
            <div className="p-6 border-b border-gray-200 bg-white">
              <div className="flex items-start justify-between">
                <div>
                  <h2 className="text-xl font-semibold text-gray-900">
                    {selectedPlan.title}
                  </h2>
                  {selectedPlan.description && (
                    <p className="text-gray-500 mt-1">
                      {selectedPlan.description}
                    </p>
                  )}
                  <div className="flex items-center gap-3 mt-3">
                    <span
                      className={clsx(
                        'text-sm px-3 py-1 rounded-full',
                        getStatusColor(selectedPlan.status)
                      )}
                    >
                      {selectedPlan.status}
                    </span>
                    <span className="text-sm text-gray-400">
                      Created{' '}
                      {new Date(selectedPlan.created_at).toLocaleDateString()}
                    </span>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {selectedPlan.status === 'draft' && (
                    <button
                      onClick={() => handleExecutePlan(selectedPlan.id)}
                      disabled={executing}
                      className="flex items-center gap-2 px-4 py-2 bg-forest-500 hover:bg-forest-600 disabled:bg-gray-300 text-white rounded-lg transition-colors"
                    >
                      {executing ? (
                        <Loader2 className="w-4 h-4 animate-spin" />
                      ) : (
                        <Play className="w-4 h-4" />
                      )}
                      Execute
                    </button>
                  )}
                  <button
                    onClick={() => handleDeletePlan(selectedPlan.id)}
                    className="p-2 hover:bg-red-50 text-gray-400 hover:text-red-500 rounded-lg transition-colors"
                  >
                    <Trash2 className="w-5 h-5" />
                  </button>
                </div>
              </div>
            </div>

            {/* Content */}
            <div className="flex-1 overflow-y-auto p-6">
              {/* Agents involved */}
              {selectedPlan.agent_ids.length > 0 && (
                <div className="mb-6">
                  <h3 className="text-sm font-semibold text-gray-700 mb-3">
                    Agents Involved
                  </h3>
                  <div className="flex flex-wrap gap-2">
                    {selectedPlan.agent_ids.map((agentId) => {
                      const agent = getAgent(agentId);
                      return agent ? (
                        <span
                          key={agentId}
                          className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg"
                          style={{
                            backgroundColor: `${agent.color}15`,
                            color: agent.color,
                          }}
                        >
                          <span
                            className="w-6 h-6 rounded flex items-center justify-center text-white text-xs font-bold"
                            style={{ backgroundColor: agent.color }}
                          >
                            {agent.name.charAt(0)}
                          </span>
                          {agent.name}
                        </span>
                      ) : null;
                    })}
                  </div>
                </div>
              )}

              {/* Plan content */}
              <div className="bg-gray-50 rounded-xl p-6">
                <h3 className="text-sm font-semibold text-gray-700 mb-3">
                  Plan Details
                </h3>
                {Object.keys(selectedPlan.content).length > 0 ? (
                  <pre className="text-sm text-gray-600 whitespace-pre-wrap">
                    {JSON.stringify(selectedPlan.content, null, 2)}
                  </pre>
                ) : (
                  <p className="text-gray-400 text-sm">
                    No plan content defined yet. Add tasks and configure the
                    plan to get started.
                  </p>
                )}
              </div>
            </div>
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center">
            <div className="text-center">
              <FileText className="w-12 h-12 mx-auto text-gray-300 mb-4" />
              <h3 className="text-lg font-medium text-gray-600 mb-2">
                Select a plan
              </h3>
              <p className="text-gray-400">
                Choose a plan from the list or create a new one
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
