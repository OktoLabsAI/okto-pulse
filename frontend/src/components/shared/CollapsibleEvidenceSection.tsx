import { useId, type ReactNode } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';

export interface CollapsibleEvidenceSectionProps {
  title: string;
  description: string;
  expanded: boolean;
  onToggle: () => void;
  testId: string;
  children: ReactNode;
}

/**
 * Neutral disclosure primitive for immutable evidence collections.
 *
 * The component intentionally knows nothing about Quality Assessment,
 * Policy Compliance, or any other evidence domain. Domain-specific labels,
 * filters, pagination, and authorization stay with the owning panel.
 */
export function CollapsibleEvidenceSection({
  title,
  description,
  expanded,
  onToggle,
  testId,
  children,
}: CollapsibleEvidenceSectionProps) {
  const contentId = useId();

  return (
    <section className="rounded-xl border border-surface-200 bg-white p-3 dark:border-surface-700 dark:bg-surface-900/30">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={contentId}
        data-testid={`${testId}-toggle`}
        className="flex w-full items-center justify-between gap-3 rounded-lg p-1 text-left hover:bg-surface-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 focus-visible:ring-offset-2 dark:hover:bg-surface-800/60 dark:focus-visible:ring-offset-surface-900"
      >
        <span>
          <span className="block text-sm font-semibold text-surface-800 dark:text-surface-100">
            {title}
          </span>
          <span className="mt-0.5 block text-[11px] text-surface-500 dark:text-surface-400">
            {description}
          </span>
        </span>
        {expanded ? (
          <ChevronUp
            size={16}
            className="shrink-0 text-surface-500"
            aria-hidden="true"
          />
        ) : (
          <ChevronDown
            size={16}
            className="shrink-0 text-surface-500"
            aria-hidden="true"
          />
        )}
      </button>
      {expanded && (
        <div
          id={contentId}
          className="mt-3 space-y-3 border-t border-surface-200 pt-3 dark:border-surface-700"
          data-testid={`${testId}-content`}
        >
          {children}
        </div>
      )}
    </section>
  );
}

export default CollapsibleEvidenceSection;
