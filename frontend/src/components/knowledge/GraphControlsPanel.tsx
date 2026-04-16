/**
 * GraphControlsPanel — left sidebar with filters, sub-view nav, search.
 */

import type { KGNodeType } from '@/types/knowledge-graph';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';

type SubView = 'graph' | 'audit' | 'pending' | 'settings' | 'global';

interface Filters {
  types: KGNodeType[];
  minConfidence: number;
  searchQuery: string;
}

interface Props {
  filters: Filters;
  onFiltersChange: (f: Filters) => void;
  subView: SubView;
  onSubViewChange: (v: SubView) => void;
  nodeCount: number;
}

const SUB_VIEWS: { key: SubView; label: string }[] = [
  { key: 'graph', label: 'Graph' },
  { key: 'global', label: 'Global Discovery' },
  { key: 'audit', label: 'Audit Log' },
  { key: 'pending', label: 'Pending Queue' },
  { key: 'settings', label: 'Settings' },
];

const ALL_NODE_TYPES = Object.keys(NODE_TYPE_CONFIG) as KGNodeType[];

export function GraphControlsPanel({ filters, onFiltersChange, subView, onSubViewChange, nodeCount }: Props) {
  return (
    <div className="p-4 space-y-6" role="navigation" aria-label="Knowledge graph controls">
      {/* Sub-view nav */}
      <div>
        <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">Views</h3>
        <div className="space-y-1">
          {SUB_VIEWS.map(sv => (
            <button
              key={sv.key}
              onClick={() => onSubViewChange(sv.key)}
              className={`w-full text-left px-3 py-1.5 rounded text-sm ${
                subView === sv.key
                  ? 'bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-400'
                  : 'text-gray-600 hover:bg-gray-50 dark:text-gray-400 dark:hover:bg-gray-800'
              }`}
            >
              {sv.label}
            </button>
          ))}
        </div>
      </div>

      {/* Search */}
      <div>
        <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">Search</h3>
        <input
          type="text"
          value={filters.searchQuery}
          onChange={e => onFiltersChange({ ...filters, searchQuery: e.target.value })}
          placeholder="Search nodes..."
          className="w-full px-3 py-1.5 text-sm border rounded dark:bg-gray-800 dark:border-gray-700"
          aria-label="Search knowledge graph nodes"
        />
      </div>

      {/* Node type filter */}
      <div>
        <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">
          Node Types ({nodeCount})
        </h3>
        <div className="space-y-1 max-h-48 overflow-y-auto">
          {ALL_NODE_TYPES.map(nt => {
            const config = NODE_TYPE_CONFIG[nt];
            const checked = filters.types.length === 0 || filters.types.includes(nt);
            return (
              <label key={nt} className="flex items-center gap-2 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => {
                    const current = filters.types.length === 0 ? [...ALL_NODE_TYPES] : [...filters.types];
                    const next = checked
                      ? current.filter(t => t !== nt)
                      : [...current, nt];
                    onFiltersChange({ ...filters, types: next.length === ALL_NODE_TYPES.length ? [] : next });
                  }}
                  className="rounded"
                />
                <span className="w-2 h-2 rounded-full" style={{ backgroundColor: config.color }} />
                <span className="text-gray-700 dark:text-gray-300">{config.icon} {nt}</span>
              </label>
            );
          })}
        </div>
      </div>

      {/* Confidence slider */}
      <div>
        <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">
          Min Confidence: {(filters.minConfidence * 100).toFixed(0)}%
        </h3>
        <input
          type="range"
          min="0"
          max="100"
          value={filters.minConfidence * 100}
          onChange={e => onFiltersChange({ ...filters, minConfidence: Number(e.target.value) / 100 })}
          className="w-full"
          aria-label="Minimum confidence filter"
        />
      </div>
    </div>
  );
}
