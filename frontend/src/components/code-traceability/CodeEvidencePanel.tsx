import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  BookOpenCheck,
  Clipboard,
  Eye,
  FileCode2,
  Link2,
  ShieldX,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { ContextualHelpLink } from '@/components/help';
import { useDashboardApi } from '@/services/api';
import type {
  CodeTraceabilityDisposition,
  CodeTraceabilityEvidence,
  CodeTraceabilityProjection,
} from '@/types';
import { ReceiptDetailModal } from './ReceiptDetailModal';
import {
  TraceabilityBadge,
  TraceabilityCurrentnessBadge,
  TraceabilityEmptyState,
} from './TraceabilityDisclosure';
import { projectedReceiptCurrentness } from './traceabilityCurrentness';
import { SubmissionGuideDialog } from './SubmissionGuideDialog';
import { useCodeTraceabilityAuthority } from './useCodeTraceabilityAuthority';

interface Props {
  boardId: string;
  subjectId: string;
  subjectVersion: number;
}

function shortId(value: string) {
  return value.length > 20 ? `${value.slice(0, 16)}…` : value;
}

function lineRange(evidence: CodeTraceabilityEvidence) {
  if (!evidence.snapshot_line_start) return null;
  return evidence.snapshot_line_end && evidence.snapshot_line_end !== evidence.snapshot_line_start
    ? `L${evidence.snapshot_line_start}–${evidence.snapshot_line_end}`
    : `L${evidence.snapshot_line_start}`;
}

function EvidenceCard({
  evidence,
  projection,
  onViewReceipt,
  canRevoke,
  onRevoke,
}: {
  evidence: CodeTraceabilityEvidence;
  projection: CodeTraceabilityProjection;
  onViewReceipt: (receiptId: string) => void;
  canRevoke: boolean;
  onRevoke: (evidenceId: string, reason: string) => Promise<void>;
}) {
  const [showRevoke, setShowRevoke] = useState(false);
  const [reason, setReason] = useState('');
  const [revoking, setRevoking] = useState(false);
  const links = projection.links.filter((link) => link.evidence_id === evidence.id);
  const dispositions = projection.dispositions.filter(
    (item: CodeTraceabilityDisposition) => item.evidence_id === evidence.id && item.active,
  );
  const currentness = projectedReceiptCurrentness(
    projection,
    evidence.investigation_receipt_id,
  );
  const range = lineRange(evidence);

  return (
    <article className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800/70">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <TraceabilityBadge kind="agent-attested" />
            <TraceabilityCurrentnessBadge currentness={currentness} />
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300">
              {evidence.evidence_type.replace(/_/g, ' ')}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium capitalize text-gray-500 dark:bg-gray-700 dark:text-gray-400">
              {evidence.lifecycle_status}
            </span>
          </div>
          <h3 className="mt-2 text-sm font-semibold leading-5 text-gray-900 dark:text-white">
            {evidence.claim || 'Agent-submitted code evidence'}
          </h3>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <button
            type="button"
            onClick={() => onViewReceipt(evidence.investigation_receipt_id)}
            className="inline-flex items-center gap-1 rounded-md border border-gray-200 px-2.5 py-1.5 text-[11px] font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <Eye size={12} /> View receipt
          </button>
          {canRevoke && evidence.lifecycle_status === 'active' && (
            <button
              type="button"
              onClick={() => setShowRevoke((value) => !value)}
              className="inline-flex items-center gap-1 rounded-md border border-red-300 px-2.5 py-1.5 text-[11px] font-medium text-red-600 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950/30"
            >
              <ShieldX size={12} /> Revoke evidence
            </button>
          )}
        </div>
      </div>

      <div className="mt-3 rounded-md border border-gray-100 bg-gray-50/70 px-3 py-2.5 dark:border-gray-700/70 dark:bg-gray-900/40">
        <div className="flex min-w-0 items-center gap-2 text-xs text-gray-700 dark:text-gray-200">
          <FileCode2 size={13} className="shrink-0 text-gray-400" />
          <span className="truncate font-mono" title={evidence.relative_path || evidence.source_ref}>
            {evidence.relative_path || 'No path included in receipt'}
          </span>
          {range && <span className="shrink-0 text-gray-400">{range}</span>}
        </div>
        {evidence.qualified_symbol && (
          <p className="mt-1 truncate pl-5 font-mono text-[11px] text-gray-500 dark:text-gray-400" title={evidence.qualified_symbol}>
            {evidence.symbol_kind ? `${evidence.symbol_kind} · ` : ''}{evidence.qualified_symbol}
          </p>
        )}
      </div>

      <dl className="mt-3 grid gap-x-4 gap-y-2 text-[11px] sm:grid-cols-3">
        <div className="min-w-0">
          <dt className="text-gray-400">Logical source</dt>
          <dd className="truncate font-mono text-gray-600 dark:text-gray-300" title={evidence.source_ref}>{evidence.source_ref}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-gray-400">Receipt</dt>
          <dd className="truncate font-mono text-gray-600 dark:text-gray-300" title={evidence.investigation_receipt_id}>{shortId(evidence.investigation_receipt_id)}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-gray-400">Workspace fingerprint</dt>
          <dd className="truncate font-mono text-gray-600 dark:text-gray-300" title={evidence.workspace_state?.workspace_state_id}>
            {evidence.workspace_state?.workspace_state_id
              ? shortId(evidence.workspace_state.workspace_state_id)
              : 'Not included'}
          </dd>
        </div>
      </dl>

      {showRevoke && canRevoke && (
        <section className="mt-3 space-y-2 rounded-lg border border-red-200 bg-red-50/60 p-3 dark:border-red-900 dark:bg-red-950/20" aria-label={`Revoke evidence ${evidence.id}`}>
          <div className="flex items-start gap-2 text-[11px] leading-4 text-red-800 dark:text-red-300">
            <AlertTriangle size={13} className="mt-0.5 shrink-0" />
            <p>
              Human governance action. Revocation changes lifecycle status but never edits or replaces the immutable agent attestation.
            </p>
          </div>
          <textarea
            aria-label="Evidence revocation reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Explain why this evidence must no longer be active…"
            rows={2}
            className="w-full resize-none rounded-md border border-red-200 bg-white px-3 py-2 text-xs text-gray-800 dark:border-red-900 dark:bg-gray-900 dark:text-gray-100"
          />
          <div className="flex justify-end gap-2">
            <button type="button" onClick={() => setShowRevoke(false)} className="btn btn-secondary text-xs">
              Cancel
            </button>
            <button
              type="button"
              onClick={() => {
                if (!reason.trim()) return;
                setRevoking(true);
                void onRevoke(evidence.id, reason.trim())
                  .then(() => {
                    setShowRevoke(false);
                    setReason('');
                  })
                  .catch(() => undefined)
                  .finally(() => setRevoking(false));
              }}
              disabled={revoking || !reason.trim()}
              className="btn btn-danger text-xs disabled:opacity-50"
            >
              {revoking ? 'Revoking…' : 'Confirm evidence revocation'}
            </button>
          </div>
        </section>
      )}

      {(links.length > 0 || dispositions.length > 0) && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-gray-100 pt-3 dark:border-gray-700">
          <Link2 size={12} className="text-gray-400" />
          {links.map((link) => (
            <span key={link.id} className="rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-[10px] text-violet-700 dark:border-violet-900 dark:bg-violet-950/30 dark:text-violet-300">
              {link.entity_type}:{shortId(link.entity_id)} · {link.relation_type.replace(/_/g, ' ')}
            </span>
          ))}
          {dispositions.map((disposition) => (
            <span key={disposition.id} className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10px] text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
              {disposition.disposition.replace(/_/g, ' ')}
            </span>
          ))}
        </div>
      )}
    </article>
  );
}

export function CodeEvidencePanel({ boardId, subjectId, subjectVersion }: Props) {
  const api = useDashboardApi();
  const { canRevokeEvidence } = useCodeTraceabilityAuthority(boardId);
  const [projection, setProjection] = useState<CodeTraceabilityProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [receiptId, setReceiptId] = useState<string | null>(null);
  const [showSubmissionGuide, setShowSubmissionGuide] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      setProjection(await api.getCodeTraceabilityProjection(
        boardId,
        'refinement',
        subjectId,
        subjectVersion,
        { profile: 'detail', signal },
      ));
    } catch (caught) {
      if (!signal?.aborted) {
        setError(caught instanceof Error ? caught.message : 'Could not load code evidence.');
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [api, boardId, subjectId, subjectVersion]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const evidence = useMemo(() => projection?.evidence ?? [], [projection]);

  const revokeEvidence = async (evidenceId: string, reason: string) => {
    try {
      await api.revokeCodeEvidence(boardId, evidenceId, { reason });
      toast.success('Evidence revocation recorded');
      await load();
    } catch (caught) {
      toast.error(caught instanceof Error ? caught.message : 'Could not revoke evidence.');
      throw caught;
    }
  };

  return (
    <div className="space-y-4" data-testid="refinement-code-evidence-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-gray-900 dark:text-white">Code evidence</h2>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            Immutable evidence accepted from authenticated external agents; human lifecycle governance stays separate.
          </p>
          <ContextualHelpLink
            sectionId="code-traceability"
            ariaLabel="Learn how Code Evidence works"
            testId="code-evidence-help-link"
            className="mt-1 text-[11px]"
          >
            How Code Evidence works
          </ContextualHelpLink>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setShowSubmissionGuide(true)}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-2.5 py-1.5 text-[11px] font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <BookOpenCheck size={12} /> Submission guide
          </button>
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard.writeText(`refinement:${subjectId}@${subjectVersion}`);
              toast.success('Refinement context copied');
            }}
            className="inline-flex items-center gap-1.5 rounded-md border border-gray-200 px-2.5 py-1.5 text-[11px] font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <Clipboard size={12} /> Copy refinement context
          </button>
        </div>
      </div>

      {loading && <div className="py-10 text-center text-xs text-gray-400" role="status">Loading accepted evidence…</div>}
      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300" role="alert">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {!loading && !error && projection && evidence.length === 0 && (
        <TraceabilityEmptyState noun="code evidence" />
      )}
      {!loading && !error && projection && evidence.length > 0 && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-gray-400">
            <span>{evidence.length} evidence item{evidence.length === 1 ? '' : 's'}</span>
            <span>{projection.direct_evidence_ids?.length ?? 0} direct</span>
            <span>{projection.inherited_evidence_ids?.length ?? 0} inherited</span>
          </div>
          {evidence.map((item) => (
            <EvidenceCard
              key={item.id}
              evidence={item}
              projection={projection}
              onViewReceipt={setReceiptId}
              canRevoke={canRevokeEvidence}
              onRevoke={revokeEvidence}
            />
          ))}
        </div>
      )}

      {receiptId && (
        <ReceiptDetailModal
          boardId={boardId}
          receiptId={receiptId}
          onClose={() => setReceiptId(null)}
          onRevoked={() => void load()}
        />
      )}
      {showSubmissionGuide && (
        <SubmissionGuideDialog
          boardId={boardId}
          subjectType="refinement"
          subjectId={subjectId}
          subjectVersion={subjectVersion}
          onClose={() => setShowSubmissionGuide(false)}
        />
      )}
    </div>
  );
}
