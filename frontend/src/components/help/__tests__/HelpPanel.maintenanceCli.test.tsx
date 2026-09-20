import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { HelpPanel } from '../HelpPanel';

describe('HelpPanel after maintenance CLI retirement', () => {
  it('explains schema unavailability without directing the user to a removed CLI', () => {
    render(<HelpPanel initialSectionId="knowledge-graph" onClose={vi.fn()} />);
    const dialog = screen.getByRole('dialog', { name: 'Help Guide' });
    expect(screen.getByRole('heading', { name: 'Schema availability' })).toBeInTheDocument();
    expect(dialog).toHaveTextContent(/schema incompatibility makes the affected graph operation unavailable/i);
    expect(dialog).toHaveTextContent(/health observations are not authority to migrate or repair storage/i);
    expect(dialog).toHaveTextContent(/project knowledge queries and semantic consolidation retain their existing authorization/i);
    expect(dialog).not.toHaveTextContent(/okto-pulse kg\b/);
    expect(dialog).not.toHaveTextContent(/a triplet is exposed/i);
    expect(screen.getByRole('heading', { name: 'Cognitive consolidation (KG-03)' })).toBeInTheDocument();
  });
});
