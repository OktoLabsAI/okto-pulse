import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import {
  GuidedHelpProvider,
  guidedHelpRegistry,
} from '@/components/guided-help';
import { HelpPanel } from '../HelpPanel';

describe('HelpPanel test-scenario semantics', () => {
  it('explains negative outcomes without treating failure as success', () => {
    render(
      <GuidedHelpProvider registry={guidedHelpRegistry} surface="help">
        <HelpPanel onClose={vi.fn()} />
      </GuidedHelpProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: /^Specs$/i }));

    expect(
      screen.getByRole('heading', { name: 'Negative scenario outcomes' }),
    ).toBeInTheDocument();
    const explanation = screen.getByText(
      (_, element) =>
        element?.tagName === 'P' &&
        /negative describes an expected failure path/i.test(
          element.textContent ?? '',
        ) &&
        /passed means the expected error\/status and the expected invariants occurred/i.test(
          element.textContent ?? '',
        ) &&
        /failed means the observed behavior diverged/i.test(
          element.textContent ?? '',
        ),
    );

    expect(explanation).toBeInTheDocument();
  });
});
