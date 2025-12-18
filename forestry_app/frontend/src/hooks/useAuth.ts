import { useState, useEffect, useCallback } from 'react';
import { auth } from '../lib/api';

interface User {
  id: number;
  username: string;
}

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const checkAuth = useCallback(async () => {
    if (!auth.isAuthenticated()) {
      setLoading(false);
      return;
    }

    const response = await auth.getUser();
    if (response.data) {
      setUser(response.data);
    } else {
      auth.logout();
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  const login = async (username: string, password: string) => {
    setError(null);
    try {
      await auth.login(username, password);
      await checkAuth();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed');
      return false;
    }
  };

  const logout = () => {
    auth.logout();
    setUser(null);
  };

  return {
    user,
    loading,
    error,
    login,
    logout,
    isAuthenticated: !!user,
  };
}
