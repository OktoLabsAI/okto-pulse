import type { ReactNode, FC } from 'react';

export interface AuthAdapter {
  Provider: FC<{ children: ReactNode }>;
  useAuth: () => {
    isLoaded: boolean;
    isSignedIn: boolean;
    getToken: (options?: { skipCache?: boolean }) => Promise<string | null>;
  };
  UserButton: FC<{ afterSignOutUrl?: string }> | null;
}
