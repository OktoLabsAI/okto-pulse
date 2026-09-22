import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { KGHelpModal } from '../KGHelpModal';

vi.mock('@/hooks/useEscapeToClose', () => ({ useEscapeToClose: vi.fn() }));

describe('KGHelpModal schema contract', () => {
  afterEach(cleanup);

  it('shows the coordinated 0.6.0 graph schema version', () => {
    render(<KGHelpModal onClose={vi.fn()} />);
    expect(screen.getByText('Schema version: 0.6.0')).toBeInTheDocument();
  });
});
