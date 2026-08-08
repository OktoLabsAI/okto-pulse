import { describe, expect, it } from 'vitest';

import {
  hasEffectivePermission,
  hasPermissionWithState,
} from '@/hooks/usePermissions';
import type { PermissionsResponse } from '@/services/permissions-api';

const CARD_ACTIONS = [
  'card.entity.edit_fields',
  'card.entity.edit_bug_fields',
  'card.entity.assign',
  'card.entity.label',
  'card.entity.link_tests',
  'card.entity.link_spec',
  'card.entity.delete',
  'card.entity.manage_dependencies',
  'card.move.in_progress_to_validation',
] as const;

const SPRINT_ACTIONS = [
  'sprint.entity.edit_fields',
  'sprint.entity.edit_coverage_flags',
  'sprint.tasks.assign',
  'sprint.entity.label',
  'sprint.entity.delete',
  'sprint.evaluations.submit',
  'sprint.move.active_to_review',
] as const;

function response(flags: Record<string, unknown>): PermissionsResponse {
  return {
    board_id: 'board-1',
    preset_name: 'Custom',
    flags,
    owner_review_required: false,
    review_reason: null,
  };
}

function setFlag(
  document: Record<string, unknown>,
  path: string,
  value: boolean,
): void {
  const parts = path.split('.');
  let current = document;
  for (const part of parts.slice(0, -1)) {
    const existing = current[part];
    if (!existing || typeof existing !== 'object' || Array.isArray(existing)) {
      current[part] = {};
    }
    current = current[part] as Record<string, unknown>;
  }
  current[parts.at(-1)!] = value;
}

function permissionChecker(flags: Record<string, unknown>) {
  const data = response(flags);
  return (flag: string) => hasEffectivePermission(data, flag);
}

describe('state-aware UI permission composition', () => {
  it.each([
    ...CARD_ACTIONS.map((action) => [action, 'card', 'in_progress'] as const),
    ...SPRINT_ACTIONS.map((action) => [action, 'sprint', 'active'] as const),
  ])('denies %s when its granular action leaf is false', (action, entity, status) => {
    const flags: Record<string, unknown> = {};
    setFlag(flags, action, false);
    setFlag(flags, `${entity}.interact_in.${status}`, true);

    expect(hasPermissionWithState(
      permissionChecker(flags),
      action,
      entity,
      status,
    )).toBe(false);
  });

  it.each([
    ['card.entity.edit_fields', 'card', 'in_progress'],
    ['sprint.entity.edit_fields', 'sprint', 'active'],
  ] as const)(
    'denies %s when %s.interact_in.%s is false',
    (action, entity, status) => {
      const flags: Record<string, unknown> = {};
      setFlag(flags, action, true);
      setFlag(flags, `${entity}.interact_in.${status}`, false);

      expect(hasPermissionWithState(
        permissionChecker(flags),
        action,
        entity,
        status,
      )).toBe(false);
    },
  );

  it('allows only when both the action and current-state leaves are true', () => {
    const flags: Record<string, unknown> = {};
    setFlag(flags, 'card.entity.assign', true);
    setFlag(flags, 'card.interact_in.started', true);

    expect(hasPermissionWithState(
      permissionChecker(flags),
      'card.entity.assign',
      'card',
      'started',
    )).toBe(true);
  });

  it('preserves the hook contract for unavailable historical permissions', () => {
    const unavailable = (flag: string) => hasEffectivePermission(null, flag);

    expect(hasPermissionWithState(
      unavailable,
      'card.entity.edit_fields',
      'card',
      'not_started',
    )).toBe(true);
    expect(hasPermissionWithState(
      unavailable,
      'sprint.entity.assign',
      'sprint',
      'draft',
    )).toBe(true);

    expect(hasPermissionWithState(
      unavailable,
      'sprint.tasks.assign',
      'sprint',
      'draft',
    )).toBe(false);
  });

  it('fails closed when an existing entity has no trustworthy current status', () => {
    expect(hasPermissionWithState(() => true, 'card.entity.delete', 'card', null))
      .toBe(false);
  });

  it('requires the backend-projected historical authority for introduced leaves', () => {
    const flags: Record<string, unknown> = {};
    setFlag(flags, 'ideation.move.review_to_approved', true);
    setFlag(flags, 'ideation.entity.read', false);
    const denied = response(flags);
    denied.introduced_historical_authorities = {
      'ideation.move.review_to_approved': 'ideation.entity.read',
    };

    expect(hasEffectivePermission(denied, 'ideation.move.review_to_approved')).toBe(false);

    setFlag(flags, 'ideation.entity.read', true);
    const allowed = response(flags);
    allowed.introduced_historical_authorities = {
      'ideation.move.review_to_approved': 'ideation.entity.read',
    };
    expect(hasEffectivePermission(allowed, 'ideation.move.review_to_approved')).toBe(true);
  });
});
