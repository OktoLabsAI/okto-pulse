import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ResourceGateDisclosure } from '../ResourceGateDisclosure';

vi.mock('../ResourceGateSummary', () => ({
  ResourceGateSummary: () => <div data-testid="resource-gate-summary" />,
}));

describe('ResourceGateDisclosure', () => {
  it('keeps the detailed gate collapsed until requested', () => {
    render(
      <ResourceGateDisclosure
        boardId="board-1"
        entityType="spec"
        entityId="spec-1"
      />,
    );

    const toggle = screen.getByTestId('resource-gate-disclosure-toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(
      screen.queryByTestId('resource-gate-summary'),
    ).not.toBeInTheDocument();

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('resource-gate-summary')).toBeInTheDocument();
  });
});
