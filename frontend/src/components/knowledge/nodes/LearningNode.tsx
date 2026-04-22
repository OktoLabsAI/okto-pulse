import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { Lightbulb } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { RoundedRectShape } from './shapes';
import type { KGNodeData } from './types';

function LearningNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.Learning;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<Lightbulb size={14} />}
      testId="kg-node-learning"
    >
      <RoundedRectShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const LearningNode = memo(LearningNodeImpl);
