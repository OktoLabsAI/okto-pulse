import axe from 'axe-core';
import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';

import {
  PreviousResultsSection,
  ValidationCycleStatusBadge,
  type ValidationCycleState,
} from '../ValidationCyclePrimitives';
import {
  getValidationWorkspaceInteractionSamples,
  resetValidationWorkspaceInteractionTelemetry,
  VALIDATION_WORKSPACE_INTERACTION_METRIC,
} from '@/services/validation-workspace-telemetry';

const HUMAN_STATES: ValidationCycleState[] = [
  'not_started',
  'in_progress',
  'passed',
  'needs_attention',
  'failed',
];

function StateMatrix() {
  return (
    <main aria-label="Validation cycle states">
      <h1>Validation cycle</h1>
      <div>
        {HUMAN_STATES.map((state) => (
          <ValidationCycleStatusBadge key={state} state={state} />
        ))}
      </div>
    </main>
  );
}

function DisclosureHarness() {
  const [expanded, setExpanded] = useState(false);
  return (
    <PreviousResultsSection
      expanded={expanded}
      onToggle={() => setExpanded((value) => !value)}
      testId="history"
    >
      <label>
        History filter
        <input aria-label="History filter" defaultValue="retained" />
      </label>
    </PreviousResultsSection>
  );
}

describe('validation-cycle accessibility contract', () => {
  it.each(['light', 'dark'] as const)(
    'has no serious or critical axe violations for all human states in %s mode',
    async (theme) => {
      const { container } = render(
        <div className={theme === 'dark' ? 'dark bg-surface-950' : 'bg-white'}>
          <StateMatrix />
        </div>,
      );

      const result = await axe.run(container);
      expect(
        result.violations.filter(
          (violation) => violation.impact === 'serious'
            || violation.impact === 'critical',
        ),
      ).toEqual([]);
    },
  );

  it('supports keyboard activation, Escape focus return and cached reopening', () => {
    render(<DisclosureHarness />);
    const trigger = screen.getByTestId('history-toggle');

    trigger.focus();
    fireEvent.keyDown(trigger, { key: 'Enter' });
    // jsdom does not synthesize a native click for Enter, so exercise the same
    // button activation that browsers perform after the key event.
    fireEvent.click(trigger);
    const filter = screen.getByLabelText('History filter');
    expect(filter).toBeVisible();

    filter.focus();
    fireEvent.keyDown(filter, { key: 'Escape' });
    expect(trigger).toHaveFocus();
    expect(filter).not.toBeVisible();

    fireEvent.click(trigger);
    expect(filter).toBeVisible();
    expect(filter).toHaveValue('retained');
  });

  it('records 20 bounded expand/collapse interactions below the 200 ms budget', () => {
    resetValidationWorkspaceInteractionTelemetry();
    render(<DisclosureHarness />);
    const trigger = screen.getByTestId('history-toggle');

    for (let index = 0; index < 20; index += 1) {
      fireEvent.click(trigger);
    }

    const samples = getValidationWorkspaceInteractionSamples();
    expect(samples).toHaveLength(20);
    expect(samples.every((sample) => (
      sample.metric === VALIDATION_WORKSPACE_INTERACTION_METRIC
      && sample.interaction === 'previous_results'
      && sample.latency_ms < 200
    ))).toBe(true);
  });
});
