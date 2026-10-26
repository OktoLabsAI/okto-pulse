import type { AuthAdapter } from './types';

const LocalProvider: AuthAdapter['Provider'] = ({ children }) => children;
const getLocalToken = async () => null as string | null;

function useLocalAuth() {
  return {
    isLoaded: true,
    isSignedIn: true,
    identityKey: 'local',
    getToken: getLocalToken,
  };
}

export const localAdapter: AuthAdapter = {
  Provider: LocalProvider,
  useAuth: useLocalAuth,
  UserButton: null,
};
