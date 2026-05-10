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
});
