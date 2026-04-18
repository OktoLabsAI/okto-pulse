/**
 * TypeScript types for the Knowledge Graph visualization layer.
 * Maps to the Pydantic models in okto_pulse_core/kg/tool_schemas.py.
 */

export type KGNodeType =
  | 'Decision' | 'Criterion' | 'Constraint' | 'Assumption'
  | 'Requirement' | 'Entity' | 'APIContract' | 'TestScenario'
  | 'Bug' | 'Learning' | 'Alternative';

export type KGEdgeType =
  | 'supersedes' | 'contradicts' | 'derives_from' | 'relates_to'
  | 'mentions' | 'depends_on' | 'violates' | 'implements'
  | 'tests' | 'validates';

export interface KGNode {
  id: string;
  title: string;
  content?: string;
  justification?: string;
  source_artifact_ref?: string;
  source_confidence: number;
  /** v0.3.0: continuous relevance in [0.0, 1.5] replaces validation_status. */
  relevance_score: number;
  query_hits?: number;
  last_queried_at?: string | null;
  created_at?: string;
  superseded_by?: string;
  node_type: KGNodeType;
}

export interface KGEdge {
  id: string;
  source: string;
  target: string;
  edge_type: KGEdgeType;
  confidence: number;
}

export interface ContradictionPair {
  id_a: string;
  title_a: string;
  id_b: string;
  title_b: string;
  confidence: number;
}

export interface AuditEntry {
  session_id: string;
  board_id: string;
  artifact_id: string;
  agent_id: string;
  committed_at: string;
  nodes_added: number;
  nodes_updated: number;
  nodes_superseded: number;
  edges_added: number;
  summary_text?: string;
  undo_status: 'none' | 'undone' | 'undo_blocked';
}

export interface KGSettings {
  consolidation_enabled: boolean;
  enable_historical_consolidation: boolean;
  retention_days?: number;
}

export interface KGStats {
  schema_version: string;
  node_counts_by_type: Record<string, number>;
  edge_counts_by_type: Record<string, number>;
  avg_confidence: number;
  pending_queue_count: number;
  last_consolidation_at?: string;
}

/** Node type visual config — shape, color, icon */
export const NODE_TYPE_CONFIG: Record<KGNodeType, {
  color: string;
  darkColor: string;
  shape: string;
  icon: string;
}> = {
  Decision:     { color: '#3B82F6', darkColor: '#60A5FA', shape: 'rounded-lg', icon: '⚖️' },
  Criterion:    { color: '#10B981', darkColor: '#34D399', shape: 'hexagon',    icon: '✓' },
  Constraint:   { color: '#EF4444', darkColor: '#F87171', shape: 'octagon',    icon: '🚫' },
  Assumption:   { color: '#F59E0B', darkColor: '#FBBF24', shape: 'diamond',    icon: '❓' },
  Requirement:  { color: '#8B5CF6', darkColor: '#A78BFA', shape: 'rounded',    icon: '📋' },
  Entity:       { color: '#06B6D4', darkColor: '#22D3EE', shape: 'circle',     icon: '🏷️' },
  APIContract:  { color: '#EC4899', darkColor: '#F472B6', shape: 'rounded-sm', icon: '📡' },
  TestScenario: { color: '#14B8A6', darkColor: '#2DD4BF', shape: 'rounded-sm', icon: '🧪' },
  Bug:          { color: '#DC2626', darkColor: '#EF4444', shape: 'diamond',    icon: '🐛' },
  Learning:     { color: '#7C3AED', darkColor: '#8B5CF6', shape: 'rounded-lg', icon: '💡' },
  Alternative:  { color: '#6B7280', darkColor: '#9CA3AF', shape: 'dashed',     icon: '↔️' },
};

/**
 * Edge type visual config — one entry for each of the 10 KGEdgeType values.
 * `color` drives the chip swatch in GraphControlsPanel AND the stroke in
 * GraphCanvas so the two stay visually consistent (Spec 8 / S4.4).
 */
export const EDGE_TYPE_CONFIG: Record<KGEdgeType, { color: string; label: string }> = {
  supersedes:   { color: '#8B5CF6', label: 'supersedes' },
  contradicts:  { color: '#EF4444', label: 'contradicts' },
  derives_from: { color: '#3B82F6', label: 'derives_from' },
  relates_to:   { color: '#6B7280', label: 'relates_to' },
  mentions:     { color: '#94A3B8', label: 'mentions' },
  depends_on:   { color: '#F59E0B', label: 'depends_on' },
  violates:     { color: '#DC2626', label: 'violates' },
  implements:   { color: '#10B981', label: 'implements' },
  tests:        { color: '#14B8A6', label: 'tests' },
  validates:    { color: '#7C3AED', label: 'validates' },
};

export const ALL_EDGE_TYPES = Object.keys(EDGE_TYPE_CONFIG) as KGEdgeType[];
