import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  ENTITY_LABELS,
  PermissionFlagsEditor,
  countAllFlags,
  setAllFlags,
  type FlagsMap,
} from './PermissionFlagsEditor';

const storyTopicFlags: FlagsMap = {
  story: {
    entity: {
      read: true,
      create: false,
      edit_fields: true,
    },
    move: {
      draft_to_ready: true,
    },
    history_read: true,
  },
  topic: {
    entity: {
      read: true,
      create: true,
      merge: false,
      delete: false,
    },
  },
};

const guidelinePolicyFlags: FlagsMap = {
  guidelines: {
    revisions: {
      read: true,
      create: true,
      retire: false,
    },
    metrics: { author: true },
    impact: { preview: true },
    adoption: { manage: true },
    assessments: { read: true, record: true },
    waiver: {
      read: true,
      request: true,
      review: false,
      revoke: false,
      revalidate: false,
    },
  },
};

const structuredSpecFlags: FlagsMap = {
  spec: {
    read: true,
    structured_entity: {
      business_rule: {
        create: false,
        update: true,
        revoke: false,
      },
      decision: {
        create: false,
        update: false,
        revoke: true,
      },
      api_contract: {
        create: true,
        update: true,
      },
    },
  },
};

/**
 * Deterministic stand-in for the canonical base registry. Its 397 boolean
 * leaves deliberately live below entity.group.type, so the former two-level
 * algorithm counts 0 rather than satisfying this catalog-coverage contract.
 */
function makeBaseCatalogFixture(): FlagsMap {
  const structuredEntity: Record<string, Record<string, boolean>> = {};
  for (let index = 0; index < 397; index++) {
    const type = `catalog_type_${Math.floor(index / 20) + 1}`;
    const action = `action_${(index % 20) + 1}`;
    structuredEntity[type] ??= {};
    structuredEntity[type][action] = true;
  }
  return { spec: { structured_entity: structuredEntity } };
}

const skbGuidelineLeaves = [
  ['guidelines.revisions.read', true],
  ['guidelines.revisions.create', true],
  ['guidelines.revisions.retire', false],
  ['guidelines.metrics.author', true],
  ['guidelines.impact.preview', true],
  ['guidelines.adoption.manage', true],
  ['guidelines.assessments.read', true],
  ['guidelines.assessments.record', true],
  ['guidelines.waiver.read', true],
  ['guidelines.waiver.request', true],
  ['guidelines.waiver.review', false],
  ['guidelines.waiver.revoke', false],
  ['guidelines.waiver.revalidate', false],
] as const;

function guidelineFlagsWithToggledLeaf(path: string): FlagsMap {
  const updated = structuredClone(guidelinePolicyFlags);
  const [, level, action] = path.split('.');
  const group = updated.guidelines[level];
  if (typeof group !== 'object' || group === null) {
    throw new Error(`Missing guideline permission group: ${level}`);
  }
  group[action] = !group[action];
  return updated;
}

describe('PermissionFlagsEditor', () => {
  it('labels Stories and Topics as native permission sections', () => {
    render(<PermissionFlagsEditor flags={storyTopicFlags} readOnly />);

    expect(ENTITY_LABELS.story).toBe('Stories');
    expect(ENTITY_LABELS.topic).toBe('Topics');
    expect(screen.getByRole('button', { name: /Stories/i })).toHaveTextContent('4/5');
    expect(screen.getByRole('button', { name: /Topics/i })).toHaveTextContent('2/4');
  });

  it('renders nested Story flags and toggles them in custom presets', () => {
    const onChange = vi.fn();
    render(<PermissionFlagsEditor flags={storyTopicFlags} onChange={onChange} />);

    fireEvent.click(screen.getByRole('button', { name: /Stories/i }));

    expect(screen.getByText('entity')).toBeInTheDocument();
    expect(screen.getByText('move')).toBeInTheDocument();
    expect(screen.getByText('history_read')).toBeInTheDocument();

    const createRow = screen.getByText('create').closest('div');
    expect(createRow).not.toBeNull();
    fireEvent.click(within(createRow as HTMLElement).getByRole('button'));

    expect(onChange).toHaveBeenCalledWith({
      ...storyTopicFlags,
      story: {
        ...storyTopicFlags.story,
        entity: {
          read: true,
          create: true,
          edit_fields: true,
        },
      },
    });
  });

  it('keeps toggles disabled in read-only mode', () => {
    render(<PermissionFlagsEditor flags={storyTopicFlags} readOnly />);

    fireEvent.click(screen.getByRole('button', { name: /Topics/i }));

    const mergeRow = screen.getByText('merge').closest('div');
    expect(mergeRow).not.toBeNull();
    expect(within(mergeRow as HTMLElement).getByRole('button')).toBeDisabled();
  });

  it('counts flat and nested Story/Topic flags together', () => {
    expect(countAllFlags(storyTopicFlags)).toEqual({ enabled: 6, total: 9 });
  });

  it('renders and immutably toggles spec.structured_entity.<type>.<action>', () => {
    const onChange = vi.fn();
    render(<PermissionFlagsEditor flags={structuredSpecFlags} onChange={onChange} />);

    expect(screen.getByRole('button', { name: /Specs/i })).toHaveTextContent('5/9');
    fireEvent.click(screen.getByRole('button', { name: /Specs/i }));

    expect(screen.getByText('structured_entity')).toBeInTheDocument();
    expect(screen.getByText('business_rule')).toBeInTheDocument();
    const toggle = screen.getByRole('button', {
      name: 'Toggle spec.structured_entity.business_rule.create',
    });
    fireEvent.click(toggle);

    expect(structuredSpecFlags.spec.structured_entity).toMatchObject({
      business_rule: { create: false },
    });
    const updated = onChange.mock.calls[0][0] as FlagsMap;
    expect(updated.spec.structured_entity).toMatchObject({
      business_rule: { create: true, update: true, revoke: false },
    });
    expect(updated).not.toBe(structuredSpecFlags);
    expect(updated.spec).not.toBe(structuredSpecFlags.spec);
    expect(updated.spec.structured_entity).not.toBe(structuredSpecFlags.spec.structured_entity);
    expect(
      (updated.spec.structured_entity as Record<string, unknown>).api_contract,
    ).toBe(
      (structuredSpecFlags.spec.structured_entity as Record<string, unknown>).api_contract,
    );
  });

  it('sets all boolean leaves in an arbitrary nested subtree', () => {
    const onChange = vi.fn();
    render(<PermissionFlagsEditor flags={structuredSpecFlags} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: /Specs/i }));

    fireEvent.click(screen.getByRole('button', {
      name: 'Turn all off in spec.structured_entity',
    }));
    const parentUpdated = onChange.mock.calls[0][0] as FlagsMap;
    expect(parentUpdated.spec.structured_entity).toMatchObject({
      business_rule: { create: false, update: false, revoke: false },
      decision: { create: false, update: false, revoke: false },
      api_contract: { create: false, update: false },
    });

    onChange.mockClear();
    fireEvent.click(screen.getByRole('button', {
      name: 'Turn all on in spec.structured_entity.decision',
    }));

    const updated = onChange.mock.calls[0][0] as FlagsMap;
    expect(updated.spec.structured_entity).toMatchObject({
      decision: { create: true, update: true, revoke: true },
      business_rule: { create: false, update: true, revoke: false },
    });
    expect(screen.getByRole('button', {
      name: 'Turn all off in spec.structured_entity',
    })).toHaveTextContent('all off');
  });

  it('enforces read-only mode for leaves and controls at every depth', () => {
    const onChange = vi.fn();
    render(
      <PermissionFlagsEditor
        flags={structuredSpecFlags}
        onChange={onChange}
        readOnly
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /Specs/i }));

    const deepToggle = screen.getByRole('button', {
      name: 'Toggle spec.structured_entity.business_rule.create',
    });
    expect(deepToggle).toBeDisabled();
    fireEvent.click(deepToggle);
    expect(screen.queryByRole('button', {
      name: 'Turn all on in spec.structured_entity.business_rule',
    })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {
      name: 'Turn all off in spec.structured_entity',
    })).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('counts and recursively updates all 397 base-catalog leaves', () => {
    const baseCatalog = makeBaseCatalogFixture();

    expect(countAllFlags(baseCatalog)).toEqual({ enabled: 397, total: 397 });
    const disabledCatalog = setAllFlags(baseCatalog, false);
    expect(countAllFlags(disabledCatalog)).toEqual({ enabled: 0, total: 397 });
    expect(baseCatalog).not.toBe(disabledCatalog);
    expect(baseCatalog.spec.structured_entity).not.toBe(disabledCatalog.spec.structured_entity);
    expect(disabledCatalog.spec.structured_entity).toMatchObject({
      catalog_type_1: { action_1: false },
      catalog_type_20: { action_17: false },
    });
  });

  it('never renders or updates an object as though it were a boolean leaf', () => {
    const onChange = vi.fn();
    render(<PermissionFlagsEditor flags={structuredSpecFlags} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: /Specs/i }));

    expect(screen.queryByRole('button', {
      name: 'Toggle spec.structured_entity.business_rule',
    })).not.toBeInTheDocument();
    expect(screen.getByRole('button', {
      name: 'Toggle spec.structured_entity.business_rule.create',
    })).toBeInTheDocument();

    const disabled = setAllFlags(structuredSpecFlags, false);
    expect(typeof disabled.spec.structured_entity).toBe('object');
    expect(typeof (
      disabled.spec.structured_entity as Record<string, unknown>
    ).business_rule).toBe('object');
    expect(disabled.spec.structured_entity).toMatchObject({
      business_rule: { create: false, update: false, revoke: false },
    });
  });

  it('renders and edits every SK-B3 guideline permission leaf generically', () => {
    const onChange = vi.fn();
    render(
      <PermissionFlagsEditor
        flags={guidelinePolicyFlags}
        onChange={onChange}
      />,
    );

    expect(
      screen.getByRole('button', { name: /Guidelines/i }),
    ).toHaveTextContent('9/13');
    fireEvent.click(screen.getByRole('button', { name: /Guidelines/i }));

    for (const level of [
      'revisions',
      'metrics',
      'impact',
      'adoption',
      'assessments',
      'waiver',
    ]) {
      expect(screen.getByText(level)).toBeInTheDocument();
    }

    for (const [path, enabled] of skbGuidelineLeaves) {
      const toggle = screen.getByRole('button', {
        name: `Toggle ${path}`,
      });
      expect(toggle).toHaveAttribute('type', 'button');
      expect(toggle).toHaveAttribute('aria-pressed', String(enabled));

      onChange.mockClear();
      fireEvent.click(toggle);
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith(
        guidelineFlagsWithToggledLeaf(path),
      );
    }

    for (const button of screen.getAllByRole('button')) {
      expect(button).toHaveAttribute('type', 'button');
    }
  });

  it('keeps every SK-B guideline permission immutable in read-only mode', () => {
    const onChange = vi.fn();
    render(
      <PermissionFlagsEditor
        flags={guidelinePolicyFlags}
        onChange={onChange}
        readOnly
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: /Guidelines/i }));

    for (const [path, enabled] of skbGuidelineLeaves) {
      const toggle = screen.getByRole('button', {
        name: `Toggle ${path}`,
      });
      expect(toggle).toBeDisabled();
      expect(toggle).toHaveAttribute('type', 'button');
      expect(toggle).toHaveAttribute('aria-pressed', String(enabled));
      fireEvent.click(toggle);
    }
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'all on' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'all off' })).not.toBeInTheDocument();
  });
});
