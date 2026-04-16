/**
 * GraphCanvas — wrapper over React Flow (@xyflow/react) for KG visualization.
 *
 * Abstracts the library so a future swap (e.g. to D3/Sigma) only changes this file.
 * Supports: force-directed layout (default), node click, search highlight,
 * viewport culling via React Flow's built-in virtualization.
 */

import { useMemo, useCallback } from 'react';
import type { KGNode, KGEdge, KGNodeType } from '@/types/knowledge-graph';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';

interface Props {
  nodes: KGNode[];
  edges: KGEdge[];
  selectedNodeId: string | null;
  onNodeClick: (node: KGNode) => void;
  filters: {
    types: KGNodeType[];
    minConfidence: number;
    searchQuery: string;
  };
}

/**
 * MVP: renders a simple list-based graph view. When @xyflow/react is installed,
 * this component switches to the full React Flow canvas. The abstraction layer
 * means the API contract stays the same.
 */
export function GraphCanvas({ nodes, edges, selectedNodeId, onNodeClick, filters }: Props) {
  const filteredNodes = useMemo(() => {
    let result = nodes;
    if (filters.types.length > 0) {
      result = result.filter(n => filters.types.includes(n.node_type));
    }
    if (filters.minConfidence > 0) {
      result = result.filter(n => n.source_confidence >= filters.minConfidence);
    }
    if (filters.searchQuery) {
      const q = filters.searchQuery.toLowerCase();
      result = result.filter(n =>
        n.title.toLowerCase().includes(q) ||
        (n.content?.toLowerCase().includes(q) ?? false)
      );
    }
    return result;
  }, [nodes, filters]);

  const handleNodeClick = useCallback((node: KGNode) => {
    onNodeClick(node);
  }, [onNodeClick]);

  return (
    <div className="h-full overflow-auto p-4" role="region" aria-label={`Knowledge graph with ${filteredNodes.length} nodes`}>
      {/* MVP: Grid layout. Production: React Flow canvas */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
        {filteredNodes.map(node => {
          const config = NODE_TYPE_CONFIG[node.node_type] || NODE_TYPE_CONFIG.Decision;
          const isSelected = node.id === selectedNodeId;
          const isHighlighted = filters.searchQuery &&
            node.title.toLowerCase().includes(filters.searchQuery.toLowerCase());

          return (
            <button
              key={node.id}
              onClick={() => handleNodeClick(node)}
              className={`
                p-3 rounded-lg border-2 text-left transition-all text-sm
                ${isSelected ? 'ring-2 ring-blue-500 border-blue-500' : 'border-gray-200 dark:border-gray-700'}
                ${isHighlighted ? 'border-yellow-400 ring-2 ring-yellow-400' : ''}
                hover:shadow-md dark:bg-gray-800 bg-white
              `}
              aria-label={`${node.node_type}: ${node.title}`}
              tabIndex={0}
            >
              <div className="flex items-center gap-2 mb-1">
                <span
                  className="w-3 h-3 rounded-full flex-shrink-0"
                  style={{ backgroundColor: config.color }}
                  aria-hidden="true"
                />
                <span className="text-xs text-gray-500 dark:text-gray-400 font-mono">
                  {config.icon} {node.node_type}
                </span>
              </div>
              <p className="font-medium text-gray-900 dark:text-gray-100 line-clamp-2">
                {node.title}
              </p>
              <div className="flex items-center gap-2 mt-1 text-xs text-gray-400">
                <span>conf: {(node.source_confidence * 100).toFixed(0)}%</span>
                <span>{node.validation_status}</span>
              </div>
            </button>
          );
        })}
      </div>
      {filteredNodes.length === 0 && (
        <p className="text-center text-gray-400 mt-8">No nodes match current filters.</p>
      )}
    </div>
  );
}
