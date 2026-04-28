/**
 * terms-acceptance-api — read/write the user's terms-of-use acceptance.
 *
 * Persistence layer is `localStorage` for the community edition (single
 * local user, no cross-device sync). The terms hash is bumped whenever
 * `TERMS_VERSION` or `TERMS_HASH` change; a hash mismatch re-prompts the
 * user. The CLI flag `--accept-terms` and env `OKTO_PULSE_TERMS_ACCEPTED=1`
 * pre-populate the same key on first load (handled in App.tsx bootstrap).
 */

import { TERMS_HASH, TERMS_VERSION } from '@/constants/terms';

const STORAGE_KEY = 'okto.terms.acceptance';

export interface TermsAcceptance {
  accepted_at: string;        // ISO 8601
  version: string;
  hash: string;               // checksum at acceptance time
  source: 'modal' | 'cli' | 'env';
}

export function loadAcceptance(): TermsAcceptance | null {
  if (typeof localStorage === 'undefined') return null;
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    return parsed as TermsAcceptance;
  } catch {
    return null;
  }
}

export function saveAcceptance(source: TermsAcceptance['source']): TermsAcceptance {
  const record: TermsAcceptance = {
    accepted_at: new Date().toISOString(),
    version: TERMS_VERSION,
    hash: TERMS_HASH,
    source,
  };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(record));
  } catch {
    // localStorage unavailable (private mode/quota) — accept is in-memory only.
  }
  return record;
}

export function clearAcceptance(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

/**
 * Check if the cached acceptance is still valid for the current terms hash.
 * Returns the record on match, or null if missing/stale (re-prompt required).
 */
export function getValidAcceptance(): TermsAcceptance | null {
  const a = loadAcceptance();
  if (!a) return null;
  if (a.hash !== TERMS_HASH) return null;
  return a;
}
