import { AlertTriangle, BookOpenCheck, Clipboard, ShieldCheck, X } from 'lucide-react';
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
  const resultSteps = subjectType === 'refinement'
    ? [
        '4. For every material source-code claim, call okto_pulse_submit_code_evidence bound to the accepted receipt, which carries revision/workspace identity. Include a normalized relative path, the narrowest useful selector (prefer a qualified symbol), the required source-content digest, an optional whole-file digest, and an excerpt hash only when a safe excerpt is supplied.',
        '5. Re-read each accepted Evidence record with okto_pulse_get_code_evidence. In every downstream Spec, call okto_pulse_link_code_evidence for each applicable inherited Evidence and target the specific Spec, FR, TR, AC, BR, Decision, API Contract, IR, OR or Test Scenario it supports; for inherited Evidence that is not applicable to that Spec, call okto_pulse_set_code_evidence_disposition with an explicit rationale.',
      ]
    : [
        '4. Ensure each material change has an Implementation Target created with okto_pulse_create_implementation_target. Resolve every active Target with okto_pulse_submit_implementation_target_resolution against the accepted receipt before implementation.',
        '5. Call okto_pulse_get_implementation_overlaps and resolve blocking overlap through a dependency or okto_pulse_acknowledge_implementation_overlap for the exact current resolution pair.',
        '6. After implementation, always run a new result-state preflight and call okto_pulse_submit_implementation_target_execution_receipt once per active Target (touched, created, replaced, deleted, superseded or not_touched with a reason).',
      ];

  return [
    `Code Traceability workflow for ${subjectReference}`,
    `Board: ${boardId}`,
    '',
    '1. Read okto-pulse://reference/code-traceability, then read the current subject context in full, retain its exact version and call okto_pulse_start_code_investigation for that subject/version.',
    '2. In your own execution environment, determine whether you can inspect the relevant source and perform bounded deterministic checks. Do not ask Pulse Community to inspect source.',
    '3. Submit one canonical receipt with okto_pulse_submit_code_investigation_receipt: accessible, partial or unavailable; the single-use challenge; and only capabilities actually exercised. For accessible/partial, include the observed source identity, revision and workspace fingerprint. For partial/unavailable, include bounded omissions; for unavailable, omit all source identity, revision and workspace fields. If delivery is uncertain, retry only the exact same payload with the same idempotency key.',
    ...resultSteps,
    `${subjectType === 'refinement' ? '6' : '7'}. Refetch the current subject and traceability projection. Entity-version, selector or source-head drift requires a new preflight; never reuse a stale receipt or resolution.`,
    '',
    'Advisory mode warning: missing Technical Anchors or Code Evidence does not block the transition, but Pulse cannot reconstruct them later. Entity or source drift can make the original investigation unusable and force the source survey, receipt and evidence work to be repeated.',
    '',
    'Boundary: Pulse Community only stores, projects and displays accepted records. It must not open, search, clone, sync or resolve source.',
  ].join('\n');
}

function resultSteps(subjectType: Props['subjectType']) {
  if (subjectType === 'refinement') {
    return [
      {
        title: '3 · Technical Evidence',
        body: 'Use okto_pulse_submit_code_evidence once per material code claim. Bind it to the accepted receipt, then record a normalized relative path, the narrowest useful selector and the required source-content digest.',
      },
      {
        title: '4 · Normative links',
        body: 'In each downstream Spec, link every applicable inherited Evidence to the specific requirement, decision, contract or scenario it supports. Record a reasoned disposition for inherited Evidence that is not applicable.',
      },
    ];
  }
  return [
      {
        title: '3 · Technical anchors',
        body: 'Use okto_pulse_create_implementation_target for each material change, then submit a current path/symbol resolution for every active Target.',
    },
    {
      title: '4 · Execution evidence',
      body: 'Resolve overlaps before work. After implementation, submit a current result receipt and one touched, created, replaced, deleted, superseded or justified not-touched disposition per Target.',
    },
  ];
}

export function SubmissionGuideDialog(props: Props) {
  const { boardId, subjectType, subjectId, subjectVersion, onClose } = props;
  const focusTrap = useDialogFocusTrap(true, '[data-submission-guide-initial-focus]');
  const subjectLabel = subjectType === 'refinement' ? 'refinement' : 'task';
  const workflow = agentWorkflow({ boardId, subjectType, subjectId, subjectVersion });
  const traceabilitySteps = resultSteps(subjectType);

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
              <span className="font-semibold text-gray-800 dark:text-gray-100">Receipt boundary</span>
              <p className="mt-0.5">The accepted receipt freezes the subject version, source scope and capabilities that authorize subsequent submissions.</p>
            </li>
            {traceabilitySteps.map((step) => (
              <li key={step.title} className="rounded-md border border-gray-200 px-3 py-2.5 dark:border-gray-700">
                <span className="font-semibold text-gray-800 dark:text-gray-100">{step.title}</span>
                <p className="mt-0.5">{step.body}</p>
              </li>
            ))}
          </ol>

          <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50/70 px-3 py-2.5 text-xs leading-5 text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200" role="note">
            <AlertTriangle size={15} className="mt-0.5 shrink-0" />
            <p>
              <strong>Advisory does not mean disposable.</strong> Missing Technical Anchors or Code Evidence will not block the transition, but Pulse cannot reconstruct them later. Version or source drift may force the investigation, receipt and evidence work to be repeated.
            </p>
          </div>

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
