/**
 * TermsAcceptanceModal — full-screen blocker shown on first run (or when
 * the cached terms hash no longer matches). The Accept button is enabled
 * only after the user scrolls to the end of the legal text — a deliberate
 * friction step so consent is informed.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ScrollText, Check } from 'lucide-react';
import { TERMS_BODY, TERMS_VERSION } from '@/constants/terms';
import { MarkdownContent } from '@/components/shared/MarkdownContent';

interface TermsAcceptanceModalProps {
  onAccept: () => void;
}

const SCROLL_TOLERANCE_PX = 24;

export function TermsAcceptanceModal({ onAccept }: TermsAcceptanceModalProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [scrolledToEnd, setScrolledToEnd] = useState(false);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distance <= SCROLL_TOLERANCE_PX) setScrolledToEnd(true);
  }, []);

  useEffect(() => {
    // If the content is already short enough to fit, treat as scrolled.
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distance <= SCROLL_TOLERANCE_PX) setScrolledToEnd(true);
  }, []);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="terms-modal-title"
      data-testid="terms-acceptance-modal"
      className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/70 backdrop-blur-sm p-4"
    >
      <div className="w-full max-w-3xl max-h-[90vh] flex flex-col bg-white dark:bg-surface-900 rounded-xl shadow-2xl border border-surface-200/50 dark:border-surface-700/40 overflow-hidden">
        <div className="flex items-center gap-3 px-6 py-4 border-b border-surface-200/50 dark:border-surface-700/40">
          <ScrollText className="text-accent-500" size={22} />
          <div className="flex-1">
            <h2 id="terms-modal-title" className="text-lg font-semibold text-gray-900 dark:text-white">
              Terms of Use & License
            </h2>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Version {TERMS_VERSION} — read to the end to enable acceptance
            </p>
          </div>
        </div>
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          data-testid="terms-modal-scroll"
          className="flex-1 overflow-y-auto px-6 py-4 prose prose-sm dark:prose-invert max-w-none"
        >
          <MarkdownContent content={TERMS_BODY} />
        </div>
        <div className="px-6 py-3 border-t border-surface-200/50 dark:border-surface-700/40 flex items-center justify-between bg-surface-50 dark:bg-surface-800/50">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {scrolledToEnd ? 'Ready to accept.' : 'Scroll to the end to enable.'}
          </p>
          <button
            type="button"
            onClick={onAccept}
            disabled={!scrolledToEnd}
            data-testid="terms-modal-accept"
            className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg
              bg-accent-500 text-white shadow-sm hover:bg-accent-600
              disabled:bg-gray-300 dark:disabled:bg-gray-700 disabled:cursor-not-allowed disabled:text-gray-500"
          >
            <Check size={14} />
            I have read, understood and accept
          </button>
        </div>
      </div>
    </div>
  );
}
