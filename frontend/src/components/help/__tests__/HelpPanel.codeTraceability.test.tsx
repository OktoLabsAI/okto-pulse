import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { HelpPanel } from '../HelpPanel';

describe('HelpPanel Code Traceability guide', () => {
  it('explains the agent boundary and the distinct Evidence views', () => {
    render(
      <HelpPanel
        initialSectionId="code-traceability"
        onClose={vi.fn()}
      />,
    );

    const dialog = screen.getByRole('dialog', { name: 'Help Guide' });
    expect(screen.getByRole('heading', {
      name: 'Code Traceability — Preserve what the agent learned about the code',
    })).toBeInTheDocument();
    expect(dialog).toHaveTextContent(/does not access, clone, scan, or resolve source code/i);
    expect(dialog).toHaveTextContent(/Code Evidence is the observation/i);
    expect(dialog).toHaveTextContent(/Code Evidence Matrix is the coverage view/i);
    expect(dialog).toHaveTextContent(/force the repository investigation to be repeated/i);
    expect(dialog).toHaveTextContent(/technical anchors: mutable implementation intent/i);
    expect(dialog).toHaveTextContent(/runs a target-bound preflight and resolves each target/i);
    expect(dialog).toHaveTextContent(/runs a fresh result-state preflight and records the execution disposition/i);
  });
});
