import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { GrafxBranding } from './GrafxBranding';
import grafxIcon from '@/assets/okto-grafx-icon.svg';

describe('Grafx attribution', () => {
  it('renders the supplied bundled SVG with fixed dimensions and no duplicate accessible name', () => {
    const { container } = render(<GrafxBranding />);
    expect(screen.getByText('Powered by Okto Grafx')).toBeInTheDocument();
    const icon = container.querySelector('img');
    expect(icon).toHaveAttribute('src', grafxIcon);
    expect(icon).toHaveAttribute('alt', '');
    expect(icon).toHaveAttribute('aria-hidden', 'true');
    expect(icon).toHaveAttribute('width', '28');
    expect(icon).toHaveAttribute('height', '28');
  });
});
