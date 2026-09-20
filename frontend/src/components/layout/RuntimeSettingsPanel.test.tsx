/**
 * Tests for RuntimeSettingsPanel — Sprint 4 of spec bdcda842.
 *
 * Covers AC11 (tabs preserve drafts) + AC12 (polling lifecycle/cleanup).
 */

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, act } from '@testing-library/react';

import { RuntimeSettingsPanel } from './RuntimeSettingsPanel';
import * as runtimeApi from '@/services/runtime-settings-api';

const permissionMock = vi.hoisted(() => ({
  has: vi.fn((_flag: string) => true),
}));

vi.mock('@/store/dashboard', () => ({
  useDashboardStore: (selector: (state: { currentBoard: { id: string } }) => unknown) =>
    selector({ currentBoard: { id: 'board-tick-retired' } }),
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Full Control',
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: permissionMock.has,
  }),
}));

const FRESH_SETTINGS: runtimeApi.RuntimeSettings = {
  kg_graph_backend: 'grafx',
  kg_global_graph_backend: 'grafx',
  kg_grafx_page_size: 8192,
  kg_grafx_descriptor_revalidation: 'generation',
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


beforeEach(() => {
  permissionMock.has.mockReset();
  permissionMock.has.mockReturnValue(true);
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.spyOn(runtimeApi, 'getRuntimeSettings').mockResolvedValue({ ...FRESH_SETTINGS });
  vi.spyOn(runtimeApi, 'putRuntimeSettings').mockImplementation(async (patch) => {
    return { ...FRESH_SETTINGS, ...patch, restart_required: false };
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// ----------------------------------------------------------------------
// AC11 — Tabs preserve unsaved drafts on switch
// ----------------------------------------------------------------------

describe('AC11 — Tabs preserve drafts on switch', () => {
  test('enforces the Pulse query-value ceiling before any save request', async () => {
    const catalog: runtimeApi.GrafxSettingDescriptor[] = [{ name: 'max_query_value_characters', default: 65536,
      minimum: 1, maximum: 65536, description: 'Pulse default and hard ceiling: 65536 characters.',
      editable: true, nullable: false, kind: 'number' }];
    vi.mocked(runtimeApi.getRuntimeSettings).mockResolvedValue({ ...FRESH_SETTINGS, grafx_settings_catalog: catalog });
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('grafx-option-max_query_value_characters')).toHaveValue(65536));
    fireEvent.click(screen.getByText('Advanced Grafx settings (1)'));
    const input = screen.getByTestId('grafx-option-max_query_value_characters');
    const save = screen.getByTestId('save-runtime-settings');
    expect(input).toHaveAttribute('min', '1');
    expect(input).toHaveAttribute('max', '65536');
    for (const invalid of ['65537', '1048576', '0', '-1', '1.5', '']) {
      fireEvent.change(input, { target: { value: invalid } });
      expect(save).toBeDisabled();
      fireEvent.click(save);
      expect(runtimeApi.putRuntimeSettings).not.toHaveBeenCalled();
    }
    fireEvent.change(input, { target: { value: '1' } });
    expect(save).toBeEnabled();
    fireEvent.change(input, { target: { value: '65536' } });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(runtimeApi.putRuntimeSettings).toHaveBeenCalledWith(
      expect.objectContaining({ kg_grafx_options: { max_query_value_characters: 65536 } }),
    ));
  });

  test('persists advanced options and keeps the desired draft while restart is pending', async () => {
    const catalog: runtimeApi.GrafxSettingDescriptor[] = [{ name: 'max_result_rows', default: null,
      description: 'A row budget refuses instead of truncating results.', editable: true,
      nullable: true, kind: 'number' }];
    vi.mocked(runtimeApi.getRuntimeSettings).mockResolvedValue({ ...FRESH_SETTINGS, grafx_settings_catalog: catalog });
    vi.mocked(runtimeApi.putRuntimeSettings).mockImplementation(async (patch) => ({ ...FRESH_SETTINGS,
      grafx_settings_catalog: catalog, desired_values: patch, restart_required: true }));
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('input-grafx-buffer-pool-mb')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Advanced Grafx settings (1)'));
    fireEvent.change(screen.getByTestId('grafx-option-max_result_rows'), { target: { value: '1500' } });
    fireEvent.change(screen.getByTestId('input-grafx-buffer-pool-mb'), { target: { value: '128' } });
    fireEvent.change(screen.getByTestId('input-grafx-read-participants'), { target: { value: '3' } });
    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    fireEvent.click(screen.getByTestId('tab-graphdb'));
    expect(screen.getByTestId('grafx-option-max_result_rows')).toHaveValue(1500);
    fireEvent.click(screen.getByTestId('save-runtime-settings'));
    await waitFor(() => expect(runtimeApi.putRuntimeSettings).toHaveBeenCalledWith({
      kg_grafx_buffer_pool_mb: 128, kg_grafx_options: { max_result_rows: 1500 }, kg_grafx_read_participants: 3,
    }));
    await waitFor(() => expect(screen.getByTestId('grafx-option-max_result_rows')).toHaveValue(1500));
  });

  test('renderiza Grafx tab por default sem controles legados', async () => {
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    expect(screen.getByText('Powered by Okto Grafx')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId('input-grafx-page-size')).toBeInTheDocument();
    });
    expect(screen.getByTestId('tab-graphdb')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('tab-eventqueue')).toHaveAttribute('aria-selected', 'false');
    expect(screen.getAllByText('Okto Grafx')).toHaveLength(2);
    expect(screen.getByTestId('input-grafx-buffer-pool-mb')).toHaveValue(64);
    expect(screen.getByTestId('input-grafx-read-participants')).toHaveValue(2);
    expect(screen.queryByText(/max database size/i)).not.toBeInTheDocument();
  });

  test('switching para Event Queue preserva draft do Grafx', async () => {
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-grafx-page-size'));

    fireEvent.change(screen.getByTestId('input-grafx-page-size'), { target: { value: '2' } });
    expect(screen.getByText('16384 bytes')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => screen.getByTestId('input-max-workers'));

    fireEvent.click(screen.getByTestId('tab-graphdb'));
    expect(screen.getByText('16384 bytes')).toBeInTheDocument();
  });

  test('Event Queue draft sobrevive switch para Grafx e volta', async () => {
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-grafx-page-size'));

    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => screen.getByTestId('input-max-workers'));

    const workersInput = screen.getByTestId('input-max-workers') as HTMLInputElement;
    fireEvent.change(workersInput, { target: { value: '8' } });
    expect(workersInput.value).toBe('8');

    fireEvent.click(screen.getByTestId('tab-graphdb'));
    await waitFor(() => screen.getByTestId('input-grafx-page-size'));
    fireEvent.click(screen.getByTestId('tab-eventqueue'));

    const restoredWorkers = screen.getByTestId('input-max-workers') as HTMLInputElement;
    expect(restoredWorkers.value).toBe('8');
  });

  test('Save persiste drafts de AMBAS as tabs em um único PUT', async () => {
    const putSpy = vi.mocked(runtimeApi.putRuntimeSettings);
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-grafx-page-size'));

    fireEvent.change(screen.getByTestId('input-descriptor-revalidation'), { target: { value: 'strict' } });
    // Switch to Event Queue, edit there
    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await waitFor(() => screen.getByTestId('input-max-workers'));
    fireEvent.change(screen.getByTestId('input-max-workers'), { target: { value: '8' } });

    fireEvent.click(screen.getByTestId('save-runtime-settings'));

    await waitFor(() => expect(putSpy).toHaveBeenCalled());
    const lastCallPayload = putSpy.mock.calls[0][0];
    expect(lastCallPayload).toEqual({
      kg_grafx_descriptor_revalidation: 'strict',
      kg_queue_max_concurrent_workers: 8,
    });
  });

  test('valor desejado pendente de restart reaparece ao reabrir a modal', async () => {
    vi.mocked(runtimeApi.getRuntimeSettings).mockResolvedValue({
      ...FRESH_SETTINGS,
      restart_required: true,
      desired_values: {
        kg_grafx_page_size: 16384,
      },
    });

    const firstMount = render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText('16384 bytes')).toBeInTheDocument(),
    );
    firstMount.unmount();

    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText('16384 bytes')).toBeInTheDocument(),
    );
    expect(runtimeApi.getRuntimeSettings).toHaveBeenCalledTimes(2);
  });

  test('Reset restaura o valor desejado persistido, não o efetivo top-level', async () => {
    vi.mocked(runtimeApi.getRuntimeSettings).mockResolvedValue({
      ...FRESH_SETTINGS,
      restart_required: true,
      desired_values: {
        kg_grafx_descriptor_revalidation: 'strict',
      },
    });

    render(<RuntimeSettingsPanel onClose={() => {}} />);
    const descriptor = await screen.findByTestId('input-descriptor-revalidation');
    expect(descriptor).toHaveValue('strict');

    fireEvent.change(descriptor, { target: { value: 'generation' } });
    fireEvent.click(screen.getByText('Reset'));

    expect(descriptor).toHaveValue('strict');
  });

  test('após PUT o draft e a baseline seguem desired_values retornado', async () => {
    vi.mocked(runtimeApi.putRuntimeSettings).mockResolvedValue({
      ...FRESH_SETTINGS,
      restart_required: true,
      desired_values: {
        kg_grafx_descriptor_revalidation: 'strict',
      },
    });

    render(<RuntimeSettingsPanel onClose={() => {}} />);
    const descriptor = await screen.findByTestId('input-descriptor-revalidation');
    fireEvent.change(descriptor, { target: { value: 'strict' } });
    fireEvent.click(screen.getByTestId('save-runtime-settings'));

    await waitFor(() => expect(descriptor).toHaveValue('strict'));

    fireEvent.change(descriptor, { target: { value: 'generation' } });
    fireEvent.click(screen.getByText('Reset'));
    expect(descriptor).toHaveValue('strict');
  });

  test('page size usa somente as geometrias válidas do Grafx', async () => {
    const putSpy = vi.mocked(runtimeApi.putRuntimeSettings);
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-grafx-page-size'));

    const pageSizeSlider = screen.getByTestId('input-grafx-page-size') as HTMLInputElement;
    expect(pageSizeSlider.type).toBe('range');

    fireEvent.change(pageSizeSlider, { target: { value: '2' } });
    fireEvent.click(screen.getByTestId('save-runtime-settings'));

    await waitFor(() => expect(putSpy).toHaveBeenCalled());
    const lastCallPayload = putSpy.mock.calls[0][0];
    expect(lastCallPayload.kg_grafx_page_size).toBe(16384);
  });
});

// ----------------------------------------------------------------------
// AC12 — Live Health polling lifecycle (every 2s while Event Queue active)
// ----------------------------------------------------------------------

describe('F4 — retired queue inspection', () => {
  test('has no inspector or queue polling on entry, tab switch or timer ticks', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('unexpected request'));
    const { unmount } = render(<RuntimeSettingsPanel onClose={() => {}} initialTab="eventqueue" />);
    await waitFor(() => screen.getByTestId('input-max-workers'));
    expect(screen.queryByTestId('live-queue-health-panel')).not.toBeInTheDocument();
    expect(screen.queryByTestId('dead-letter-inspector-link')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('tab-graphdb'));
    fireEvent.click(screen.getByTestId('tab-eventqueue'));
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

// ----------------------------------------------------------------------
// f9732afc Decay Tick tab — Save and run now button + 3 fields
// ----------------------------------------------------------------------

describe('Decay Tick tab — f9732afc', () => {
  test('Decay Tick tab renderiza 3 fields persistidos', async () => {
    render(<RuntimeSettingsPanel onClose={() => {}} />);
    await waitFor(() => screen.getByTestId('input-grafx-page-size'));

    fireEvent.click(screen.getByTestId('tab-decaytick'));
    await waitFor(() => screen.getByTestId('input-tick-interval-minutes'));

    expect(screen.getByTestId('input-tick-interval-minutes')).toBeInTheDocument();
    expect(screen.getByTestId('input-tick-staleness-days')).toBeInTheDocument();
    expect(screen.getByTestId('input-tick-max-age-days')).toBeInTheDocument();
  });

  test('does not offer manual tick or poll health on the saved Decay Tick tab', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('unexpected request'));
    const { unmount } = render(<RuntimeSettingsPanel onClose={() => {}} initialTab="decaytick" />);
    await waitFor(() => screen.getByTestId('input-tick-interval-minutes'));
    expect(screen.queryByTestId('save-and-run-now')).not.toBeInTheDocument();
    expect(screen.queryByText(/run tick now|save & run now/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('tab-graphdb'));
    fireEvent.click(screen.getByTestId('tab-decaytick'));
    await act(async () => { await vi.advanceTimersByTimeAsync(45000); });
    expect(runtimeApi.putRuntimeSettings).not.toHaveBeenCalled();
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('runtime permission projection', () => {
  test('does not read settings without runtime.settings.read', async () => {
    permissionMock.has.mockReturnValue(false);

    render(<RuntimeSettingsPanel onClose={() => {}} />);

    expect(
      await screen.findByText('You do not have permission to read runtime settings'),
    ).toBeInTheDocument();
    expect(runtimeApi.getRuntimeSettings).not.toHaveBeenCalled();
  });

  test('keeps the save action disabled without runtime.settings.write', async () => {
    permissionMock.has.mockImplementation((flag: string) => flag !== 'runtime.settings.write');
    const putSpy = vi.mocked(runtimeApi.putRuntimeSettings);

    render(<RuntimeSettingsPanel onClose={() => {}} />);

    const save = await screen.findByTestId('save-runtime-settings');
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute('title', 'Requires runtime.settings.write');
    fireEvent.click(save);
    expect(putSpy).not.toHaveBeenCalled();
  });
});
