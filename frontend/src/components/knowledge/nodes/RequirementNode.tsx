import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { ClipboardList } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { RoundedRectShape } from './shapes';
import type { KGNodeData } from './types';

function RequirementNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.Requirement;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<ClipboardList size={14} />}
      testId="kg-node-requirement"
    >
      <RoundedRectShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const RequirementNode = memo(RequirementNodeImpl);
