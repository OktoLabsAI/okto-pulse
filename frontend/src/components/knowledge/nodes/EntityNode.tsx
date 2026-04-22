import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { Tag } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { CircleShape } from './shapes';
import type { KGNodeData } from './types';

function EntityNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.Entity;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<Tag size={14} />}
      testId="kg-node-entity"
    >
      <CircleShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const EntityNode = memo(EntityNodeImpl);
