import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { GuidedHelpProvider, useGuidedHelp } from '../GuidedHelpProvider';
import type { GuidedHelpRegistry } from '../types';

const registry: GuidedHelpRegistry = {
  tours: [
    {
      id: 'board.overview',
      title: 'Board overview',
      surface: 'board',
      version: '1',
      steps: [
        {
          id: 'board.step.one',
          title: 'First board step',
          body: 'First',
          anchor: 'board.one',
          kind: 'navigation',
          order: 10,
        },
        {
          id: 'board.step.two',
          title: 'Second board step',
          body: 'Second',
          anchor: 'board.two',
          kind: 'feature',
          order: 20,
        },
      ],
    },
  ],
};

function Probe() {
  const guidedHelp = useGuidedHelp();

  return (
    <div>
      <div data-testid="active-step">{guidedHelp.activeStep?.id ?? 'none'}</div>
      <div data-testid="suppressed">{String(guidedHelp.isSuppressed)}</div>
      <div data-testid="skipped-all">{String(guidedHelp.skippedAll)}</div>
      <button type="button" data-testid="probe-complete" onClick={() => guidedHelp.completeStep()}>
        Complete
      </button>
      <button type="button" data-testid="probe-skip-all" onClick={() => guidedHelp.skipAll()}>
        Skip all
      </button>
      <button type="button" data-testid="probe-undo-skip-all" onClick={() => guidedHelp.undoSkipAll()}>
        Undo skip all
      </button>
    </div>
  );
}

function renderProvider(suppressWhen: ComponentProps<typeof GuidedHelpProvider>['suppressWhen'] = {}) {
  return render(
    <GuidedHelpProvider registry={registry} surface="board" suppressWhen={suppressWhen}>
      <Probe />
    </GuidedHelpProvider>,
  );
}

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

beforeEach(() => {
  localStorage.clear();
});

describe('GuidedHelpProvider', () => {
  it('suppresses the active step while overlays are open and resumes after they close', async () => {
    const view = renderProvider({ onboardingOpen: true });

    expect(screen.getByTestId('suppressed')).toHaveTextContent('true');
    expect(screen.getByTestId('active-step')).toHaveTextContent('none');

    view.rerender(
      <GuidedHelpProvider registry={registry} surface="board" suppressWhen={{ onboardingOpen: false }}>
        <Probe />
      </GuidedHelpProvider>,
    );

    expect(screen.getByTestId('suppressed')).toHaveTextContent('false');
    await waitFor(() => expect(screen.getByTestId('active-step')).toHaveTextContent('board.step.one'));
    await waitFor(() => expect(screen.getByTestId('guided-help-popover')).toBeTruthy());
  });

  it('resumes after repeated overlay toggles without duplicating the popover', async () => {
    const view = renderProvider({ modalStackActive: true });

    expect(screen.queryByTestId('guided-help-popover')).toBeNull();

    view.rerender(
      <GuidedHelpProvider registry={registry} surface="board" suppressWhen={{ modalStackActive: false }}>
        <Probe />
      </GuidedHelpProvider>,
    );

    await waitFor(() => expect(screen.getAllByTestId('guided-help-popover')).toHaveLength(1));

    view.rerender(
      <GuidedHelpProvider registry={registry} surface="board" suppressWhen={{ modalStackActive: true }}>
        <Probe />
      </GuidedHelpProvider>,
    );

    expect(screen.queryByTestId('guided-help-popover')).toBeNull();

    view.rerender(
      <GuidedHelpProvider registry={registry} surface="board" suppressWhen={{ modalStackActive: false }}>
        <Probe />
      </GuidedHelpProvider>,
    );

    await waitFor(() => expect(screen.getAllByTestId('guided-help-popover')).toHaveLength(1));
  });

  it('advances steps, blocks on Skip all, and restores eligibility with Undo skip all', async () => {
    renderProvider();

    await waitFor(() => expect(screen.getByTestId('active-step')).toHaveTextContent('board.step.one'));

    fireEvent.click(screen.getByTestId('probe-complete'));
    await waitFor(() => expect(screen.getByTestId('active-step')).toHaveTextContent('board.step.two'));

    fireEvent.click(screen.getByTestId('probe-skip-all'));
    expect(screen.getByTestId('skipped-all')).toHaveTextContent('true');
    expect(screen.getByTestId('active-step')).toHaveTextContent('none');

    fireEvent.click(screen.getByTestId('probe-undo-skip-all'));
    expect(screen.getByTestId('skipped-all')).toHaveTextContent('false');
    await waitFor(() => expect(screen.getByTestId('active-step')).toHaveTextContent('board.step.two'));
  });

  it('safe-scrolls an initially unmeasurable anchor before using anchored placement', async () => {
    const anchor = document.createElement('button');
    anchor.dataset.tourId = 'board.one';
    let measured = false;
    anchor.scrollIntoView = vi.fn(() => {
      measured = true;
    });
    anchor.getBoundingClientRect = vi.fn(() =>
      measured ? rect(120, 120, 80, 40) : rect(0, 0, 0, 0),
    );
    document.body.append(anchor);

    renderProvider();

    await waitFor(() => expect(anchor.scrollIntoView).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByTestId('guided-help-popover')).toHaveAttribute('data-fallback', 'false'),
    );
  });
});
