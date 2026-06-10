/**
 * CandidateDecisionPanel — review surface for KG-03A.4/.5 candidate
 * decisions (KG-03A.6 / api_2d0d274d + api_4b5e0f1c).
 *
 * Responsibilities:
 *
 *   - Reads candidate decisions via ``useCandidateDecisions(boardId)``.
 *     ONE HTTP request per boardId/status change.
 *   - Renders one row per candidate with title, rationale, status pill,
 *     and provenance refs (source_ref + generation + session).
 *   - Status ``proposed`` exposes four explicit actions
 *     (promote_to_spec_decision / link_existing_decision / dismiss /
 *     no_action_required) wired to the command POST endpoint.
 *   - Terminal statuses (promoted/linked/dismissed/no_action_required)
 *     are read-only; the row shows the formal_decision_ref or
 *     dismissed_reason_code instead of action buttons.
 *
 * Invariants:
 *
 *   - Mutation flows through ``submitCandidateDecisionCommand`` only.
 *     The panel never writes directly to spec.decisions.
 *   - After a successful command, the panel calls ``refresh()`` to
 *     re-read the bounded list. No optimistic updates.
 *   - The panel does NOT inject instructional copy; it stays
 *     operational and dense.
 */

import { useCallback, useState } from 'react';

import {
  type CandidateDecisionCommandAction,
  type CandidateDecisionCommandRequest,
  type CandidateDecisionItem,
  type CandidateDecisionStatus,
  submitCandidateDecisionCommand,
} from '@/services/candidate-decisions-api';
import { useCandidateDecisions } from '@/hooks/useCandidateDecisions';

interface CandidateDecisionPanelProps {
  boardId: string | null;
}

const STATUS_TONE: Record<CandidateDecisionStatus, string> = {
  proposed:
    'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200',
  promoted:
    'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200',
  linked:
    'bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200',
  dismissed:
    'bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-200',
  no_action_required:
    'bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-200',
};

interface PendingCommandState {
  candidateId: string;
  action: CandidateDecisionCommandAction;
  specId: string;
  formalDecisionId: string;
  reasonCode: string;
}

const EMPTY_COMMAND: Omit<PendingCommandState, 'candidateId' | 'action'> = {
  specId: '',
  formalDecisionId: '',
  reasonCode: '',
};

export function CandidateDecisionPanel({
  boardId,
}: CandidateDecisionPanelProps) {
  const { items, counts, loading, error, refresh } = useCandidateDecisions(
    boardId,
  );
  const [pending, setPending] = useState<PendingCommandState | null>(null);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const startCommand = useCallback(
    (candidate: CandidateDecisionItem, action: CandidateDecisionCommandAction) => {
      setSubmitError(null);
      setPending({
        candidateId: candidate.candidate_id,
        action,
        ...EMPTY_COMMAND,
      });
    },
    [],
  );

  const cancelCommand = useCallback(() => {
    setPending(null);
    setSubmitError(null);
  }, []);

  const submitCommand = useCallback(async () => {
    if (!pending || !boardId) return;
    const request: CandidateDecisionCommandRequest = {
      board_id: boardId,
      action: pending.action,
    };
    if (pending.action === 'promote_to_spec_decision') {
      if (!pending.specId.trim()) {
        setSubmitError('spec_id is required');
        return;
      }
      request.spec_id = pending.specId.trim();
    } else if (pending.action === 'link_existing_decision') {
      if (!pending.specId.trim() || !pending.formalDecisionId.trim()) {
        setSubmitError('spec_id and formal_decision_id are required');
        return;
      }
      request.spec_id = pending.specId.trim();
      request.formal_decision_id = pending.formalDecisionId.trim();
    } else {
      if (!pending.reasonCode.trim()) {
        setSubmitError('reason_code is required');
        return;
      }
      request.reason_code = pending.reasonCode.trim();
    }
    try {
      setSubmitting(true);
      setSubmitError(null);
      await submitCandidateDecisionCommand(pending.candidateId, request);
      setPending(null);
      refresh();
    } catch (err) {
      const message = (err as Error)?.message ?? 'command failed';
      setSubmitError(message);
    } finally {
      setSubmitting(false);
    }
  }, [pending, boardId, refresh]);

  if (!boardId) {
    return null;
  }

  return (
    <section
      data-testid="candidate-decision-panel"
      className="rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900"
    >
      <header className="flex items-center justify-between border-b border-slate-200 px-3 py-2 text-xs uppercase tracking-wide text-slate-600 dark:border-slate-700 dark:text-slate-300">
        <span>Candidate decisions</span>
        <span data-testid="candidate-decision-counts">
          {counts.proposed} proposed · {counts.promoted} promoted ·{' '}
          {counts.linked} linked · {counts.dismissed} dismissed ·{' '}
          {counts.no_action_required} no-action · {counts.total} total
        </span>
      </header>
      {loading && (
        <div className="px-3 py-2 text-xs text-slate-500">Loading…</div>
      )}
      {error && (
        <div
          data-testid="candidate-decision-error"
          className="px-3 py-2 text-xs text-red-600"
        >
          {error.message}
        </div>
      )}
      {!loading && !error && items.length === 0 && (
        <div className="px-3 py-2 text-xs text-slate-500">
          No candidate decisions for this board.
        </div>
      )}
      <ul className="divide-y divide-slate-200 dark:divide-slate-700">
        {items.map((candidate) => {
          const tone = STATUS_TONE[candidate.status];
          const isProposed = candidate.status === 'proposed';
          const isPendingForThis =
            pending !== null && pending.candidateId === candidate.candidate_id;
          return (
            <li
              key={candidate.candidate_id}
              data-testid="candidate-decision-row"
              data-candidate-id={candidate.candidate_id}
              data-status={candidate.status}
              className="px-3 py-2 text-sm"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span
                      data-testid="candidate-decision-status"
                      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${tone}`}
                    >
                      {candidate.status}
                    </span>
                    <span className="truncate font-medium text-slate-900 dark:text-slate-100">
                      {candidate.title}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-600 dark:text-slate-300">
                    {candidate.rationale}
                  </p>
                  <p className="mt-1 text-[10px] text-slate-500">
                    src {candidate.source_ref} · gen{' '}
                    {candidate.source_generation_id.slice(0, 8)} · sess{' '}
                    {candidate.consolidation_session_id.slice(0, 8)}
                    {candidate.formal_decision_ref && (
                      <>
                        {' · formal '}
                        <span data-testid="candidate-decision-formal-ref">
                          {candidate.formal_decision_ref}
                        </span>
                      </>
                    )}
                    {candidate.dismissed_reason_code && (
                      <> · reason {candidate.dismissed_reason_code}</>
                    )}
                  </p>
                </div>
                {isProposed && !isPendingForThis && (
                  <div
                    data-testid="candidate-decision-actions"
                    className="flex flex-shrink-0 flex-wrap gap-1"
                  >
                    <ActionButton
                      label="Promote"
                      onClick={() =>
                        startCommand(candidate, 'promote_to_spec_decision')
                      }
                    />
                    <ActionButton
                      label="Link"
                      onClick={() =>
                        startCommand(candidate, 'link_existing_decision')
                      }
                    />
                    <ActionButton
                      label="Dismiss"
                      onClick={() => startCommand(candidate, 'dismiss')}
                    />
                    <ActionButton
                      label="No action"
                      onClick={() =>
                        startCommand(candidate, 'no_action_required')
                      }
                    />
                  </div>
                )}
              </div>
              {isPendingForThis && pending !== null && (
                <CommandForm
                  state={pending}
                  onChange={setPending}
                  onCancel={cancelCommand}
                  onSubmit={submitCommand}
                  submitting={submitting}
                  error={submitError}
                />
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

interface ActionButtonProps {
  label: string;
  onClick: () => void;
}

function ActionButton({ label, onClick }: ActionButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-100 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200 dark:hover:bg-slate-700"
    >
      {label}
    </button>
  );
}

interface CommandFormProps {
  state: PendingCommandState;
  onChange: (state: PendingCommandState) => void;
  onCancel: () => void;
  onSubmit: () => void;
  submitting: boolean;
  error: string | null;
}

function CommandForm({
  state,
  onChange,
  onCancel,
  onSubmit,
  submitting,
  error,
}: CommandFormProps) {
  const needsSpec =
    state.action === 'promote_to_spec_decision' ||
    state.action === 'link_existing_decision';
  const needsFormal = state.action === 'link_existing_decision';
  const needsReason =
    state.action === 'dismiss' || state.action === 'no_action_required';
  return (
    <div
      data-testid="candidate-decision-command-form"
      data-action={state.action}
      className="mt-2 flex flex-wrap items-center gap-2 rounded border border-dashed border-slate-300 bg-slate-50 p-2 text-xs dark:border-slate-700 dark:bg-slate-800"
    >
      {needsSpec && (
        <input
          aria-label="spec_id"
          data-testid="candidate-decision-spec-id"
          type="text"
          placeholder="spec_id"
          value={state.specId}
          onChange={(e) => onChange({ ...state, specId: e.target.value })}
          className="rounded border border-slate-300 px-1.5 py-0.5 text-xs dark:border-slate-600 dark:bg-slate-900"
        />
      )}
      {needsFormal && (
        <input
          aria-label="formal_decision_id"
          data-testid="candidate-decision-formal-decision-id"
          type="text"
          placeholder="dec_..."
          value={state.formalDecisionId}
          onChange={(e) =>
            onChange({ ...state, formalDecisionId: e.target.value })
          }
          className="rounded border border-slate-300 px-1.5 py-0.5 text-xs dark:border-slate-600 dark:bg-slate-900"
        />
      )}
      {needsReason && (
        <input
          aria-label="reason_code"
          data-testid="candidate-decision-reason-code"
          type="text"
          placeholder="reason_code"
          value={state.reasonCode}
          onChange={(e) => onChange({ ...state, reasonCode: e.target.value })}
          className="rounded border border-slate-300 px-1.5 py-0.5 text-xs dark:border-slate-600 dark:bg-slate-900"
        />
      )}
      <button
        type="button"
        data-testid="candidate-decision-command-submit"
        onClick={onSubmit}
        disabled={submitting}
        className="rounded bg-blue-600 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-white hover:bg-blue-700 disabled:opacity-60"
      >
        {submitting ? 'Submitting…' : state.action}
      </button>
      <button
        type="button"
        data-testid="candidate-decision-command-cancel"
        onClick={onCancel}
        disabled={submitting}
        className="rounded border border-slate-300 px-2 py-0.5 text-[11px] font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-60 dark:border-slate-600 dark:text-slate-200 dark:hover:bg-slate-700"
      >
        Cancel
      </button>
      {error && (
        <span
          data-testid="candidate-decision-command-error"
          className="text-[11px] text-red-600"
        >
          {error}
        </span>
      )}
    </div>
  );
}
