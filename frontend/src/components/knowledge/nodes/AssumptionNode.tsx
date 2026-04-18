import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { HelpCircle } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { DiamondShape } from './shapes';
import type { KGNodeData } from './types';

function AssumptionNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.Assumption;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<HelpCircle size={14} />}
      testId="kg-node-assumption"
    >
      <DiamondShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const AssumptionNode = memo(AssumptionNodeImpl);
