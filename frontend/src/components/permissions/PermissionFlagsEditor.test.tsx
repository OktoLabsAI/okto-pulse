import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  ENTITY_LABELS,
  PermissionFlagsEditor,
  countAllFlags,
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
