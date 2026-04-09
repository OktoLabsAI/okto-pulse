/**
 * Safe auth hook — delegates to the active auth adapter.
 *
 * Kept as a thin re-export so existing consumers don't need to change their
 * import paths. New code should import from '@/adapters' directly.
 */

import { authAdapter } from '@/adapters';

export const useSafeAuth = authAdapter.useAuth;
