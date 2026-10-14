import { useEffect, type RefObject } from 'react';

/** Focus only the exact native row in this modal. Expanding a marked header is read-only. */
export function useNativeReferenceFocus(root: RefObject<HTMLElement>, reference: string | null | undefined, scopeKey: string) {
  useEffect(() => {
    const container = root.current;
    if (!container || !reference) return;
    let disposed = false;
    const focus = () => {
      const row = Array.from(container.querySelectorAll<HTMLElement>('[data-readiness-ref]'))
        .find(element => element.dataset.readinessRef === reference);
      if (!row) return;
      observer.disconnect();
      const expand = row.querySelector<HTMLElement>('[data-readiness-expand]');
      if (expand?.getAttribute('aria-expanded') === 'false') expand.click();
      queueMicrotask(() => {
        if (disposed || !row.isConnected) return;
        row.focus();
        row.scrollIntoView?.({ block: 'nearest', behavior: 'instant' });
      });
    };
    const observer = new MutationObserver(focus);
    observer.observe(container, { childList: true, subtree: true });
    focus();
    return () => { disposed = true; observer.disconnect(); };
  }, [root, reference, scopeKey]);
}
