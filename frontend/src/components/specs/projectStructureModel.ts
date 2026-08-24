import type {
  CardType,
  ProjectStructureNode,
  ProjectStructureProjectionNode,
} from '@/types';

export interface VisibleProjectStructureNode {
  node: ProjectStructureNode;
  depth: number;
  hasChildren: boolean;
}

export interface ProjectStructureParseResult {
  nodes: ProjectStructureNode[];
  issues: string[];
}

export function shouldShowProjectStructureTab(
  canRead: boolean,
  canAuthor: boolean,
  structure: unknown,
): boolean {
  return canRead && (canAuthor || (structure !== null && structure !== undefined));
}

export function canMutateProjectStructureInStatus(
  permissionWithState: boolean,
  status: string | null | undefined,
): boolean {
  return permissionWithState && status === 'draft';
}

const PROJECT_STRUCTURE_RELATION_STATUSES = new Set([
  'draft',
  'approved',
  'validated',
  'in_progress',
  'done',
]);

/**
 * Task/Test links are traceability-only mutations. Core permits them after
 * approval without reopening the authored tree; all other writes stay Draft-only.
 */
export function canRelateProjectStructureInStatus(
  permissionWithState: boolean,
  status: string | null | undefined,
): boolean {
  return permissionWithState && PROJECT_STRUCTURE_RELATION_STATUSES.has(status ?? '');
}

const NODE_KINDS = new Set(['folder', 'file', 'artifact']);
const CLASSIFICATIONS = new Set(['as_is', 'to_be', 'reference_scaffold']);
const STATUSES = new Set(['active', 'revoked']);

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string');
}

/**
 * Defensive read-boundary parser. The server owns validation; the UI avoids a
 * modal-wide crash if an older row or a partially upgraded adapter returns a
 * malformed node and surfaces the degradation to the human instead.
 */
export function parseProjectStructureNodes(value: unknown): ProjectStructureParseResult {
  if (!Array.isArray(value)) return { nodes: [], issues: [] };
  const issues: string[] = [];
  const seen = new Set<string>();
  const nodes: ProjectStructureNode[] = [];

  for (const [index, raw] of value.entries()) {
    if (!raw || typeof raw !== 'object') {
      issues.push(`Node ${index + 1} is not an object.`);
      continue;
    }
    const record = raw as Record<string, unknown>;
    const id = typeof record.id === 'string' ? record.id.trim() : '';
    if (!id || seen.has(id)) {
      issues.push(
        id ? `Duplicate node ID ${id}.` : `Node ${index + 1} has no ID.`,
      );
      continue;
    }
    seen.add(id);

    const kind = typeof record.kind === 'string' && NODE_KINDS.has(record.kind)
      ? record.kind as ProjectStructureNode['kind']
      : 'artifact';
    const classification =
      typeof record.classification === 'string'
      && CLASSIFICATIONS.has(record.classification)
        ? record.classification as ProjectStructureNode['classification']
        : 'to_be';
    const status = typeof record.status === 'string' && STATUSES.has(record.status)
      ? record.status as ProjectStructureNode['status']
      : 'active';
    const name = typeof record.name === 'string' && record.name.trim()
      ? record.name.trim()
      : '[Unnamed node]';
    const parentId = record.parent_id === null || record.parent_id === undefined
      ? null
      : typeof record.parent_id === 'string'
        ? record.parent_id
        : null;
    const position = typeof record.position === 'number'
      && Number.isInteger(record.position)
      && record.position >= 0
      ? record.position
      : 0;

    if (kind !== record.kind) issues.push(`Node ${id} has an unknown kind.`);
    if (classification !== record.classification) {
      issues.push(`Node ${id} has an unknown classification.`);
    }
    if (status !== record.status) issues.push(`Node ${id} has an unknown status.`);
    if (name === '[Unnamed node]') issues.push(`Node ${id} has no usable name.`);
    if (position !== record.position) issues.push(`Node ${id} has an invalid position.`);

    nodes.push({
      id,
      parent_id: parentId,
      position,
      kind,
      name,
      note: typeof record.note === 'string' ? record.note : '',
      classification,
      status,
      state: typeof record.state === 'string'
        && ['existing', 'planned', 'modified', 'removed'].includes(record.state)
        ? record.state as ProjectStructureNode['state']
        : null,
      interpretation_limit: typeof record.interpretation_limit === 'string'
        ? record.interpretation_limit
        : null,
      task_references: Array.isArray(record.task_references)
        ? record.task_references.flatMap((reference) => {
            if (!reference || typeof reference !== 'object') return [];
            const value = reference as Record<string, unknown>;
            return typeof value.task_id === 'string'
              && typeof value.role === 'string'
              && ['create', 'modify', 'read', 'remove'].includes(value.role)
              ? [{
                  task_id: value.task_id,
                  role: value.role as ProjectStructureNode['task_references'][number]['role'],
                  classification_at_link: typeof value.classification_at_link === 'string'
                    && CLASSIFICATIONS.has(value.classification_at_link)
                    ? value.classification_at_link as ProjectStructureNode['classification']
                    : null,
                }]
              : [];
          })
        : [],
      test_references: Array.isArray(record.test_references)
        ? record.test_references.flatMap((reference) => {
            if (!reference || typeof reference !== 'object') return [];
            const value = reference as Record<string, unknown>;
            return typeof value.test_id === 'string'
              && typeof value.role === 'string'
              && ['target', 'test_file', 'fixture', 'integration_point'].includes(value.role)
              ? [{
                  test_id: value.test_id,
                  role: value.role as ProjectStructureNode['test_references'][number]['role'],
                  classification_at_link: typeof value.classification_at_link === 'string'
                    && CLASSIFICATIONS.has(value.classification_at_link)
                    ? value.classification_at_link as ProjectStructureNode['classification']
                    : null,
                }]
              : [];
          })
        : [],
      evidence_ids: stringList(record.evidence_ids),
    });
  }

  return { nodes, issues };
}

function compareNodes(left: ProjectStructureNode, right: ProjectStructureNode): number {
  return left.position - right.position
    || left.name.localeCompare(right.name)
    || left.id.localeCompare(right.id);
}

export function siblingNodes(
  nodes: readonly ProjectStructureNode[],
  parentId: string | null,
  includeRevoked = false,
): ProjectStructureNode[] {
  return nodes
    .filter((node) => (
      node.parent_id === parentId
      && (includeRevoked || node.status === 'active')
    ))
    .sort(compareNodes);
}

export function flattenProjectStructure(
  nodes: readonly ProjectStructureNode[],
  expandedIds: ReadonlySet<string>,
  includeRevoked = false,
): { rows: VisibleProjectStructureNode[]; issues: string[] } {
  const included = nodes.filter((node) => includeRevoked || node.status === 'active');
  const byId = new Map(included.map((node) => [node.id, node]));
  const children = new Map<string | null, ProjectStructureNode[]>();
  const issues: string[] = [];

  for (const node of included) {
    const parentId = node.parent_id && byId.has(node.parent_id)
      ? node.parent_id
      : null;
    if (node.parent_id && parentId === null) {
      issues.push(`Node ${node.id} references a missing parent.`);
    }
    const group = children.get(parentId) ?? [];
    group.push(node);
    children.set(parentId, group);
  }
  for (const group of children.values()) group.sort(compareNodes);

  const rows: VisibleProjectStructureNode[] = [];
  const emitted = new Set<string>();
  const reachableFromRoot = new Set<string>();
  const markReachable = (node: ProjectStructureNode, lineage: Set<string>) => {
    if (lineage.has(node.id) || reachableFromRoot.has(node.id)) return;
    reachableFromRoot.add(node.id);
    const nextLineage = new Set(lineage);
    nextLineage.add(node.id);
    for (const child of children.get(node.id) ?? []) markReachable(child, nextLineage);
  };
  const visit = (node: ProjectStructureNode, depth: number, lineage: Set<string>) => {
    if (lineage.has(node.id)) {
      issues.push(`Cycle detected at node ${node.id}.`);
      return;
    }
    if (emitted.has(node.id)) return;
    emitted.add(node.id);
    const descendants = children.get(node.id) ?? [];
    rows.push({ node, depth, hasChildren: descendants.length > 0 });
    if (!expandedIds.has(node.id)) return;
    const nextLineage = new Set(lineage);
    nextLineage.add(node.id);
    for (const child of descendants) visit(child, depth + 1, nextLineage);
  };

  for (const root of children.get(null) ?? []) {
    markReachable(root, new Set());
    visit(root, 0, new Set());
  }
  // A corrupt cycle may have no root. Surface it as a bounded top-level row
  // instead of dropping data or recursing forever.
  for (const node of included.sort(compareNodes)) {
    if (!reachableFromRoot.has(node.id) && !emitted.has(node.id)) {
      issues.push(`Node ${node.id} is disconnected from the visible root set.`);
      visit(node, 0, new Set());
    }
  }
  return { rows, issues };
}

export function projectStructureProjectionForCard(
  nodes: readonly ProjectStructureNode[],
  cardId: string,
  cardType: CardType | string | undefined,
): ProjectStructureProjectionNode[] {
  if (cardType === 'bug') return [];
  const active = nodes.filter((node) => node.status === 'active');
  const byId = new Map(active.map((node) => [node.id, node]));
  const directIds = new Set(
    active
      .filter((node) => (
        cardType === 'test'
          ? node.test_references.some((reference) => reference.test_id === cardId)
          : node.task_references.some((reference) => reference.task_id === cardId)
      ))
      .map((node) => node.id),
  );
  const projectedIds = new Set(directIds);

  for (const directId of directIds) {
    let cursor = byId.get(directId);
    const lineage = new Set<string>();
    while (cursor?.parent_id && !lineage.has(cursor.id)) {
      lineage.add(cursor.id);
      const parent = byId.get(cursor.parent_id);
      if (!parent) break;
      projectedIds.add(parent.id);
      cursor = parent;
    }
  }

  const projected = active.filter((node) => projectedIds.has(node.id));
  const projectedById = new Map(projected.map((node) => [node.id, node]));
  const children = new Map<string | null, ProjectStructureNode[]>();
  for (const node of projected) {
    const parentId = node.parent_id && projectedById.has(node.parent_id)
      ? node.parent_id
      : null;
    const group = children.get(parentId) ?? [];
    group.push(node);
    children.set(parentId, group);
  }
  for (const group of children.values()) group.sort(compareNodes);

  const result: ProjectStructureProjectionNode[] = [];
  const visit = (node: ProjectStructureNode, depth: number) => {
    result.push({
      node,
      depth,
      direct: directIds.has(node.id),
      context_only: !directIds.has(node.id),
      reference_role: cardType === 'test'
        ? node.test_references.find((reference) => reference.test_id === cardId)?.role ?? null
        : node.task_references.find((reference) => reference.task_id === cardId)?.role ?? null,
    });
    for (const child of children.get(node.id) ?? []) visit(child, depth + 1);
  };
  for (const root of children.get(null) ?? []) visit(root, 1);
  return result;
}

export function nextSiblingPosition(
  nodes: readonly ProjectStructureNode[],
  parentId: string | null,
): number {
  // Revoked nodes are outside the active sibling ordering contract. Including
  // their historical positions can create a sparse/out-of-range append index.
  const siblings = siblingNodes(nodes, parentId);
  return siblings.length === 0
    ? 0
    : siblings.length;
}

export function descendantIds(
  nodes: readonly ProjectStructureNode[],
  nodeId: string,
): Set<string> {
  const result = new Set<string>();
  const queue = [nodeId];
  while (queue.length > 0) {
    const parentId = queue.shift()!;
    for (const child of nodes.filter((node) => node.parent_id === parentId)) {
      if (result.has(child.id)) continue;
      result.add(child.id);
      queue.push(child.id);
    }
  }
  return result;
}
