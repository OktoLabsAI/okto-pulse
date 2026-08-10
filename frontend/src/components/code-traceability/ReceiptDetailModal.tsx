import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, Clipboard, Clock3, ShieldX, X } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';
import { useDashboardApi } from '@/services/api';
import type { CodeInvestigationReceiptReadResult } from '@/types';
import { TraceabilityBadge, TraceabilityDisclosure } from './TraceabilityDisclosure';
import { useCodeTraceabilityAuthority } from './useCodeTraceabilityAuthority';

interface Props {
  boardId: string;
  receiptId: string;
  onClose: () => void;
  onRevoked?: () => void;
}

function formatDate(value: string | null | undefined) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function KeyValue({ label, value, mono = false }: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0 rounded-md border border-gray-200 bg-gray-50/70 px-3 py-2 dark:border-gray-700 dark:bg-gray-900/40">
      <dt className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">
        {label}
      </dt>
      <dd className={`mt-1 break-words text-xs text-gray-700 dark:text-gray-200 ${mono ? 'font-mono' : ''}`}>
        {value || '—'}
      </dd>
    </div>
  );
}

export function ReceiptDetailModal({ boardId, receiptId, onClose, onRevoked }: Props) {
  const api = useDashboardApi();
  const { canRevokeReceipt: canRevoke } =
    useCodeTraceabilityAuthority(boardId);
  const [result, setResult] = useState<CodeInvestigationReceiptReadResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showRevoke, setShowRevoke] = useState(false);
  const [reasonCode, setReasonCode] = useState('operator_revocation');
  const [justification, setJustification] = useState('');
  const [revoking, setRevoking] = useState(false);
  const focusTrap = useDialogFocusTrap(true, '[data-receipt-initial-focus]');

  useEscapeToClose(onClose, {
    enabled: true,
    canClose: !revoking,
    priority: 50,
  });

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      setResult(await api.getCodeInvestigationReceipt(boardId, receiptId, signal));
    } catch (caught) {
      if (signal?.aborted) return;
      setError(caught instanceof Error ? caught.message : 'Could not load receipt.');
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, boardId, receiptId]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const receipt = result?.receipt;
  const isRevoked = result?.currentness === 'revoked';

  const revoke = async () => {
    if (!canRevoke || !justification.trim() || !reasonCode.trim()) return;
    setRevoking(true);
    try {
      await api.revokeCodeInvestigationReceipt(boardId, receiptId, {
        reason_code: reasonCode.trim(),
        justification: justification.trim(),
      });
      toast.success('Receipt revocation appended');
      setShowRevoke(false);
      setJustification('');
      await load();
      onRevoked?.();
    } catch (caught) {
      toast.error(caught instanceof Error ? caught.message : 'Could not revoke receipt.');
    } finally {
      setRevoking(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={focusTrap.dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="receipt-detail-title"
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl bg-white shadow-2xl dark:bg-gray-800"
      >
        <header className="flex items-start justify-between gap-3 border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 id="receipt-detail-title" className="text-base font-semibold text-gray-900 dark:text-white">
                Investigation receipt
              </h2>
              {result?.currentness === 'current' && <TraceabilityBadge kind="current" />}
              {receipt?.acceptance_status === 'accepted' && <TraceabilityBadge kind="receipt-accepted" />}
              {result && result.currentness !== 'current' && (
                <span className="rounded-full border border-gray-300 px-2 py-0.5 text-[10px] font-semibold capitalize text-gray-500 dark:border-gray-700 dark:text-gray-400">
                  {result.currentness}
                </span>
              )}
            </div>
            <p className="mt-1 truncate font-mono text-[11px] text-gray-400" title={receiptId}>
              {receiptId}
            </p>
          </div>
          <button data-receipt-initial-focus type="button" onClick={onClose} aria-label="Close receipt detail" className="rounded-md p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-700 dark:hover:text-gray-200">
            <X size={18} />
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          <TraceabilityDisclosure compact />

          {loading && (
            <div className="py-12 text-center text-sm text-gray-400" role="status">
              Loading immutable receipt…
            </div>
          )}
          {error && (
            <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-800 dark:bg-red-950/30 dark:text-red-300" role="alert">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {receipt && (
            <>
              <section>
                <div className="mb-2 flex items-center justify-between">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                    Accepted attestation
                  </h3>
                  <span className="flex items-center gap-1 text-[10px] text-gray-400">
                    <Clock3 size={11} /> immutable record
                  </span>
                </div>
                <dl className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  <KeyValue label="Outcome" value={receipt.outcome} />
                  <KeyValue label="Attestor" value={receipt.attestor_actor_id} mono />
                  <KeyValue label="Generation" value={`PF-${receipt.generation}`} />
                  <KeyValue label="Logical source ref" value={receipt.source_ref} mono />
                  <KeyValue label="Declared revision" value={receipt.declared_revision} mono />
                  <KeyValue label="Trust" value={receipt.trust_level.replace(/_/g, ' ')} />
                  <KeyValue label="Observed by agent" value={formatDate(receipt.observed_at)} />
                  <KeyValue label="Received by Pulse" value={formatDate(receipt.received_at)} />
                  <KeyValue label="Expires" value={formatDate(receipt.expires_at)} />
                </dl>
              </section>

              <section className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                <h3 className="text-xs font-semibold text-gray-700 dark:text-gray-200">
                  Agent-declared capabilities
                </h3>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {receipt.capabilities.map((capability) => (
                    <span key={capability} className="rounded-full bg-cyan-50 px-2 py-1 text-[10px] font-medium text-cyan-700 dark:bg-cyan-950/40 dark:text-cyan-300">
                      {capability.replace(/_/g, ' ')}
                    </span>
                  ))}
                  {receipt.capabilities.length === 0 && (
                    <span className="text-xs text-gray-400">No capabilities declared.</span>
                  )}
                </div>
              </section>

              <section className="grid gap-2 sm:grid-cols-2">
                <KeyValue label="Workspace fingerprint" value={receipt.workspace_state?.workspace_state_id} mono />
                <KeyValue label="Manifest digest" value={receipt.workspace_state?.manifest_digest} mono />
                <KeyValue
                  label="Workspace state"
                  value={receipt.workspace_state
                    ? `${receipt.workspace_state.declared_dirty ? 'Dirty' : 'Committed'} (agent-declared)`
                    : null}
                />
                <KeyValue label="Fingerprint algorithm" value={receipt.workspace_state?.fingerprint_algorithm} mono />
                <KeyValue label="Tool" value={[receipt.tooling.tool_id, receipt.tooling.tool_version].filter(Boolean).join(' · ')} />
                <KeyValue label="Method" value={receipt.tooling.method_id} mono />
              </section>

              <section className="rounded-lg border border-gray-200 p-3 dark:border-gray-700">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-xs font-semibold text-gray-700 dark:text-gray-200">
                    Omission manifest ({receipt.omission_count})
                  </h3>
                  <button
                    type="button"
                    onClick={() => {
                      void navigator.clipboard.writeText(receipt.payload_sha256);
                      toast.success('Payload digest copied');
                    }}
                    className="inline-flex items-center gap-1 text-[10px] text-blue-600 hover:underline dark:text-blue-400"
                  >
                    <Clipboard size={11} /> Copy payload digest
                  </button>
                </div>
                {receipt.omission_manifest.length > 0 ? (
                  <ul className="mt-2 space-y-1.5">
                    {receipt.omission_manifest.map((omission, index) => (
                      <li key={`${omission.reason_code}-${index}`} className="rounded bg-amber-50 px-2.5 py-2 text-[11px] text-amber-800 dark:bg-amber-950/30 dark:text-amber-300">
                        <span className="font-semibold">{omission.reason_code.replace(/_/g, ' ')}</span>
                        <span>{` · ${omission.count} affected`}</span>
                        <span className="mt-1 block break-all font-mono text-[10px] opacity-80">
                          Scope digest: {omission.affected_scope_digest}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-2 text-xs text-gray-400">No omissions declared.</p>
                )}
              </section>

              <section className="rounded-lg border border-gray-200 bg-gray-50/60 p-3 dark:border-gray-700 dark:bg-gray-900/30">
                <div className="flex items-start gap-2">
                  <CheckCircle2 size={14} className="mt-0.5 shrink-0 text-emerald-500" />
                  <div>
                    <h3 className="text-xs font-semibold text-gray-700 dark:text-gray-200">
                      Receipt accepted, not independently checked by Pulse
                    </h3>
                    <p className="mt-0.5 text-[11px] leading-4 text-gray-500 dark:text-gray-400">
                      Pulse validated the authenticated envelope, scope, profiles,
                      hashes, limits, authorization, and lineage. It did not open
                      the source or repeat the agent&apos;s observations.
                    </p>
                  </div>
                </div>
              </section>

              {canRevoke && !isRevoked && (
                <section className="border-t border-gray-200 pt-4 dark:border-gray-700">
                  {!showRevoke ? (
                    <button
                      type="button"
                      onClick={() => setShowRevoke(true)}
                      className="inline-flex items-center gap-1.5 rounded-md border border-red-300 px-3 py-1.5 text-xs font-medium text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950/30"
                    >
                      <ShieldX size={13} /> Revoke receipt
                    </button>
                  ) : (
                    <div className="space-y-3 rounded-lg border border-red-200 bg-red-50/60 p-3 dark:border-red-900 dark:bg-red-950/20">
                      <div className="flex items-start gap-2 text-xs text-red-800 dark:text-red-300">
                        <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                        <p>
                          Revocation is a separate append-only operator record.
                          The immutable receipt remains in history.
                        </p>
                      </div>
                      <input
                        aria-label="Receipt revocation reason code"
                        value={reasonCode}
                        onChange={(event) => setReasonCode(event.target.value)}
                        className="w-full rounded-md border border-red-200 bg-white px-3 py-2 font-mono text-xs dark:border-red-900 dark:bg-gray-900"
                      />
                      <textarea
                        aria-label="Receipt revocation justification"
                        value={justification}
                        onChange={(event) => setJustification(event.target.value)}
                        placeholder="Explain why this accepted receipt must no longer be current…"
                        rows={3}
                        className="w-full resize-none rounded-md border border-red-200 bg-white px-3 py-2 text-xs dark:border-red-900 dark:bg-gray-900"
                      />
                      <div className="flex justify-end gap-2">
                        <button type="button" onClick={() => setShowRevoke(false)} className="btn btn-secondary text-xs">
                          Cancel
                        </button>
                        <button type="button" onClick={() => void revoke()} disabled={revoking || !reasonCode.trim() || !justification.trim()} className="btn btn-danger text-xs disabled:opacity-50">
                          {revoking ? 'Appending…' : 'Append revocation'}
                        </button>
                      </div>
                    </div>
                  )}
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
