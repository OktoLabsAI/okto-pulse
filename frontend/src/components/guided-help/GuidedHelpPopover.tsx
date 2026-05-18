import { type KeyboardEvent, useEffect, useId, useMemo, useRef } from 'react';
import { createPortal } from 'react-dom';
import { Check, ChevronLeft, ChevronRight } from 'lucide-react';

import { calculateGuidedHelpPosition, GUIDED_HELP_POPOVER_SIZE } from './positioning';
import type { GuidedHelpPopoverProps } from './types';

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => !element.hasAttribute('disabled'));
}

function arrowClass(arrowSide: string): string {
  if (arrowSide === 'top') {
    return 'left-1/2 top-[-5px] -translate-x-1/2 border-l border-t';
  }
  if (arrowSide === 'bottom') {
    return 'bottom-[-5px] left-1/2 -translate-x-1/2 border-b border-r';
  }
  if (arrowSide === 'left') {
    return 'left-[-5px] top-1/2 -translate-y-1/2 border-b border-l';
  }
  if (arrowSide === 'right') {
    return 'right-[-5px] top-1/2 -translate-y-1/2 border-r border-t';
  }
  return 'hidden';
}

export function GuidedHelpPopover({
  step,
  anchorRect,
  placement = 'bottom',
  progress,
  canGoBack = false,
  onBack,
  onNext,
  onDone,
  onSkipStep,
  onSkipAll,
}: GuidedHelpPopoverProps) {
  const titleId = useId();
  const bodyId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);
  const position = useMemo(
    () => calculateGuidedHelpPosition(anchorRect, placement),
    [anchorRect, placement],
  );
  const isLastStep = progress.current >= progress.total;

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    primaryRef.current?.focus();
    return () => {
      previouslyFocused?.focus();
    };
  }, [step.id]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      onSkipStep();
      return;
    }
    if (event.key !== 'Tab' || !dialogRef.current) return;

    const focusables = focusableElements(dialogRef.current);
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];

    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  if (typeof document === 'undefined') return null;

  return createPortal(
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      aria-describedby={bodyId}
      data-testid="guided-help-popover"
      data-placement={position.placement}
      data-fallback={position.fallback ? 'true' : 'false'}
      className="fixed z-[60] max-w-[calc(100vw-32px)] rounded-lg border border-cyan-500/60 bg-white p-4 text-sm text-gray-900 shadow-2xl shadow-cyan-950/20 outline-none dark:border-cyan-400/50 dark:bg-gray-950 dark:text-gray-100"
      style={{
        top: position.top,
        left: position.left,
        width: GUIDED_HELP_POPOVER_SIZE.width,
      }}
      tabIndex={-1}
      onKeyDown={onKeyDown}
    >
      <span
        aria-hidden="true"
        className={`absolute h-3 w-3 rotate-45 bg-white dark:bg-gray-950 ${arrowClass(position.arrowSide)}`}
      />
      <div className="mb-1 flex items-center justify-between gap-3">
        <p className="text-[11px] font-semibold uppercase text-cyan-700 dark:text-cyan-300">
          Guided help
        </p>
        <p className="shrink-0 text-xs text-gray-500 dark:text-gray-400">
          {progress.current} / {progress.total}
        </p>
      </div>
      <h2 id={titleId} className="text-sm font-semibold leading-5 text-gray-950 dark:text-white">
        {step.title}
      </h2>
      <p id={bodyId} className="mt-2 max-h-28 overflow-auto break-words text-xs leading-5 text-gray-600 dark:text-gray-300">
        {step.body}
      </p>
      {position.fallback && (
        <p className="mt-2 rounded-md bg-amber-50 px-2 py-1 text-[11px] leading-4 text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          This step is available even when its anchor is not visible.
        </p>
      )}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onBack}
            disabled={!canGoBack}
            className="inline-flex h-8 items-center gap-1 rounded-md border border-gray-200 px-2 text-xs font-medium text-gray-600 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-800 dark:text-gray-300 dark:hover:bg-gray-900"
          >
            <ChevronLeft size={14} aria-hidden="true" />
            Back
          </button>
          <button
            type="button"
            onClick={onSkipStep}
            className="h-8 rounded-md px-2 text-xs font-medium text-gray-500 transition-colors hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-900"
          >
            Skip step
          </button>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onSkipAll}
            className="h-8 rounded-md px-2 text-xs font-medium text-rose-600 transition-colors hover:bg-rose-50 dark:text-rose-300 dark:hover:bg-rose-950/40"
          >
            Skip all
          </button>
          <button
            ref={primaryRef}
            type="button"
            onClick={isLastStep ? onDone : onNext}
            className="inline-flex h-8 items-center gap-1 rounded-md bg-cyan-500 px-3 text-xs font-semibold text-gray-950 transition-colors hover:bg-cyan-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-950"
          >
            {isLastStep ? (
              <>
                <Check size={14} aria-hidden="true" />
                Done
              </>
            ) : (
              <>
                Next
                <ChevronRight size={14} aria-hidden="true" />
              </>
            )}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
