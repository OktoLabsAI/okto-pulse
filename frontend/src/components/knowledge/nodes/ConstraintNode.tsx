import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { Shield } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { OctagonShape } from './shapes';
import type { KGNodeData } from './types';

function ConstraintNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.Constraint;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<Shield size={14} />}
      testId="kg-node-constraint"
    >
      <OctagonShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const ConstraintNode = memo(ConstraintNodeImpl);
