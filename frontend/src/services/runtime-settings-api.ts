/**
 * API client for the runtime settings endpoint shipped in 0.1.4.
 *
 * Backs the "Settings" menu (Kùzu memory tuning knobs). Reads and writes
 * persist through the backend's settings_service and only take effect on
 * the next process restart because kuzu.Database() is constructor-time.
 */

export interface RuntimeSettings {
  kg_kuzu_buffer_pool_mb: number;
  kg_kuzu_max_db_size_gb: number;
  kg_connection_pool_size: number;
  restart_required: boolean;
}

export type RuntimeSettingsPatch = Partial<
  Omit<RuntimeSettings, 'restart_required'>
>;

const BASE = '/api/v1';

export async function getRuntimeSettings(): Promise<RuntimeSettings> {
  const resp = await fetch(`${BASE}/settings/runtime`, {
    headers: { 'Content-Type': 'application/json' },
  });
  if (!resp.ok) {
    const err = await resp
      .json()
      .catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
  }
  return resp.json();
}

export async function putRuntimeSettings(
  patch: RuntimeSettingsPatch,
): Promise<RuntimeSettings> {
  const resp = await fetch(`${BASE}/settings/runtime`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (!resp.ok) {
    const err = await resp
      .json()
      .catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || err.message || `HTTP ${resp.status}`);
  }
  return resp.json();
}
