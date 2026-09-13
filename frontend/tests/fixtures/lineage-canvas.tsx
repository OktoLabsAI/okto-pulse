import React from 'react';
import { createRoot } from 'react-dom/client';
import { ReactFlow, Background, MiniMap, Controls } from '@xyflow/react';
import { LineageNode, layoutEdges, miniMapNodeColor } from '../../src/components/traceability/LineageGraphModal';
import { useMeasuredLineageNodes } from '../../src/components/traceability/useMeasuredLineageNodes';
import type { LineageGraphResponse } from '../../src/types';
import '../../src/index.css';

const graph: LineageGraphResponse = {
  board_id: 'synthetic', selected: { entity_type: 'ideation', entity_id: 'root' }, root_ideation: { id: 'root', title: 'Origin', status: 'done' },
  resolution_path: [], summary: {}, warnings: [],
  nodes: [
    { id: 'root', entity_id: 'root', entity_type: 'ideation', title: 'Origin', label: 'Origin', stage: 0, status: 'done' },
    ...['draft', 'review', 'approved', 'cancelled'].map((status) => ({ id: status, entity_id: status, entity_type: 'refinement', title: `Refinement — ${status}`, label: status, stage: 1, status })),
  ],
  edges: ['draft', 'review', 'approved', 'cancelled'].map(status => ({ id: `edge-${status}`, source: 'root', target: status, relationship: 'has_refinement' })),
};
const nodes = graph.nodes.map((node, index) => ({
  id: node.id, type: 'lineage', position: { x: node.stage * 400, y: index ? (index - 1) * 160 : 240 },
  data: { lineageNode: node, selected: false, onOpenDetails: () => {} },
}));
function Fixture() {
  const measured = useMeasuredLineageNodes(nodes);
  return <main className="h-screen bg-gray-950 text-white p-6">
  <h1>Pulse lineage — synthetic data, no backend</h1>
  <div style={{ height: '85vh' }}>
    <ReactFlow nodes={measured.nodes} onNodesChange={measured.onNodesChange} edges={layoutEdges(graph, null)} nodeTypes={{ lineage: LineageNode }} fitView>
      <Background /><MiniMap nodeColor={miniMapNodeColor} /><Controls />
    </ReactFlow>
  </div>
</main>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
