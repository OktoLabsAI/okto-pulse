import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ContextualHelpLink } from '../ContextualHelpLink';
import {
  CONTEXTUAL_HELP_EVENT,
  openContextualHelp,
  subscribeContextualHelp,
} from '../contextualHelp';

describe('contextual Help events', () => {
  it('dispatches and subscribes with a typed section id', () => {
    const listener = vi.fn();
    const unsubscribe = subscribeContextualHelp(listener);

    openContextualHelp('requirement-lint');

    expect(listener).toHaveBeenCalledWith({
      sectionId: 'requirement-lint',
    });
    unsubscribe();
    openContextualHelp('curated-spec-checklist');
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it('ignores malformed events at the runtime boundary', () => {
    const listener = vi.fn();
    const unsubscribe = subscribeContextualHelp(listener);

    window.dispatchEvent(
      new CustomEvent(CONTEXTUAL_HELP_EVENT, {
        detail: { sectionId: 'not-a-help-section' },
      }),
    );

    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });

  it('provides an accessible reusable link that opens the requested section', () => {
    const listener = vi.fn();
    const unsubscribe = subscribeContextualHelp(listener);
    render(
      <ContextualHelpLink
        sectionId="requirement-lint"
        ariaLabel="Learn how requirement lint is calculated"
      >
        How is this calculated?
      </ContextualHelpLink>,
    );

    fireEvent.click(
      screen.getByRole('button', {
        name: 'Learn how requirement lint is calculated',
      }),
    );

    expect(listener).toHaveBeenCalledWith({
      sectionId: 'requirement-lint',
    });
    unsubscribe();
  });

  it('opens Policy Governance without submitting a surrounding form', () => {
    const listener = vi.fn();
    const onSubmit = vi.fn((event: React.FormEvent) => event.preventDefault());
    const unsubscribe = subscribeContextualHelp(listener);
    render(
      <form onSubmit={onSubmit}>
        <ContextualHelpLink
          sectionId="policy-governance"
          testId="policy-governance-help"
        >
          Policy governance help
        </ContextualHelpLink>
      </form>,
    );

    fireEvent.click(screen.getByTestId('policy-governance-help'));

    expect(listener).toHaveBeenCalledWith({
      sectionId: 'policy-governance',
    });
    expect(onSubmit).not.toHaveBeenCalled();
    unsubscribe();
  });
});
