/**
 * Slide 2 — numbered steps for connecting the first agent. UI labels
 * (Menu, Agents, Local Agent, Board Access) appear as mono pills so they
 * read as concrete UI affordances the user can find in the app.
 */

export const QUICK_START_SLIDE_TITLE_ID = 'onboarding-slide-2-title';

const STEPS: Array<{ title: React.ReactNode; subtitle: string }> = [
  {
    title: (
      <>
        Open the <Pill>Menu (☰)</Pill>
      </>
    ),
    subtitle: 'Top-left of the sidebar.',
  },
  {
    title: (
      <>
        Go to <Pill>Agents</Pill>
      </>
    ),
    subtitle: 'Manage identities and API keys.',
  },
  {
    title: (
      <>
        Create a new agent — <em className="not-italic text-gray-500 dark:text-gray-400">or</em>{' '}
        use the default <Pill>Local Agent</Pill>
      </>
    ),
    subtitle: 'Copy the API key — it is shown only once.',
  },
  {
    title: (
      <>
        Grant <Pill>Board Access</Pill>
      </>
    ),
    subtitle: 'From the Board Access tab, link the agent to this board.',
  },
];

function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="mono inline-block px-1.5 py-0.5 rounded bg-blue-500/10 text-blue-600 dark:text-blue-400 text-[0.92em]">
      {children}
    </span>
  );
}

export function QuickStartSlide() {
  return (
    <div>
      <div className="mono text-[10px] uppercase tracking-[0.2em] text-gray-400 dark:text-gray-500 mb-3.5">
        02 &middot; Quick start
      </div>
      <h2
        id={QUICK_START_SLIDE_TITLE_ID}
        className="text-2xl font-semibold tracking-tight text-gray-900 dark:text-white mb-5"
      >
        Set up your <span className="onboarding-accent">first agent</span>
      </h2>
      <ol className="space-y-3.5 list-none m-0 p-0">
        {STEPS.map((step, i) => (
          <li key={i} className="flex items-start gap-3.5">
            <span
              aria-hidden="true"
              className="flex-shrink-0 w-7 h-7 rounded-lg flex items-center justify-center text-[13px] font-semibold text-white"
              style={{ background: 'linear-gradient(135deg, #22d3ee, #3b82f6)' }}
            >
              {i + 1}
            </span>
            <div>
              <div className="text-[14.5px] font-medium text-gray-900 dark:text-white">
                {step.title}
              </div>
              <div className="text-[12.5px] text-gray-500 dark:text-gray-400 mt-0.5">
                {step.subtitle}
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
