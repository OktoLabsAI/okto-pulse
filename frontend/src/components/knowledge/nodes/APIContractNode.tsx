import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { Radio } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { SquareShape } from './shapes';
import type { KGNodeData } from './types';

function APIContractNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.APIContract;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<Radio size={14} />}
      testId="kg-node-apicontract"
    >
      <SquareShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const APIContractNode = memo(APIContractNodeImpl);
