/**
 * Tests for RuntimeSettingsPanel — Sprint 4 of spec bdcda842.
 *
 * Covers AC11 (tabs preserve drafts) + AC12 (polling lifecycle/cleanup).
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, act } from '@testing-library/react';

import { RuntimeSettingsPanel } from './RuntimeSettingsPanel';
import * as runtimeApi from '@/services/runtime-settings-api';
import * as healthApi from '@/services/queue-health-api';
import * as kgTickApi from '@/services/kg-tick-api';

const FRESH_SETTINGS: runtimeApi.RuntimeSettings = {
  kg_kuzu_buffer_pool_mb: 48,
  kg_kuzu_max_db_size_gb: 8,
  kg_connection_pool_size: 4,
  kg_queue_max_concurrent_workers: 4,
  kg_queue_min_interval_ms: 100,
  kg_queue_claim_timeout_s: 300,
  kg_queue_max_attempts: 5,
  kg_queue_alert_threshold: 5000,
  // Spec 54399628 (Wave 2 NC f9732afc) — defaults match CoreSettings.
  kg_decay_tick_interval_minutes: 1440,
  kg_decay_tick_staleness_days: 7,
  kg_decay_tick_max_age_days: 0,
  restart_required: false,
};

const FRESH_HEALTH: healthApi.QueueHealth = {
  queue_depth: 47,
  oldest_pending_age_s: 3.2,
  claimed_count: 3,
  claimed_boards: ['board-a', 'board-b'],
  dead_letter_count: 0,
  claims_per_min_1m: 124,
  claims_per_min_5m: 98,
  alert_threshold: 5000,
  alert_active: false,
  alert_fired_total: 0,
  workers_active: 3,
  workers_idle: 1,
  workers_draining_count: 0,
  kuzu_lock_retries_5m: 2,
};

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.spyOn(runtimeApi, 'getRuntimeSettings').mockResolvedValue({ ...FRESH_SETTINGS });
  vi.spyOn(runtimeApi, 'putRuntimeSettings').mockImplementation(async (patch) => {
    return { ...FRESH_SETTINGS, ...patch, restart_required: false };
  });
  vi.spyOn(healthApi, 'getQueueHealth').mockResolvedValue({ ...FRESH_HEALTH });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// ----------------------------------------------------------------------
// AC11 — Tabs preserve unsaved drafts on switch
// ----------------------------------------------------------------------

describe('AC11 — Tabs preserve drafts on switch', () => {
  test('renderiza Graph DB tab por default', async () => {
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => {
      expect(screen.getByTestId('input-buffer-pool-mb')).toBeInTheDocument();
    });
    expect(screen.getByTestId('tab-graphdb')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('tab-eventqueue')).toHaveAttribute('aria-selected', 'false');
  });

  test('switching para Event Queue tab preserva drafts não-salvos do Graph DB', async () => {
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));

    const bufferInput = screen.getByTestId('input-buffer-pool-mb') as HTMLInputElement;
    fireEvent.change(bufferInput, { target: { value: '128' } });
    expect(bufferInput.value).toBe('128');

    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => screen.getByTestId('input-max-workers'));

    fireEvent.click(screen.getByTestId('tab-graphdb'));
    const restoredBuffer = screen.getByTestId('input-buffer-pool-mb') as HTMLInputElement;
    expect(restoredBuffer.value).toBe('128');
  });

  test('Event Queue draft sobrevive switch para Graph DB e volta', async () => {
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));

    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => screen.getByTestId('input-max-workers'));

    const workersInput = screen.getByTestId('input-max-workers') as HTMLInputElement;
    fireEvent.change(workersInput, { target: { value: '8' } });
    expect(workersInput.value).toBe('8');

    fireEvent.click(screen.getByTestId('tab-graphdb'));
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));
    fireEvent.click(screen.getByTestId('tab-eventqueue'));

    const restoredWorkers = screen.getByTestId('input-max-workers') as HTMLInputElement;
    expect(restoredWorkers.value).toBe('8');
  });

  test('Save persiste drafts de AMBAS as tabs em um único PUT', async () => {
    const putSpy = vi.mocked(runtimeApi.putRuntimeSettings);
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));

    // Edit Graph DB field
    fireEvent.change(screen.getByTestId('input-buffer-pool-mb'), { target: { value: '96' } });
    // Switch to Event Queue, edit there
    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => screen.getByTestId('input-max-workers'));
    fireEvent.change(screen.getByTestId('input-max-workers'), { target: { value: '8' } });

    fireEvent.click(screen.getByTestId('save-runtime-settings'));

    await waitFor(() => expect(putSpy).toHaveBeenCalled());
    const lastCallPayload = putSpy.mock.calls[0][0];
    expect(lastCallPayload.kg_kuzu_buffer_pool_mb).toBe(96);
    expect(lastCallPayload.kg_queue_max_concurrent_workers).toBe(8);
  });
});

// ----------------------------------------------------------------------
// AC12 — Live Health polling lifecycle (every 2s while Event Queue active)
// ----------------------------------------------------------------------

describe('AC12 — Live Queue Health polling lifecycle', () => {
  test('Event Queue tab faz fetch inicial + polling 2s', async () => {
    const healthSpy = vi.mocked(healthApi.getQueueHealth);
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));

    expect(healthSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => screen.getByTestId('live-queue-health-panel'));
    await waitFor(() => expect(healthSpy).toHaveBeenCalledTimes(1));

    // Tick 2s twice — should fire 2 more requests.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(healthSpy).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(healthSpy).toHaveBeenCalledTimes(3);
  });

  test('Switching para Graph DB para o polling imediatamente', async () => {
    const healthSpy = vi.mocked(healthApi.getQueueHealth);
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));

    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => expect(healthSpy).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByTestId('tab-graphdb'));
    const baseline = healthSpy.mock.calls.length;

    // Avançar 10s — não deve aumentar contador.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(healthSpy.mock.calls.length).toBe(baseline);
  });

  test('Close modal (unmount) cancela polling sem network leak', async () => {
    const healthSpy = vi.mocked(healthApi.getQueueHealth);
    const { unmount } = render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));

    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => expect(healthSpy).toHaveBeenCalledTimes(1));

    unmount();
    const baseline = healthSpy.mock.calls.length;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(healthSpy.mock.calls.length).toBe(baseline);
  });

  test('Live Health panel exibe métricas do health response', async () => {
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));

    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => screen.getByTestId('live-queue-health-panel'));
    await waitFor(() => expect(screen.queryByText('47')).toBeInTheDocument());

    // 14 fields exposed in the response → at least the headline metrics
    // are visible (queue_depth=47, claims_per_min_1m=124, lock retries=2).
    expect(screen.getByText('47')).toBeInTheDocument();
    expect(screen.getByText('124')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument(); // kuzu_lock_retries_5m
  });
});

// ----------------------------------------------------------------------
// f9732afc Decay Tick tab — Save and run now button + 3 fields
// ----------------------------------------------------------------------

describe('Decay Tick tab — f9732afc', () => {
  test('Decay Tick tab renderiza 3 fields persistidos', async () => {
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));

    fireEvent.click(screen.getByTestId('tab-decaytick'));
    await waitFor(() => screen.getByTestId('input-tick-interval-minutes'));

    expect(screen.getByTestId('input-tick-interval-minutes')).toBeInTheDocument();
    expect(screen.getByTestId('input-tick-staleness-days')).toBeInTheDocument();
    expect(screen.getByTestId('input-tick-max-age-days')).toBeInTheDocument();
  });

  test('Save and run now button so aparece no Decay Tick tab e dispara triggerKGTick', async () => {
    const tickSpy = vi
      .spyOn(kgTickApi, 'triggerKGTick')
      .mockResolvedValue({ tick_id: 't-123', status: 'started' } as any);
    const putSpy = vi.mocked(runtimeApi.putRuntimeSettings);

    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-buffer-pool-mb'));

    // Initially on graphdb tab — Save and run now should not exist.
    expect(screen.queryByTestId('save-and-run-now')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('tab-decaytick'));
    await waitFor(() => screen.getByTestId('save-and-run-now'));

    fireEvent.click(screen.getByTestId('save-and-run-now'));

    await waitFor(() => expect(putSpy).toHaveBeenCalled());
    await waitFor(() => expect(tickSpy).toHaveBeenCalledTimes(1));
  });
});
