import { memo } from 'react';
import type { NodeProps } from '@xyflow/react';
import { FlaskConical } from 'lucide-react';
import { NODE_TYPE_CONFIG } from '@/types/knowledge-graph';
import { NodeShell } from './NodeShell';
import { SquareShape } from './shapes';
import type { KGNodeData } from './types';

function TestScenarioNodeImpl({ data }: NodeProps) {
  const cfg = NODE_TYPE_CONFIG.TestScenario;
  return (
    <NodeShell
      data={data as KGNodeData}
      color={cfg.color}
      darkColor={cfg.darkColor}
      icon={<FlaskConical size={14} />}
      testId="kg-node-testscenario"
    >
      <SquareShape color={cfg.color} darkColor={cfg.darkColor} />
    </NodeShell>
  );
}

export const TestScenarioNode = memo(TestScenarioNodeImpl);
