/**
 * Tests for DeadLetterInspectorModal — Wave 2 deferred vitest (spec
 * 5cb09dbc / IMPL-E). Cobre render lista, expand row, empty state e
 * error state.
 */

import { afterEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { DeadLetterInspectorModal } from './DeadLetterInspectorModal';
import * as dlqApi from '@/services/dead-letter-api';

const ROW_FIXTURE: dlqApi.DeadLetterRow = {
  id: 'dlq-1',
  board_id: 'board-test',
  artifact_type: 'spec',
  artifact_id: 'spec-abc',
  original_queue_id: 'q-42',
  attempts: 5,
  errors: [
    {
      attempt: 1,
      occurred_at: '2026-04-27T10:00:00',
      error_type: 'KuzuLockTimeout',
      message: 'lock contention on board-test',
      traceback: 'File "kg/...", line 42',
    },
    {
      attempt: 2,
      occurred_at: '2026-04-27T10:01:00',
      error_type: 'KuzuLockTimeout',
      message: 'lock contention on board-test (retry)',
      traceback: null,
    },
  ],
  dead_lettered_at: '2026-04-27T10:05:00',
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe('DeadLetterInspectorModal', () => {
  test('renderiza lista de DLQ rows após fetch success', async () => {
    vi.spyOn(dlqApi, 'getDeadLetterRows').mockResolvedValue({
      rows: [ROW_FIXTURE],
      total: 1,
      limit: 50,
      offset: 0,
    });

    render(
      <DeadLetterInspectorModal boardId="board-test" onClose={() => {}} />,
    );

    await waitFor(() => {
      expect(screen.getByText(/spec-abc/)).toBeInTheDocument();
    });
    expect(screen.getByText(/showing 1 of 1/i)).toBeInTheDocument();
  });

  test('expand de row mostra error history (entradas attempt + error_type)', async () => {
    vi.spyOn(dlqApi, 'getDeadLetterRows').mockResolvedValue({
      rows: [ROW_FIXTURE],
      total: 1,
      limit: 50,
      offset: 0,
    });

    render(
      <DeadLetterInspectorModal boardId="board-test" onClose={() => {}} />,
    );

    await waitFor(() => screen.getByText(/spec-abc/));

    fireEvent.click(screen.getByTestId('dlq-expand-dlq-1'));

    await waitFor(() => {
      expect(screen.getAllByText(/KuzuLockTimeout/).length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getAllByText(/lock contention/).length).toBeGreaterThanOrEqual(1);
  });

  test('empty state quando rows = []', async () => {
    vi.spyOn(dlqApi, 'getDeadLetterRows').mockResolvedValue({
      rows: [],
      total: 0,
      limit: 50,
      offset: 0,
    });

    render(
      <DeadLetterInspectorModal boardId="board-test" onClose={() => {}} />,
    );

    await waitFor(() => {
      expect(screen.getByTestId('dlq-empty-state')).toBeInTheDocument();
    });
    expect(screen.getByText(/no dead-lettered rows/i)).toBeInTheDocument();
  });

  test('error state quando fetch falha', async () => {
    vi.spyOn(dlqApi, 'getDeadLetterRows').mockRejectedValue(
      new Error('Network unavailable'),
    );

    render(
      <DeadLetterInspectorModal boardId="board-test" onClose={() => {}} />,
    );

    await waitFor(() => {
      expect(screen.getByText(/network unavailable/i)).toBeInTheDocument();
    });
  });

  test('close handler eh chamado quando overlay eh clicado', async () => {
    vi.spyOn(dlqApi, 'getDeadLetterRows').mockResolvedValue({
      rows: [],
      total: 0,
      limit: 50,
      offset: 0,
    });
    const onClose = vi.fn();

    render(
      <DeadLetterInspectorModal boardId="board-test" onClose={onClose} />,
    );

    await waitFor(() => screen.getByTestId('dead-letter-inspector-modal'));
    fireEvent.click(screen.getByTestId('dead-letter-inspector-modal'));
    expect(onClose).toHaveBeenCalled();
  });
});
