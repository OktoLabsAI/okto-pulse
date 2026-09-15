/**
 * Tests for DeadLetterInspectorModal — Wave 2 deferred vitest (spec
 * 5cb09dbc / IMPL-E). Cobre render lista, expand row, empty state e
 * error state.
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const permissionHas = vi.hoisted(() => vi.fn((_flag: string) => true));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ preset: 'Full Control', isLoading: false, error: null, ownerReviewRequired: false, has: permissionHas }),
}));

import { DeadLetterInspectorModal } from './DeadLetterInspectorModal';
import * as dlqApi from '@/services/dead-letter-api';
import toast from 'react-hot-toast';

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

beforeEach(() => {
  permissionHas.mockReset();
  permissionHas.mockReturnValue(true);
});

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

  test('expand de row mostra error history com error_type normalizado para UI', async () => {
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
      expect(screen.getAllByText(/GraphDBLockTimeout/).length).toBeGreaterThanOrEqual(1);
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

  test('redrive requeues the row and refreshes the inspector', async () => {
    vi.spyOn(dlqApi, 'getDeadLetterRows')
      .mockResolvedValueOnce({ rows: [ROW_FIXTURE], total: 1, limit: 50, offset: 0 })
      .mockResolvedValueOnce({ rows: [], total: 0, limit: 50, offset: 0 });
    const redriveSpy = vi.spyOn(dlqApi, 'redriveDeadLetterRows').mockResolvedValue({
      success: true,
      blocked: false,
      mutated: true,
      scope: 'generic',
      requested: 1,
      selected: 1,
      requeued_count: 1,
      already_queued_count: 0,
      process_now_mode: 'signalled_app_runner',
    });

    render(
      <DeadLetterInspectorModal boardId="board-test" onClose={() => {}} />,
    );

    await waitFor(() => screen.getByTestId('dlq-redrive-dlq-1'));
    fireEvent.click(screen.getByTestId('dlq-redrive-dlq-1'));

    await waitFor(() => {
      expect(redriveSpy).toHaveBeenCalledWith('board-test', ['dlq-1'], 'generic');
    });
    await waitFor(() => screen.getByTestId('dlq-empty-state'));
  });

  test('redrive requires the queue reprocess permission', async () => {
    permissionHas.mockImplementation(
      (flag: string) => flag !== 'kg.operations.queue.reprocess',
    );
    vi.spyOn(dlqApi, 'getDeadLetterRows').mockResolvedValue({
      rows: [ROW_FIXTURE], total: 1, limit: 50, offset: 0,
    });

    render(
      <DeadLetterInspectorModal boardId="board-test" onClose={() => {}} />,
    );

    const button = await screen.findByTestId('dlq-redrive-dlq-1');
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'Requires kg.operations.queue.reprocess');
    expect(screen.getByTestId('dlq-redrive-all')).toBeDisabled();
  });

  test('redrive all requires confirmation and refreshes after draining the board DLQ', async () => {
    vi.spyOn(dlqApi, 'getDeadLetterRows')
      .mockResolvedValueOnce({ rows: [ROW_FIXTURE], total: 1, limit: 50, offset: 0 })
      .mockResolvedValueOnce({ rows: [], total: 0, limit: 50, offset: 0 });
    const redriveAllSpy = vi.spyOn(dlqApi, 'redriveAllDeadLetterRows').mockResolvedValue({
      success: true,
      blocked: false,
      mutated: true,
      scope: 'all',
      requested: 1,
      selected: 1,
      requeued_count: 1,
      already_queued_count: 0,
      remaining: 0,
      batches: 1,
      process_now_mode: 'signalled_app_runner',
    });

    render(
      <DeadLetterInspectorModal boardId="board-test" onClose={() => {}} />,
    );

    const button = await screen.findByTestId('dlq-redrive-all');
    fireEvent.click(button);
    expect(redriveAllSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId('dlq-redrive-all-confirmation')).toHaveTextContent(
      'Redrive all 1 accessible DLQ row(s)?',
    );

    fireEvent.click(screen.getByTestId('dlq-redrive-all-confirm'));

    await waitFor(() => {
      expect(redriveAllSpy).toHaveBeenCalledWith('board-test');
    });
    await waitFor(() => screen.getByTestId('dlq-empty-state'));
  });

  test.each([
    ['refused', 'all'], ['http_failure', 'all'],
    ['refused', 'selected'], ['http_failure', 'selected'],
  ])('refreshes after partial/failed redrive without a false success: %s %s', async (outcome, selection) => {
    const read = vi.spyOn(dlqApi, 'getDeadLetterRows')
      .mockResolvedValueOnce({ rows: [ROW_FIXTURE], total: 1, limit: 50, offset: 0 })
      .mockResolvedValueOnce({ rows: [], total: 0, limit: 50, offset: 0 });
    const redrive = vi.spyOn(dlqApi, selection === 'all' ? 'redriveAllDeadLetterRows' : 'redriveDeadLetterRows');
    if (outcome === 'http_failure') redrive.mockRejectedValue(new Error('Later batch refused'));
    else redrive.mockResolvedValue({ success: false, blocked: false, mutated: true, scope: 'all', requested: 1, selected: 1, requeued_count: 1, already_queued_count: 0, remaining: 0 });
    const error = vi.spyOn(toast, 'error');
    const success = vi.spyOn(toast, 'success');
    render(<DeadLetterInspectorModal boardId="board-test" onClose={() => {}} />);
    if (selection === 'all') {
      fireEvent.click(await screen.findByTestId('dlq-redrive-all'));
      fireEvent.click(screen.getByTestId('dlq-redrive-all-confirm'));
    } else {
      fireEvent.click(await screen.findByTestId('dlq-redrive-dlq-1'));
    }
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(error).toHaveBeenCalledTimes(1);
    expect(success).not.toHaveBeenCalled();
    await waitFor(() => screen.getByTestId('dlq-empty-state'));
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
