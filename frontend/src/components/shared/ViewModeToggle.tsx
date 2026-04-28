/**
 * ViewModeToggle — compact two-button switch for list/grid view modes.
 */

import { LayoutGrid, List } from 'lucide-react';
import type { ViewMode } from '@/hooks/useViewMode';

interface ViewModeToggleProps {
  value: ViewMode;
  onChange: (next: ViewMode) => void;
  testId?: string;
  className?: string;
}

export function ViewModeToggle({ value, onChange, testId = 'view-mode-toggle', className = '' }: ViewModeToggleProps) {
  return (
    <div
      role="group"
      aria-label="View mode"
      data-testid={testId}
      className={`inline-flex items-center gap-0.5 border border-gray-200 dark:border-gray-700 rounded-lg p-0.5 ${className}`}
    >
      <button
        type="button"
        onClick={() => onChange('list')}
        aria-pressed={value === 'list'}
        data-testid={`${testId}-list`}
        title="List view"
        className={`p-1.5 rounded text-gray-500 hover:text-gray-800 dark:hover:text-gray-200 ${
          value === 'list' ? 'bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-white' : ''
        }`}
      >
        <List size={14} />
      </button>
      <button
        type="button"
        onClick={() => onChange('grid')}
        aria-pressed={value === 'grid'}
        data-testid={`${testId}-grid`}
        title="Grid view"
        className={`p-1.5 rounded text-gray-500 hover:text-gray-800 dark:hover:text-gray-200 ${
          value === 'grid' ? 'bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-white' : ''
        }`}
      >
        <LayoutGrid size={14} />
      </button>
    </div>
  );
}
