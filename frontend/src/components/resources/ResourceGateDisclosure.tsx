import { useId, useState } from 'react';
import { ChevronDown, ChevronUp, Shield } from 'lucide-react';

import type { ResourceGateEntityType } from '@/types';
import { ResourceGateSummary } from './ResourceGateSummary';

interface ResourceGateDisclosureProps {
  boardId: string;
  entityType: ResourceGateEntityType;
  entityId: string;
  refreshKey?: string | number;
  defaultExpanded?: boolean;
}

/**
 * Compact, consistent Resource Gate entry point for entity Resources tabs.
 */
export function ResourceGateDisclosure({
  boardId,
  entityType,
  entityId,
  refreshKey = 0,
  defaultExpanded = false,
}: ResourceGateDisclosureProps) {
  const contentId = useId();
  const [expanded, setExpanded] = useState(defaultExpanded);

  return (
    <section className="overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900/30">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        aria-controls={contentId}
        className="flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left hover:bg-gray-50 dark:hover:bg-gray-800/50"
        data-testid="resource-gate-disclosure-toggle"
      >
        <span>
          <span className="flex items-center gap-1.5 text-sm font-semibold text-gray-800 dark:text-gray-100">
            <Shield size={14} aria-hidden="true" /> Resource Gate
          </span>
          <span className="mt-0.5 block text-xs text-gray-500 dark:text-gray-400">
            Architecture and Mockups are blocking; Knowledge is advisory.
          </span>
        </span>
        {expanded ? (
          <ChevronUp size={16} className="shrink-0 text-gray-500" aria-hidden="true" />
        ) : (
          <ChevronDown size={16} className="shrink-0 text-gray-500" aria-hidden="true" />
        )}
      </button>
      {expanded && (
        <div
          id={contentId}
          className="border-t border-gray-200 p-3 dark:border-gray-700"
        >
          <ResourceGateSummary
            key={refreshKey}
            boardId={boardId}
            entityType={entityType}
            entityId={entityId}
            compact
          />
        </div>
      )}
    </section>
  );
}
