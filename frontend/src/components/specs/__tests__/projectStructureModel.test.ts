import { describe, expect, it } from 'vitest';

import type { ProjectStructureNode } from '@/types';
import {
  canMutateProjectStructureInStatus,
  canRelateProjectStructureInStatus,
  flattenProjectStructure,
  nextSiblingPosition,
  parseProjectStructureNodes,
  projectStructureProjectionForCard,
  shouldShowProjectStructureTab,
} from '../projectStructureModel';

function node(
  id: string,
  parentId: string | null,
  position: number,
  overrides: Partial<ProjectStructureNode> = {},
): ProjectStructureNode {
  return {
    id,
    parent_id: parentId,
    position,
    kind: 'folder',
    name: id,
    note: `Note for ${id}`,
    classification: 'to_be',
    state: 'planned',
    interpretation_limit: null,
    status: 'active',
    task_references: [],
    test_references: [],
    evidence_ids: [],
    ...overrides,
  };
}

describe('projectStructureModel', () => {
  it('keeps the optional tab quiet for read-only absent state, but visible to authors or once authored', () => {
    expect(shouldShowProjectStructureTab(true, false, undefined)).toBe(false);
    expect(shouldShowProjectStructureTab(true, false, null)).toBe(false);
    expect(shouldShowProjectStructureTab(true, true, undefined)).toBe(true);
    expect(shouldShowProjectStructureTab(true, false, [])).toBe(true);
    expect(shouldShowProjectStructureTab(false, true, [])).toBe(false);
    expect(canMutateProjectStructureInStatus(true, 'draft')).toBe(true);
    expect(canMutateProjectStructureInStatus(true, 'approved')).toBe(false);
    expect(canMutateProjectStructureInStatus(true, 'in_progress')).toBe(false);
    expect(canMutateProjectStructureInStatus(false, 'draft')).toBe(false);
    expect(canRelateProjectStructureInStatus(true, 'draft')).toBe(true);
    expect(canRelateProjectStructureInStatus(true, 'approved')).toBe(true);
    expect(canRelateProjectStructureInStatus(true, 'validated')).toBe(true);
    expect(canRelateProjectStructureInStatus(true, 'in_progress')).toBe(true);
    expect(canRelateProjectStructureInStatus(true, 'done')).toBe(true);
    expect(canRelateProjectStructureInStatus(true, 'review')).toBe(false);
    expect(canRelateProjectStructureInStatus(false, 'approved')).toBe(false);
  });

  it('flattens one synchronized visible sequence and hides collapsed descendants', () => {
    const nodes = [
      node('psn_root', null, 0),
      node('psn_child_b', 'psn_root', 1, { kind: 'file' }),
      node('psn_child_a', 'psn_root', 0, { kind: 'artifact' }),
    ];

    expect(flattenProjectStructure(nodes, new Set()).rows.map((row) => row.node.id))
      .toEqual(['psn_root']);
    expect(flattenProjectStructure(nodes, new Set(['psn_root'])).rows.map((row) => [row.node.id, row.depth]))
      .toEqual([
        ['psn_root', 0],
        ['psn_child_a', 1],
        ['psn_child_b', 1],
      ]);
  });

  it('projects direct Task/Test references plus ordered active ancestors and excludes Bugs', () => {
    const nodes = [
      node('psn_leaf', 'psn_folder', 0, {
        kind: 'file',
        task_references: [{ task_id: 'task-1', role: 'modify' }],
        test_references: [{ test_id: 'test-1', role: 'target' }],
      }),
      node('psn_folder', 'psn_root', 0),
      node('psn_root', null, 0),
      node('psn_unrelated', null, 1),
    ];

    const taskProjection = projectStructureProjectionForCard(nodes, 'task-1', 'normal');
    expect(taskProjection.map((item) => [item.node.id, item.depth, item.direct, item.context_only, item.reference_role]))
      .toEqual([
        ['psn_root', 1, false, true, null],
        ['psn_folder', 2, false, true, null],
        ['psn_leaf', 3, true, false, 'modify'],
      ]);
    expect(projectStructureProjectionForCard(nodes, 'test-1', 'test').at(-1)?.reference_role)
      .toBe('target');
    expect(projectStructureProjectionForCard(nodes, 'task-1', 'bug')).toEqual([]);
  });

  it('preserves the canonical classification-at-link and degrades malformed nodes safely', () => {
    const parsed = parseProjectStructureNodes([
      {
        ...node('psn_valid', null, 0),
        classification: 'as_is',
        task_references: [{ task_id: 'task-1', role: 'read', classification_at_link: 'as_is' }],
      },
      null,
    ]);

    expect(parsed.nodes[0].task_references[0]).toEqual({
      task_id: 'task-1',
      role: 'read',
      classification_at_link: 'as_is',
    });
    expect(parsed.issues).toHaveLength(1);
  });

  it('appends against active siblings only when revoked positions are stale or colliding', () => {
    const nodes = [
      node('psn_active', null, 0),
      node('psn_revoked_a', null, 1, { status: 'revoked' }),
      node('psn_revoked_b', null, 9, { status: 'revoked' }),
    ];

    expect(nextSiblingPosition(nodes, null)).toBe(1);
  });
});
