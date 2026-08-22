import {
  type KeyboardEvent,
  type RefObject,
  useEffect,
  useRef,
} from 'react';

const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  ).filter(
    (element) =>
      element.getAttribute('aria-hidden') !== 'true'
      && !element.hasAttribute('hidden'),
  );
}

export interface DialogFocusTrap {
  dialogRef: RefObject<HTMLDivElement>;
  onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
}

/**
 * Keeps keyboard focus inside a mounted dialog and restores the opener on
 * unmount. Mutation permissions are deliberately unrelated to focusability:
 * when every action is disabled, the dialog container itself receives focus.
 */
export function useDialogFocusTrap(
  active = true,
  initialFocusSelector?: string,
  restoreFocusFallback?: () => HTMLElement | null,
): DialogFocusTrap {
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!active) return undefined;
    const previouslyFocused =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const focusInitial = () => {
      const container = dialogRef.current;
      if (!container) return;
      const preferred = initialFocusSelector
        ? container.querySelector<HTMLElement>(initialFocusSelector)
        : null;
      (preferred ?? focusableElements(container)[0] ?? container).focus();
    };
    const usesAnimationFrame = typeof requestAnimationFrame === 'function';
    const frame = usesAnimationFrame
      ? requestAnimationFrame(focusInitial)
      : window.setTimeout(focusInitial, 0);

    return () => {
      if (usesAnimationFrame && typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(frame);
      } else {
        window.clearTimeout(frame);
      }
      const explicitRestoreTarget = restoreFocusFallback?.() ?? null;
      const restoreTarget = explicitRestoreTarget?.isConnected
        ? explicitRestoreTarget
        : previouslyFocused?.isConnected
          ? previouslyFocused
          : null;
      if (restoreTarget?.isConnected) restoreTarget.focus();
    };
  }, [active, initialFocusSelector, restoreFocusFallback]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Tab' || !dialogRef.current) return;
    const focusables = focusableElements(dialogRef.current);
    if (focusables.length === 0) {
      event.preventDefault();
      dialogRef.current.focus();
      return;
    }
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

  return { dialogRef, onKeyDown };
}
