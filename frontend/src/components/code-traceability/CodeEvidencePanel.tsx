import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  CodeEvidenceSourceRole,
  CodeTraceabilityDisposition,
  CodeTraceabilityEvidence,
  CodeTraceabilityProjection,
  SourceContextClassificationInputV2,
  SourceContextEvidenceItemV2,
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
import { SourceContextOverview } from './SourceContextOverview';
import {
  codeEvidenceBaselinePresenceLabel,
  codeEvidenceContextOriginLabel,
  codeEvidenceSourceRoleLabel,
  groupSourceContextEvidence,
} from './sourceContextPresentation';
import { sanitizeCodeEvidenceProjectionForAuthority } from './codeEvidenceAuthority';
import {
  LegacyEvidenceClassificationDrawer,
  type LegacyEvidenceClassificationSnapshot,
} from './LegacyEvidenceClassificationDrawer';

interface Props {
  boardId: string;
  subjectId: string;
  subjectVersion: number;
}

interface OpenLegacyClassificationDrawer {
  evidenceIds: readonly string[];
  snapshot: LegacyEvidenceClassificationSnapshot;
  opener: HTMLElement | null;
}

function isCurrentRefinementProjection(
  projection: CodeTraceabilityProjection,
  subjectId: string,
  subjectVersion: number,
): boolean {
  return projection.subject_type === 'refinement'
    && projection.subject_id === subjectId
    && projection.subject_version === subjectVersion
    && projection.profile === 'detail'
    && projection.context_scope === 'default';
}

function legacyClassificationSnapshot(
  projection: CodeTraceabilityProjection,
  evidenceIds: readonly string[],
): LegacyEvidenceClassificationSnapshot {
  const selected = new Set(evidenceIds);
  return {
    classificationInputs: (projection.source_context_classification_inputs ?? []).filter(
      (input: SourceContextClassificationInputV2) => selected.has(input.evidence_id),
    ),
    effectiveItems: (projection.source_context_items ?? []).filter(
      (item) => selected.has(item.evidence_id),
    ),
    evidence: projection.evidence.filter((item) => selected.has(item.id)),
  };
}

function lineRange(evidence: CodeTraceabilityEvidence) {
  if (!evidence.snapshot_line_start) return null;
  return evidence.snapshot_line_end && evidence.snapshot_line_end !== evidence.snapshot_line_start
    ? `L${evidence.snapshot_line_start}–${evidence.snapshot_line_end}`
    : `L${evidence.snapshot_line_start}`;
}

function TechnicalValue({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string | number | null | undefined;
  mono?: boolean;
}) {
  if (value === null || value === undefined || value === '') return null;
  return (
    <div className="min-w-0">
      <dt className="text-gray-400">{label}</dt>
      <dd className={`break-words text-gray-600 dark:text-gray-300 ${mono ? 'font-mono' : ''}`}>
        {value}
      </dd>
    </div>
  );
}

const SOURCE_ROLE_BADGE_STYLES: Readonly<Record<CodeEvidenceSourceRole, string>> = {
  current_implementation: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300',
  existing_scaffold: 'bg-sky-50 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300',
  existing_constraint: 'bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300',
  reference_pattern: 'bg-violet-50 text-violet-700 dark:bg-violet-950/40 dark:text-violet-300',
  uncategorized_legacy: 'bg-orange-50 text-orange-700 dark:bg-orange-950/40 dark:text-orange-300',
};

const CLASSIFIED_SOURCE_ROLE_ORDER = [
  'current_implementation',
  'existing_scaffold',
  'existing_constraint',
  'reference_pattern',
] as const satisfies readonly CodeEvidenceSourceRole[];

function EvidenceCard({
  evidence,
  sourceContextItem,
  projection,
  onViewReceipt,
  canRevoke,
  canChangeClassification,
  onRevoke,
  onChangeClassification,
}: {
  evidence: CodeTraceabilityEvidence;
  sourceContextItem: SourceContextEvidenceItemV2 | null;
  projection: CodeTraceabilityProjection;
  onViewReceipt: (receiptId: string) => void;
  canRevoke: boolean;
  canChangeClassification: boolean;
  onRevoke: (evidenceId: string, reason: string) => Promise<void>;
  onChangeClassification: (evidenceId: string, opener: HTMLElement) => void;
}) {
  const [showRevoke, setShowRevoke] = useState(false);
  const [reason, setReason] = useState('');
  const [revoking, setRevoking] = useState(false);
  const [technicalDetailsOpen, setTechnicalDetailsOpen] = useState(false);
  const links = projection.links.filter((link) => link.evidence_id === evidence.id);
  const dispositions = projection.dispositions.filter(
    (item: CodeTraceabilityDisposition) => item.evidence_id === evidence.id && item.active,
  );
  const currentness = projectedReceiptCurrentness(
    projection,
    evidence.investigation_receipt_id,
  );
  const range = lineRange(evidence);
  const claimTitle = evidence.claim?.trim() || 'Agent-submitted code evidence';
  const technicalDetailsId = `technical-evidence-details-${evidence.id}`;

  return (
    <article className="rounded-lg border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800/70">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            {sourceContextItem ? (
              <>
                <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${SOURCE_ROLE_BADGE_STYLES[sourceContextItem.source_role]}`}>
                  {codeEvidenceSourceRoleLabel(sourceContextItem.source_role)}
                </span>
                {sourceContextItem.evidence_applicable === true && (
                  <span className="rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
                    Implementation evidence
                  </span>
                )}
                {sourceContextItem.evidence_applicable === false && (
                  <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-700 dark:bg-blue-950/40 dark:text-blue-300">
                    Context only
                  </span>
                )}
                {sourceContextItem.evidence_applicable === null && (
                  <span className="rounded-full bg-orange-50 px-2 py-0.5 text-[10px] font-medium text-orange-700 dark:bg-orange-950/40 dark:text-orange-300">
                    Applicability unresolved
                  </span>
                )}
              </>
            ) : (
              <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                Context not projected
              </span>
            )}
          </div>
          <h3 className="mt-2 text-sm font-semibold leading-5 text-gray-900 dark:text-white">
            {claimTitle}
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

      {sourceContextItem ? (
        <div className="mt-3 space-y-3">
          {sourceContextItem.source_origin && (
            <p className="flex items-start gap-1 text-xs leading-5 text-gray-700 dark:text-gray-200">
              <span className="shrink-0 font-semibold text-gray-500 dark:text-gray-400">Origin:</span>
              <span className="min-w-0 line-clamp-2" title={sourceContextItem.source_origin}>
                {sourceContextItem.source_origin}
              </span>
            </p>
          )}
          {sourceContextItem.relevance_summary && (
            <p className="text-xs leading-5 text-gray-800 dark:text-gray-200">
              <span className="font-semibold text-gray-500 dark:text-gray-400">Relevance:</span>{' '}
              {sourceContextItem.relevance_summary}
            </p>
          )}
          {sourceContextItem.interpretation_limit && (
            <p className="rounded-md border border-amber-200 bg-amber-50/70 px-3 py-2 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-200">
              <span className="font-semibold">Interpretation limit:</span>{' '}
              {sourceContextItem.interpretation_limit}
            </p>
          )}
          {sourceContextItem.evidence_applicable === false && (
            <p className="text-xs leading-5 text-blue-800 dark:text-blue-300">
              Context only — excluded from implementation evidence coverage.
            </p>
          )}
          {sourceContextItem.evidence_applicable === null && (
            <p className="text-xs leading-5 text-orange-800 dark:text-orange-300">
              Applicability remains unresolved; this item is not treated as implementation evidence.
            </p>
          )}
        </div>
      ) : (
        <p className="mt-3 rounded-md border border-amber-200 bg-amber-50/70 px-3 py-2 text-xs leading-5 text-amber-800 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-300">
          Canonical context was not included for this record. Its role and applicability are not inferred from the claim or source path.
        </p>
      )}

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

      <details
        className="mt-3 rounded-md border border-gray-100 px-3 py-2 text-xs dark:border-gray-700"
        open={technicalDetailsOpen}
        onToggle={(event) => setTechnicalDetailsOpen(event.currentTarget.open)}
      >
        <summary
          className="cursor-pointer font-medium text-gray-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 dark:text-gray-300"
          aria-expanded={technicalDetailsOpen}
          aria-controls={technicalDetailsId}
        >
          Technical evidence details
        </summary>
        <div id={technicalDetailsId} className="mt-3 space-y-3">
          <div className="flex flex-wrap items-center gap-1.5" aria-label="Evidence protocol metadata">
            <TraceabilityBadge kind="agent-attested" />
            <TraceabilityCurrentnessBadge currentness={currentness} />
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-600 dark:bg-gray-700 dark:text-gray-300">
              {evidence.evidence_type.replace(/_/g, ' ')}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium capitalize text-gray-500 dark:bg-gray-700 dark:text-gray-400">
              {evidence.lifecycle_status}
            </span>
          </div>
          {canChangeClassification && (
            <div className="flex justify-end">
              <button
                type="button"
                onClick={(event) => onChangeClassification(evidence.id, event.currentTarget)}
                className="btn btn-secondary text-xs"
              >
                Change classification
              </button>
            </div>
          )}
          <div className="rounded-md border border-gray-100 bg-gray-50/70 px-3 py-2.5 dark:border-gray-700/70 dark:bg-gray-900/40">
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

          <dl className="grid gap-x-4 gap-y-2 text-[11px] sm:grid-cols-2 xl:grid-cols-3">
            <TechnicalValue label="Evidence identifier" value={evidence.id} mono />
            <TechnicalValue label="Board identifier" value={evidence.board_id} mono />
            <TechnicalValue label="Receipt identifier" value={evidence.investigation_receipt_id} mono />
            <TechnicalValue
              label="Evidence subject"
              value={`${evidence.parent_type}:${evidence.parent_id}@${evidence.parent_version}`}
              mono
            />
            <TechnicalValue label="Logical source" value={evidence.source_ref} mono />
            <TechnicalValue label="Selector kind" value={evidence.selector_kind} mono />
            <TechnicalValue label="Language" value={evidence.language} />
            <TechnicalValue label="Symbol kind" value={evidence.symbol_kind} />
            <TechnicalValue label="Qualified symbol" value={evidence.qualified_symbol} mono />
            <TechnicalValue label="Symbol signature" value={evidence.symbol_signature} mono />
            <TechnicalValue label="Attestation state" value={evidence.attestation_state} mono />
            <TechnicalValue label="Attestation basis" value={evidence.attestation_basis} mono />
            <TechnicalValue label="Supersedes Evidence" value={evidence.supersedes_evidence_id} mono />
            <TechnicalValue label="Revocation reason" value={evidence.revocation_reason} />
            <TechnicalValue label="Submitted by" value={evidence.submitted_by} mono />
            <TechnicalValue label="Received at" value={evidence.received_at} />
            <TechnicalValue label="Evidence payload digest" value={evidence.payload_sha256} mono />
            <TechnicalValue label="File blob digest" value={evidence.declared_file_blob_sha256} mono />
            <TechnicalValue label="Source content digest" value={evidence.declared_source_content_sha256} mono />
            <TechnicalValue label="Excerpt digest" value={evidence.excerpt_sha256} mono />
            <TechnicalValue label="Excerpt omitted reason" value={evidence.excerpt_omitted_reason} mono />
            <TechnicalValue
              label="Excerpt truncated"
              value={evidence.excerpt_truncated == null
                ? null
                : evidence.excerpt_truncated ? 'Yes' : 'No'}
            />
            {sourceContextItem && (
              <>
                <TechnicalValue
                  label="Source role"
                  value={codeEvidenceSourceRoleLabel(sourceContextItem.source_role)}
                />
                <TechnicalValue
                  label="Context origin"
                  value={codeEvidenceContextOriginLabel(sourceContextItem.context_origin)}
                />
                <TechnicalValue label="Relevance summary" value={sourceContextItem.relevance_summary} />
                <TechnicalValue label="Relation to delivery" value={sourceContextItem.scope_relation} />
                <TechnicalValue label="Human source origin" value={sourceContextItem.source_origin} />
                <TechnicalValue label="Interpretation limit" value={sourceContextItem.interpretation_limit} />
                <TechnicalValue
                  label="Evidence applicability"
                  value={sourceContextItem.evidence_applicable === true
                    ? 'Implementation evidence'
                    : sourceContextItem.evidence_applicable === false
                      ? 'Context only'
                      : 'Unresolved'}
                />
                <TechnicalValue
                  label="Context contract"
                  value={sourceContextItem.context_contract_version == null
                    ? null
                    : `V${sourceContextItem.context_contract_version}`}
                />
                <TechnicalValue
                  label="Classification revision"
                  value={sourceContextItem.classification_revision}
                />
                <TechnicalValue label="Classification identifier" value={sourceContextItem.classification_id} mono />
                <TechnicalValue label="Classification digest" value={sourceContextItem.classification_sha256} mono />
                <TechnicalValue label="Classified by" value={sourceContextItem.classified_by} mono />
                <TechnicalValue label="Classified at" value={sourceContextItem.classified_at} />
              </>
            )}
            {evidence.workspace_state && (
              <>
                <TechnicalValue label="Workspace fingerprint" value={evidence.workspace_state.workspace_state_id} mono />
                <TechnicalValue label="Declared revision" value={evidence.workspace_state.declared_revision} mono />
                <TechnicalValue
                  label="Workspace status"
                  value={evidence.workspace_state.declared_dirty ? 'Dirty' : 'Committed'}
                />
                <TechnicalValue label="Workspace observed at" value={evidence.workspace_state.observed_at} />
                <TechnicalValue label="Reproducibility claim" value={evidence.workspace_state.reproducibility_claim} mono />
                <TechnicalValue label="Fingerprint algorithm" value={evidence.workspace_state.fingerprint_algorithm} mono />
                <TechnicalValue label="Manifest digest" value={evidence.workspace_state.manifest_digest} mono />
                <TechnicalValue label="Manifest entries" value={evidence.workspace_state.manifest_entry_count} />
              </>
            )}
            {sourceContextItem?.baseline_provenance && (
              <>
                <TechnicalValue
                  label="Baseline"
                  value={codeEvidenceBaselinePresenceLabel(sourceContextItem.baseline_provenance.presence)}
                />
                <TechnicalValue
                  label="Baseline workspace"
                  value={sourceContextItem.baseline_provenance.workspace_state_id}
                  mono
                />
                <TechnicalValue label="Baseline note" value={sourceContextItem.baseline_provenance.provenance_note} />
              </>
            )}
          </dl>

          {evidence.excerpt && (
            <div>
              <p className="text-[11px] text-gray-400">Evidence excerpt</p>
              <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-md bg-gray-950 p-3 font-mono text-[11px] text-gray-100">
                {evidence.excerpt}
              </pre>
            </div>
          )}

          {(links.length > 0 || dispositions.length > 0) && (
            <section className="space-y-2 border-t border-gray-100 pt-3 dark:border-gray-700" aria-label="Evidence linkage metadata">
              <h4 className="flex items-center gap-1.5 text-[11px] font-semibold text-gray-500 dark:text-gray-400">
                <Link2 size={12} /> Links and dispositions
              </h4>
              <ul className="space-y-2">
                {links.map((link) => (
                  <li key={link.id} className="rounded-md border border-violet-200 bg-violet-50/60 px-2.5 py-2 text-[10px] text-violet-800 dark:border-violet-900 dark:bg-violet-950/20 dark:text-violet-300">
                    <p className="font-semibold">{link.relation_type.replace(/_/g, ' ')}</p>
                    <p className="mt-0.5 break-all font-mono">
                      {link.entity_type}:{link.entity_id} · Spec {link.spec_id} · Link {link.id}
                    </p>
                    {link.rationale && <p className="mt-0.5">{link.rationale}</p>}
                  </li>
                ))}
                {dispositions.map((disposition) => (
                  <li key={disposition.id} className="rounded-md border border-amber-200 bg-amber-50/60 px-2.5 py-2 text-[10px] text-amber-800 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-300">
                    <p className="font-semibold">{disposition.disposition.replace(/_/g, ' ')}</p>
                    <p className="mt-0.5 break-all font-mono">Disposition {disposition.id}</p>
                    {disposition.justification && <p className="mt-0.5">{disposition.justification}</p>}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </details>
    </article>
  );
}

function CodeEvidenceLoadingState() {
  return (
    <div
      className="grid gap-3 sm:grid-cols-2"
      data-testid="code-evidence-loading"
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <span className="sr-only">Loading code evidence…</span>
      <div className="h-24 animate-pulse rounded-lg bg-gray-100 dark:bg-gray-800" aria-hidden="true" />
      <div className="h-24 animate-pulse rounded-lg bg-gray-100 dark:bg-gray-800" aria-hidden="true" />
      <div className="h-36 animate-pulse rounded-lg bg-gray-100 sm:col-span-2 dark:bg-gray-800" aria-hidden="true" />
    </div>
  );
}

export function CodeEvidencePanel({ boardId, subjectId, subjectVersion }: Props) {
  const api = useDashboardApi();
  const {
    canReadProjection,
    canClassifyLegacyEvidence,
    canRevokeEvidence,
    isLoading: authorityLoading,
    error: authorityError,
  } = useCodeTraceabilityAuthority(boardId);
  const [projection, setProjection] = useState<CodeTraceabilityProjection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [receiptId, setReceiptId] = useState<string | null>(null);
  const [showSubmissionGuide, setShowSubmissionGuide] = useState(false);
  const [classificationDrawer, setClassificationDrawer] = useState<OpenLegacyClassificationDrawer | null>(null);
  const [classificationSuccess, setClassificationSuccess] = useState<string | null>(null);
  const classificationSuccessRef = useRef<HTMLParagraphElement>(null);
  const classificationFocusFallbackRef = useRef<HTMLHeadingElement>(null);
  const projectionRequestGeneration = useRef(0);

  const load = useCallback(async (signal?: AbortSignal) => {
    const requestGeneration = ++projectionRequestGeneration.current;
    if (!canReadProjection) {
      setProjection(null);
      setLoading(false);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    setProjection(null);
    try {
      const nextProjection = await api.getCodeTraceabilityProjection(
        boardId,
        'refinement',
        subjectId,
        subjectVersion,
        { profile: 'detail', signal },
      );
      if (
        !signal?.aborted
        && projectionRequestGeneration.current === requestGeneration
      ) {
        setProjection(sanitizeCodeEvidenceProjectionForAuthority(
          nextProjection,
          canClassifyLegacyEvidence,
        ));
      }
    } catch (caught) {
      if (
        !signal?.aborted
        && projectionRequestGeneration.current === requestGeneration
      ) {
        setProjection(null);
        setError(caught instanceof Error ? caught.message : 'Could not load code evidence.');
      }
    } finally {
      if (
        !signal?.aborted
        && projectionRequestGeneration.current === requestGeneration
      ) setLoading(false);
    }
  }, [api, boardId, canClassifyLegacyEvidence, canReadProjection, subjectId, subjectVersion]);

  useEffect(() => {
    setReceiptId(null);
    setShowSubmissionGuide(false);
    setClassificationDrawer(null);
    setClassificationSuccess(null);
    if (!canReadProjection) {
      projectionRequestGeneration.current += 1;
      setProjection(null);
      setError(null);
      setLoading(authorityLoading);
      return;
    }
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [authorityLoading, canReadProjection, load]);

  const evidenceGroups = useMemo(() => {
    if (!projection) return [];
    const groups = groupSourceContextEvidence({
      evidence: projection.evidence,
      sourceContextItems: projection.source_context_items ?? [],
      classificationInputs: canClassifyLegacyEvidence
        ? projection.source_context_classification_inputs ?? []
        : [],
      obligationMappings: projection.obligation_evidence_mappings ?? [],
    }).flatMap((group) => (
      group.evidence ? [{ ...group, evidence: group.evidence }] : []
    ));
    return groups;
  }, [canClassifyLegacyEvidence, projection]);

  const classifiedEvidenceSections = useMemo(() => CLASSIFIED_SOURCE_ROLE_ORDER
    .map((role) => ({
      role,
      groups: evidenceGroups.filter((group) => group.sourceContextItem?.source_role === role),
    }))
    .filter((section) => section.groups.length > 0), [evidenceGroups]);

  const unclassifiedEvidenceGroups = useMemo(() => evidenceGroups.filter((group) => (
    !group.sourceContextItem
    || group.sourceContextItem.source_role === 'uncategorized_legacy'
  )), [evidenceGroups]);

  const currentClassificationInputs = useMemo(() => {
    if (
      !projection
      || !canClassifyLegacyEvidence
      || !isCurrentRefinementProjection(projection, subjectId, subjectVersion)
    ) return [];
    return projection.source_context_classification_inputs ?? [];
  }, [canClassifyLegacyEvidence, projection, subjectId, subjectVersion]);

  const classificationInputIds = useMemo(
    () => new Set(currentClassificationInputs.map((input) => input.evidence_id)),
    [currentClassificationInputs],
  );

  const unclassifiedClassificationIds = useMemo(() => {
    if (!projection) return [];
    const evidenceIds = new Set(projection.evidence.map((item) => item.id));
    return (projection.source_context_items ?? [])
      .filter((item) => (
        classificationInputIds.has(item.evidence_id)
        && evidenceIds.has(item.evidence_id)
        && item.context_origin === 'unclassified_legacy'
        && item.source_role === 'uncategorized_legacy'
      ))
      .map((item) => item.evidence_id);
  }, [classificationInputIds, projection]);

  const reclassifiableEvidenceIds = useMemo(() => new Set(
    (projection?.source_context_items ?? [])
      .filter((item) => (
        classificationInputIds.has(item.evidence_id)
        && item.context_origin === 'human_legacy_classification'
      ))
      .map((item) => item.evidence_id),
  ), [classificationInputIds, projection]);

  const openLegacyClassification = useCallback((
    evidenceIds: readonly string[],
    opener: HTMLElement,
  ) => {
    if (
      !projection
      || !canClassifyLegacyEvidence
      || !isCurrentRefinementProjection(projection, subjectId, subjectVersion)
    ) return;
    const snapshot = legacyClassificationSnapshot(projection, evidenceIds);
    if (
      snapshot.classificationInputs.length === 0
      || snapshot.classificationInputs.length !== evidenceIds.length
      || snapshot.evidence.length !== evidenceIds.length
    ) return;
    setClassificationSuccess(null);
    setClassificationDrawer({
      evidenceIds: snapshot.classificationInputs.map((input) => input.evidence_id),
      snapshot,
      opener,
    });
  }, [canClassifyLegacyEvidence, projection, subjectId, subjectVersion]);

  const refetchAfterLegacyClassification = useCallback(async (
    evidenceIds: readonly string[],
    signal: AbortSignal,
  ): Promise<LegacyEvidenceClassificationSnapshot> => {
    const requestGeneration = ++projectionRequestGeneration.current;
    const nextProjection = await api.getCodeTraceabilityProjection(
      boardId,
      'refinement',
      subjectId,
      subjectVersion,
      { profile: 'detail', signal },
    );
    if (signal.aborted || projectionRequestGeneration.current !== requestGeneration) {
      throw new DOMException('Canonical projection request was superseded.', 'AbortError');
    }
    if (!isCurrentRefinementProjection(nextProjection, subjectId, subjectVersion)) {
      throw new Error('The canonical response no longer matches this Refinement.');
    }
    const sanitized = sanitizeCodeEvidenceProjectionForAuthority(
      nextProjection,
      canClassifyLegacyEvidence,
    );
    setProjection(sanitized);
    setError(null);
    setLoading(false);
    return legacyClassificationSnapshot(sanitized, evidenceIds);
  }, [api, boardId, canClassifyLegacyEvidence, subjectId, subjectVersion]);

  const closeLegacyClassification = useCallback(() => {
    const opener = classificationDrawer?.opener ?? null;
    setClassificationDrawer(null);
    const restoreFocus = () => {
      if (opener?.isConnected) opener.focus();
      else (classificationSuccessRef.current ?? classificationFocusFallbackRef.current)?.focus();
    };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(restoreFocus);
    else window.setTimeout(restoreFocus, 0);
  }, [classificationDrawer]);

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

  if (authorityLoading) return <CodeEvidenceLoadingState />;
  if (authorityError || !canReadProjection) return null;

  return (
    <div
      className="space-y-4"
      data-testid="refinement-code-evidence-panel"
      aria-busy={loading}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2
            ref={classificationFocusFallbackRef}
            tabIndex={-1}
            className="text-sm font-semibold text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 dark:text-white"
          >
            Code evidence
          </h2>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            Understand what existed before delivery, how it may be used, and the immutable agent records behind it.
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

      {loading && <CodeEvidenceLoadingState />}
      {error && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300" role="alert">
          <div className="flex min-w-0 items-start gap-2">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
          <button
            type="button"
            onClick={() => void load()}
            className="rounded-md border border-red-300 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-red-700 hover:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 dark:border-red-800 dark:bg-gray-900 dark:text-red-300 dark:hover:bg-red-950/40"
          >
            Retry
          </button>
        </div>
      )}
      {classificationSuccess && (
        <p
          ref={classificationSuccessRef}
          className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-200"
          role="status"
          aria-live="polite"
          tabIndex={-1}
        >
          {classificationSuccess}
        </p>
      )}
      {!loading && !error && projection && (
        <SourceContextOverview
          sourceContext={projection.source_context}
          contextualEvidenceCoverage={projection.contextual_evidence_coverage}
          visibleContextItemCount={projection.source_context_items?.length ?? 0}
          currentReceipts={projection.current_receipts ?? []}
          unclassifiedActionCount={unclassifiedClassificationIds.length}
          onReviewUnclassifiedEvidence={(opener) => openLegacyClassification(
            unclassifiedClassificationIds,
            opener,
          )}
        />
      )}
      {!loading && !error && projection && !projection.source_context && evidenceGroups.length === 0 && (
        <TraceabilityEmptyState noun="code evidence" />
      )}
      {!loading && !error && projection && evidenceGroups.length > 0 && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Technical evidence
            </h3>
            <span className="text-[11px] text-gray-400">
              {evidenceGroups.length} visible record{evidenceGroups.length === 1 ? '' : 's'}
            </span>
          </div>
          {classifiedEvidenceSections.map(({ role, groups }) => (
            <section
              key={role}
              aria-labelledby={`code-evidence-role-${role}`}
              className="space-y-2"
            >
              <h4
                id={`code-evidence-role-${role}`}
                className="text-xs font-semibold text-gray-700 dark:text-gray-200"
              >
                {codeEvidenceSourceRoleLabel(role)}
              </h4>
              {groups.map((group) => (
                <EvidenceCard
                  key={group.evidenceId}
                  evidence={group.evidence}
                  sourceContextItem={group.sourceContextItem}
                  projection={projection}
                  onViewReceipt={setReceiptId}
                  canRevoke={canRevokeEvidence}
                  canChangeClassification={reclassifiableEvidenceIds.has(group.evidenceId)}
                  onRevoke={revokeEvidence}
                  onChangeClassification={(evidenceId, opener) => openLegacyClassification(
                    [evidenceId],
                    opener,
                  )}
                />
              ))}
            </section>
          ))}
          {unclassifiedEvidenceGroups.length > 0 && (
            <section aria-labelledby="code-evidence-unclassified" className="space-y-2">
              <h4
                id="code-evidence-unclassified"
                className="text-xs font-semibold text-orange-800 dark:text-orange-300"
              >
                Classification not provided
              </h4>
              {unclassifiedEvidenceGroups.map((group) => (
                <EvidenceCard
                  key={group.evidenceId}
                  evidence={group.evidence}
                  sourceContextItem={group.sourceContextItem}
                  projection={projection}
                  onViewReceipt={setReceiptId}
                  canRevoke={canRevokeEvidence}
                  canChangeClassification={reclassifiableEvidenceIds.has(group.evidenceId)}
                  onRevoke={revokeEvidence}
                  onChangeClassification={(evidenceId, opener) => openLegacyClassification(
                    [evidenceId],
                    opener,
                  )}
                />
              ))}
            </section>
          )}
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
      {classificationDrawer && (
        <LegacyEvidenceClassificationDrawer
          snapshot={classificationDrawer.snapshot}
          canClassify={canClassifyLegacyEvidence}
          opener={classificationDrawer.opener}
          focusFallback={classificationFocusFallbackRef.current}
          onClose={closeLegacyClassification}
          onApplyBatch={(request, signal) => api.classifyLegacyCodeEvidence(
            boardId,
            request,
            signal,
          )}
          onCanonicalRefetch={(signal) => refetchAfterLegacyClassification(
            classificationDrawer.evidenceIds,
            signal,
          )}
          onApplied={() => {
            const count = classificationDrawer.evidenceIds.length;
            setClassificationSuccess(
              `${count} Evidence classification${count === 1 ? '' : 's'} updated.`,
            );
          }}
        />
      )}
    </div>
  );
}
