import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiMock = vi.hoisted(() => ({
  getChecklistBinding: vi.fn(),
  listChecklistTemplates: vi.fn(),
  updateChecklistBinding: vi.fn(),
}));

vi.mock('@/services/api', () => ({ useDashboardApi: () => apiMock }));

import { ChecklistBindingSettings } from '../ChecklistBindingSettings';

const binding = {
  id: 'a'.repeat(64),
  board_id: 'board-1',
  target_type: 'spec' as const,
  phase: 'spec_validation' as const,
  mode: 'advisory' as const,
  version: 3,
  expected_revision: 3,
  digest: 'a'.repeat(64),
  template_version_id: '/specify/v1' as const,
};

describe('ChecklistBindingSettings', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getChecklistBinding.mockResolvedValue(binding);
    apiMock.listChecklistTemplates.mockResolvedValue({
      total: 1,
      items: [
        {
          template_id: 'specify',
          version: '/specify/v1',
          digest: 'b'.repeat(64),
          items: Array.from({ length: 10 }, (_, index) => ({
            item_id: `item-${index}`,
            title_en: `Item ${index}`,
            title_pt: `Item ${index}`,
            description_en: 'Description',
            description_pt: 'Descrição',
            allow_na: false,
          })),
        },
      ],
    });
    apiMock.updateChecklistBinding.mockResolvedValue({
      binding_id: 'c'.repeat(64),
      revision: 4,
      effective: {
        ...binding,
        id: 'c'.repeat(64),
        digest: 'c'.repeat(64),
        mode: 'blocking',
        version: 4,
        expected_revision: 4,
      },
    });
  });

  it('saves a human-selected mode with the immutable template and CAS revision', async () => {
    render(<ChecklistBindingSettings boardId="board-1" />);

    await screen.findByTestId('checklist-binding-settings');
    await waitFor(() =>
      expect(screen.getByTestId('checklist-mode-advisory')).toHaveAttribute(
        'aria-checked',
        'true',
      ),
    );

    fireEvent.click(screen.getByTestId('checklist-mode-blocking'));
    fireEvent.click(screen.getByRole('button', { name: 'Save policy' }));

    await waitFor(() =>
      expect(apiMock.updateChecklistBinding).toHaveBeenCalledWith('board-1', {
        mode: 'blocking',
        template_version_id: '/specify/v1',
        expected_revision: 3,
      }),
    );
  });

  it('uses the returned real revision for a consecutive update', async () => {
    const blocking = {
      ...binding,
      id: 'c'.repeat(64),
      digest: 'c'.repeat(64),
      mode: 'blocking' as const,
      version: 4,
      expected_revision: 4,
    };
    apiMock.updateChecklistBinding
      .mockResolvedValueOnce({
        binding_id: blocking.id,
        revision: 4,
        effective: blocking,
      })
      .mockResolvedValueOnce({
        binding_id: 'd'.repeat(64),
        revision: 5,
        effective: {
          ...blocking,
          id: 'd'.repeat(64),
          digest: 'd'.repeat(64),
          mode: 'advisory',
          version: 5,
          expected_revision: 5,
        },
      });

    render(<ChecklistBindingSettings boardId="board-1" />);
    await waitFor(() =>
      expect(screen.getByTestId('checklist-mode-advisory')).toHaveAttribute(
        'aria-checked',
        'true',
      ),
    );

    fireEvent.click(screen.getByTestId('checklist-mode-blocking'));
    fireEvent.click(screen.getByRole('button', { name: 'Save policy' }));
    await waitFor(() =>
      expect(screen.getByTestId('checklist-mode-blocking')).toHaveAttribute(
        'aria-checked',
        'true',
      ),
    );

    fireEvent.click(screen.getByTestId('checklist-mode-advisory'));
    fireEvent.click(screen.getByRole('button', { name: 'Save policy' }));

    await waitFor(() => expect(apiMock.updateChecklistBinding).toHaveBeenCalledTimes(2));
    expect(apiMock.updateChecklistBinding.mock.calls[1][1]).toEqual({
      mode: 'advisory',
      template_version_id: '/specify/v1',
      expected_revision: 4,
    });
  });

  it('offers a contextual link to the Curated Spec Checklist help page', async () => {
    const onOpenHelp = vi.fn();
    render(
      <ChecklistBindingSettings
        boardId="board-1"
        onOpenHelp={onOpenHelp}
      />,
    );

    fireEvent.click(await screen.findByTestId('checklist-help-link'));

    expect(onOpenHelp).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('checklist-help-link')).toHaveAccessibleName(
      'Learn about Curated Spec Checklist',
    );
  });
});
