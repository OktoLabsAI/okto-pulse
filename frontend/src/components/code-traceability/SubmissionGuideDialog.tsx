import { BookOpenCheck, Clipboard, ShieldCheck, X } from 'lucide-react';
import toast from 'react-hot-toast';
import { useDialogFocusTrap } from '@/hooks/useDialogFocusTrap';
import { useEscapeToClose } from '@/hooks/useEscapeToClose';

interface Props {
  boardId: string;
  subjectType: 'refinement' | 'card';
  subjectId: string;
  subjectVersion: number;
  onClose: () => void;
}

function agentWorkflow({
  boardId,
  subjectType,
  subjectId,
  subjectVersion,
}: Omit<Props, 'onClose'>) {
  const subjectReference = `${subjectType}:${subjectId}@${subjectVersion}`;
  const resultKind = subjectType === 'refinement'
    ? 'scoped Code Evidence'
    : 'Implementation Target resolutions';

  return [
    `Code Traceability workflow for ${subjectReference}`,
    `Board: ${boardId}`,
    '',
    '1. Before investigating, determine whether your execution environment has suitable source-inspection capability and access to the relevant source.',
    '2. If access is unavailable or partial, submit a typed preflight receipt with that outcome and explicit omissions. Do not ask Pulse Community to inspect source.',
    '3. If access is available, perform deterministic checks in your own environment and submit a scoped preflight receipt with the observed revision, workspace fingerprint, capabilities and omissions.',
    `4. Submit ${resultKind} through authenticated Code Traceability agent tools, linked to that accepted preflight receipt.`,
    '5. Submit a newer preflight receipt when the observed source state changes.',
    '',
    'Boundary: Pulse Community only stores, projects and displays accepted records. It must not open, search, clone, sync or resolve source.',
  ].join('\n');
}

export function SubmissionGuideDialog(props: Props) {
  const { boardId, subjectType, subjectId, subjectVersion, onClose } = props;
  const focusTrap = useDialogFocusTrap(true, '[data-submission-guide-initial-focus]');
  const subjectLabel = subjectType === 'refinement' ? 'refinement' : 'task';
  const workflow = agentWorkflow({ boardId, subjectType, subjectId, subjectVersion });

  useEscapeToClose(onClose, { enabled: true, priority: 90 });

  return (
    <div
      className="fixed inset-0 z-[85] flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={focusTrap.dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="submission-guide-title"
        tabIndex={-1}
        onKeyDown={focusTrap.onKeyDown}
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl bg-white shadow-2xl dark:bg-gray-800"
      >
        <header className="flex items-start justify-between gap-3 border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <div className="min-w-0">
            <h2 id="submission-guide-title" className="flex items-center gap-2 text-base font-semibold text-gray-900 dark:text-white">
              <BookOpenCheck size={18} className="text-blue-500" /> Submission guide
            </h2>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Hand this workflow to an authenticated external agent for the current {subjectLabel}.
            </p>
          </div>
          <button
            data-submission-guide-initial-focus
            type="button"
            onClick={onClose}
            aria-label="Close submission guide"
            className="rounded-md p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <X size={18} />
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          <div className="flex items-start gap-2 rounded-lg border border-blue-200 bg-blue-50/60 px-3 py-2.5 text-xs leading-5 text-blue-800 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-300" role="note">
            <ShieldCheck size={15} className="mt-0.5 shrink-0" />
            <p>
              No source check runs from this dialog. The agent first checks its own capability and access, performs any investigation externally, then submits attestations for Pulse to validate and project.
            </p>
          </div>

          <ol className="space-y-3 text-xs leading-5 text-gray-600 dark:text-gray-300">
            <li className="rounded-md border border-gray-200 px-3 py-2.5 dark:border-gray-700">
              <span className="font-semibold text-gray-800 dark:text-gray-100">1 · Capability check</span>
              <p className="mt-0.5">The agent determines whether its own execution environment can inspect the relevant source.</p>
            </li>
            <li className="rounded-md border border-gray-200 px-3 py-2.5 dark:border-gray-700">
              <span className="font-semibold text-gray-800 dark:text-gray-100">2 · Scoped preflight</span>
              <p className="mt-0.5">The agent submits accessible, partial or unavailable with its observed state, capabilities and omissions.</p>
            </li>
            <li className="rounded-md border border-gray-200 px-3 py-2.5 dark:border-gray-700">
              <span className="font-semibold text-gray-800 dark:text-gray-100">3 · Agent-attested result</span>
              <p className="mt-0.5">
                The agent submits {subjectType === 'refinement' ? 'Code Evidence' : 'concrete target resolutions'} linked to the accepted preflight receipt.
              </p>
            </li>
          </ol>

          <div className="rounded-lg border border-gray-200 bg-gray-50/70 p-3 dark:border-gray-700 dark:bg-gray-900/40">
            <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">Context reference</p>
            <code className="mt-1 block break-all text-xs text-gray-700 dark:text-gray-200">
              {subjectType}:{subjectId}@{subjectVersion}
            </code>
          </div>
        </div>

        <footer className="flex justify-end gap-2 border-t border-gray-200 px-5 py-3 dark:border-gray-700">
          <button type="button" onClick={onClose} className="btn btn-secondary text-xs">
            Close
          </button>
          <button
            type="button"
            onClick={() => {
              void navigator.clipboard.writeText(workflow);
              toast.success('Agent workflow copied');
            }}
            className="btn btn-primary inline-flex items-center gap-1.5 text-xs"
          >
            <Clipboard size={13} /> Copy agent workflow
          </button>
        </footer>
      </div>
    </div>
  );
}
