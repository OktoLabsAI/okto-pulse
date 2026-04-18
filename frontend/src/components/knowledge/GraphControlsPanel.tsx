/**
 * GraphControlsPanel — left sidebar with filters, sub-view nav, search.
 *
 * Spec 8 / Sprint 4:
 *   - S4.4: 10 coloured chips, one per KGEdgeType; independent toggle.
 *   - S4.5: confidence slider restricted to 0..1 with step 0.05.
 *   - S4.6: node-limit dropdown (50/100/200/500); `onNodeLimitChange` bubbles
 *     up so the parent can refetch with the new page size.
 */

import type { KGEdgeType, KGNodeType } from '@/types/knowledge-graph';
import {
  ALL_EDGE_TYPES,
  EDGE_TYPE_CONFIG,
  NODE_TYPE_CONFIG,
} from '@/types/knowledge-graph';

type SubView = 'graph' | 'audit' | 'pending' | 'pending_tree' | 'settings' | 'global';

export interface Filters {
  types: KGNodeType[];
  edgeTypes: KGEdgeType[];
  minConfidence: number;
  searchQuery: string;
}

interface Props {
  filters: Filters;
  onFiltersChange: (f: Filters) => void;
  subView: SubView;
  onSubViewChange: (v: SubView) => void;
  nodeCount: number;
  /** Current page size driving the /graph fetch (50/100/200/500). */
  nodeLimit: number;
  /** Notified when the user picks a new page size — parent refetches. */
  onNodeLimitChange: (limit: number) => void;
}

const SUB_VIEWS: { key: SubView; label: string }[] = [
  { key: 'graph', label: 'Graph' },
  { key: 'global', label: 'Global Discovery' },
  { key: 'audit', label: 'Audit Log' },
  { key: 'pending', label: 'Pending Queue' },
  { key: 'pending_tree', label: 'Pending Tree' },
  { key: 'settings', label: 'Settings' },
];

const ALL_NODE_TYPES = Object.keys(NODE_TYPE_CONFIG) as KGNodeType[];
export const NODE_LIMIT_OPTIONS = [50, 100, 200, 500] as const;

export function GraphControlsPanel({
  filters,
  onFiltersChange,
  subView,
  onSubViewChange,
  nodeCount,
  nodeLimit,
  onNodeLimitChange,
}: Props) {
  const updateFilters = (patch: Partial<Filters>) => onFiltersChange({ ...filters, ...patch });

  return (
    <div className="p-4 space-y-6" role="navigation" aria-label="Knowledge graph controls">
      {/* Sub-view nav */}
      <div>
        <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">Views</h3>
        <div className="space-y-1">
          {SUB_VIEWS.map((sv) => (
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
          onChange={(e) => updateFilters({ searchQuery: e.target.value })}
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
          {ALL_NODE_TYPES.map((nt) => {
            const config = NODE_TYPE_CONFIG[nt];
            const checked = filters.types.length === 0 || filters.types.includes(nt);
            return (
              <label key={nt} className="flex items-center gap-2 text-xs cursor-pointer">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => {
                    let next: KGNodeType[];
                    if (filters.types.length === 0) {
                      next = ALL_NODE_TYPES.filter((t) => t !== nt);
                    } else if (checked) {
                      next = filters.types.filter((t) => t !== nt);
                    } else {
                      next = [...filters.types, nt];
                    }
                    updateFilters({ types: next });
                  }}
                  className="rounded"
                />
                <span className="w-2 h-2 rounded-full" style={{ backgroundColor: config.color }} />
                <span className="text-gray-700 dark:text-gray-300">
                  {config.icon} {nt}
                </span>
              </label>
            );
          })}
        </div>
        {filters.types.length > 0 && (
          <button
            onClick={() => updateFilters({ types: [] })}
            className="mt-2 text-xs text-blue-600 hover:text-blue-700 dark:text-blue-400"
          >
            Show all types
          </button>
        )}
      </div>

      {/* Edge type chips (S4.4) */}
      <div>
        <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">Edge Types</h3>
        <div className="flex flex-wrap gap-1" role="group" aria-label="Edge type filter">
          {ALL_EDGE_TYPES.map((et) => {
            const active =
              filters.edgeTypes.length === 0 || filters.edgeTypes.includes(et);
            const cfg = EDGE_TYPE_CONFIG[et];
            return (
              <button
                key={et}
                type="button"
                role="switch"
                aria-checked={active}
                data-testid={`kg-edge-chip-${et}`}
                onClick={() => {
                  let next: KGEdgeType[];
                  if (filters.edgeTypes.length === 0) {
                    next = ALL_EDGE_TYPES.filter((t) => t !== et);
                  } else if (active) {
                    next = filters.edgeTypes.filter((t) => t !== et);
                  } else {
                    next = [...filters.edgeTypes, et];
                  }
                  updateFilters({ edgeTypes: next });
                }}
                className={`px-2 py-0.5 rounded-full text-[11px] border transition ${
                  active
                    ? 'text-white'
                    : 'text-gray-500 bg-transparent border-gray-300 dark:border-gray-600'
                }`}
                style={
                  active
                    ? { backgroundColor: cfg.color, borderColor: cfg.color }
                    : undefined
                }
              >
                {cfg.label}
              </button>
            );
          })}
        </div>
        {filters.edgeTypes.length > 0 && (
          <button
            onClick={() => updateFilters({ edgeTypes: [] })}
            className="mt-2 text-xs text-blue-600 hover:text-blue-700 dark:text-blue-400"
          >
            Show all edge types
          </button>
        )}
      </div>

      {/* Confidence slider (S4.5: 0..1 step 0.05) */}
      <div>
        <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">
          Min Confidence: {(filters.minConfidence * 100).toFixed(0)}%
        </h3>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={filters.minConfidence}
          onChange={(e) => updateFilters({ minConfidence: Number(e.target.value) })}
          className="w-full"
          aria-label="Minimum confidence filter"
          data-testid="kg-confidence-slider"
        />
      </div>

      {/* Node limit dropdown (S4.6) */}
      <div>
        <h3 className="text-xs font-medium text-gray-500 uppercase mb-2">Nodes per Page</h3>
        <select
          value={nodeLimit}
          onChange={(e) => onNodeLimitChange(Number(e.target.value))}
          className="w-full px-3 py-1.5 text-sm border rounded dark:bg-gray-800 dark:border-gray-700"
          aria-label="Nodes per page"
          data-testid="kg-node-limit"
        >
          {NODE_LIMIT_OPTIONS.map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
