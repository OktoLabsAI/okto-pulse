import type {
  CodeEvidenceSourceRole,
  CodeInvestigationReceipt,
  ContextualEvidenceCoverage,
  SourceContextRoleCountsV2,
  SourceContextSummaryV2,
} from '@/types';
import { ShieldCheck } from 'lucide-react';
import {
  codeEvidenceSourceRoleLabel,
  contextualInvestigationOutcomeLabel,
  deliveryContextLabel,
  presentContextualEvidenceCoverage,
} from './sourceContextPresentation';

interface Props {
  sourceContext: SourceContextSummaryV2 | null | undefined;
  contextualEvidenceCoverage: ContextualEvidenceCoverage | null | undefined;
  visibleContextItemCount: number;
  currentReceipts: readonly Pick<
    CodeInvestigationReceipt,
    'id' | 'outcome' | 'source_ref' | 'omission_manifest'
  >[];
  unclassifiedActionCount?: number;
  onReviewUnclassifiedEvidence?: (opener: HTMLButtonElement) => void;
}

interface OmissionPresentation {
  label: string;
  action: string;
}

const OMISSION_PRESENTATIONS: Readonly<Record<string, OmissionPresentation>> = {
  size_cap: {
    label: 'Source size limit reached',
    action: 'Narrow or split the source scope, then run the investigation again.',
  },
  secret_redaction: {
    label: 'Sensitive content redacted',
    action: 'Provide an approved non-secret representation, then run the investigation again.',
  },
  binary_content: {
    label: 'Binary content not inspected',
    action: 'Provide a text-readable source or an approved inspection method, then run the investigation again.',
  },
  permission_denied: {
    label: 'Source access denied',
    action: 'Grant the investigating agent access to the affected source, then run the investigation again.',
  },
  path_policy: {
    label: 'Source path blocked by policy',
    action: 'Select an approved path or update the allowed source scope, then run the investigation again.',
  },
  unsupported_language: {
    label: 'Source language not supported',
    action: 'Use an approved investigator that supports the source language, then run the investigation again.',
  },
  timeout: {
    label: 'Investigation timed out',
    action: 'Reduce the source scope or increase the approved execution window, then run the investigation again.',
  },
  submodule_skipped: {
    label: 'Submodule not inspected',
    action: 'Make the submodule available to the investigator, then run the investigation again.',
  },
  other_bounded: {
    label: 'Bounded source scope omitted',
    action: 'Review the receipt details, resolve the reported limitation, then run the investigation again.',
  },
};

const UNKNOWN_OMISSION_PRESENTATION: OmissionPresentation = {
  label: 'Investigation limitation reported',
  action: 'Review the receipt details, resolve the reported limitation, then run the investigation again.',
};

interface RoleCountDefinition {
  role: CodeEvidenceSourceRole;
  countKey: keyof SourceContextRoleCountsV2;
  badgeClassName: string;
}

const ROLE_COUNTS: readonly RoleCountDefinition[] = [
  {
    role: 'current_implementation',
    countKey: 'current_implementation_count',
    badgeClassName: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300',
  },
  {
    role: 'existing_scaffold',
    countKey: 'existing_scaffold_count',
    badgeClassName: 'bg-sky-50 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300',
  },
  {
    role: 'existing_constraint',
    countKey: 'existing_constraint_count',
    badgeClassName: 'bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300',
  },
  {
    role: 'reference_pattern',
    countKey: 'reference_pattern_count',
    badgeClassName: 'bg-violet-50 text-violet-700 dark:bg-violet-950/40 dark:text-violet-300',
  },
  {
    role: 'uncategorized_legacy',
    countKey: 'uncategorized_legacy_count',
    badgeClassName: 'bg-orange-50 text-orange-700 dark:bg-orange-950/40 dark:text-orange-300',
  },
] as const;

function provenanceLabel(sourceContext: SourceContextSummaryV2): string | null {
  const provenance = sourceContext.delivery_context_provenance;
  if (!provenance) return null;
  if ('source_spec_id' in provenance) {
    return `Recorded on Spec version ${provenance.source_spec_version}`;
  }
  if ('inherited_value' in provenance) {
    return `Inherited from Refinement version ${provenance.source_refinement_version}`;
  }
  return `Recorded on Refinement version ${provenance.source_refinement_version}`;
}

function SourceContextUnavailable() {
  return (
    <section
      aria-labelledby="source-context-unavailable-heading"
      className="rounded-lg border border-amber-200 bg-amber-50/70 p-4 dark:border-amber-900 dark:bg-amber-950/20"
      data-testid="source-context-unavailable"
    >
      <h3
        id="source-context-unavailable-heading"
        className="text-sm font-semibold text-amber-900 dark:text-amber-200"
      >
        Source context unavailable
      </h3>
      <p className="mt-1 text-xs leading-5 text-amber-800 dark:text-amber-300">
        This projection predates the contextual contract. Technical evidence remains available
        below for audit, but its role and applicability cannot be inferred safely.
      </p>
    </section>
  );
}

export function SourceContextOverview({
  sourceContext,
  contextualEvidenceCoverage,
  visibleContextItemCount,
  currentReceipts,
  unclassifiedActionCount = 0,
  onReviewUnclassifiedEvidence,
}: Props) {
  if (!sourceContext) return <SourceContextUnavailable />;

  const populatedRoleCounts = ROLE_COUNTS.filter(
    ({ countKey }) => sourceContext.role_counts[countKey] > 0,
  );
  const authoritativeItemCount = ROLE_COUNTS.reduce(
    (total, { countKey }) => total + sourceContext.role_counts[countKey],
    0,
  );
  const provenance = provenanceLabel(sourceContext);
  const deliveryContext = sourceContext.delivery_context
    ? deliveryContextLabel(sourceContext.delivery_context)
    : 'Delivery context not recorded';
  const outcome = sourceContext.investigation_outcome
    ? contextualInvestigationOutcomeLabel(sourceContext.investigation_outcome)
    : 'Investigation outcome not recorded';
  const investigationIsIncomplete = sourceContext.investigation_outcome === 'partial'
    || sourceContext.investigation_outcome === 'unavailable';
  const absenceReported = sourceContext.evidence_applicable === false
    && sourceContext.investigation_outcome === 'no_relevant_existing_implementation';
  const coveragePresentation = presentContextualEvidenceCoverage(
    contextualEvidenceCoverage,
    sourceContext,
  );
  const noExistingImplementation = !investigationIsIncomplete
    && coveragePresentation.kind === 'not_applicable';
  const pureGreenfieldAbsence = noExistingImplementation
    && sourceContext.delivery_context === 'greenfield';
  const boundedDetails = sourceContext.technical_details_available
    && visibleContextItemCount < authoritativeItemCount;
  const overrideReason = sourceContext.delivery_context_provenance
    && 'inherited_value' in sourceContext.delivery_context_provenance
    ? sourceContext.delivery_context_provenance.override_reason
    : null;
  const reportedOmissions = investigationIsIncomplete
    ? currentReceipts.flatMap((receipt) => receipt.omission_manifest.map(
      (omission, index) => ({
        key: `${receipt.id}:${omission.reason_code}:${index}`,
        count: omission.count,
        presentation: OMISSION_PRESENTATIONS[omission.reason_code]
          ?? UNKNOWN_OMISSION_PRESENTATION,
      }),
    ))
    : [];

  return (
    <section
      aria-labelledby="source-context-heading"
      className="space-y-4 rounded-lg border border-gray-200 bg-white p-4 shadow-sm dark:border-gray-700 dark:bg-gray-800/70"
      data-testid="source-context-overview"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 id="source-context-heading" className="text-sm font-semibold text-gray-900 dark:text-white">
            Source context
          </h3>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-gray-500 dark:text-gray-400">
            What existed before this delivery and how each source may be interpreted.
          </p>
        </div>
        <div className="flex flex-wrap gap-1.5" aria-label="Context classification">
          {!investigationIsIncomplete && (
            <span className="rounded-full bg-cyan-50 px-2.5 py-1 text-[11px] font-medium text-cyan-700 dark:bg-cyan-950/40 dark:text-cyan-300">
              {deliveryContext}
            </span>
          )}
          <span className="rounded-full bg-gray-100 px-2.5 py-1 text-[11px] font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-200">
            {outcome}
          </span>
        </div>
      </div>

      {!investigationIsIncomplete && sourceContext.interpretation_rule && (
        <div className="rounded-md border border-blue-100 bg-blue-50/60 px-3 py-2 text-xs leading-5 text-blue-900 dark:border-blue-900 dark:bg-blue-950/20 dark:text-blue-200">
          <span className="font-semibold">Interpretation rule:</span>{' '}
          {sourceContext.interpretation_rule}
        </div>
      )}

      {!investigationIsIncomplete && overrideReason && (
        <div className="rounded-md border border-violet-200 bg-violet-50/70 px-3 py-2 text-xs leading-5 text-violet-900 dark:border-violet-900 dark:bg-violet-950/20 dark:text-violet-200">
          <span className="font-semibold">Delivery context override:</span>{' '}
          {overrideReason}
        </div>
      )}

      {noExistingImplementation && (
        <div
          className="rounded-md border border-emerald-200 bg-emerald-50/70 px-3 py-2 dark:border-emerald-900 dark:bg-emerald-950/20"
          role="note"
        >
          <p className="text-xs font-semibold text-emerald-900 dark:text-emerald-200">
            Code evidence is not applicable
          </p>
          {pureGreenfieldAbsence ? (
            <>
              <p className="mt-0.5 text-xs leading-5 text-emerald-800 dark:text-emerald-300">
                No existing implementation was found
              </p>
              <p className="text-xs leading-5 text-emerald-800 dark:text-emerald-300">
                This is the expected result for this delivery context.
              </p>
            </>
          ) : (
            <p className="mt-0.5 text-xs leading-5 text-emerald-800 dark:text-emerald-300">
              The investigation found no relevant existing implementation. This is the expected
              result for this delivery context.
            </p>
          )}
        </div>
      )}
      {absenceReported && !noExistingImplementation && (
        <div
          className="rounded-md border border-gray-200 bg-gray-50/70 px-3 py-2 dark:border-gray-700 dark:bg-gray-900/30"
          role="note"
        >
          <p className="text-xs font-semibold text-gray-800 dark:text-gray-200">
            Applicability not finalized
          </p>
          <p className="mt-0.5 text-xs leading-5 text-gray-600 dark:text-gray-400">
            The investigation reported no relevant existing implementation, but the authoritative
            contextual projection does not yet confirm this outcome.
          </p>
        </div>
      )}

      {investigationIsIncomplete && (
        <section
          className={`rounded-md border px-3 py-2 text-xs leading-5 ${sourceContext.investigation_outcome === 'partial'
            ? 'border-amber-200 bg-amber-50/70 text-amber-800 dark:border-amber-900 dark:bg-amber-950/20 dark:text-amber-300'
            : 'border-red-200 bg-red-50/70 text-red-800 dark:border-red-900 dark:bg-red-950/20 dark:text-red-300'}`}
          aria-labelledby="source-context-investigation-limit-heading"
          role="note"
        >
          <h4 id="source-context-investigation-limit-heading" className="font-semibold">
            {sourceContext.investigation_outcome === 'partial'
              ? 'Only partial source context was available'
              : 'The source investigation was unavailable'}
          </h4>
          <p className="mt-0.5">
            {sourceContext.investigation_outcome === 'partial'
              ? 'Do not treat the visible items as a complete inventory.'
              : 'No code context or applicability is implied.'}
          </p>
          {reportedOmissions.length > 0 ? (
            <ul className="mt-2 space-y-2" aria-label="Reported source omissions">
              {reportedOmissions.map(({ key, count, presentation }) => (
                <li key={key} className="rounded-md border border-current/20 bg-white/50 px-2.5 py-2 dark:bg-gray-900/30">
                  <p className="font-semibold">
                    {presentation.label} · {count} affected source item{count === 1 ? '' : 's'}
                  </p>
                  <p className="mt-0.5"><span className="font-semibold">Next:</span> {presentation.action}</p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2">
              No omission details were included in the current receipt. Review the receipt before retrying.
            </p>
          )}
        </section>
      )}

      <div>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Source roles
          </h4>
          <span className="text-[11px] text-gray-400">
            {authoritativeItemCount} recorded item{authoritativeItemCount === 1 ? '' : 's'}
          </span>
        </div>
        {populatedRoleCounts.length > 0 ? (
          <dl className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {populatedRoleCounts.map(({ role, countKey, badgeClassName }) => (
              <div
                key={role}
                className="flex items-center justify-between gap-3 rounded-md border border-gray-100 px-3 py-2 dark:border-gray-700"
              >
                <dt className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${badgeClassName}`}>
                  {codeEvidenceSourceRoleLabel(role)}
                </dt>
                <dd className="text-sm font-semibold tabular-nums text-gray-800 dark:text-gray-100">
                  {sourceContext.role_counts[countKey]}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            No source items were recorded for this investigation outcome.
          </p>
        )}
      </div>

      {sourceContext.items_not_current_implementation_count > 0 && (
        <p className="text-xs leading-5 text-gray-600 dark:text-gray-300">
          {sourceContext.items_not_current_implementation_count} source item
          {sourceContext.items_not_current_implementation_count === 1 ? ' is' : 's are'} context
          only and must not be presented as current implementation.
        </p>
      )}

      {sourceContext.classification_state.uncategorized_legacy_count > 0 && (
        <div
          className="rounded-md border border-orange-200 bg-orange-50/70 px-3 py-3 text-orange-800 dark:border-orange-900 dark:bg-orange-950/20 dark:text-orange-300 sm:flex sm:items-center sm:justify-between sm:gap-5"
          role="note"
        >
          <div>
            <p className="text-xs font-semibold leading-5">
              {sourceContext.classification_state.uncategorized_legacy_count} legacy item
              {sourceContext.classification_state.uncategorized_legacy_count === 1
                ? ' needs classification'
                : 's need classification'}
            </p>
            <p className="mt-0.5 text-xs leading-5">
              Their original Evidence is preserved. Choose what each observation means before
              using it for delivery decisions.
            </p>
          </div>
          {unclassifiedActionCount > 0 && onReviewUnclassifiedEvidence && (
            <button
              type="button"
              onClick={(event) => onReviewUnclassifiedEvidence(event.currentTarget)}
              className="btn btn-primary mt-3 inline-flex w-full shrink-0 items-center justify-center gap-1.5 text-xs sm:mt-0 sm:w-auto"
            >
              <ShieldCheck size={12} aria-hidden="true" /> Review unclassified Evidence
            </button>
          )}
        </div>
      )}

      {!sourceContext.technical_details_available && (
        <p className="text-xs text-gray-500 dark:text-gray-400">
          Technical source details are not included in this projection.
        </p>
      )}
      {boundedDetails && (
        <p className="text-xs text-gray-500 dark:text-gray-400" role="note">
          Showing {visibleContextItemCount} of {authoritativeItemCount} contextual items. Role counts
          above are authoritative; this bounded detail list is not a replacement for them.
        </p>
      )}

      {provenance && (
        <details className="rounded-md border border-gray-100 px-3 py-2 text-xs dark:border-gray-700">
          <summary className="cursor-pointer font-medium text-gray-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 dark:text-gray-300">
            Delivery context provenance
          </summary>
          <div className="mt-2 space-y-1 text-gray-500 dark:text-gray-400">
            <p>{provenance}</p>
          </div>
        </details>
      )}
    </section>
  );
}
