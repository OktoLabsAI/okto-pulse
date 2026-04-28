/**
 * Tests for EvidenceGateSkipBanner — Wave 2 frontend spec 5cb09dbc.
 *
 * Cobre TS4 (conditional render + ausencia de dismiss) e TS5 (link abre
 * Board Settings via callback).
 */

import { describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { EvidenceGateSkipBanner } from './EvidenceGateSkipBanner';

describe('EvidenceGateSkipBanner', () => {
  test('TS4 — não renderiza nada quando skipActive=false', () => {
    const { container } = render(
      <EvidenceGateSkipBanner skipActive={false} onOpenBoardSettings={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId('evidence-gate-skip-banner')).not.toBeInTheDocument();
  });

  test('TS4 — renderiza banner com copy "Evidence gate bypassed" quando skipActive=true', () => {
    render(
      <EvidenceGateSkipBanner skipActive={true} onOpenBoardSettings={() => {}} />,
    );
    expect(screen.getByTestId('evidence-gate-skip-banner')).toBeInTheDocument();
    expect(screen.getByText(/evidence gate bypassed/i)).toBeInTheDocument();
    expect(screen.getByText(/skip active/i)).toBeInTheDocument();
  });

  test('TS4 — não tem botão dismiss/close visível', () => {
    render(
      <EvidenceGateSkipBanner skipActive={true} onOpenBoardSettings={() => {}} />,
    );
    // No close/dismiss buttons; only the Board Settings link is interactive.
    expect(screen.queryByLabelText(/dismiss/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/close/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /×/i })).not.toBeInTheDocument();
    // Exactly one button (the Board Settings link).
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  test('TS5 — click no link Board Settings dispara callback uma vez', () => {
    const onOpen = vi.fn();
    render(
      <EvidenceGateSkipBanner skipActive={true} onOpenBoardSettings={onOpen} />,
    );
    fireEvent.click(screen.getByTestId('evidence-gate-skip-banner-link'));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  test('skipActive=false não renderiza link mesmo se callback foi passado', () => {
    const onOpen = vi.fn();
    render(
      <EvidenceGateSkipBanner skipActive={false} onOpenBoardSettings={onOpen} />,
    );
    expect(
      screen.queryByTestId('evidence-gate-skip-banner-link'),
    ).not.toBeInTheDocument();
    expect(onOpen).not.toHaveBeenCalled();
  });
});
