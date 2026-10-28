import type { ReactNode, FC } from 'react';

export interface AuthAdapter {
  Provider: FC<{ children: ReactNode }>;
  useAuth: () => {
    isLoaded: boolean;
    isSignedIn: boolean;
    /** Stable realm/actor key; changing it creates a new API client and drops scoped caches. */
    identityKey: string;
    getToken: (options?: { skipCache?: boolean }) => Promise<string | null>;
  };
  UserButton: FC<{ afterSignOutUrl?: string }> | null;
}
