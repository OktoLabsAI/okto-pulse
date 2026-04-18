import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { GitBranch } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { SquareShape } from './shapes';
import type { KGNodeData } from './types';

function AlternativeNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.Alternative;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<GitBranch size={14} />}
      testId="kg-node-alternative"
    >
      <SquareShape color={cfg.color} darkColor={cfg.darkColor} strokeDasharray="6 4" />
    </NodeShell>
  );
}

export const AlternativeNode = memo(AlternativeNodeImpl);
