/**
 * Shared types for the 11 KG node components — Spec 8 / S2.1.
 *
 * Every type-specific wrapper (DecisionNode, CriterionNode, …) receives its
 * KGNode payload plus the selection/connected flags that {@link NodeShell}
 * uses to drive the AC-3 "fade unconnected" visual. Keeping the contract
 * narrow here means a new node type only needs to set its shape + colour;
 * selection/dark-mode/handles are all inherited from the shell.
 */

import type { KGNode } from '@/types/knowledge-graph';

export interface KGNodeData extends Record<string, unknown> {
  /** The raw graph node from the backend. */
  kgNode: KGNode;
  /** True when this node is the currently-selected node in the canvas. */
  isSelected?: boolean;
  /** True when there is a selected node that shares an edge with this one. */
  isConnectedToSelected?: boolean;
  /** True while any node is selected — used to decide whether to fade. */
  hasSelection?: boolean;
}
