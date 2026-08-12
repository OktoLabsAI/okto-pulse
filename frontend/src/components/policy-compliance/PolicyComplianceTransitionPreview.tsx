import { AlertTriangle, CheckCircle2, ShieldAlert } from 'lucide-react';

import {
  projectPolicyTransitions,
  type GovernedPolicyTransition,
  type PolicyTransitionRejection,
  type PolicyTransitionPreviewLoadState,
  type PolicyTransitionPresentationMode,
} from './policyTransitionPreviewModel';

export interface PolicyComplianceTransitionPreviewProps {
  preview: PolicyTransitionPreviewLoadState;
  rejection?: PolicyTransitionRejection | null;
  presentationMode?: PolicyTransitionPresentationMode;
}

function transitionTone(transition: GovernedPolicyTransition) {
  if (transition.decision.allowed === true) {
    return {
      label: 'Ready',
      icon: CheckCircle2,
      classes:
        'border-emerald-200 bg-emerald-50/60 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/20 dark:text-emerald-200',
    };
  }
  if (transition.decision.allowed === false) {
    return {
      label: 'Blocked',
      icon: ShieldAlert,
      classes:
        'border-red-200 bg-red-50/60 text-red-800 dark:border-red-800 dark:bg-red-950/20 dark:text-red-200',
    };
  }
  return {
    label: 'Subject required',
    icon: AlertTriangle,
    classes:
      'border-amber-200 bg-amber-50/60 text-amber-800 dark:border-amber-800 dark:bg-amber-950/20 dark:text-amber-200',
  };
}

function TransitionRejectionAlert({
  rejection,
  presentationMode,
}: {
  rejection: PolicyTransitionRejection | null;
  presentationMode: PolicyTransitionPresentationMode;
}) {
  if (!rejection) return null;
  if (presentationMode === 'lifecycle-edition') {
    const blocking = rejection.decision.blocking_metric_count ?? 0;
    return (
      <aside
        className="rounded-xl border border-red-200 bg-red-50 p-4 text-red-800 dark:border-red-800 dark:bg-red-950/30 dark:text-red-200"
        data-testid="policy-transition-rejection"
        role="alert"
      >
        <p className="text-sm font-semibold">
          The last transition attempt needs attention.
        </p>
        <p className="mt-1 text-xs">
          {blocking > 0
            ? `${blocking} blocking policy ${blocking === 1 ? 'issue must' : 'issues must'} be resolved for the current edition before trying again.`
            : 'A current Policy Compliance result is required before trying again.'}
        </p>
      </aside>
    );
  }
  return (
    <aside
      className="rounded-xl border border-red-200 bg-red-50 p-4 text-red-800 dark:border-red-800 dark:bg-red-950/30 dark:text-red-200"
      data-testid="policy-transition-rejection"
      role="alert"
    >
      <p className="text-sm font-semibold">
        The last transition attempt was rejected by Policy Compliance.
      </p>
      <p className="mt-1 text-xs">
        <span className="font-mono">{rejection.code}</span>
        {' · '}
        Receipts {rejection.decision.receipt_ids.join(', ') || 'none'}
        {' · '}
        {rejection.decision.currentness ?? 'currentness not recorded'}
        {' · '}
        {rejection.decision.blocking_metric_count ?? 0} blocking metrics
      </p>
      <p className="mt-1 break-all text-[11px]">
        Decision {rejection.decision.decision_digest}
      </p>
    </aside>
  );
}

export function PolicyComplianceTransitionPreview({
  preview,
  rejection = null,
  presentationMode = 'legacy',
}: PolicyComplianceTransitionPreviewProps) {
  if (preview.status === 'loading') {
    return (
      <div className="space-y-3">
        <TransitionRejectionAlert
          rejection={rejection}
          presentationMode={presentationMode}
        />
        <section
          className="rounded-xl border border-surface-200 p-4 dark:border-surface-700"
          aria-busy="true"
          data-testid="policy-transition-preview"
        >
          <p role="status" className="text-sm text-surface-500 dark:text-surface-400">
            {presentationMode === 'lifecycle-edition'
              ? 'Loading lifecycle readiness…'
              : 'Loading the authoritative lifecycle gate preview…'}
          </p>
        </section>
      </div>
    );
  }
  if (preview.status === 'error') {
    return (
      <div className="space-y-3">
        <TransitionRejectionAlert
          rejection={rejection}
          presentationMode={presentationMode}
        />
        <section
          className="rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-950/30"
          data-testid="policy-transition-preview"
        >
          <p role="alert" className="text-sm text-red-700 dark:text-red-300">
            {presentationMode === 'lifecycle-edition' ? (
              'Lifecycle readiness could not be loaded. Try again.'
            ) : (
              <>Policy Compliance gate preview is unavailable. {preview.error}</>
            )}
          </p>
        </section>
      </div>
    );
  }

  let projection;
  try {
    projection = projectPolicyTransitions(preview.transitions);
  } catch (caught) {
    return (
      <div className="space-y-3">
        <TransitionRejectionAlert
          rejection={rejection}
          presentationMode={presentationMode}
        />
        <section
          className="rounded-xl border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-950/30"
          data-testid="policy-transition-preview"
        >
          <p role="alert" className="text-sm text-red-700 dark:text-red-300">
            {presentationMode === 'lifecycle-edition' ? (
              'Lifecycle readiness could not be verified. The transition remains unavailable until this information can be verified.'
            ) : (
              <>
                {caught instanceof Error
                  ? caught.message
                  : 'Policy Compliance gate preview is malformed.'}
                {' '}No admission is inferred from this response.
              </>
            )}
          </p>
        </section>
      </div>
    );
  }

  if (presentationMode === 'lifecycle-edition') {
    return (
      <div className="space-y-3">
        <TransitionRejectionAlert
          rejection={rejection}
          presentationMode={presentationMode}
        />
        <section
          className="space-y-3 rounded-xl border border-surface-200 p-4 dark:border-surface-700"
          data-testid="policy-transition-preview"
        >
          <div>
            <h3 className="text-sm font-semibold text-surface-900 dark:text-white">
              Lifecycle readiness
            </h3>
            <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
              Shows whether the current edition can move forward based on Policy Compliance.
            </p>
          </div>

          {projection.governed.length === 0 ? (
            <p
              className="rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-300"
              data-testid="policy-transition-no-enforcement"
            >
              Policy Compliance does not apply to the currently available transitions.
            </p>
          ) : (
            <div className="space-y-2">
              {projection.governed.map((transition) => {
                const tone = transitionTone(transition);
                const Icon = tone.icon;
                const counts = transition.decision;
                const resultLabel = counts.currentness === 'current'
                  ? 'Current edition'
                  : counts.currentness === null
                    ? 'Not assessed'
                    : 'Previous edition';
                const failed = counts.failed_metric_count ?? 0;
                const blocking = counts.blocking_metric_count ?? 0;
                const advisory = counts.advisory_issue_count ?? 0;
                const waived = counts.waived_metric_count ?? 0;
                return (
                  <article
                    key={transition.toStatus}
                    className={`rounded-lg border p-3 ${tone.classes}`}
                    data-testid={`policy-transition-${transition.toStatus}`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-1.5">
                        <Icon size={15} aria-hidden="true" />
                        <span className="text-sm font-semibold">
                          To {transition.label}
                        </span>
                      </div>
                      <span className="rounded-full bg-white/70 px-2 py-0.5 text-[11px] font-semibold dark:bg-black/20">
                        {transition.decision.allowed === false
                          ? 'Needs attention'
                          : tone.label}
                      </span>
                    </div>
                    <dl className="mt-2 grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
                      <div>
                        <dt className="inline font-semibold">Result: </dt>
                        <dd className="inline">{resultLabel}</dd>
                      </div>
                      <div>
                        <dt className="inline font-semibold">Policy issues: </dt>
                        <dd className="inline">{failed}</dd>
                      </div>
                      <div>
                        <dt className="inline font-semibold">Blocking issues: </dt>
                        <dd className="inline">{blocking}</dd>
                      </div>
                      <div>
                        <dt className="inline font-semibold">Advisory observations: </dt>
                        <dd className="inline">{advisory}</dd>
                      </div>
                      {waived > 0 && (
                        <div>
                          <dt className="inline font-semibold">Waivers: </dt>
                          <dd className="inline">{waived}</dd>
                        </div>
                      )}
                    </dl>
                  </article>
                );
              })}
            </div>
          )}

          {projection.ungoverned.length > 0 && (
            <p
              className="flex items-start gap-1.5 rounded-lg border border-blue-200 bg-blue-50/60 p-3 text-xs text-blue-800 dark:border-blue-800 dark:bg-blue-950/20 dark:text-blue-200"
              data-testid="policy-transition-recovery-note"
            >
              <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
              <span>
                Policy Compliance does not apply to these available actions:{' '}
                {projection.ungoverned.map((item) => item.label).join(', ')}.
              </span>
            </p>
          )}
        </section>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <TransitionRejectionAlert
        rejection={rejection}
        presentationMode={presentationMode}
      />
      <section
        className="space-y-3 rounded-xl border border-surface-200 p-4 dark:border-surface-700"
        data-testid="policy-transition-preview"
      >
      <div>
        <h3 className="text-sm font-semibold text-surface-900 dark:text-white">
          Lifecycle gate preview
        </h3>
        <p className="mt-1 text-xs text-surface-500 dark:text-surface-400">
          This is the backend decision for the current subject and lifecycle
          edge. The UI never recomputes admission from a score.
        </p>
      </div>

      {projection.governed.length === 0 ? (
        <p
          className="rounded-lg border border-surface-200 bg-surface-50 p-3 text-xs text-surface-600 dark:border-surface-700 dark:bg-surface-900/40 dark:text-surface-300"
          data-testid="policy-transition-no-enforcement"
        >
          None of the currently exposed lifecycle edges is a frozen Policy
          Compliance enforcement point.
        </p>
      ) : (
        <div className="space-y-2">
          {projection.governed.map((transition) => {
            const tone = transitionTone(transition);
            const Icon = tone.icon;
            const counts = transition.decision;
            return (
              <article
                key={transition.toStatus}
                className={`rounded-lg border p-3 ${tone.classes}`}
                data-testid={`policy-transition-${transition.toStatus}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5">
                    <Icon size={15} aria-hidden="true" />
                    <span className="text-sm font-semibold">
                      To {transition.label}
                    </span>
                  </div>
                  <span className="rounded-full bg-white/70 px-2 py-0.5 text-[11px] font-semibold dark:bg-black/20">
                    {tone.label}
                  </span>
                </div>
                <dl className="mt-2 grid gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
                  <div>
                    <dt className="inline font-semibold">State: </dt>
                    <dd className="inline font-mono">{counts.state}</dd>
                  </div>
                  <div>
                    <dt className="inline font-semibold">Gate: </dt>
                    <dd className="inline font-mono">{transition.gate}</dd>
                  </div>
                  <div>
                    <dt className="inline font-semibold">Receipts: </dt>
                    <dd className="inline font-mono">
                      {counts.receipt_ids.join(', ') || 'none'}
                    </dd>
                  </div>
                  <div>
                    <dt className="inline font-semibold">Currentness: </dt>
                    <dd className="inline">{counts.currentness ?? 'not recorded'}</dd>
                  </div>
                  <div>
                    <dt className="inline font-semibold">Failed metrics: </dt>
                    <dd className="inline">{counts.failed_metric_count ?? 'n/a'}</dd>
                  </div>
                  <div>
                    <dt className="inline font-semibold">Blocking metrics: </dt>
                    <dd className="inline">{counts.blocking_metric_count ?? 'n/a'}</dd>
                  </div>
                  <div>
                    <dt className="inline font-semibold">Waived metrics: </dt>
                    <dd className="inline">{counts.waived_metric_count ?? 'n/a'}</dd>
                  </div>
                  <div>
                    <dt className="inline font-semibold">Skipped bindings: </dt>
                    <dd className="inline">{counts.skipped_binding_count ?? 'n/a'}</dd>
                  </div>
                  <div>
                    <dt className="inline font-semibold">Advisory issues: </dt>
                    <dd className="inline">{counts.advisory_issue_count ?? 'n/a'}</dd>
                  </div>
                  <div>
                    <dt className="inline font-semibold">Applicable blocking metrics: </dt>
                    <dd className="inline">
                      {counts.applicable_blocking_metric_count ?? 'n/a'}
                    </dd>
                  </div>
                </dl>
                {transition.blockedReason && (
                  <p className="mt-2 text-xs">
                    <span className="font-semibold">Combined gate result: </span>
                    {transition.blockedReason}
                  </p>
                )}
                {counts.currentness_reasons.length > 0 && (
                  <p className="mt-2 text-xs">
                    <span className="font-semibold">Stale because: </span>
                    {counts.currentness_reasons.join(', ')}
                  </p>
                )}
                {counts.binding_decisions.length > 0 && (
                  <div className="mt-3 space-y-1 border-t border-current/15 pt-2 text-xs">
                    {counts.binding_decisions.map((binding) => (
                      <p key={binding.binding_id}>
                        <span className="font-semibold">
                          {binding.guideline_id}
                        </span>
                        {' · '}
                        {binding.failed_metric_count}/{binding.applicable_metric_count}
                        {' failed · '}
                        {binding.skipped
                          ? 'human skip active'
                          : binding.inadmissibility_cause
                            ? binding.inadmissibility_cause
                            : binding.currentness ?? 'no assessment'}
                        {binding.diagnostic_codes.length > 0
                          ? ` · ${binding.diagnostic_codes.join(', ')}`
                          : ''}
                      </p>
                    ))}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}

      {projection.ungoverned.length > 0 && (
        <p
          className="flex items-start gap-1.5 rounded-lg border border-blue-200 bg-blue-50/60 p-3 text-xs text-blue-800 dark:border-blue-800 dark:bg-blue-950/20 dark:text-blue-200"
          data-testid="policy-transition-recovery-note"
        >
          <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>
            Policy Compliance does not block these currently exposed actions:
            {' '}
            {projection.ungoverned.map((item) => item.label).join(', ')}.
            Their own lifecycle and recovery gates still apply.
          </span>
        </p>
      )}
      </section>
    </div>
  );
}

export default PolicyComplianceTransitionPreview;
