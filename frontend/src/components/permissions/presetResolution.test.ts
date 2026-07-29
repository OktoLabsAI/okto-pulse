import { describe, expect, it } from 'vitest';
import type { PermissionPreset } from '@/types';
import { applyPermissionDelta } from './permissionLayers';
import {
  disabledFullControlTemplate,
  findFullControlPreset,
  resolveAgentPermissionBase,
  resolvePresetLineage,
} from './presetResolution';

function preset(
  id: string,
  name: string,
  flags: Record<string, unknown>,
  options: Partial<PermissionPreset> = {},
): PermissionPreset {
  return {
    id,
    owner_id: null,
    name,
    description: null,
    is_builtin: true,
    base_preset_id: null,
    flags: flags as PermissionPreset['flags'],
    owner_review_required: false,
    review_reason: null,
    created_at: '2026-07-27T00:00:00Z',
    updated_at: null,
    ...options,
  };
}

const executor = preset(
  'builtin-executor',
  'Executor',
  { board: { entity: { read: false, update: false } } },
);
const fullControl = preset(
  'builtin-full-control',
  'Full Control',
  { board: { entity: { read: true, update: true } } },
);

describe('preset resolution', () => {
  it('locates Full Control by trusted identity/name, never array position', () => {
    const customImpostor = preset(
      'custom-impostor',
      'Full Control',
      { board: { entity: { read: false, update: false } } },
      { is_builtin: false },
    );
    const shuffled = [executor, customImpostor, fullControl];

    expect(findFullControlPreset(shuffled)).toBe(fullControl);
    expect(disabledFullControlTemplate(shuffled)).toEqual({
      board: { entity: { read: false, update: false } },
    });
  });

  it('uses the real Full Control base for a preset-less agent with a delta', () => {
    const resolution = resolveAgentPermissionBase(
      null,
      [executor, fullControl],
    );
    expect(resolution.preset).toBe(fullControl);
    expect(resolution.label).toBe('Full Control');
    expect(
      applyPermissionDelta(resolution.flags!, {
        board: { entity: { read: false } },
      }),
    ).toEqual({
      board: { entity: { read: false, update: true } },
    });
  });

  it('resolves a custom preset direct base and full lineage when shuffled', () => {
    const child = preset(
      'custom-child',
      'Custom Child',
      { board: { entity: { read: false, update: false } } },
      {
        is_builtin: false,
        base_preset_id: executor.id,
      },
    );
    const lineage = resolvePresetLineage(
      child,
      [fullControl, child, executor],
    );

    expect(lineage.directBase).toBe(executor);
    expect(lineage.baseLabel).toBe('Executor');
    expect(lineage.state).toBe('resolved');
    expect(lineage.chainLabel).toBe('Custom Child → Executor');
    expect(lineage.canResetToBase).toBe(true);
  });

  it.each([
    {
      label: 'dangling',
      presets: () => {
        const child = preset(
          'dangling',
          'Dangling',
          {},
          {
            is_builtin: false,
            base_preset_id: 'missing-base',
            owner_review_required: true,
            review_reason: 'dangling_base_preset',
          },
        );
        return { child, catalog: [executor, child] };
      },
      state: 'dangling',
      baseLabel: 'missing-base',
    },
    {
      label: 'cycle',
      presets: () => {
        const first = preset(
          'cycle-a',
          'Cycle A',
          {},
          {
            is_builtin: false,
            base_preset_id: 'cycle-b',
            owner_review_required: true,
            review_reason: 'preset_lineage_cycle',
          },
        );
        const second = preset(
          'cycle-b',
          'Cycle B',
          {},
          {
            is_builtin: false,
            base_preset_id: 'cycle-a',
            owner_review_required: true,
            review_reason: 'preset_lineage_cycle',
          },
        );
        return { child: first, catalog: [second, executor, first] };
      },
      state: 'cycle',
      baseLabel: 'Cycle B',
    },
  ])(
    'makes $label lineage and owner review explicit',
    ({ presets, state, baseLabel }) => {
      const { child, catalog } = presets();
      const lineage = resolvePresetLineage(child, catalog);

      expect(lineage.state).toBe(state);
      expect(lineage.baseLabel).toBe(baseLabel);
      expect(lineage.ownerReviewRequired).toBe(true);
      expect(lineage.canResetToBase).toBe(false);
    },
  );

  it('preserves a selected preset owner-review state for agent editing', () => {
    const dangerous = preset(
      'custom-danger',
      'Dangerous lineage',
      { board: { entity: { read: false, update: false } } },
      {
        is_builtin: false,
        base_preset_id: 'missing',
        owner_review_required: true,
        review_reason: 'dangling_base_preset',
      },
    );

    const resolution = resolveAgentPermissionBase(
      dangerous.id,
      [executor, dangerous, fullControl],
    );
    expect(resolution.flags).toEqual(dangerous.flags);
    expect(resolution.ownerReviewRequired).toBe(true);
    expect(resolution.reviewReason).toBe('dangling_base_preset');
  });
});
