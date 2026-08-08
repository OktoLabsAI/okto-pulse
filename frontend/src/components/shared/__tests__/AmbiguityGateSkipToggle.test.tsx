import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { AmbiguityGateSkipToggle } from '../AmbiguityGateSkipToggle';

describe('AmbiguityGateSkipToggle', () => {
  it.each(['ideation', 'refinement'] as const)(
    'uses the shared accessible switch pattern for %s',
    (subjectLabel) => {
      const onCheckedChange = vi.fn();

      render(
        <AmbiguityGateSkipToggle
          subjectLabel={subjectLabel}
          checked={false}
          onCheckedChange={onCheckedChange}
        />,
      );

      const toggle = screen.getByRole('switch', {
        name: `Skip the Max ambiguity gate for this ${subjectLabel}`,
      });
      expect(toggle).toHaveAttribute('aria-checked', 'false');
      expect(screen.getByTestId('ambiguity-gate-skip-control')).toHaveTextContent(
        `Allow this ${subjectLabel} to complete without the board ambiguity gate.`,
      );

      fireEvent.click(toggle);
      expect(onCheckedChange).toHaveBeenCalledWith(true);
    },
  );
});
