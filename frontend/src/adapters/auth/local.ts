import type { AuthAdapter } from './types';

const LocalProvider: AuthAdapter['Provider'] = ({ children }) => children;

function useLocalAuth() {
  return {
    isLoaded: true,
    isSignedIn: true,
    getToken: async () => null as string | null,
  };
}

export const localAdapter: AuthAdapter = {
  Provider: LocalProvider,
  useAuth: useLocalAuth,
  UserButton: null,
};
