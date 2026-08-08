import { useEffect, useRef, useState } from 'react';
import { HelpCircle, ListChecks, RefreshCw, Save } from 'lucide-react';
import toast from 'react-hot-toast';

import { ChecklistModeSelector } from '@/components/board/ChecklistModeSelector';
import { useDashboardApi } from '@/services/api';
import type {
  ChecklistBinding,
  ChecklistMode,
  ChecklistTemplate,
} from '@/types';

interface ChecklistBindingSettingsProps {
  boardId: string;
  onOpenHelp?: () => void;
}

export function ChecklistBindingSettings({
  boardId,
  onOpenHelp,
}: ChecklistBindingSettingsProps) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  const [binding, setBinding] = useState<ChecklistBinding | null>(null);
  const [template, setTemplate] = useState<ChecklistTemplate | null>(null);
  const [mode, setMode] = useState<ChecklistMode>('off');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const [resolvedBinding, templates] = await Promise.all([
        apiRef.current.getChecklistBinding(boardId),
        apiRef.current.listChecklistTemplates(),
      ]);
      setBinding(resolvedBinding);
      setMode(resolvedBinding.mode);
      setTemplate(
        templates.items.find(
          (item) => item.version === resolvedBinding.template_version_id,
        ) ?? null,
      );
    } catch (error: any) {
      toast.error(error?.message || 'Failed to load Spec checklist governance');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [boardId]);

  const save = async () => {
    if (!binding || saving || mode === binding.mode) return;
    setSaving(true);
    try {
      const updated = await apiRef.current.updateChecklistBinding(boardId, {
        mode,
        template_version_id: '/specify/v1',
        expected_revision: binding.expected_revision,
      });
      setBinding(updated.effective);
      setMode(updated.effective.mode);
      toast.success('Spec checklist policy updated');
    } catch (error: any) {
      toast.error(error?.message || 'Failed to update Spec checklist policy');
      await load();
    } finally {
      setSaving(false);
    }
  };

  return (
    <section
      className="mt-4 rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900/40"
      data-testid="checklist-binding-settings"
    >
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h4 className="flex items-center gap-1.5 text-xs font-semibold text-gray-800 dark:text-gray-100">
            <ListChecks size={12} />
            Curated Spec Checklist
          </h4>
          <p className="mt-0.5 text-[11px] leading-4 text-gray-500 dark:text-gray-400">
            Human-owned governance for the immutable /specify/v1 checklist used
            by Spec Validation.
          </p>
          {onOpenHelp && (
            <button
              type="button"
              onClick={onOpenHelp}
              aria-label="Learn about Curated Spec Checklist"
              data-testid="checklist-help-link"
              className="mt-1 inline-flex items-center gap-1 text-[10px] font-medium text-violet-600 hover:text-violet-700 hover:underline dark:text-violet-300 dark:hover:text-violet-200"
            >
              <HelpCircle size={11} />
              How this works
            </button>
          )}
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading || saving}
          aria-label="Refresh checklist governance"
          className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 disabled:opacity-50 dark:hover:bg-gray-800"
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {loading && !binding ? (
        <p className="text-xs text-gray-500">Loading checklist governance…</p>
      ) : binding ? (
        <div className="space-y-3">
          <ChecklistModeSelector
            value={mode}
            onChange={setMode}
            disabled={saving}
          />

          <div className="flex items-center justify-between gap-3 rounded border border-gray-100 bg-gray-50 px-3 py-2 text-[10px] dark:border-gray-800 dark:bg-gray-800/70">
            <div className="min-w-0 text-gray-500 dark:text-gray-400">
              <span className="font-semibold text-gray-700 dark:text-gray-200">
                {template?.version ?? binding.template_version_id}
              </span>
              {' · '}
              {template?.items.length ?? 10} ordered items
              {' · immutable'}
              <span className="ml-2 font-mono" title={binding.digest}>
                binding v{binding.version}
              </span>
            </div>
            <button
              type="button"
              onClick={() => void save()}
              disabled={saving || mode === binding.mode}
              className="inline-flex shrink-0 items-center gap-1 rounded bg-violet-600 px-2.5 py-1.5 text-[11px] font-medium text-white hover:bg-violet-700 disabled:cursor-not-allowed disabled:bg-gray-400"
            >
              <Save size={11} />
              {saving ? 'Saving…' : 'Save policy'}
            </button>
          </div>
        </div>
      ) : (
        <p className="text-xs text-red-600 dark:text-red-400">
          Checklist governance is unavailable.
        </p>
      )}
    </section>
  );
}
