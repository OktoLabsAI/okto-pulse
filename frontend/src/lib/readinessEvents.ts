/** Reuse the existing cross-scope invalidation signal; never persist a score. */
export const READINESS_CHANGED = 'pulse:delivery-evidence-changed';

export function affectsDeliveryReadiness(url: string, method = 'GET'): boolean {
  if (!['POST', 'PUT', 'PATCH', 'DELETE'].includes(method.toUpperCase())) return false;
  const pathname = new URL(url, 'http://pulse.local').pathname.replace(/^\/api\/v1(?=\/)/, '');
  // Native dry-run/query surfaces do not mutate the authoritative facts.
  if (/(?:^|\/)(?:preview|search|query|dry-run)(?:\/|$)/.test(pathname)) return false;
  return /^\/(?:cards|specs)\/[^/]+(?:\/|$)/.test(pathname)
    || /^\/boards\/[^/]+$/.test(pathname)
    || /^\/boards\/[^/]+\/(?:specs|cards|bugs|archive|restore|shares|guidelines|guideline-policy|policy|policy-waivers|permissions|code-[^/]+)(?:\/|$)/.test(pathname)
    || /^\/resource-gate\/(?:card|spec)\/[^/]+(?:\/|$)/.test(pathname)
    || /^\/agents\/[^/]+\/boards\/[^/]+(?:\/|$)/.test(pathname)
    || /^\/(?:presets|permission-presets|guidelines)(?:\/|$)/.test(pathname);
}

let pending = false;
export function notifyReadinessChanged() {
  if (pending || typeof window === 'undefined') return;
  pending = true;
  queueMicrotask(() => {
    pending = false;
    window.dispatchEvent(new Event(READINESS_CHANGED));
  });
}

export function notifyReadinessMutation(url: string, method: string | undefined, succeeded: boolean) {
  if (succeeded && affectsDeliveryReadiness(url, method)) notifyReadinessChanged();
}
