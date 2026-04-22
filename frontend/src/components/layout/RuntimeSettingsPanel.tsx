/**
 * Runtime Settings panel (0.1.4 — Kùzu memory tuning).
 *
 * Exposes the three process-wide tuning knobs that drive Kùzu memory usage.
 * Values persist server-side via `/api/v1/settings/runtime` but only take
 * effect on the next process restart because `kuzu.Database()` is
 * constructor-time — the UI surfaces a persistent "Restart required"
 * banner after a successful save.
 *
 * Budget formula (mirrors BR6 "Orçamento UI é cálculo simples client-side"):
 *
 *     pool_size * buffer_pool_mb + 620 MB
 *
 * The 620 MB baseline accounts for the MiniLM embedding singleton,
 * query caches, and the Python/FastAPI runtime.
 */

import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import toast from 'react-hot-toast';

import {
  getRuntimeSettings,
  putRuntimeSettings,
  type RuntimeSettings,
} from '@/services/runtime-settings-api';

interface RuntimeSettingsPanelProps {
  onClose: () => void;
}

const RANGES = {
  kg_kuzu_buffer_pool_mb: { min: 16, max: 512 },
  kg_kuzu_max_db_size_gb: { min: 1, max: 64 },
  kg_connection_pool_size: { min: 1, max: 32 },
} as const;

// Non-Kùzu baseline: embedding singleton (~120 MB) + query caches (~100 MB) +
// Python/FastAPI runtime (~300 MB) + session/transaction state (~100 MB).
const BUDGET_BASELINE_MB = 620;

export function RuntimeSettingsPanel({ onClose }: RuntimeSettingsPanelProps) {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [values, setValues] = useState<RuntimeSettings | null>(null);
  // Draft state lets the user type freely without triggering saves.
  const [draft, setDraft] = useState({
    kg_kuzu_buffer_pool_mb: 0,
    kg_kuzu_max_db_size_gb: 0,
    kg_connection_pool_size: 0,
  });
  // True once a successful PUT happens; only cleared when the user dismisses.
  const [restartRequired, setRestartRequired] = useState(false);

  useEffect(() => {
    let active = true;
    getRuntimeSettings()
      .then((data) => {
        if (!active) return;
        setValues(data);
        setDraft({
          kg_kuzu_buffer_pool_mb: data.kg_kuzu_buffer_pool_mb,
          kg_kuzu_max_db_size_gb: data.kg_kuzu_max_db_size_gb,
          kg_connection_pool_size: data.kg_connection_pool_size,
        });
        setRestartRequired(data.restart_required);
      })
      .catch((err) => {
        if (!active) return;
        setError(err?.message ?? 'Failed to load runtime settings');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const budgetMb =
    draft.kg_connection_pool_size * draft.kg_kuzu_buffer_pool_mb +
    BUDGET_BASELINE_MB;

  const rangeFor = (key: keyof typeof RANGES) => RANGES[key];

  const outOfRange = (Object.keys(RANGES) as Array<keyof typeof RANGES>).some(
    (key) => {
      const v = draft[key];
      const { min, max } = rangeFor(key);
      return !Number.isFinite(v) || v < min || v > max;
    },
  );

  const onInputChange = (key: keyof typeof RANGES, raw: string) => {
    const parsed = Number(raw);
    setDraft((d) => ({
      ...d,
      [key]: Number.isFinite(parsed) ? parsed : 0,
    }));
  };

  const onReset = () => {
    if (!values) return;
    setDraft({
      kg_kuzu_buffer_pool_mb: values.kg_kuzu_buffer_pool_mb,
      kg_kuzu_max_db_size_gb: values.kg_kuzu_max_db_size_gb,
      kg_connection_pool_size: values.kg_connection_pool_size,
    });
  };

  const onSave = async () => {
    if (outOfRange) return;
    setSaving(true);
    try {
      const resp = await putRuntimeSettings(draft);
      setValues(resp);
      setRestartRequired(true);
      toast.success('Runtime settings saved');
    } catch (err: any) {
      toast.error(err?.message ?? 'Failed to save runtime settings');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="relative w-[520px] max-w-[90vw] bg-white dark:bg-gray-900 rounded-xl shadow-2xl border border-gray-200 dark:border-gray-800 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        data-testid="runtime-settings-panel"
      >
        <button
          onClick={onClose}
          className="absolute top-3 right-3 p-1.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-white/10 rounded-lg transition-colors z-10"
          aria-label="Close settings"
        >
          <X size={16} />
        </button>

        <div className="px-6 pt-5 pb-3 border-b border-gray-200 dark:border-gray-800">
          <h2 className="text-base font-semibold text-gray-900 dark:text-white">
            Settings
          </h2>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Knowledge Graph — Kùzu memory tuning
          </p>
        </div>

        {restartRequired && (
          <div
            className="px-6 py-2.5 bg-amber-50 dark:bg-amber-900/20 border-b border-amber-200 dark:border-amber-800/50 text-xs text-amber-900 dark:text-amber-200"
            data-testid="restart-required-banner"
          >
            <strong>Restart required.</strong> New values persist but only
            take effect after restarting the Okto Pulse process
            (Kùzu constructor-time).
          </div>
        )}

        {loading ? (
          <div className="px-6 py-10 text-sm text-gray-500 dark:text-gray-400 text-center">
            Loading runtime settings…
          </div>
        ) : error ? (
          <div className="px-6 py-10 text-sm text-red-500 text-center">
            {error}
          </div>
        ) : (
          <div className="px-6 py-5 space-y-4">
            <SettingField
              label="Kùzu buffer pool per board (MB)"
              description="Recommended 32-128 MB. Safe default: 48."
              value={draft.kg_kuzu_buffer_pool_mb}
              range={rangeFor('kg_kuzu_buffer_pool_mb')}
              onChange={(v) => onInputChange('kg_kuzu_buffer_pool_mb', v)}
              testId="input-buffer-pool-mb"
            />
            <SettingField
              label="Kùzu max database size per board (GB)"
              description="Virtual address space. Does not commit memory until used."
              value={draft.kg_kuzu_max_db_size_gb}
              range={rangeFor('kg_kuzu_max_db_size_gb')}
              onChange={(v) => onInputChange('kg_kuzu_max_db_size_gb', v)}
              testId="input-max-db-size-gb"
            />
            <SettingField
              label="Connection pool cap (simultaneous boards)"
              description="Boards kept alive in the LRU cache."
              value={draft.kg_connection_pool_size}
              range={rangeFor('kg_connection_pool_size')}
              onChange={(v) => onInputChange('kg_connection_pool_size', v)}
              testId="input-pool-size"
            />

            <div
              className="mt-4 px-3 py-2 bg-gray-50 dark:bg-gray-800 rounded-lg text-xs text-gray-600 dark:text-gray-300"
              data-testid="budget-display"
            >
              <strong>Estimated budget:</strong>{' '}
              {draft.kg_connection_pool_size} × {draft.kg_kuzu_buffer_pool_mb}{' '}
              + {BUDGET_BASELINE_MB} ={' '}
              <strong>{budgetMb} MB</strong>
              <span className="text-gray-400"> committed (non-Kùzu baseline {BUDGET_BASELINE_MB} MB)</span>
            </div>
          </div>
        )}

        <div className="px-6 py-3 border-t border-gray-200 dark:border-gray-800 flex items-center justify-end gap-2">
          <button
            onClick={onReset}
            disabled={!values || saving}
            className="px-3 py-1.5 text-xs text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors disabled:opacity-50"
          >
            Reset
          </button>
          <button
            onClick={onSave}
            disabled={loading || saving || outOfRange}
            className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg disabled:opacity-50"
            data-testid="save-runtime-settings"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}

interface SettingFieldProps {
  label: string;
  description: string;
  value: number;
  range: { min: number; max: number };
  onChange: (raw: string) => void;
  testId: string;
}

function SettingField({
  label,
  description,
  value,
  range,
  onChange,
  testId,
}: SettingFieldProps) {
  const outOfRange = !Number.isFinite(value) || value < range.min || value > range.max;
  return (
    <div>
      <label className="text-xs font-medium text-gray-700 dark:text-gray-200 block mb-0.5">
        {label}
      </label>
      <p className="text-[10px] text-gray-400 mb-1.5">{description}</p>
      <div className="flex items-center gap-2">
        <input
          type="number"
          min={range.min}
          max={range.max}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          data-testid={testId}
          className={`w-28 text-xs px-2 py-1 border rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 ${
            outOfRange
              ? 'border-red-400 dark:border-red-700'
              : 'border-gray-300 dark:border-gray-600'
          }`}
        />
        <span className="text-[10px] text-gray-400">
          {range.min}-{range.max}
        </span>
      </div>
    </div>
  );
}
