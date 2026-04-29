/**
 * Slide 4 — closing nudge: ask the AI assistant to start the first
 * ideation. Pure presentational component — no state, no fetch, no
 * deep-link to external clients (BR4 of spec c90a6e85).
 */

export const START_IDEATION_SLIDE_TITLE_ID = 'onboarding-slide-4-title';

export function StartIdeationSlide() {
  return (
    <div>
      <div className="mono text-[10px] uppercase tracking-[0.2em] text-gray-400 dark:text-gray-500 mb-3.5">
        04 &middot; Get started
      </div>
      <h2
        id={START_IDEATION_SLIDE_TITLE_ID}
        className="text-2xl font-semibold tracking-tight text-gray-900 dark:text-white mb-4"
      >
        Now, start your first ideation on{' '}
        <span className="onboarding-accent">Okto Pulse</span>
      </h2>
      <p className="text-[13.5px] leading-snug text-gray-500 dark:text-gray-400">
        Ask your AI assistant to start an ideation on Okto Pulse. The board is
        ready, your agent is wired in, and a single prompt is enough to kick
        off the spec-driven loop.
      </p>
    </div>
  );
}
