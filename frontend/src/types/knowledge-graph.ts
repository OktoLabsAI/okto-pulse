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

/** Node type visual config — shape, color, icon, short human-readable
 *  description. `description` is the single source of truth for both
 *  the node tooltip preamble and the KG help modal — keep wording tight
 *  (1-2 sentences) so it fits in either surface.
 */
export const NODE_TYPE_CONFIG: Record<KGNodeType, {
  color: string;
  darkColor: string;
  shape: string;
  icon: string;
  description: string;
}> = {
  Decision:     { color: '#3B82F6', darkColor: '#60A5FA', shape: 'rounded-lg', icon: '⚖️',
    description: 'An architectural or product decision recorded on the board. Captures the chosen option, the alternatives considered, and the rationale. Can be superseded by a newer Decision or revoked.' },
  Criterion:    { color: '#10B981', darkColor: '#34D399', shape: 'hexagon',    icon: '✓',
    description: 'A measurable acceptance criterion used to validate a spec or card — the "done line" for a given concern.' },
  Constraint:   { color: '#EF4444', darkColor: '#F87171', shape: 'octagon',    icon: '🚫',
    description: 'A hard boundary the solution must not cross (regulatory, compliance, performance, cost). Violating a Constraint is a blocker, not a trade-off.' },
  Assumption:   { color: '#F59E0B', darkColor: '#FBBF24', shape: 'diamond',    icon: '❓',
    description: 'A working assumption that is currently unverified. If it turns out to be false, the work that depends on it has to be revisited.' },
  Requirement:  { color: '#8B5CF6', darkColor: '#A78BFA', shape: 'rounded',    icon: '📋',
    description: 'A functional or non-functional requirement. States what the system must do (or how well) without prescribing a specific implementation.' },
  Entity:       { color: '#06B6D4', darkColor: '#22D3EE', shape: 'circle',     icon: '🏷️',
    description: 'A first-class domain object the product talks about — users, orders, invoices, sessions. Often surfaces in specs, contracts, and bug reports alike.' },
  APIContract:  { color: '#EC4899', darkColor: '#F472B6', shape: 'rounded-sm', icon: '📡',
    description: 'An HTTP/RPC contract definition: endpoint, request, response, error shape. The executable reference for integrating or regression-testing a service.' },
  TestScenario: { color: '#14B8A6', darkColor: '#2DD4BF', shape: 'rounded-sm', icon: '🧪',
    description: 'A Given/When/Then scenario that proves a requirement or business rule. Linked from cards to track "implemented + verified" status.' },
  Bug:          { color: '#DC2626', darkColor: '#EF4444', shape: 'diamond',    icon: '🐛',
    description: 'A defect found after work was accepted. Tracks expected vs observed behaviour, severity, and the originating task.' },
  Learning:     { color: '#7C3AED', darkColor: '#8B5CF6', shape: 'rounded-lg', icon: '💡',
    description: 'A lesson distilled from shipping, operating, or debugging the system. Fuels the knowledge graph\'s long-term memory so mistakes are not repeated.' },
  Alternative:  { color: '#6B7280', darkColor: '#9CA3AF', shape: 'dashed',     icon: '↔️',
    description: 'An option that was considered but not chosen. Preserved alongside Decisions so future reviewers can see the full decision space, not just the winner.' },
};

/**
 * Edge type visual config — one entry for each of the 10 KGEdgeType values.
 * `color` drives the chip swatch in GraphControlsPanel AND the stroke in
 * GraphCanvas so the two stay visually consistent (Spec 8 / S4.4).
 * `description` is consumed by the KG help modal (Connection Types section).
 */
export const EDGE_TYPE_CONFIG: Record<KGEdgeType, {
  color: string;
  label: string;
  description: string;
}> = {
  supersedes:   { color: '#8B5CF6', label: 'supersedes',
    description: 'A new Decision replaces an earlier one. The older Decision stays in the graph (for audit) but is no longer active.' },
  contradicts:  { color: '#EF4444', label: 'contradicts',
    description: 'Two nodes assert logically incompatible things. Surfaces in the Contradictions view so the team can reconcile or pick a winner.' },
  derives_from: { color: '#3B82F6', label: 'derives_from',
    description: 'The target is the source\'s parent in the SDLC pipeline — a Spec derives_from a Refinement, a Refinement derives_from an Ideation.' },
  relates_to:   { color: '#6B7280', label: 'relates_to',
    description: 'A soft semantic link without hierarchy. Use when two artifacts share a topic or theme but neither depends on the other.' },
  mentions:     { color: '#94A3B8', label: 'mentions',
    description: 'Extracted reference: node A explicitly talks about node B (by id, title, or entity name) in its content, but has no stronger relationship.' },
  depends_on:   { color: '#F59E0B', label: 'depends_on',
    description: 'A needs B to be in place before it can be worked on or trusted. Used for cards, decisions, requirements — the core blocker relation.' },
  violates:     { color: '#DC2626', label: 'violates',
    description: 'The source breaks the target Constraint. Any node marked as violating a Constraint is a blocker until reconciled.' },
  implements:   { color: '#10B981', label: 'implements',
    description: 'A Component, Card, or Spec concretises a Requirement or Decision — the "this code closes that promise" edge.' },
  tests:        { color: '#14B8A6', label: 'tests',
    description: 'A TestScenario exercises a Requirement, Business Rule, or API Contract. Key edge for coverage reporting.' },
  validates:    { color: '#7C3AED', label: 'validates',
    description: 'A successful validation run vouches for the target node. Similar to `tests` but aimed at gate outcomes rather than scenarios.' },
};

export const ALL_EDGE_TYPES = Object.keys(EDGE_TYPE_CONFIG) as KGEdgeType[];
