import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { SpecSummary } from '@/types';
import {
  RefinementReferencesPanel,
  RefinementToSummary,
} from '../RefinementReferencesPanel';

const pushMock = vi.hoisted(() => vi.fn());

vi.mock('@/contexts/ModalStackContext', () => ({
  useOptionalModalStack: () => ({ push: pushMock }),
}));

function spec(
  overrides: Partial<SpecSummary> = {},
): SpecSummary {
  return {
    id: 'spec-1',
    board_id: 'board-1',
    ideation_id: 'ideation-1',
    refinement_id: 'refinement-1',
    title: 'Derived spec',
    description: null,
    status: 'approved',
    edition: 1,
    version: 4,
    assignee_id: null,
    created_by: 'agent-1',
    created_at: '2026-07-27T10:00:00Z',
    updated_at: '2026-07-27T10:00:00Z',
    labels: [],
    ...overrides,
  };
}

describe('RefinementToSummary', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the three lineage summary variants', () => {
    const onSeeReferences = vi.fn();
    const { rerender } = render(
      <RefinementToSummary specs={[]} onSeeReferences={onSeeReferences} />,
    );
    expect(screen.getByText('Not derived')).toBeInTheDocument();

    rerender(
      <RefinementToSummary
        specs={[
          spec({
            title: 'Validated spec',
            status: 'validated',
            edition: 2,
            version: 8,
          }),
        ]}
        onSeeReferences={onSeeReferences}
      />,
    );
    const validated = screen.getByRole('button', {
      name: 'Open spec Validated spec',
    });
    expect(validated).toHaveTextContent('Validated');
    expect(validated).toHaveTextContent('Edition 2');
    expect(
      screen.getByLabelText('Edition 2'),
    ).toBeInTheDocument();
    expect(screen.getByText('Validated')).toHaveClass('bg-purple-100');
    fireEvent.click(validated);
    expect(pushMock).toHaveBeenCalledWith({
      type: 'spec',
      id: 'spec-1',
    });

    rerender(
      <RefinementToSummary
        specs={[
          spec(),
          spec({ id: 'spec-2', title: 'Second spec' }),
        ]}
        onSeeReferences={onSeeReferences}
      />,
    );
    fireEvent.click(screen.getByText('2 specs · See references'));
    expect(onSeeReferences).toHaveBeenCalledTimes(1);
  });
});

describe('RefinementReferencesPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('navigates the origin and keeps cancelled or archived derived specs visible', () => {
    const onTabChange = vi.fn();
    const derivedSpecs = [
      spec({
        id: 'cancelled-spec',
        title: 'Cancelled spec',
        status: 'cancelled',
      }),
      spec({
        id: 'archived-spec',
        title: 'Archived spec',
        status: 'done',
        archived: true,
      }),
    ];
    const { rerender } = render(
      <RefinementReferencesPanel
        originId="ideation-1"
        origin={{
          id: 'ideation-1',
          title: 'Source ideation',
          version: 3,
        }}
        specs={derivedSpecs}
        activeTab="ideation"
        onTabChange={onTabChange}
        canDeriveSpec={false}
        derivingSpec={false}
        onCreateSpec={vi.fn()}
      />,
    );

    fireEvent.click(
      screen.getByRole('button', { name: 'Open ideation Source ideation' }),
    );
    expect(pushMock).toHaveBeenCalledWith({
      type: 'ideation',
      id: 'ideation-1',
    });

    fireEvent.click(screen.getByRole('tab', { name: /Derived specs/ }));
    expect(onTabChange).toHaveBeenCalledWith('specs');

    rerender(
      <RefinementReferencesPanel
        originId="ideation-1"
        origin={{
          id: 'ideation-1',
          title: 'Source ideation',
          version: 3,
        }}
        specs={derivedSpecs}
        activeTab="specs"
        onTabChange={onTabChange}
        canDeriveSpec={false}
        derivingSpec={false}
        onCreateSpec={vi.fn()}
      />,
    );

    expect(screen.getByText('Cancelled spec')).toBeInTheDocument();
    expect(screen.getByText('Cancelled')).toBeInTheDocument();
    expect(screen.getByText('Archived spec')).toBeInTheDocument();
    expect(screen.getByText('Archived')).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole('button', { name: 'Open spec Archived spec' }),
    );
    expect(pushMock).toHaveBeenLastCalledWith({
      type: 'spec',
      id: 'archived-spec',
    });
  });
});
