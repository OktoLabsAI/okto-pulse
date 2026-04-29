/**
 * Versioned localStorage flag that gates the OnboardingModal first-run
 * display. Bumping the version (`v1` → `v2`) re-shows the modal on the
 * next app boot to all returning users without a server-side migration.
 */

const STORAGE_KEY = 'okto.onboarding.completed.v1';
const COMPLETION_EVENT = 'okto:onboarding-completed';

export function isCompleted(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

export function markCompleted(): void {
  try {
    localStorage.setItem(STORAGE_KEY, 'true');
  } catch {
    // ignore — onboarding will simply re-prompt next session
  }
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(COMPLETION_EVENT));
  }
}

export function reset(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

export const onboardingStorage = {
  isCompleted,
  markCompleted,
  reset,
  STORAGE_KEY,
  COMPLETION_EVENT,
};
