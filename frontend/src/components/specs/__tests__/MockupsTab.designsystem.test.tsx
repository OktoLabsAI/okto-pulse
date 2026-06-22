// Spec 3a006f65 / card 0192f58d / FR6 — the mockup creation UI carries the Design
// System consumption metadata (design_system_ref / version / evidence) into the saved
// mockup, and the viewer surfaces a Design System badge. The server-side
// MockupDesignSystemGate enforces it; the UI is the surface that feeds it.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { MockupsTab } from '../MockupsTab';
import type { ScreenMockup } from '@/types';

describe('MockupsTab Design System fields', () => {
  beforeEach(() => vi.clearAllMocks());

  it('includes design_system_ref + version + evidence in the created mockup', async () => {
    const onUpdate = vi.fn().mockResolvedValue(undefined);
    render(<MockupsTab screenMockups={[]} onUpdate={onUpdate} />);

    fireEvent.click(screen.getByText('Add Mockup'));
    fireEvent.change(screen.getByPlaceholderText('e.g. Login Page'), { target: { value: 'Login' } });
    // the HTML textarea is the only textarea in the form.
    fireEvent.change(screen.getByTestId('mockup-ds-ref'), { target: { value: 'ds-123' } });
    fireEvent.change(screen.getByTestId('mockup-ds-version'), { target: { value: '2' } });
    fireEvent.change(screen.getByTestId('mockup-ds-evidence'), { target: { value: 'figma://proof' } });
    // fill the html content (required) — it's the form's textarea.
    const textarea = document.querySelector('textarea')!;
    fireEvent.change(textarea, { target: { value: '<div/>' } });

    fireEvent.click(screen.getByText('Add Mockup'));
    await waitFor(() => expect(onUpdate).toHaveBeenCalled());
    const saved = onUpdate.mock.calls[0][0] as ScreenMockup[];
    const created = saved[saved.length - 1];
    expect(created.design_system_ref).toEqual({ design_system_id: 'ds-123', version: 2 });
    expect(created.design_system_evidence).toBe('figma://proof');
  });

  it('renders a Design System badge with evidence state on the selected mockup', () => {
    const mockups: ScreenMockup[] = [{
      id: 'm1', title: 'Login', description: null, screen_type: 'page', html_content: '<div/>',
      annotations: null, order: 0,
      design_system_ref: { design_system_id: 'ds-9', version: 3 },
      design_system_evidence: 'proof',
    }];
    render(<MockupsTab screenMockups={mockups} />);
    const badge = screen.getByTestId('mockup-ds-badge');
    expect(badge.textContent).toMatch(/ds-9/);
    expect(badge.textContent).toMatch(/v3/);
    expect(badge.textContent).toMatch(/evidence ✓/);
  });

  it('shows the backend gate message on a rejected save (not the generic fallback)', async () => {
    // BUG 3 teeth: the MockupDesignSystemGate (blocking) rejects the save; authFetch throws
    // an Error whose `.message` carries the backend's structured message. The UI must surface
    // it, NOT the generic "Failed to save mockup." fallback (which is what the old
    // `e.detail`-only read produced for an Error that has no `.detail`).
    const message =
      "This board enforces a Design System: the mockup must reference the board's effective Design System (design_system_ref).";
    const onUpdate = vi.fn().mockRejectedValue(new Error(message));
    render(<MockupsTab screenMockups={[]} onUpdate={onUpdate} />);

    fireEvent.click(screen.getByText('Add Mockup')); // open the form
    fireEvent.change(screen.getByPlaceholderText('e.g. Login Page'), { target: { value: 'Login' } });
    const textarea = document.querySelector('textarea')!;
    fireEvent.change(textarea, { target: { value: '<div/>' } });
    fireEvent.click(screen.getByText('Add Mockup')); // submit -> onUpdate rejects

    const err = await screen.findByTestId('mockup-gate-error');
    expect(err.textContent).toMatch(/This board enforces a Design System/);
    expect(err.textContent).not.toMatch(/Failed to save mockup/);
  });
});
