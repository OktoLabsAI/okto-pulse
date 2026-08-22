import { Bot, Info } from 'lucide-react';
import type {
  CodeTraceabilityReceiptCurrentness,
} from '@/types';

export function TraceabilityDisclosure({ compact = false }: { compact?: boolean }) {
  return (
    <div
      className={`flex items-start gap-2 rounded-lg border border-sky-200 bg-sky-50 text-sky-900 dark:border-sky-900/70 dark:bg-sky-950/30 dark:text-sky-200 ${compact ? 'px-3 py-2 text-[11px]' : 'px-4 py-3 text-xs'}`}
      role="note"
      data-testid="traceability-agent-mediated-disclosure"
    >
      {compact
        ? <Info size={14} className="mt-0.5 shrink-0" aria-hidden />
        : <Bot size={16} className="mt-0.5 shrink-0" aria-hidden />}
      <p className="leading-5">
        <strong>Agent-mediated.</strong> Pulse does not access source code. An
        authenticated external agent checks access and capabilities in its own
        environment, then submits an <strong>accessible</strong>,{' '}
        <strong>partial</strong>, or <strong>unavailable</strong> attestation.
      </p>
    </div>
  );
}

export function TraceabilityBadge({
  kind,
}: {
  kind: 'agent-attested' | 'receipt-accepted' | 'current' | 'historical';
}) {
  const styles = {
    'agent-attested': 'border-cyan-300 bg-cyan-50 text-cyan-700 dark:border-cyan-800 dark:bg-cyan-950/30 dark:text-cyan-300',
    'receipt-accepted': 'border-emerald-300 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-300',
    current: 'border-blue-300 bg-blue-50 text-blue-700 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-300',
    historical: 'border-gray-300 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-400',
  } as const;
  const labels = {
    'agent-attested': 'Agent-attested',
    'receipt-accepted': 'Receipt accepted',
    current: 'Current receipt',
    historical: 'Historical',
  } as const;
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold ${styles[kind]}`}>
      {labels[kind]}
    </span>
  );
}

export function TraceabilityCurrentnessBadge({
  currentness,
}: {
  currentness: CodeTraceabilityReceiptCurrentness;
}) {
  if (currentness === 'current') return <TraceabilityBadge kind="current" />;

  const labels: Record<Exclude<CodeTraceabilityReceiptCurrentness, 'current'>, string> = {
    outdated: 'Historical',
    expired: 'Expired',
    revoked: 'Revoked',
    conflicted: 'Conflicted',
    unknown: 'Currentness unknown',
  };
  const caution = currentness === 'expired' || currentness === 'conflicted';
  const revoked = currentness === 'revoked';
  return (
    <span className={`inline-flex rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
      revoked
        ? 'border-red-300 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300'
        : caution
          ? 'border-amber-300 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300'
          : 'border-gray-300 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-400'
    }`}>
      {labels[currentness]}
    </span>
  );
}

export function TraceabilityEmptyState({ noun }: { noun: string }) {
  return (
    <div className="rounded-lg border border-dashed border-gray-300 px-5 py-8 text-center dark:border-gray-700">
      <p className="text-sm font-medium text-gray-600 dark:text-gray-300">
        No {noun} submitted
      </p>
      <p className="mx-auto mt-1 max-w-lg text-xs leading-5 text-gray-400 dark:text-gray-500">
        Pulse waits for an authenticated external agent to check its own
        environment and submit a structured receipt. Nothing is scanned or
        resolved by Pulse Community.
      </p>
    </div>
  );
}
