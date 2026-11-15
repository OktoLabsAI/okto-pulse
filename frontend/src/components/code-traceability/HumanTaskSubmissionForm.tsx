import { useEffect, useId, useRef, useState } from 'react';
import { useDashboardApi } from '@/services/api';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { HumanTaskSubmission, HumanTaskSubmissionResult, TaskDeliverySummary } from '@/types/task-delivery';
import { ContextualHelpLink } from '@/components/help/ContextualHelpLink';

interface Props {
  boardId: string; cardId: string; summary: TaskDeliverySummary;
  action: 'progress' | 'delivery_report'; requestedStatus?: 'validation' | 'done'; active?: boolean;
  onAccepted: (result: HumanTaskSubmissionResult) => void; onRefresh: () => void; onCancel: () => void;
  correction?: { submissionId: string; content: string; remaining?: string };
}
export function HumanTaskSubmissionForm({ boardId, cardId, summary, action, requestedStatus, active = true,
  onAccepted, onRefresh, onCancel, correction }: Props) {
  const api = useDashboardApi();
  const id = useId();
  const [content, setContent] = useState(correction?.content ?? '');
  const [remaining, setRemaining] = useState(correction?.remaining ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const [review, setReview] = useState(false);
  const [basis, setBasis] = useState({ card: summary.card_version, edition: summary.spec_edition });
  const intent = useRef<HumanTaskSubmission | null>(null);
  const locked = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const changed = basis.card !== summary.card_version || basis.edition !== summary.spec_edition;
  async function submit() {
    if (!active || locked.current || !content.trim() || (!uncertain && (review || changed))) return;
    locked.current = true; setBusy(true); setError('');
    const body = intent.current ?? {
      contract_version: 'human-task-submission/v1' as const, action,
      content, ...(remaining.trim() ? { remaining } : {}),
      expected_card_version: basis.card, expected_spec_edition: basis.edition,
      idempotency_key: crypto.randomUUID(),
      ...(action === 'delivery_report' ? { requested_status: requestedStatus ?? 'validation' } : {}),
      ...(correction ? { supersedes_submission_id: correction.submissionId } : {}),
    };
    intent.current = body;
    let accepted: HumanTaskSubmissionResult | undefined;
    try {
      accepted = await api.submitHumanTask(boardId, cardId, body);
      if (!mounted.current) return;
      setUncertain(false); intent.current = null;
    } catch (e) {
      if (!mounted.current) return;
      const definite = e instanceof AuthenticatedFetchError && e.status >= 400 && e.status < 500;
      setUncertain(!definite); setReview(definite);
      setError(definite ? e.message : 'The response was interrupted. Retry the same submission to confirm whether it was saved.');
    } finally {
      locked.current = false;
      if (mounted.current) setBusy(false);
    }
    // A presentation callback cannot turn a confirmed write into an uncertain retry.
    if (accepted && mounted.current) onAccepted(accepted);
  }
  return <form aria-label={action === 'progress' ? 'Task update' : 'Delivery report'} className="space-y-3 rounded-lg border p-4 dark:border-gray-600"
    onSubmit={event => { event.preventDefault(); void submit(); }}>
    <h4 className="font-semibold">{correction ? 'Correct an update' : action === 'progress' ? 'Record an update' : 'Submit delivery'}</h4>
    <p className="text-sm text-gray-600 dark:text-gray-300">Describe what you delivered. The application saves its audit record automatically.</p>
    <fieldset disabled={busy || uncertain || !active} className="space-y-3">
      <label className="block" htmlFor={`${id}-content`}>What was delivered or changed?</label>
      <textarea id={`${id}-content`} required maxLength={20000} value={content} rows={4} className="input w-full"
        onChange={e => { setContent(e.target.value); intent.current = null; }} />
      <label className="block" htmlFor={`${id}-remaining`}>What remains? <span className="text-sm">(optional)</span></label>
      <textarea id={`${id}-remaining`} maxLength={8000} value={remaining} rows={2} className="input w-full"
        onChange={e => { setRemaining(e.target.value); intent.current = null; }} />
    </fieldset>
    {correction && <p className="text-sm">This creates a new record linked to the original. The history is preserved.</p>}
    {error && <p role="alert" className="text-sm text-red-700 dark:text-red-300">{error}</p>}
    {!uncertain && (review || changed) && <div className="space-y-2 text-sm">
      <p>Review the current task and your text before submitting again. Your draft is preserved.</p>
      <button type="button" className="btn btn-secondary" onClick={onRefresh}>Refresh task</button>{' '}
      <button type="button" className="btn btn-secondary" onClick={() => {
        setBasis({ card: summary.card_version, edition: summary.spec_edition }); setReview(false); setError(''); intent.current = null;
      }}>I reviewed the current task</button>
    </div>}
    <div className="flex flex-wrap items-center gap-3">
      <button type="submit" className="btn btn-primary" disabled={busy || !active || !content.trim() || (!uncertain && (review || changed))}>
        {busy ? 'Saving…' : uncertain ? 'Retry same submission' : action === 'progress' ? 'Save update' : requestedStatus === 'done' ? 'Submit and complete' : 'Submit for validation'}
      </button>
      <button type="button" className="btn btn-secondary" disabled={busy || uncertain} onClick={onCancel}>Cancel</button>
      <ContextualHelpLink sectionId="delivery-progress" />
    </div>
  </form>;
}
