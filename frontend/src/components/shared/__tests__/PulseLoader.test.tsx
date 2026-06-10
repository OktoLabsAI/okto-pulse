import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PulseLoader } from '../PulseLoader';

describe('PulseLoader', () => {
  it('renders the hero pulse trace with an accessible status role', () => {
    const { container } = render(<PulseLoader label="Loading specs..." />);
    expect(screen.getByRole('status')).toHaveAttribute(
      'aria-label',
      'Loading specs...',
    );
    expect(screen.getByText('Loading specs...')).toBeInTheDocument();
    const trace = container.querySelector('.pulse-loader__trace');
    expect(trace).not.toBeNull();
    // Fidelidade ao hero: mesmo path do ECG e pathLength normalizado.
    expect(trace?.getAttribute('d')).toContain('324 62');
    expect(trace?.getAttribute('pathLength')).toBe('1260');
  });

  it('full-screen variant adds the hero grid backdrop', () => {
    const { container } = render(<PulseLoader fullScreen />);
    expect(container.querySelector('.pulse-loader__grid')).not.toBeNull();
    expect(screen.getByRole('status')).toHaveAttribute('aria-label', 'Loading');
  });

  it('two instances do not collide on SVG defs ids', () => {
    const { container } = render(
      <>
        <PulseLoader />
        <PulseLoader />
      </>,
    );
    const gradients = container.querySelectorAll('linearGradient');
    expect(gradients).toHaveLength(2);
    expect(gradients[0].id).not.toBe(gradients[1].id);
  });
});
