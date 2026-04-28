/**
 * Forensic banner shown app-wide when the active board has
 * `boardSettings.skip_test_evidence_global = true` (NC-9 spec 873e98cc /
 * frontend spec 5cb09dbc).
 *
 * Non-dismissable by design — operators must turn the flag off in Board
 * Settings to remove it. Decision dec_a1d4d22f explains why a session
 * dismiss would defeat the gate's defensive purpose.
 */

import { AlertTriangle } from 'lucide-react';

interface EvidenceGateSkipBannerProps {
  skipActive: boolean;
  onOpenBoardSettings: () => void;
}

export function EvidenceGateSkipBanner({
  skipActive,
  onOpenBoardSettings,
}: EvidenceGateSkipBannerProps) {
  if (!skipActive) {
    return null;
  }

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="evidence-gate-skip-banner"
      className="bg-amber-50 dark:bg-amber-900/30 border-b-2 border-amber-400 dark:border-amber-700 px-4 py-3"
    >
      <div className="max-w-7xl mx-auto flex items-center gap-3">
        <div className="flex-shrink-0">
          <AlertTriangle
            size={20}
            className="text-amber-600 dark:text-amber-400"
            aria-hidden="true"
          />
        </div>
        <div className="flex-1 text-sm text-amber-900 dark:text-amber-100">
          <span className="font-semibold">Evidence gate bypassed.</span>{' '}
          <span>
            Test scenarios can be marked passed/automated/failed without proof
            of real execution. Disable in{' '}
          </span>
          <button
            type="button"
            onClick={onOpenBoardSettings}
            className="font-semibold underline hover:text-amber-700 dark:hover:text-amber-300 focus:outline-none focus:ring-2 focus:ring-amber-500 rounded-sm"
            data-testid="evidence-gate-skip-banner-link"
          >
            Board Settings &rarr;
          </button>
        </div>
        <span className="flex-shrink-0 text-xs text-amber-700 dark:text-amber-300 bg-amber-100 dark:bg-amber-900/50 px-2 py-0.5 rounded font-medium">
          SKIP ACTIVE
        </span>
      </div>
    </div>
  );
}
