import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { Bug } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { DiamondShape } from './shapes';
import type { KGNodeData } from './types';

function BugNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.Bug;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<Bug size={14} />}
      testId="kg-node-bug"
    >
      <DiamondShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const BugNode = memo(BugNodeImpl);
