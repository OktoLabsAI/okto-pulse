/**
 * API Context - provides authenticated API client to components
 */

import React, { createContext, useContext, useMemo, useCallback, useEffect } from 'react';
import { AuthenticatedFetch, initAuthFetch } from '@/lib/authFetch';
import { authAdapter } from '@/adapters';

const API_BASE_URL = import.meta.env.VITE_API_URL || '/api/v1';

interface ApiContextValue {
  apiClient: AuthenticatedFetch;
  getFreshToken: () => Promise<string | null>;
  getToken: () => Promise<string | null>;
  isReady: boolean;
}

const ApiContext = createContext<ApiContextValue | null>(null);

interface ApiProviderProps {
  children: React.ReactNode;
  onAuthFailure?: () => void;
}

export function ApiProvider({ children }: ApiProviderProps) {
  const { getToken: adapterGetToken, isLoaded, isSignedIn } = authAdapter.useAuth();

  const tokenGetter = useCallback(
    async (options?: { skipCache?: boolean }): Promise<string | null> => {
      if (!isLoaded || !isSignedIn) {
        return null;
      }

      try {
        const token = await adapterGetToken({
          skipCache: options?.skipCache ?? false,
        });
        return token;
      } catch (error) {
        console.error('[ApiProvider] Error getting token:', error);
        return null;
      }
    },
    [adapterGetToken, isLoaded, isSignedIn]
  );

  const apiClient = useMemo(() => {
    return new AuthenticatedFetch(tokenGetter, API_BASE_URL);
  }, [tokenGetter]);

  useEffect(() => {
    initAuthFetch(tokenGetter, API_BASE_URL);
  }, [tokenGetter]);

  const getFreshToken = useCallback(async (): Promise<string | null> => {
    return tokenGetter({ skipCache: true });
  }, [tokenGetter]);

  const getToken = useCallback(async (): Promise<string | null> => {
    return tokenGetter();
  }, [tokenGetter]);

  const value = useMemo(
    () => ({
      apiClient,
      getFreshToken,
      getToken,
      isReady: isLoaded,
    }),
    [apiClient, getFreshToken, getToken, isLoaded]
  );

  return <ApiContext.Provider value={value}>{children}</ApiContext.Provider>;
}

export function useApiContext(): ApiContextValue {
  const context = useContext(ApiContext);
  if (!context) {
    throw new Error('useApiContext must be used within an ApiProvider');
  }
  return context;
}

export function useApiClient(): AuthenticatedFetch {
  const { apiClient } = useApiContext();
  return apiClient;
}
