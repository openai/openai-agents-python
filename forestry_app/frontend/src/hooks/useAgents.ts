import { useState, useEffect, useCallback } from 'react';
import { agents, Agent, Team } from '../lib/api';

export function useAgents() {
  const [agentList, setAgentList] = useState<Agent[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [teams, setTeams] = useState<Record<string, Team>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchAgents = useCallback(async () => {
    setLoading(true);
    const [agentsResponse, teamsResponse] = await Promise.all([
      agents.list(),
      agents.getTeams(),
    ]);

    if (agentsResponse.data) {
      setAgentList(agentsResponse.data.agents);
      setCategories(agentsResponse.data.categories);
    } else if (agentsResponse.error) {
      setError(agentsResponse.error);
    }

    if (teamsResponse.data) {
      setTeams(teamsResponse.data.teams);
    }

    setLoading(false);
  }, []);

  useEffect(() => {
    fetchAgents();
  }, [fetchAgents]);

  const getAgent = (id: string): Agent | undefined => {
    return agentList.find((a) => a.id === id);
  };

  const getAgentsByCategory = (category: string): Agent[] => {
    return agentList.filter((a) => a.category === category);
  };

  const routeMessage = async (message: string) => {
    const response = await agents.route(message);
    return response.data;
  };

  return {
    agents: agentList,
    categories,
    teams,
    loading,
    error,
    getAgent,
    getAgentsByCategory,
    routeMessage,
    refresh: fetchAgents,
  };
}
