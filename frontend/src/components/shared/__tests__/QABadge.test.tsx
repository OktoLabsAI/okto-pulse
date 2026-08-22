import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { QABadge } from '../QABadge';

describe('QABadge', () => {
  it('announces singular and plural unanswered-question counts', () => {
    const { rerender } = render(<QABadge count={1} />);

    let badge = screen.getByLabelText('1 unanswered question');
    expect(badge).toHaveTextContent('1 open Q&A');
    expect(badge.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');

    rerender(<QABadge count={2} />);

    badge = screen.getByLabelText('2 unanswered questions');
    expect(badge).toHaveTextContent('2 open Q&A');
  });

  it('keeps the compact badge understandable to assistive technology', () => {
    render(<QABadge count={3} compact />);

    const badge = screen.getByLabelText('3 unanswered questions');
    expect(badge).toHaveTextContent('3');
    expect(badge).not.toHaveTextContent('open Q&A');
  });

  it.each([0, -1, null, undefined])('omits non-positive count %s', (count) => {
    render(<QABadge count={count} />);

    expect(screen.queryByTestId('qa-open-badge')).not.toBeInTheDocument();
  });
});
