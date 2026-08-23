import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  RotateCcw,
  Save,
  Settings2,
} from 'lucide-react';
import { useDashboardApi } from '@/services/api';
import type {
  FlowHealthSettings,
  FlowHealthSettingsResponse,
} from './analyticsCanonicalTypes';

interface FlowHealthSettingsPageProps {
  boardId: string;
  onBack: () => void;
}

const DEFAULT_GENERAL_STALE_HOURS = 72;
const DEFAULT_REJECTED_STALE_HOURS = 96;
const OVERRIDE_STATES = ['backlog', 'pending', 'in_progress', 'rejected', 'done'] as const;

function words(value: string): string {
  return value.replace(/[._-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function validHours(value: number): boolean {
  return Number.isInteger(value) && value >= 1 && value <= 8760;
}

export function FlowHealthSettingsPage({ boardId, onBack }: FlowHealthSettingsPageProps) {
  const api = useDashboardApi();
  const [settings, setSettings] = useState<FlowHealthSettings | null>(null);
  const [generalHours, setGeneralHours] = useState(DEFAULT_GENERAL_STALE_HOURS);
  const [rejectedHours, setRejectedHours] = useState(DEFAULT_REJECTED_STALE_HOURS);
  const [overrides, setOverrides] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const requestSequence = useRef(0);

  const applyResponse = (response: FlowHealthSettingsResponse) => {
    setSettings(response.settings);
    setGeneralHours(response.settings.general_stale_hours);
    setRejectedHours(response.settings.rejected_stale_hours);
    setOverrides(response.settings.overrides ?? {});
  };

  const loadSettings = async () => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    setSaving(false);
    setSettings(null);
    setError(null);
    setMessage(null);
    try {
      const response = await api.getBoardFlowHealthSettings(boardId);
      if (requestSequence.current === sequence) applyResponse(response);
    } catch (reason) {
      if (requestSequence.current === sequence) {
        setError(reason instanceof Error ? reason.message : 'Could not load Flow Health settings.');
      }
    } finally {
      if (requestSequence.current === sequence) setLoading(false);
    }
  };

  useEffect(() => {
    void loadSettings();
    return () => {
      requestSequence.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardId]);

  const dirty = useMemo(() => {
    if (!settings) return false;
    return generalHours !== settings.general_stale_hours
      || rejectedHours !== settings.rejected_stale_hours
      || JSON.stringify(overrides) !== JSON.stringify(settings.overrides ?? {});
  }, [generalHours, overrides, rejectedHours, settings]);

  const validate = (): string | null => {
    if (!validHours(generalHours) || !validHours(rejectedHours)) {
      return 'Thresholds must be whole hours between 1 and 8760.';
    }
    if (Object.values(overrides).some((value) => !validHours(value))) {
      return 'Per-state overrides must be whole hours between 1 and 8760.';
    }
    return null;
  };

  const save = async () => {
    if (!settings || saving) return;
    const validationError = validate();
    if (validationError) {
      setError(validationError);
      setMessage(null);
      return;
    }
    const sequence = ++requestSequence.current;
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const response = await api.saveBoardFlowHealthSettings(boardId, {
        expected_version: settings.version,
        general_stale_hours: generalHours,
        rejected_stale_hours: rejectedHours,
        overrides,
      });
      if (requestSequence.current === sequence) {
        applyResponse(response);
        setMessage('Flow Health policy saved. The next Analytics refresh will use this version.');
      }
    } catch (reason) {
      if (requestSequence.current === sequence) {
        setError(reason instanceof Error ? reason.message : 'Could not save Flow Health settings. No setting was changed.');
      }
    } finally {
      if (requestSequence.current === sequence) setSaving(false);
    }
  };

  const restore = async () => {
    if (!settings || saving) return;
    const sequence = ++requestSequence.current;
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const response = await api.restoreBoardFlowHealthSettings(boardId, settings.version);
      if (requestSequence.current === sequence) {
        applyResponse(response);
        setMessage('Safe defaults restored: 72 hours general and 96 hours rejected.');
      }
    } catch (reason) {
      if (requestSequence.current === sequence) {
        setError(reason instanceof Error ? reason.message : 'Could not restore safe defaults. No setting was changed.');
      }
    } finally {
      if (requestSequence.current === sequence) setSaving(false);
    }
  };

  const authority = settings
    && settings.general_stale_hours === DEFAULT_GENERAL_STALE_HOURS
    && settings.rejected_stale_hours === DEFAULT_REJECTED_STALE_HOURS
    && Object.keys(settings.overrides ?? {}).length === 0
    ? 'Default authority'
    : 'Board override';

  return (
    <section aria-labelledby="flow-health-settings-heading" className="mx-auto max-w-5xl rounded-xl border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800" data-testid="flow-health-settings-page">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-violet-500"><Settings2 className="h-3.5 w-3.5" aria-hidden="true" /> Board settings</p>
          <h1 id="flow-health-settings-heading" className="mt-1 text-xl font-semibold text-gray-900 dark:text-white">Flow Health thresholds</h1>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Authorized Board policy is edited separately from the read-only Analytics view.</p>
        </div>
        <button type="button" onClick={onBack} className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 px-3 py-2 text-xs font-semibold dark:border-gray-600"><ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" /> Back to Flow Health</button>
      </div>

      {loading && <p className="mt-6 text-sm text-gray-500" role="status">Loading Flow Health settings…</p>}
      {!loading && error && !settings && (
        <div className="mt-6 flex items-center justify-between gap-3 rounded-lg bg-red-50 px-4 py-3 dark:bg-red-950/25" role="alert">
          <span className="text-sm text-red-700 dark:text-red-300">{error}</span>
          <button type="button" onClick={() => void loadSettings()} className="text-xs font-semibold text-red-700 dark:text-red-300">Retry</button>
        </div>
      )}

      {!loading && settings && (
        <>
          <div className="mt-6 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-violet-200 bg-violet-50/60 px-4 py-3 dark:border-violet-800 dark:bg-violet-950/20">
            <div><p className="text-xs font-semibold text-violet-900 dark:text-violet-100">Effective policy v{settings.version}</p><p className="mt-0.5 text-[10px] text-violet-700 dark:text-violet-300">{authority} · optimistic concurrency protects concurrent edits.</p></div>
            <span className="rounded-full border border-violet-300 px-2.5 py-1 text-[10px] font-semibold text-violet-800 dark:border-violet-700 dark:text-violet-200">{dirty ? 'Unsaved changes' : 'Current'}</span>
          </div>

          <div className="mt-6 grid gap-4 md:grid-cols-2">
            <label className="text-xs font-semibold text-gray-600 dark:text-gray-300">General stale after
              <div className="mt-1 flex rounded-md border border-gray-300 dark:border-gray-600"><input aria-label="General stale after" type="number" min={1} max={8760} step={1} disabled={saving} value={generalHours} onChange={(event) => setGeneralHours(Number(event.target.value))} className="min-h-10 min-w-0 flex-1 rounded-l-md bg-transparent px-3 text-sm" /><span className="border-l border-gray-300 px-3 py-2 text-sm dark:border-gray-600">hours</span></div>
              <span className="mt-1 block text-[10px] font-normal text-gray-400">Safe default: 72 hours</span>
            </label>
            <label className="text-xs font-semibold text-gray-600 dark:text-gray-300">Rejected work stale after
              <div className="mt-1 flex rounded-md border border-gray-300 dark:border-gray-600"><input aria-label="Rejected work stale after" type="number" min={1} max={8760} step={1} disabled={saving} value={rejectedHours} onChange={(event) => setRejectedHours(Number(event.target.value))} className="min-h-10 min-w-0 flex-1 rounded-l-md bg-transparent px-3 text-sm" /><span className="border-l border-gray-300 px-3 py-2 text-sm dark:border-gray-600">hours</span></div>
              <span className="mt-1 block text-[10px] font-normal text-gray-400">Safe default: 96 hours</span>
            </label>
          </div>

          <fieldset className="mt-6">
            <legend className="text-xs font-semibold text-gray-600 dark:text-gray-300">Per-state overrides</legend>
            <p className="mt-1 text-[10px] text-gray-400">Leave a state empty to inherit its governed default.</p>
            <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              {OVERRIDE_STATES.map((state) => (
                <label key={state} className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">{words(state)}
                  <input aria-label={`${words(state)} override`} type="number" min={1} max={8760} step={1} disabled={saving} value={overrides[state] ?? ''} placeholder="Default" onChange={(event) => setOverrides((previous) => { const next = { ...previous }; if (event.target.value === '') delete next[state]; else next[state] = Number(event.target.value); return next; })} className="mt-1 min-h-10 w-full rounded-md border border-gray-300 bg-transparent px-3 text-sm normal-case tracking-normal dark:border-gray-600" />
                </label>
              ))}
            </div>
          </fieldset>

          {error && <p className="mt-5 rounded-lg bg-red-50 px-4 py-3 text-sm text-red-700 dark:bg-red-950/25 dark:text-red-300" role="alert">{error}</p>}
          {message && <p className="mt-5 rounded-lg bg-emerald-50 px-4 py-3 text-sm text-emerald-700 dark:bg-emerald-950/25 dark:text-emerald-300" role="status">{message}</p>}

          <div className="mt-6 flex flex-wrap justify-end gap-3">
            <button type="button" disabled={saving} onClick={() => void restore()} className="inline-flex items-center gap-1.5 rounded-md border border-gray-300 px-4 py-2 text-xs font-semibold disabled:opacity-50 dark:border-gray-600"><RotateCcw className="h-3.5 w-3.5" aria-hidden="true" /> Restore safe defaults</button>
            <button type="button" disabled={saving || !dirty} onClick={() => void save()} className="inline-flex items-center gap-1.5 rounded-md bg-violet-600 px-4 py-2 text-xs font-semibold text-white disabled:opacity-50"><Save className="h-3.5 w-3.5" aria-hidden="true" /> {saving ? 'Saving…' : 'Save thresholds'}</button>
          </div>
        </>
      )}
    </section>
  );
}
