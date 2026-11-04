import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, CircleCheck, CircleX, Clock3, Info } from 'lucide-react';
import { ContextualHelpLink } from '@/components/help/ContextualHelpLink';
import type { CardDeliveryBatchDraft } from '@/types/delivery-evidence';
import type { TaskDeliveryState } from '@/types/task-delivery';
import { CardLedgerPanel } from './CardLedgerPanel';
import { HumanTaskSubmissionForm } from './HumanTaskSubmissionForm';
import { TaskRequirementControl } from './TaskRequirementControl';
import { TaskDeliveryDiagnosis } from './TaskDeliveryDiagnosis';
import { useTaskDelivery } from './useTaskDelivery';
import type { ReadinessCardLocation } from './readinessNavigation';

interface Props {
  active?: boolean; revision?: number; refreshRequest?: number; focusDiagnosis?: boolean; focusRecordId?: string;
  onOpenReadinessCard?: (id: string, target?: ReadinessCardLocation) => void;
  boardId: string; card: { id: string; card_type: string; spec_id: string; title?: string };
  canRecord?: boolean; canTest?: boolean; canWaiver?: boolean; canProgress?: boolean;
  onChanged?: () => void; onStage?: (draft: CardDeliveryBatchDraft) => void; onOpenTests?: () => void;
  reportRequest?: { status: 'validation' | 'done'; requestId: string };
  onReportClosed?: () => void;
}
const titles: Record<TaskDeliveryState, string> = {
  updating_or_unknown: 'Checking delivery', optional_or_waived: 'Not required', verification_failed: 'Verification failed',
  historical_done: 'Previously completed', implementation_pending: 'Work remaining', verification_pending: 'Awaiting verification',
  completion_pending: 'Awaiting completion', delivered: 'Delivered', ready: 'Ready to complete',
};

export function CardDeliveryDoDPanel({ boardId, card, active = true, revision = 0, refreshRequest = 0, canWaiver = false,
  canProgress = false, onChanged, onOpenTests, focusDiagnosis, focusRecordId, onOpenReadinessCard, reportRequest, onReportClosed }: Props) {
  const projection = useTaskDelivery(boardId, card.spec_id, card.id, active, revision, refreshRequest);
  const [details, setDetails] = useState(Boolean(focusDiagnosis || focusRecordId));
  const [form, setForm] = useState<'progress' | 'delivery_report' | null>(null);
  const [notice, setNotice] = useState('');
  const [correction, setCorrection] = useState<{ submissionId: string; content: string; remaining?: string }>();
  const [detailsVisited, setDetailsVisited] = useState(Boolean(focusDiagnosis || focusRecordId));
  const [reportStatus, setReportStatus] = useState<'validation' | 'done'>('validation');
  const button = useRef<HTMLButtonElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const summary = projection.value?.task_delivery;
  const current = projection.value?.projection.freshness === 'current' && !projection.error && !projection.loading;
  const state = current && summary ? summary.state : 'updating_or_unknown';
  const action = current ? summary?.next_action : null;
  const Icon = state === 'delivered' ? CircleCheck : state === 'verification_failed' ? CircleX
    : state === 'updating_or_unknown' || state.endsWith('_pending') ? Clock3 : Info;
  useEffect(() => { if (focusDiagnosis || focusRecordId) setDetails(true); }, [focusDiagnosis, focusRecordId]);
  useEffect(() => { if (details) setDetailsVisited(true); }, [details]);
  useEffect(() => { if (reportRequest) { setReportStatus(reportRequest.status); setForm('delivery_report'); } }, [reportRequest]);
  useEffect(() => { if (details && (focusDiagnosis || focusRecordId)) heading.current?.focus(); }, [details, focusDiagnosis, focusRecordId]);
  function primary() {
    if (action?.kind === 'retry') projection.retry();
    else if (action?.kind === 'record_progress') { setCorrection(undefined); setForm('progress'); }
    else if (action?.kind === 'submit_report' || action?.kind === 'conclude') {
      setReportStatus(action.kind === 'conclude' ? 'done' : 'validation'); setForm('delivery_report');
    }
    else setDetails(true);
  }
  return <section aria-label="Task delivery" className="min-w-0 space-y-5 text-sm">
    <div className="flex items-center justify-between gap-4"><h3 className="text-lg font-semibold">Delivery</h3>
      <ContextualHelpLink sectionId="delivery-status" /></div>
    <div className={`flex items-start gap-3 rounded-xl border p-5 ${state === 'delivered' ? 'border-green-300 bg-green-50 dark:bg-green-950/20'
      : state === 'verification_failed' ? 'border-red-300 bg-red-50 dark:bg-red-950/20' : 'border-gray-200 dark:border-gray-700'}`}>
      <Icon size={30} aria-hidden="true" className="shrink-0" />
      <div className="space-y-1"><h4 className="font-semibold" role="status">{projection.error ? 'Delivery unavailable' : titles[state]}</h4>
        <p>{projection.error || (current ? summary?.reason : 'The current assessment is being prepared. Delivery has not been confirmed.')}</p>
      </div>
    </div>
    {summary?.last_update && <div className="space-y-2">
      <h4 className="font-semibold">Latest update</h4>
      <p className="whitespace-pre-wrap break-words">{summary.last_update.content}</p>
      <h4 className="font-semibold">Remaining</h4>
      <p className="whitespace-pre-wrap break-words">{summary.last_update.remaining || 'No remaining work was described in this update. See the delivery status above.'}</p>
      {summary.last_update.text_truncated && <p>Select Show details to read the full update.</p>}
    </div>}
    {notice && <p role="status">{notice}</p>}
    {!form && (projection.error ? <button className="btn btn-primary" onClick={projection.retry}>Retry delivery assessment</button>
      : action && (action.owner !== 'user' || canProgress || action.kind === 'open_details' || action.kind === 'retry')
        ? <button ref={button} type="button" className="btn btn-primary" onClick={primary}>{action.label}</button> : null)}
    {form && summary && <HumanTaskSubmissionForm key={`${card.id}:${form}:${correction?.submissionId ?? ''}`} boardId={boardId} cardId={card.id} summary={summary}
      correction={form === 'progress' ? correction : undefined}
      action={form} requestedStatus={reportStatus} active={active && current}
      onRefresh={projection.retry} onCancel={() => { setForm(null); onReportClosed?.(); button.current?.focus(); }}
      onAccepted={result => { setForm(null); onReportClosed?.(); setNotice(result.replayed ? 'Submission confirmed.' : 'Submission saved.'); projection.refresh(); onChanged?.(); button.current?.focus(); }} />}
    <div className="flex">
      <button type="button" aria-expanded={details} aria-controls={`delivery-details-${card.id}`} className="btn btn-secondary inline-flex items-center gap-2"
        onClick={() => setDetails(open => !open)}>
        {details ? <ChevronUp size={16} aria-hidden="true" /> : <ChevronDown size={16} aria-hidden="true" />}
        {details ? 'Hide details' : 'Show details'}
      </button>
    </div>
    <div id={`delivery-details-${card.id}`} hidden={!details}>
      {detailsVisited && summary && <div className="space-y-5">
        <h4 ref={heading} tabIndex={-1} className="font-semibold">Delivery details</h4>
        <p>Updates are declarations. Technical verification and completion are assessed separately.</p>
        {canProgress && summary.historical_status === 'in_progress' && !form && <button type="button" className="btn btn-secondary"
          onClick={() => { setCorrection(undefined); setForm('progress'); }}>Record an update</button>}
        <TaskDeliveryDiagnosis boardId={boardId} specId={card.spec_id} cardId={card.id} active={active && details}
          revision={`${projection.value?.projection.generation_id ?? ''}:${refreshRequest}`} onOpenReadinessCard={onOpenReadinessCard} />
        <CardLedgerPanel boardId={boardId} cardId={card.id} specId={card.spec_id} edition={summary.spec_edition} initialRecordId={focusRecordId}
          active={active && details} onCorrect={canProgress ? entry => { setCorrection(entry); setForm('progress'); } : undefined} />
        <TaskRequirementControl key={card.id} boardId={boardId} specId={card.spec_id} cards={[{ id: card.id, title: card.title || 'This task' }]}
          active={active && details} canEdit={canWaiver} revision={`${revision}:${refreshRequest}`} onChanged={() => { projection.refresh(); onChanged?.(); }} />
        {onOpenTests && <button type="button" className="btn btn-secondary" onClick={onOpenTests}>View test results</button>}
      </div>}
    </div>
  </section>;
}
