/**
 * Force-directed layout for the Knowledge Graph canvas — Spec 8 / S3.2.
 *
 * Computes (x, y) positions for a set of {@link KGNode} instances using
 * d3-force. The function is intentionally synchronous and pure: it
 * runs {@link SIMULATION_TICKS} ticks of the simulation and returns the
 * final positions as a Map. Callers are expected to memoize on
 * (nodes, edges) so layout only recomputes when the graph shape actually
 * changes — selection changes must NOT invalidate the cached Map.
 *
 * Edge density drives repulsion via {@link computeChargeStrength}: sparse
 * graphs get the -400 floor, dense graphs ratchet up charge so nodes
 * don't collapse into clusters.
 */

import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force';
import type { KGEdge, KGNode } from '@/types/knowledge-graph';

// Spacing constants — tuned after user feedback that nodes were
// overlapping at the default density. Tripled the link distance, ~2x
// the collision radius, and pushed the charge floor deeper so dense
// graphs (1 edge per node or more) still keep neighbours visibly apart.
export const SIMULATION_TICKS = 400;
export const LINK_DISTANCE = 220;
export const COLLIDE_RADIUS = 110;
export const CHARGE_FLOOR = -900;
export const CHARGE_DENSITY_COEFFICIENT = 300;

export interface NodePosition {
  x: number;
  y: number;
}

/**
 * Resolve the chargeStrength for a graph of the given shape.
 *
 * Formula: `-Math.max(CHARGE_FLOOR_ABS, COEFFICIENT * (edgeCount / max(nodeCount, 1)))`
 *
 * - Sparse graphs hit the -400 floor so nodes don't drift infinitely apart.
 * - Dense graphs ratchet charge up so neighbours don't collapse on top of
 *   each other.
 * - `Math.max(nodeCount, 1)` guards against division-by-zero during a
 *   transient empty-graph render (e.g. while pagination is loading).
 */
export function computeChargeStrength(nodeCount: number, edgeCount: number): number {
  const density = edgeCount / Math.max(nodeCount, 1);
  return -Math.max(CHARGE_FLOOR * -1, CHARGE_DENSITY_COEFFICIENT * density);
}

type SimNode = SimulationNodeDatum & { id: string };
type SimLink = SimulationLinkDatum<SimNode>;

export function computeForceLayout(
  nodes: readonly KGNode[],
  edges: readonly KGEdge[],
): Map<string, NodePosition> {
  const positions = new Map<string, NodePosition>();
  if (nodes.length === 0) {
    return positions;
  }

  const simNodes: SimNode[] = nodes.map((n) => ({ id: n.id }));
  const nodeIds = new Set(simNodes.map((n) => n.id));
  const simLinks: SimLink[] = edges
    .filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target))
    .map((e) => ({ source: e.source, target: e.target }));

  const simulation = forceSimulation<SimNode>(simNodes)
    .force(
      'link',
      forceLink<SimNode, SimLink>(simLinks)
        .id((d) => d.id)
        .distance(LINK_DISTANCE),
    )
    .force('charge', forceManyBody<SimNode>().strength(computeChargeStrength(nodes.length, edges.length)))
    .force('center', forceCenter(0, 0))
    .force('collide', forceCollide<SimNode>(COLLIDE_RADIUS));

  simulation.stop();
  for (let i = 0; i < SIMULATION_TICKS; i++) {
    simulation.tick();
  }

  for (const node of simNodes) {
    positions.set(node.id, { x: node.x ?? 0, y: node.y ?? 0 });
  }
  return positions;
}
