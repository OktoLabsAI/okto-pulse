/**
 * Slide 1 — hero treatment matching the landing page (`pulse.oktolabs.ai`):
 * brand-name and the ADLC concept rendered with the Pulse gradient via
 * `background-clip: text` (the `.accent` class).
 */

export const WELCOME_SLIDE_TITLE_ID = 'onboarding-slide-1-title';

export function WelcomeSlide() {
  return (
    <div className="text-center">
      <div className="mono text-[10px] uppercase tracking-[0.2em] text-gray-400 dark:text-gray-500 mb-4">
        OktoLabs &middot; Product no. 01
      </div>
      <h1
        id={WELCOME_SLIDE_TITLE_ID}
        className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-white mb-4 leading-tight"
      >
        Welcome to <span className="onboarding-accent">Okto Pulse</span>
      </h1>
      <p className="text-[15px] leading-relaxed text-gray-500 dark:text-gray-400 max-w-md mx-auto mb-6">
        Spec-driven project management for the{' '}
        <span className="onboarding-accent">Agentic Development Life Cycle</span>{' '}
        — where humans and AI agents plan, refine, and ship together on the same board.
      </p>
      <div className="inline-flex items-center gap-2 px-3.5 py-1.5 rounded-full border border-gray-200 dark:border-gray-700 text-xs text-gray-500 dark:text-gray-400 mono">
        <span className="w-1.5 h-1.5 rounded-full bg-gradient-to-r from-cyan-400 to-blue-500 inline-block" />
        ADLC &middot; Ideation &rarr; Refinement &rarr; Spec &rarr; Tasks
      </div>
    </div>
  );
}
