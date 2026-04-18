/**
 * Barrel export of the 11 KG node components — Spec 8 / S2.6.
 *
 * The {@link nodeTypes} map is passed straight to React Flow's `nodeTypes`
 * prop. Keys match {@link KGNodeType} exactly so the backend payload can
 * drive node dispatch without a lookup table on the caller side.
 */

import type { ComponentType } from 'react';
import type { NodeProps } from '@xyflow/react';
import type { KGNodeType } from '@/types/knowledge-graph';

import { DecisionNode } from './DecisionNode';
import { CriterionNode } from './CriterionNode';
import { ConstraintNode } from './ConstraintNode';
import { AssumptionNode } from './AssumptionNode';
import { RequirementNode } from './RequirementNode';
import { EntityNode } from './EntityNode';
import { APIContractNode } from './APIContractNode';
import { TestScenarioNode } from './TestScenarioNode';
import { BugNode } from './BugNode';
import { LearningNode } from './LearningNode';
import { AlternativeNode } from './AlternativeNode';

export { NodeShell, NODE_WIDTH, NODE_HEIGHT } from './NodeShell';
export type { KGNodeData } from './types';

export const nodeTypes: Record<KGNodeType, ComponentType<NodeProps>> = {
  Decision: DecisionNode,
  Criterion: CriterionNode,
  Constraint: ConstraintNode,
  Assumption: AssumptionNode,
  Requirement: RequirementNode,
  Entity: EntityNode,
  APIContract: APIContractNode,
  TestScenario: TestScenarioNode,
  Bug: BugNode,
  Learning: LearningNode,
  Alternative: AlternativeNode,
};

export {
  DecisionNode,
  CriterionNode,
  ConstraintNode,
  AssumptionNode,
  RequirementNode,
  EntityNode,
  APIContractNode,
  TestScenarioNode,
  BugNode,
  LearningNode,
  AlternativeNode,
};
