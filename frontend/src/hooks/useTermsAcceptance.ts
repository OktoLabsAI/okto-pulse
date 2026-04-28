/**
 * useTermsAcceptance — gate the app behind explicit acceptance of the
 * current terms text. Re-prompts on hash change.
 *
 * Reads from `localStorage` via `terms-acceptance-api`. Also honors a
 * one-shot CLI/env pre-acceptance: if the URL contains `?accept_terms=1`
 * (set by the CLI flag `--accept-terms` redirector) or the global
 * `window.OKTO_PULSE_TERMS_ACCEPTED` is `'1'` (set by env var injected
 * at build time), accept on first load and clear the URL flag.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  getValidAcceptance,
  saveAcceptance,
  type TermsAcceptance,
} from '@/services/terms-acceptance-api';

declare global {
  interface Window {
    OKTO_PULSE_TERMS_ACCEPTED?: string;
  }
}

export interface UseTermsAcceptanceResult {
  acceptance: TermsAcceptance | null;
  needsAcceptance: boolean;
  accept: () => TermsAcceptance;
  loading: boolean;
}

export function useTermsAcceptance(): UseTermsAcceptanceResult {
  const [acceptance, setAcceptance] = useState<TermsAcceptance | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    const bootstrap = async () => {
      // 1) URL flag (CLI redirector emit) — one-shot.
      if (typeof window !== 'undefined') {
        const params = new URLSearchParams(window.location.search);
        if (params.get('accept_terms') === '1') {
          const rec = saveAcceptance('cli');
          if (!cancelled) setAcceptance(rec);
          params.delete('accept_terms');
          const newUrl = window.location.pathname + (params.toString() ? `?${params.toString()}` : '');
          window.history.replaceState(window.history.state, '', newUrl);
          if (!cancelled) setLoading(false);
          return;
        }
        if (window.OKTO_PULSE_TERMS_ACCEPTED === '1') {
          const rec = saveAcceptance('env');
          if (!cancelled) setAcceptance(rec);
          if (!cancelled) setLoading(false);
          return;
        }
      }

      // 2) Local cache.
      const cached = getValidAcceptance();
      if (cached) {
        if (!cancelled) {
          setAcceptance(cached);
          setLoading(false);
        }
        return;
      }

      // 3) Backend system-flags (CLI --accept-terms or env in the server process).
      try {
        const resp = await fetch('/api/v1/me/system-flags', { credentials: 'omit' });
        if (resp.ok) {
          const data = await resp.json();
          const ta = data?.terms_acceptance;
          if (ta?.pre_accepted) {
            const rec = saveAcceptance(ta.source === 'env' ? 'env' : 'cli');
            if (!cancelled) setAcceptance(rec);
            if (!cancelled) setLoading(false);
            return;
          }
        }
      } catch {
        // backend unavailable — fall through to needsAcceptance=true
      }

      if (!cancelled) {
        setAcceptance(null);
        setLoading(false);
      }
    };

    bootstrap();
    return () => {
      cancelled = true;
    };
  }, []);

  const accept = useCallback(() => {
    const rec = saveAcceptance('modal');
    setAcceptance(rec);
    return rec;
  }, []);

  return {
    acceptance,
    needsAcceptance: !loading && acceptance === null,
    accept,
    loading,
  };
}
