import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { CheckSquare } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { HexagonShape } from './shapes';
import type { KGNodeData } from './types';

function CriterionNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.Criterion;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<CheckSquare size={14} />}
      testId="kg-node-criterion"
    >
      <HexagonShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const CriterionNode = memo(CriterionNodeImpl);
