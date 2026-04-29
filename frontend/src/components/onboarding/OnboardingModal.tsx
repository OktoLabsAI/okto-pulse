/**
 * OnboardingModal — first-run, 3-slide intro to Okto Pulse and the
 * Agentic Development Life Cycle. Shown once after Terms of Use accept;
 * dismissal (Get started, X, Esc, backdrop) marks completion.
 *
 * Visual identity follows the landing page (`pulse.oktolabs.ai`):
 * Pulse gradient accent (cyan→blue→violet) on key concepts, IBM Plex
 * typography (inherited from the global theme), light/dark parity.
 */

import { useCallback, useEffect, useReducer, useRef } from 'react';
import { Sun, Moon, X } from 'lucide-react';
import { useTheme } from '@/hooks/useTheme';
import { markCompleted } from './onboardingStorage';
import { WelcomeSlide, WELCOME_SLIDE_TITLE_ID } from './WelcomeSlide';
import { QuickStartSlide, QUICK_START_SLIDE_TITLE_ID } from './QuickStartSlide';
import {
  AssistantBindingSlide,
  ASSISTANT_BINDING_SLIDE_TITLE_ID,
} from './AssistantBindingSlide';
import {
  StartIdeationSlide,
  START_IDEATION_SLIDE_TITLE_ID,
} from './StartIdeationSlide';

type SlideState = 'slide-1' | 'slide-2' | 'slide-3' | 'slide-4' | 'closed';
type SlideEvent = { type: 'NEXT' | 'BACK' | 'CLOSE' };

const TOTAL_SLIDES = 4;

function reducer(state: SlideState, event: SlideEvent): SlideState {
  if (state === 'closed') return state;
  if (event.type === 'CLOSE') return 'closed';
  if (event.type === 'NEXT') {
    if (state === 'slide-1') return 'slide-2';
    if (state === 'slide-2') return 'slide-3';
    if (state === 'slide-3') return 'slide-4';
    // slide-4 is the last; closing happens via the CTA's close() path,
    // not via NEXT (e.g. ArrowRight on the last slide is a no-op — AC4
    // of spec c90a6e85).
    return state;
  }
  if (event.type === 'BACK') {
    if (state === 'slide-4') return 'slide-3';
    if (state === 'slide-3') return 'slide-2';
    if (state === 'slide-2') return 'slide-1';
    return state;
  }
  return state;
}

function slideIndex(state: SlideState): 1 | 2 | 3 | 4 | 0 {
  if (state === 'slide-1') return 1;
  if (state === 'slide-2') return 2;
  if (state === 'slide-3') return 3;
  if (state === 'slide-4') return 4;
  return 0;
}

function slideTitleId(state: SlideState): string {
  if (state === 'slide-2') return QUICK_START_SLIDE_TITLE_ID;
  if (state === 'slide-3') return ASSISTANT_BINDING_SLIDE_TITLE_ID;
  if (state === 'slide-4') return START_IDEATION_SLIDE_TITLE_ID;
  return WELCOME_SLIDE_TITLE_ID;
}

interface OnboardingModalProps {
  /** MCP URL displayed on slide 3 (passed by the parent that knows the active agent). */
  mcpUrl?: string;
  /** Fires when the modal is dismissed via any path (Get started, X, Esc, backdrop). */
  onClose: () => void;
}

export function OnboardingModal({ mcpUrl, onClose }: OnboardingModalProps) {
  const [state, dispatch] = useReducer(reducer, 'slide-1' as SlideState);
  const { theme, toggle: toggleTheme } = useTheme();
  const containerRef = useRef<HTMLDivElement>(null);
  const ctaRef = useRef<HTMLButtonElement>(null);
  const liveRegionRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    markCompleted();
    dispatch({ type: 'CLOSE' });
    onClose();
  }, [onClose]);

  // Keyboard contract: Esc closes; Left/Right navigate slides (when focus is
  // not in an input/textarea); Tab/Shift-Tab focus-trap inside the modal.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        close();
        return;
      }
      const target = e.target as HTMLElement | null;
      const inEditable =
        target?.tagName === 'INPUT' ||
        target?.tagName === 'TEXTAREA' ||
        target?.isContentEditable;
      if (e.key === 'ArrowRight' && !inEditable) {
        e.preventDefault();
        dispatch({ type: 'NEXT' });
        return;
      }
      if (e.key === 'ArrowLeft' && !inEditable) {
        e.preventDefault();
        dispatch({ type: 'BACK' });
        return;
      }
      if (e.key === 'Tab' && containerRef.current) {
        const focusables = containerRef.current.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        );
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [close]);

  // Initial focus on the primary CTA + announce the slide on every change.
  useEffect(() => {
    if (state === 'closed') return;
    ctaRef.current?.focus();
    if (liveRegionRef.current) {
      const labels = {
        'slide-1': 'Slide 1 of 4: Welcome to Okto Pulse',
        'slide-2': 'Slide 2 of 4: Quick start',
        'slide-3': 'Slide 3 of 4: Assistant binding',
        'slide-4': 'Slide 4 of 4: Start your first ideation',
      } as const;
      liveRegionRef.current.textContent = labels[state];
    }
  }, [state]);

  if (state === 'closed') return null;

  const idx = slideIndex(state);
  const isLast = idx === TOTAL_SLIDES;

  const handleBackdrop = (e: React.MouseEvent<HTMLDivElement>) => {
    if (e.target === e.currentTarget) close();
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={slideTitleId(state)}
      data-testid="onboarding-modal"
      data-slide={idx}
      className="fixed inset-0 z-[1100] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onMouseDown={handleBackdrop}
    >
      <div
        ref={containerRef}
        className="w-full max-w-xl bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700/40 rounded-2xl shadow-2xl overflow-hidden"
      >
        {/* Header */}
        <header className="flex items-center justify-between px-6 py-3.5 border-b border-gray-200 dark:border-gray-700/40">
          <span
            data-testid="onboarding-step-indicator"
            className="mono text-[11px] uppercase tracking-[0.15em] text-gray-400 dark:text-gray-500"
          >
            0{idx} / 04 &middot; Onboarding
          </span>
          <div className="flex items-center gap-2.5">
            <button
              type="button"
              onClick={toggleTheme}
              aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
              data-testid="onboarding-theme-toggle"
              className="w-8 h-8 rounded-lg border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors flex items-center justify-center"
            >
              {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
            </button>
            <button
              type="button"
              onClick={close}
              aria-label="Close onboarding"
              data-testid="onboarding-close-button"
              className="w-8 h-8 rounded-lg border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors flex items-center justify-center"
            >
              <X size={14} />
            </button>
          </div>
        </header>

        {/* Body */}
        <main className="px-10 py-9 min-h-[340px] text-gray-900 dark:text-white">
          {state === 'slide-1' && <WelcomeSlide />}
          {state === 'slide-2' && <QuickStartSlide />}
          {state === 'slide-3' && <AssistantBindingSlide mcpUrl={mcpUrl} />}
          {state === 'slide-4' && <StartIdeationSlide />}
        </main>

        {/* Footer */}
        <footer className="flex items-center justify-between px-6 py-3.5 border-t border-gray-200 dark:border-gray-700/40">
          <div
            role="tablist"
            aria-label="Slide indicator"
            className="flex items-center gap-1.5"
            data-testid="onboarding-dot-indicator"
          >
            {Array.from({ length: TOTAL_SLIDES }, (_, i) => {
              const active = i + 1 === idx;
              return (
                <span
                  key={i}
                  role="tab"
                  aria-selected={active}
                  aria-label={`Slide ${i + 1}${active ? ' (current)' : ''}`}
                  data-testid={`onboarding-dot-${i + 1}`}
                  data-active={active}
                  className={
                    active
                      ? 'w-8 h-1 rounded-full'
                      : 'w-2 h-1 rounded-full bg-gray-300 dark:bg-gray-700'
                  }
                  style={
                    active
                      ? { background: 'linear-gradient(90deg, #22d3ee, #3b82f6)' }
                      : undefined
                  }
                />
              );
            })}
          </div>
          <div className="flex items-center gap-2.5">
            {idx > 1 && (
              <button
                type="button"
                onClick={() => dispatch({ type: 'BACK' })}
                data-testid="onboarding-back-button"
                className="text-sm text-gray-500 dark:text-gray-400 px-3 py-1.5 rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
              >
                &larr; Back
              </button>
            )}
            <button
              ref={ctaRef}
              type="button"
              onClick={() => (isLast ? close() : dispatch({ type: 'NEXT' }))}
              data-testid="onboarding-primary-cta"
              className="text-[13px] font-semibold text-white px-4 py-2 rounded-lg border-0 cursor-pointer tracking-wide"
              style={{
                background: 'linear-gradient(90deg, #22d3ee, #3b82f6, #1e40af)',
              }}
            >
              {isLast ? 'Get started' : 'Next \u2192'}
            </button>
          </div>
        </footer>

        {/* Polite live region for slide-change + Copy success announcements */}
        <div
          ref={liveRegionRef}
          role="status"
          aria-live="polite"
          aria-atomic="true"
          className="sr-only"
          data-testid="onboarding-live-region"
        />
      </div>
    </div>
  );
}
