import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { ValidationGateOverride } from './ValidationGateOverride';

function renderOverride({
  requireValue = null,
  minConfidence = null,
  minCompleteness = null,
  maxDrift = null,
  onUpdate = vi.fn().mockResolvedValue(undefined),
}: Partial<ComponentProps<typeof ValidationGateOverride>> = {}) {
  const view = render(
    <ValidationGateOverride
      requireValue={requireValue}
      minConfidence={minConfidence}
      minCompleteness={minCompleteness}
      maxDrift={maxDrift}
      parentLabel="Board default"
      onUpdate={onUpdate}
    />,
  );
  return { ...view, onUpdate };
}

describe('ValidationGateOverride', () => {
  it('exposes numeric overrides independently while the gate requirement is inherited', () => {
    renderOverride({
      requireValue: null,
      minConfidence: 88,
      minCompleteness: 91,
      maxDrift: 7,
    });

    expect(screen.getByLabelText('Min Confidence')).toHaveValue(88);
    expect(screen.getByLabelText('Min Completeness')).toHaveValue(91);
    expect(screen.getByLabelText('Max Drift')).toHaveValue(7);
    expect(screen.getByText('Inherited from Board default')).toBeInTheDocument();
  });

  it('changes only the boolean override and preserves independent thresholds', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    renderOverride({
      requireValue: true,
      minConfidence: 88,
      minCompleteness: 91,
      maxDrift: 7,
      onUpdate,
    });

    fireEvent.click(screen.getByRole('button', { name: 'Inherit' }));

    await waitFor(() => {
      expect(onUpdate).toHaveBeenCalledWith({ require_task_validation: null });
    });
    expect(screen.getByLabelText('Min Confidence')).toHaveValue(88);
    expect(screen.getByLabelText('Min Completeness')).toHaveValue(91);
    expect(screen.getByLabelText('Max Drift')).toHaveValue(7);
  });

  it('updates and clears each numeric override without coupling fields', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    renderOverride({ onUpdate });

    const confidence = screen.getByLabelText('Min Confidence');
    fireEvent.change(confidence, { target: { value: '0' } });
    fireEvent.blur(confidence);
    const drift = screen.getByLabelText('Max Drift');
    fireEvent.change(drift, { target: { value: '' } });
    fireEvent.blur(drift);

    await waitFor(() => {
      expect(onUpdate).toHaveBeenNthCalledWith(1, {
        validation_min_confidence: 0,
      });
      expect(onUpdate).toHaveBeenNthCalledWith(2, {
        validation_max_drift: null,
      });
    });
  });
});
