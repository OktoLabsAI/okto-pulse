/**
 * HierarchicalList — generic renderer that switches between list and grid
 * layouts and optionally groups items by a parent key with collapsible
 * group headers.
 *
 * Headless data shape: pass an array of items + a `getGroupKey(item)`
 * resolver + a `getGroupTitle(groupKey)` resolver. Items with the same
 * group key cluster together; items without a group key fall into a
 * configurable "Standalone" bucket.
 */

import { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { ViewMode } from '@/hooks/useViewMode';

export interface HierarchicalListProps<T> {
  items: readonly T[];
  viewMode: ViewMode;
  /** Stable key used in React `key=` and to track collapse state. */
  getItemKey: (item: T) => string;
  /** Renders one item card. Receives the current viewMode for layout hints. */
  renderItem: (item: T, viewMode: ViewMode) => React.ReactNode;

  /** Group items by a parent key. Return null/undefined for ungrouped items. */
  getGroupKey?: (item: T) => string | null | undefined;
  /** Display title for a group key. Falls back to the key itself. */
  getGroupTitle?: (groupKey: string) => string;
  /** When true (default), grouping is applied. When false, render flat list/grid. */
  groupingEnabled?: boolean;
  /** Bucket title for items without a group key. Defaults to "Standalone". */
  ungroupedLabel?: string;
  /** Grid columns at md breakpoint and up (default 3). */
  gridCols?: 2 | 3 | 4;
  /** className override for the root */
  className?: string;
  /** Optional testId for vitest selectors */
  testId?: string;
}

const UNGROUPED = '__ungrouped__';

const COLS_CLASS: Record<NonNullable<HierarchicalListProps<unknown>['gridCols']>, string> = {
  2: 'grid-cols-1 sm:grid-cols-2',
  3: 'grid-cols-1 sm:grid-cols-2 md:grid-cols-3',
  4: 'grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4',
};

export function HierarchicalList<T>({
  items,
  viewMode,
  getItemKey,
  renderItem,
  getGroupKey,
  getGroupTitle,
  groupingEnabled = true,
  ungroupedLabel = 'Standalone',
  gridCols = 3,
  className = '',
  testId = 'hierarchical-list',
}: HierarchicalListProps<T>) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const groups = useMemo(() => {
    if (!groupingEnabled || !getGroupKey) {
      return [{ key: UNGROUPED, title: '', items: [...items] }];
    }
    const buckets = new Map<string, T[]>();
    for (const it of items) {
      const k = getGroupKey(it) || UNGROUPED;
      const bucket = buckets.get(k) || [];
      bucket.push(it);
      buckets.set(k, bucket);
    }
    return Array.from(buckets.entries()).map(([key, groupItems]) => ({
      key,
      title: key === UNGROUPED ? ungroupedLabel : (getGroupTitle ? getGroupTitle(key) : key),
      items: groupItems,
    }));
  }, [items, groupingEnabled, getGroupKey, getGroupTitle, ungroupedLabel]);

  const toggleGroup = (key: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const renderItems = (slice: T[]) => {
    if (viewMode === 'grid') {
      return (
        <div className={`grid gap-3 ${COLS_CLASS[gridCols]}`} data-testid={`${testId}-grid`}>
          {slice.map((it) => (
            <div key={getItemKey(it)} data-testid={`${testId}-item-${getItemKey(it)}`}>
              {renderItem(it, viewMode)}
            </div>
          ))}
        </div>
      );
    }
    return (
      <div className="space-y-2" data-testid={`${testId}-list`}>
        {slice.map((it) => (
          <div key={getItemKey(it)} data-testid={`${testId}-item-${getItemKey(it)}`}>
            {renderItem(it, viewMode)}
          </div>
        ))}
      </div>
    );
  };

  if (!groupingEnabled || !getGroupKey || groups.length === 1 && groups[0].key === UNGROUPED) {
    return (
      <div className={className} data-testid={testId}>
        {renderItems(groups[0]?.items ?? [])}
      </div>
    );
  }

  return (
    <div className={`space-y-4 ${className}`} data-testid={testId}>
      {groups.map((g) => {
        const isCollapsed = collapsed.has(g.key);
        return (
          <section key={g.key} data-testid={`${testId}-group-${g.key}`}>
            <header
              className="flex items-center gap-2 mb-2 cursor-pointer select-none"
              onClick={() => toggleGroup(g.key)}
              data-testid={`${testId}-group-header-${g.key}`}
            >
              {isCollapsed ? <ChevronRight size={14} className="text-gray-400" /> : <ChevronDown size={14} className="text-gray-400" />}
              <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">{g.title || ungroupedLabel}</h3>
              <span className="text-xs text-gray-400" data-testid={`${testId}-group-count-${g.key}`}>
                ({g.items.length})
              </span>
            </header>
            {!isCollapsed && renderItems(g.items)}
          </section>
        );
      })}
    </div>
  );
}
