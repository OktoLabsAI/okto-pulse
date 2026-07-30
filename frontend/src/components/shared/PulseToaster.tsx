import { useEffect, useId, useMemo, useRef, useState } from 'react';
import {
  Check,
  Info,
  LoaderCircle,
  X,
} from 'lucide-react';
import toast, {
  resolveValue,
  Toaster,
  type Toast,
} from 'react-hot-toast';
import { isPulseErrorMessage } from '@/lib/toastError';

interface ToastText {
  summary: string;
  details: string | null;
  copyText: string;
}

const LONG_ERROR_THRESHOLD = 180;
const SUMMARY_TARGET_LENGTH = 132;

function splitErrorText(message: string): ToastText {
  const normalized = message.trim();
  const paragraphs = normalized
    .split(/\n\s*\n|\r?\n/)
    .map((part) => part.trim())
    .filter(Boolean);

  if (paragraphs.length > 1) {
    return {
      summary: paragraphs[0],
      details: paragraphs.slice(1).join('\n\n'),
      copyText: normalized,
    };
  }

  if (normalized.length > LONG_ERROR_THRESHOLD) {
    const sentenceEnd = normalized.slice(0, SUMMARY_TARGET_LENGTH + 24).search(
      /[.!?](?:\s|$)/,
    );
    const preferredEnd = sentenceEnd >= 60 ? sentenceEnd + 1 : -1;
    const whitespaceEnd = normalized.lastIndexOf(' ', SUMMARY_TARGET_LENGTH);
    const splitAt = preferredEnd > 0
      ? preferredEnd
      : whitespaceEnd > 60
        ? whitespaceEnd
        : SUMMARY_TARGET_LENGTH;

    return {
      summary: `${normalized.slice(0, splitAt).trimEnd()}…`,
      details: normalized,
      copyText: normalized,
    };
  }

  return {
    summary: normalized,
    details: null,
    copyText: normalized,
  };
}

function copyWithSelection(text: string): boolean {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  return copied;
}

async function copyToClipboard(text: string): Promise<void> {
  // Keep the synchronous path first so copying remains inside the user's
  // activation window even when the async Clipboard API is permission-gated.
  if (typeof document.execCommand === 'function' && copyWithSelection(text)) {
    return;
  }
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  throw new Error('Clipboard API is unavailable');
}

function toastTone(item: Toast) {
  if (item.type === 'success') {
    return {
      label: 'Success',
      icon: <Check size={29} strokeWidth={2.75} aria-hidden="true" />,
      iconClass:
        'bg-emerald-600 text-white shadow-emerald-950/20 '
        + 'dark:bg-emerald-500 dark:shadow-black/40',
    };
  }
  if (item.type === 'error') {
    return {
      label: 'Error',
      icon: (
        <span
          className="font-display text-[2rem] font-semibold leading-none"
          aria-hidden="true"
        >
          !
        </span>
      ),
      iconClass:
        'bg-red-600 text-white shadow-red-950/25 '
        + 'dark:bg-red-500 dark:shadow-black/40',
    };
  }
  if (item.type === 'loading') {
    return {
      label: 'In progress',
      icon: (
        <LoaderCircle
          size={27}
          className="animate-spin"
          aria-hidden="true"
        />
      ),
      iconClass:
        'bg-accent-600 text-white shadow-accent-950/20 '
        + 'dark:bg-accent-500 dark:shadow-black/40',
    };
  }
  return {
    label: 'Notice',
    icon: item.icon ?? <Info size={27} aria-hidden="true" />,
    iconClass:
      'bg-surface-700 text-white shadow-surface-950/20 '
      + 'dark:bg-surface-500 dark:shadow-black/40',
  };
}

export function PulseToastCard({ item }: { item: Toast }) {
  const detailsId = useId();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>(
    'idle',
  );
  const copyResetTimer = useRef<number | null>(null);
  const resolvedMessage = resolveValue(item.message, item);
  const errorText = useMemo(
    () => (
      item.type !== 'error'
        ? null
        : isPulseErrorMessage(resolvedMessage)
          ? {
              summary: resolvedMessage.props.summary,
              details: resolvedMessage.props.details,
              copyText: resolvedMessage.props.copyText,
            }
          : typeof resolvedMessage === 'string'
            ? splitErrorText(resolvedMessage)
            : null
    ),
    [item.type, resolvedMessage],
  );
  const tone = toastTone(item);

  useEffect(() => () => {
    if (copyResetTimer.current !== null) {
      window.clearTimeout(copyResetTimer.current);
    }
  }, []);

  const handleCopy = async () => {
    if (!errorText) return;
    if (copyResetTimer.current !== null) {
      window.clearTimeout(copyResetTimer.current);
    }
    try {
      await copyToClipboard(errorText.copyText);
      setCopyState('copied');
      copyResetTimer.current = window.setTimeout(
        () => setCopyState('idle'),
        1800,
      );
    } catch {
      setCopyState('failed');
      copyResetTimer.current = window.setTimeout(
        () => setCopyState('idle'),
        1800,
      );
    }
  };

  const messageAriaProps = item.type === 'error'
    ? { role: 'alert' as const, 'aria-live': 'assertive' as const }
    : item.type === 'success'
      ? { role: 'status' as const, 'aria-live': 'polite' as const }
      : item.ariaProps;

  return (
    <article
      data-visible={item.visible ? 'true' : 'false'}
      data-toast-type={item.type}
      data-expanded={detailsOpen ? 'true' : 'false'}
      className={[
        'pulse-toast pointer-events-auto relative isolate',
        'w-[min(25rem,calc(100vw-1.5rem))] overflow-visible',
      ].join(' ')}
    >
      <button
        type="button"
        aria-label="Dismiss notification"
        title="Dismiss"
        onClick={() => toast.dismiss(item.id)}
        className={[
          'absolute -left-4 -top-2 z-30 inline-flex h-7 w-7 items-center',
          'justify-center rounded-full border border-surface-300 bg-white',
          'text-surface-500 shadow-sm transition hover:bg-surface-100',
          'hover:text-surface-900 focus:outline-none focus-visible:ring-2',
          'focus-visible:ring-accent-500 dark:border-surface-600',
          'dark:bg-surface-900 dark:text-surface-300',
          'dark:hover:bg-surface-800 dark:hover:text-white',
        ].join(' ')}
      >
        <X size={14} strokeWidth={2.5} aria-hidden="true" />
      </button>

      <div
        data-testid="pulse-toast-status-icon"
        className={[
          'absolute left-2 top-2 z-20 flex h-16 w-16 items-center',
          'justify-center rounded-full shadow-lg ring-4 ring-white',
          'dark:ring-surface-900',
          tone.iconClass,
        ].join(' ')}
        aria-hidden="true"
      >
        {tone.icon}
      </div>

      <div
        className={[
          'min-h-20 rounded-l-[2.5rem] rounded-r-none border-y border-l',
          'border-r-0 border-surface-300 bg-white pl-20 pr-4',
          'py-3 text-surface-900',
          'shadow-[0_18px_45px_-18px_rgba(15,23,42,0.5)]',
          'dark:border-surface-700 dark:bg-surface-900',
          'dark:text-surface-50',
          'dark:shadow-[0_18px_50px_-18px_rgba(0,0,0,0.85)]',
        ].join(' ')}
      >
        <div className="min-w-0" {...messageAriaProps}>
          <span className="sr-only">{tone.label}: </span>
          <div className="break-words text-sm font-semibold leading-5">
            {errorText?.summary ?? resolvedMessage}
          </div>

          {errorText?.details && detailsOpen && (
            <pre
              id={detailsId}
              aria-label="Error details"
              tabIndex={0}
              className={[
                'mt-1.5 max-h-44 overflow-auto whitespace-pre-wrap',
                'break-words font-sans text-xs font-normal leading-[1.125rem]',
                'text-surface-600 outline-none focus-visible:ring-2',
                'focus-visible:ring-accent-500 dark:text-surface-300',
              ].join(' ')}
            >
              {errorText.details}
            </pre>
          )}
        </div>

        {errorText?.details && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              aria-label={copyState === 'copied'
                ? 'Copied'
                : copyState === 'failed'
                  ? 'Retry copying error'
                  : 'Copy error'}
              onClick={handleCopy}
              className={[
                'inline-flex min-h-7 items-center rounded-full border',
                'border-surface-300 px-3 py-1 text-[11px] font-semibold',
                'text-surface-600 transition hover:bg-surface-100',
                'hover:text-surface-900 focus:outline-none',
                'focus-visible:ring-2 focus-visible:ring-accent-500',
                'dark:border-surface-600 dark:text-surface-300',
                'dark:hover:bg-surface-800 dark:hover:text-white',
              ].join(' ')}
            >
              {copyState === 'copied'
                ? 'Copied'
                : copyState === 'failed'
                  ? 'Retry'
                  : 'Copy'}
            </button>

            <button
              type="button"
              aria-label={detailsOpen
                ? 'Hide error details'
                : 'Show error details'}
              aria-expanded={detailsOpen}
              aria-controls={detailsId}
              onClick={() => setDetailsOpen((open) => !open)}
              className={[
                'inline-flex min-h-7 items-center rounded-full border',
                'border-surface-300 px-3 py-1 text-[11px] font-semibold',
                'text-surface-600 transition hover:bg-surface-100',
                'hover:text-surface-900 focus:outline-none',
                'focus-visible:ring-2 focus-visible:ring-accent-500',
                'dark:border-surface-600 dark:text-surface-300',
                'dark:hover:bg-surface-800 dark:hover:text-white',
              ].join(' ')}
            >
              {detailsOpen ? 'Less' : 'Details'}
            </button>
          </div>
        )}
      </div>
    </article>
  );
}

/**
 * Application toast host. It is mounted in the zero-height anchor immediately
 * after the top command bar, so notifications cannot cover Menu or Refresh.
 */
export function PulseToaster() {
  return (
    <div className="relative z-[20000] h-0 shrink-0">
      <Toaster
        position="top-right"
        gutter={10}
        containerStyle={{
          position: 'absolute',
          inset: '0 0 auto 1.5rem',
          transform: 'translateY(0.75rem)',
          zIndex: 20000,
        }}
        toastOptions={{
          duration: 5000,
          success: { duration: 4000 },
          error: { duration: 8000 },
        }}
      >
        {(item) => <PulseToastCard item={item} />}
      </Toaster>
    </div>
  );
}
