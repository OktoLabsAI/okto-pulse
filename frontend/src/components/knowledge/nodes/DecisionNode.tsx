import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { Gavel } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { RoundedRectShape } from './shapes';
import type { KGNodeData } from './types';

function DecisionNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.Decision;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<Gavel size={14} />}
      testId="kg-node-decision"
    >
      <RoundedRectShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const DecisionNode = memo(DecisionNodeImpl);
