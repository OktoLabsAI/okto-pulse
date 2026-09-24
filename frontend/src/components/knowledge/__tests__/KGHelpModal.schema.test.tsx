import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { KGHelpModal } from '../KGHelpModal';

vi.mock('@/hooks/useEscapeToClose', () => ({ useEscapeToClose: vi.fn() }));

describe('KGHelpModal schema contract', () => {
  afterEach(cleanup);

  it('shows the coordinated 0.7.0 graph schema version', () => {
    render(<KGHelpModal onClose={vi.fn()} />);
    expect(screen.getByText('Schema version: 0.7.0')).toBeInTheDocument();
  });

  it.each(['overview', 'consolidation-process', 'how-to-explore'])(
    'keeps %s guidance free of retired planning and maintenance workflows', (section) => {
      render(<KGHelpModal onClose={vi.fn()} initialSectionId={section} />);
      const content = screen.getByTestId(`kg-help-content-${section}`);
      expect(content).not.toHaveTextContent(/sprint|historical consolidation|backfill|retry button|toggle consolidation/i);
    },
  );
});
