/**
 * KnowledgeGraphPage — container component orchestrating the KG visualization.
 *
 * Renders: GraphCanvas (center) + GraphControlsPanel (left) + NodeDetailPanel (right)
 * Sub-views: main graph, audit log, pending queue, settings
 */

import { useState, useEffect } from 'react';
import { GraphCanvas } from './GraphCanvas';
import { NodeDetailPanel } from './NodeDetailPanel';
import { GraphControlsPanel } from './GraphControlsPanel';
import { EmptyState } from './EmptyState';
import type { KGNode, KGEdge, KGNodeType } from '@/types/knowledge-graph';
import * as kgApi from '@/services/kg-api';

interface Props {
  boardId: string;
}

type SubView = 'graph' | 'audit' | 'pending' | 'settings' | 'global';

export function KnowledgeGraphPage({ boardId }: Props) {
  const [nodes, setNodes] = useState<KGNode[]>([]);
  const [edges, setEdges] = useState<KGEdge[]>([]);
  const [selectedNode, setSelectedNode] = useState<KGNode | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [subView, setSubView] = useState<SubView>('graph');
  const [filters, setFilters] = useState({
    types: [] as KGNodeType[],
    minConfidence: 0.5,
    searchQuery: '',
  });

  useEffect(() => {
    loadGraph();
  }, [boardId]);

  async function loadGraph() {
    setLoading(true);
    setError(null);
    try {
      const data = await kgApi.getSubgraph(boardId, { max_nodes: 200 });
      setNodes(data.nodes || []);
      setEdges(data.edges || []);
    } catch (err: any) {
      setError(err.message || 'Failed to load graph');
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="animate-pulse text-gray-400 dark:text-gray-600">
          Loading Knowledge Graph...
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4">
        <p className="text-red-500">{error}</p>
        <button
          onClick={loadGraph}
          className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          Retry
        </button>
      </div>
    );
  }

  if (nodes.length === 0 && subView === 'graph') {
    return <EmptyState boardId={boardId} />;
  }

  return (
    <div className="flex h-full">
      {/* Left: Controls */}
      <div className="w-64 border-r border-gray-200 dark:border-gray-700 overflow-y-auto">
        <GraphControlsPanel
          filters={filters}
          onFiltersChange={setFilters}
          subView={subView}
          onSubViewChange={setSubView}
          nodeCount={nodes.length}
        />
      </div>

      {/* Center: Graph or sub-view */}
      <div className="flex-1 relative">
        {subView === 'graph' ? (
          <GraphCanvas
            nodes={nodes}
            edges={edges}
            selectedNodeId={selectedNode?.id ?? null}
            onNodeClick={setSelectedNode}
            filters={filters}
          />
        ) : (
          <div className="p-6 text-gray-500 dark:text-gray-400">
            <h2 className="text-lg font-semibold mb-4 capitalize">{subView}</h2>
            <p>View coming soon in future sprint.</p>
          </div>
        )}
      </div>

      {/* Right: Node detail */}
      {selectedNode && (
        <div className="w-80 border-l border-gray-200 dark:border-gray-700 overflow-y-auto">
          <NodeDetailPanel
            node={selectedNode}
            onClose={() => setSelectedNode(null)}
          />
        </div>
      )}
    </div>
  );
}
