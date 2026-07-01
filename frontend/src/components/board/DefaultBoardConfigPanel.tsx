// Administrative panel for the default board-configuration template
// (spec 9df814bc / card 7da43521 / FR8). Opened from Menu > Board > Global Default.
//
// The gate-configuration controls reuse the SAME <BoardSettingsForm> as the
// Board Config screen so the disposition is EXACTLY identical — the only
// difference is persistence: here every change creates a new (immutable)
// template version via createActiveTemplateVersion instead of mutating a board.
// Template-specific surfaces (version lifecycle, guideline defaults, Design
// System default, board diff, version history) stay around the shared form.
import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, GitCompare, ListChecks, Palette, Plus, RotateCcw } from 'lucide-react';

import { BoardSettingsForm, normalizeDesignSystemGateMode } from '@/components/board/BoardSettingsForm';
import { useDashboardApi } from '@/services/api';
import type {
  BoardSettings,
  CreateDefaultBoardConfigVersionRequest,
  DefaultBoardConfigActiveResponse,
  DefaultBoardConfigDiff,
  DefaultBoardConfigTemplate,
  DefaultBoardConfigVersionsResponse,
  DefaultGuidelineCandidate,
  DefaultGuidelineCandidatesResponse,
  SpecResourceAutoDeriveType,
} from '@/types';

type GateMode = 'off' | 'advisory' | 'blocking';

// Version history is paginated so a long template lineage doesn't dominate the panel.
const VERSION_PAGE_SIZE = 5;

const DEFAULT_TEMPLATE_SETTINGS: Record<string, unknown> = {
  max_scenarios_per_card: 3,
  skip_test_coverage_global: false,
  skip_rules_coverage_global: false,
  skip_trs_coverage_global: false,
  skip_contract_coverage_global: false,
  skip_ir_coverage_global: false,
  skip_or_coverage_global: false,
  skip_decisions_coverage_global: false,
  skip_cognitive_consolidation: false,
  allow_agent_self_answering: false,
  require_full_context_for_critical_actions: true,
  qa_require_role_separation: false,
  design_system_gate_mode: 'off',
  require_task_validation: true,
  min_confidence: 70,
  min_completeness: 80,
  max_drift: 50,
  require_spec_validation: true,
  min_spec_completeness: 80,
  min_spec_assertiveness: 80,
  max_spec_ambiguity: 30,
  require_ideation_ambiguity_gate: false,
  max_ideation_ambiguity: 3,
  require_spec_resource_task_coverage: true,
  auto_derive_spec_resources_enabled: false,
  auto_derive_spec_resource_types: [],
  require_test_task_for_bug: true,
  bug_test_gate_min_severity: 'minor',
  skip_test_evidence_global: false,
};

function loadErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error ?? 'Unknown error');
  if (/guideline not found/i.test(message)) {
    return 'A stale guideline reference was ignored while loading admin data.';
  }
  return message;
}

function badgeClass(tone: 'green' | 'slate' | 'amber') {
  if (tone === 'green') return 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-200';
  if (tone === 'amber') return 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-200';
  return 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300';
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function omitUndefinedValues<T extends Record<string, unknown>>(value: T): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([, entry]) => entry !== undefined));
}

function shortId(value: string | undefined | null): string {
  if (!value) return 'none';
  return value.length > 12 ? `${value.slice(0, 8)}...` : value;
}

// Coerce the template's loose settings payload into a typed BoardSettings so the
// shared form (which expects concrete keys) renders the template defaults. Same
// defaults the Board Config screen applies for a board with no stored value.
function toBoardSettings(raw: Record<string, unknown>): BoardSettings {
  const bool = (key: string, fallback: boolean) =>
    typeof raw[key] === 'boolean' ? (raw[key] as boolean) : fallback;
  const num = (key: string, fallback: number) =>
    typeof raw[key] === 'number' ? (raw[key] as number) : fallback;
  return {
    max_scenarios_per_card: num('max_scenarios_per_card', 3),
    skip_test_coverage_global: bool('skip_test_coverage_global', false),
    skip_rules_coverage_global: bool('skip_rules_coverage_global', false),
    skip_trs_coverage_global: bool('skip_trs_coverage_global', false),
    skip_contract_coverage_global: bool('skip_contract_coverage_global', false),
    skip_ir_coverage_global: bool('skip_ir_coverage_global', false),
    skip_or_coverage_global: bool('skip_or_coverage_global', false),
    skip_task_requirement_link_gate_global: typeof raw.skip_task_requirement_link_gate_global === 'boolean'
      ? (raw.skip_task_requirement_link_gate_global as boolean)
      : undefined,
    skip_decisions_coverage_global: bool('skip_decisions_coverage_global', false),
    skip_cognitive_consolidation: bool('skip_cognitive_consolidation', false),
    allow_agent_self_answering: bool('allow_agent_self_answering', false),
    require_full_context_for_critical_actions: bool('require_full_context_for_critical_actions', true),
    qa_require_role_separation: bool('qa_require_role_separation', false),
    skip_test_evidence_global: bool('skip_test_evidence_global', false),
    require_task_validation: bool('require_task_validation', true),
    min_confidence: num('min_confidence', 70),
    min_completeness: num('min_completeness', 80),
    max_drift: num('max_drift', 50),
    require_spec_validation: bool('require_spec_validation', true),
    min_spec_completeness: num('min_spec_completeness', 80),
    min_spec_assertiveness: num('min_spec_assertiveness', 80),
    max_spec_ambiguity: num('max_spec_ambiguity', 30),
    require_ideation_ambiguity_gate: bool('require_ideation_ambiguity_gate', false),
    max_ideation_ambiguity: num('max_ideation_ambiguity', 3),
    require_spec_resource_task_coverage: bool('require_spec_resource_task_coverage', true),
    auto_derive_spec_resources_enabled: bool('auto_derive_spec_resources_enabled', false),
    auto_derive_spec_resource_types: Array.isArray(raw.auto_derive_spec_resource_types)
      ? (raw.auto_derive_spec_resource_types as SpecResourceAutoDeriveType[])
      : [],
    design_system_gate_mode: normalizeDesignSystemGateMode(raw.design_system_gate_mode),
  };
}

function StatCard({
  label,
  value,
  hint,
  tone = 'slate',
  testId,
}: {
  label: string;
  value: string | number;
  hint: string;
  tone?: 'slate' | 'green' | 'amber';
  testId?: string;
}) {
  const toneClass = tone === 'green'
    ? 'text-emerald-700 dark:text-emerald-300'
    : tone === 'amber'
      ? 'text-amber-700 dark:text-amber-300'
      : 'text-gray-500 dark:text-gray-400';

  return (
    <div className="rounded-md border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900/60">
      <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
      <div className="mt-1 text-xl font-semibold text-gray-900 dark:text-white" data-testid={testId}>
        {value}
      </div>
      <div className={`mt-2 text-xs ${toneClass}`}>{hint}</div>
    </div>
  );
}

export function DefaultBoardConfigPanel({ boardId }: { boardId: string }) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;
  // Latest-draft ref so Save always reads the most recent edit, even if it is clicked
  // in the same tick as the last change (before the handler closure is re-attached).
  const draftRef = useRef<BoardSettings | null>(null);

  const [active, setActive] = useState<DefaultBoardConfigActiveResponse | null>(null);
  const [versions, setVersions] = useState<DefaultBoardConfigVersionsResponse | null>(null);
  const [diff, setDiff] = useState<DefaultBoardConfigDiff | null>(null);
  const [candidates, setCandidates] = useState<DefaultGuidelineCandidatesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [candidatesError, setCandidatesError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [versionPage, setVersionPage] = useState(0);
  // Local drafts of the editable template config. Edits accumulate here (no API,
  // no reload); a single new template version is created only on Save. null == not
  // editing that facet.
  const [draft, setDraft] = useState<BoardSettings | null>(null);
  const [draftGuidelineRefs, setDraftGuidelineRefs] = useState<Array<{ guideline_id: string; priority: number }> | null>(null);
  const guidelineRefsRef = useRef<Array<{ guideline_id: string; priority: number }> | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setCandidatesError(null);

    const [activeResult, versionsResult, diffResult, candidatesResult] = await Promise.allSettled([
      apiRef.current.getActiveDefaultBoardConfig(),
      apiRef.current.listDefaultBoardConfigVersions(),
      apiRef.current.getBoardDefaultConfigDiff(boardId),
      apiRef.current.listDefaultGuidelineCandidates(),
    ]);

    const errors: string[] = [];
    if (activeResult.status === 'fulfilled') setActive(activeResult.value);
    else {
      setActive(null);
      errors.push(loadErrorMessage(activeResult.reason));
    }

    if (versionsResult.status === 'fulfilled') setVersions(versionsResult.value);
    else {
      setVersions(null);
      errors.push(loadErrorMessage(versionsResult.reason));
    }

    if (diffResult.status === 'fulfilled') setDiff(diffResult.value);
    else {
      setDiff(null);
      errors.push(loadErrorMessage(diffResult.reason));
    }

    if (candidatesResult.status === 'fulfilled') setCandidates(candidatesResult.value);
    else {
      setCandidates(null);
      setCandidatesError('Default guideline candidates are unavailable.');
    }

    setError(errors.length > 0 ? Array.from(new Set(errors)).join(' ') : null);
    setLoading(false);
  }, [boardId]);

  useEffect(() => {
    void load();
  }, [load]);

  const runAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      try {
        await fn();
        await load();
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  if (loading) {
    return <div data-testid="dbc-loading" className="p-4 text-sm text-gray-500">Loading default board configuration...</div>;
  }

  const activeTemplate = active?.active ?? null;
  const mergedSettings = {
    ...DEFAULT_TEMPLATE_SETTINGS,
    ...(activeTemplate?.settings_payload ?? {}),
  };
  // The persisted baselines; the editors stage local drafts on top of them.
  const baseSettings = toBoardSettings(mergedSettings);
  const baseGuidelineRefs = (candidates?.candidates ?? [])
    .filter((c) => c.is_default)
    .map((c) => ({ guideline_id: c.guideline_id, priority: c.priority ?? 0 }));
  const formSettings = draft ?? baseSettings;
  const effectiveGuidelineRefs = draftGuidelineRefs ?? baseGuidelineRefs;
  const guidelineRefIds = new Map(effectiveGuidelineRefs.map((r) => [r.guideline_id, r.priority]));
  draftRef.current = draft;
  guidelineRefsRef.current = draftGuidelineRefs;

  const settingsDirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(baseSettings);
  const guidelinesDirty = draftGuidelineRefs !== null && JSON.stringify(draftGuidelineRefs) !== JSON.stringify(baseGuidelineRefs);
  const isDirty = settingsDirty || guidelinesDirty;

  const onDraftChange = (patch: Partial<BoardSettings>) => {
    setDraft((prev) => ({ ...(prev ?? baseSettings), ...patch }));
  };
  const toggleGuidelineDefault = (candidate: DefaultGuidelineCandidate) => {
    setDraftGuidelineRefs((prev) => {
      const current = prev ?? baseGuidelineRefs;
      const already = current.some((r) => r.guideline_id === candidate.guideline_id);
      if (already) return current.filter((r) => r.guideline_id !== candidate.guideline_id);
      const maxPriority = current.reduce((m, r) => Math.max(m, r.priority ?? 0), 0);
      return [...current, { guideline_id: candidate.guideline_id, priority: maxPriority + 1 }];
    });
  };
  const discardDraft = () => {
    setDraft(null);
    setDraftGuidelineRefs(null);
  };
  const saveDraft = () => {
    const settingsCurrent = draftRef.current ?? baseSettings;
    const refsCurrent = guidelineRefsRef.current ?? baseGuidelineRefs;
    if (draftRef.current === null && guidelineRefsRef.current === null) return;
    // Build ONE new active version from the accumulated drafts, mirroring the gate
    // mode into the Design System default ref so the two never drift apart.
    const nextSettings = omitUndefinedValues({
      ...DEFAULT_TEMPLATE_SETTINGS,
      ...(activeTemplate?.settings_payload ?? {}),
      ...settingsCurrent,
    });
    const nextDesignSystemRef = activeTemplate?.design_system_default_ref
      ? { ...activeTemplate.design_system_default_ref }
      : null;
    if (nextDesignSystemRef && typeof settingsCurrent.design_system_gate_mode === 'string') {
      nextDesignSystemRef.gate_mode = settingsCurrent.design_system_gate_mode as GateMode;
    }
    const payload: CreateDefaultBoardConfigVersionRequest = {
      settings_payload: nextSettings,
      guideline_default_refs: refsCurrent,
      design_system_default_ref: nextDesignSystemRef,
      activate: true,
    };
    void runAction(async () => {
      await apiRef.current.createDefaultBoardConfigVersion(payload);
      setDraft(null);
      setDraftGuidelineRefs(null);
    });
  };

  const ds = activeTemplate?.design_system_default_ref ?? null;
  const versionCount = versions?.versions.length ?? 0;
  // Clamp the page derived from state so a reload that shrinks the list never
  // strands the view on an out-of-range page (no effect needed).
  const versionTotalPages = Math.max(1, Math.ceil(versionCount / VERSION_PAGE_SIZE));
  const versionPageSafe = Math.min(Math.max(0, versionPage), versionTotalPages - 1);
  const pagedVersions = (versions?.versions ?? []).slice(
    versionPageSafe * VERSION_PAGE_SIZE,
    (versionPageSafe + 1) * VERSION_PAGE_SIZE,
  );
  const guidelineDefaultCount = activeTemplate?.guideline_default_refs.length ?? 0;
  const dsGate = ds?.gate_mode ?? (mergedSettings.design_system_gate_mode as string | undefined) ?? 'off';
  const isLegacy = diff?.snapshot_state === 'legacy_no_snapshot';
  const hasOverrides = (diff?.fields.length ?? 0) > 0;

  return (
    <div data-testid="default-board-config-panel" className="space-y-5 p-4">
      {error && (
        <div data-testid="dbc-error" className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900 dark:text-white">Default board configuration</h3>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            Edit gate defaults and guideline defaults freely — a new template version is created only when you save.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {isDirty && (
            <span data-testid="dbc-template-dirty" className="text-[11px] font-medium text-amber-600 dark:text-amber-400">
              Unsaved changes
            </span>
          )}
          <button
            type="button"
            disabled={busy || !isDirty}
            onClick={discardDraft}
            data-testid="dbc-discard-template"
            className="rounded-md border border-gray-300 px-3 py-1.5 text-xs font-medium text-gray-600 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300"
          >
            Discard
          </button>
          <button
            type="button"
            disabled={busy || !isDirty}
            onClick={saveDraft}
            data-testid="dbc-save-template"
            className="inline-flex items-center gap-1.5 rounded-md bg-gray-900 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40 dark:bg-white dark:text-gray-900"
          >
            <Plus size={13} />
            Save as new version
          </button>
        </div>
      </div>

      <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Active template"
          value={activeTemplate ? `v${activeTemplate.version}` : 'None'}
          hint={activeTemplate ? `${activeTemplate.status} for new boards` : 'BoardSettings fallback'}
          tone={activeTemplate ? 'green' : 'amber'}
          testId="dbc-active-version"
        />
        <StatCard
          label="Template versions"
          value={versionCount}
          hint="Version history"
        />
        <StatCard
          label="Default guidelines"
          value={guidelineDefaultCount}
          hint="Global catalog only"
          testId="dbc-guideline-count"
        />
        <StatCard
          label="Design system"
          value={dsGate === 'blocking' ? 'Required' : dsGate}
          hint={ds?.design_system_id ? shortId(ds.design_system_id) : 'No default Design System'}
          tone={dsGate === 'blocking' ? 'green' : dsGate === 'advisory' ? 'amber' : 'slate'}
          testId="dbc-design-system"
        />
      </section>

      {!activeTemplate && (
        <div data-testid="dbc-no-active" className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200">
          No active default template. New boards are created with BoardSettings defaults until a template is activated.
        </div>
      )}

      {/* Gate configuration — same disposition as the Board Config screen. Edits
          accumulate in a local draft; ONE new template version is created on Save. */}
      <section data-testid="dbc-template-settings-form">
        <div className="mb-3">
          <h4 className="text-xs font-semibold uppercase text-gray-500 dark:text-gray-400">Template gate defaults</h4>
          <p className="mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
            Same controls as Board Config. Changes are staged until you Save (top right).
          </p>
        </div>
        <BoardSettingsForm settings={formSettings} onChange={onDraftChange} />
      </section>

      <section className="grid gap-5 xl:grid-cols-2">
        <div className="rounded-md border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900/60">
          <div className="flex items-center gap-1.5 border-b border-gray-200 px-4 py-3 text-xs font-semibold uppercase text-gray-500 dark:border-gray-800 dark:text-gray-400">
            <ListChecks size={13} />
            Guideline defaults
          </div>
          <div className="p-4">
            {candidatesError ? (
              <p data-testid="dbc-candidates-error" className="text-xs text-amber-700 dark:text-amber-300">
                {candidatesError}
              </p>
            ) : candidates && candidates.candidates.length > 0 ? (
              <div data-testid="dbc-guideline-candidates" className="overflow-hidden rounded-md border border-gray-200 dark:border-gray-800">
                <div className="grid grid-cols-[minmax(0,1fr)_92px_112px] bg-gray-50 px-3 py-2 text-[10px] font-semibold uppercase text-gray-500 dark:bg-gray-800/70 dark:text-gray-400">
                  <div>Guideline</div>
                  <div>Priority</div>
                  <div>Default</div>
                </div>
                <div className="divide-y divide-gray-100 text-xs dark:divide-gray-800">
                  {candidates.candidates.map((c) => {
                    // is_default reflects the staged draft (or the persisted base), so the
                    // table updates live without creating a version until Save.
                    const isDef = guidelineRefIds.has(c.guideline_id);
                    const prio = guidelineRefIds.get(c.guideline_id);
                    return (
                      <div key={c.guideline_id} data-testid={`dbc-cand-${c.guideline_id}`} className="grid grid-cols-[minmax(0,1fr)_92px_112px] items-center gap-3 px-3 py-2">
                        <div className="min-w-0">
                          <div className="truncate font-medium text-gray-900 dark:text-white">{c.title}</div>
                          <div className="text-[10px] text-gray-400">global · v{c.guideline_version ?? '-'}</div>
                        </div>
                        <div>
                          {isDef ? (
                            <span data-testid={`dbc-cand-default-${c.guideline_id}`} className="rounded bg-blue-50 px-2 py-1 text-[10px] text-blue-700 dark:bg-blue-500/15 dark:text-blue-200">
                              p{prio}
                            </span>
                          ) : (
                            <span className="text-gray-400">-</span>
                          )}
                        </div>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => toggleGuidelineDefault(c)}
                          data-testid={`dbc-toggle-default-${c.guideline_id}`}
                          className={`rounded border px-2 py-1 text-[10px] disabled:opacity-50 ${
                            isDef
                              ? 'border-blue-500 bg-blue-500 text-white'
                              : 'border-gray-300 text-gray-600 dark:border-gray-700 dark:text-gray-300'
                          }`}
                        >
                          {isDef ? 'Unset' : 'Set default'}
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : (
              <p data-testid="dbc-no-candidates" className="text-xs text-gray-500 dark:text-gray-400">
                No global catalog guidelines.
              </p>
            )}
          </div>
        </div>

        <div className="rounded-md border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900/60" data-testid="dbc-design-system-detail">
          <div className="flex items-center gap-1.5 border-b border-gray-200 px-4 py-3 text-xs font-semibold uppercase text-gray-500 dark:border-gray-800 dark:text-gray-400">
            <Palette size={13} />
            Design system default
          </div>
          <div className="grid gap-2 p-4 text-xs sm:grid-cols-3">
            <div>
              <span className="text-gray-500 dark:text-gray-400">Reference</span>
              <div className="mt-0.5 font-mono text-gray-900 dark:text-white">{ds?.design_system_id ?? 'none'}</div>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">Version</span>
              <div className="mt-0.5 text-gray-900 dark:text-white">{ds?.version ?? '-'}</div>
            </div>
            <div>
              <span className="text-gray-500 dark:text-gray-400">Gate</span>
              <div className="mt-0.5 text-gray-900 dark:text-white">{dsGate}</div>
            </div>
          </div>
        </div>
      </section>

      <section className="grid gap-5 xl:grid-cols-2">
        <div className="rounded-md border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900/60">
          <div className="flex items-center gap-1.5 border-b border-gray-200 px-4 py-3 text-sm font-semibold text-gray-900 dark:border-gray-800 dark:text-white">
            <GitCompare size={14} />
            Board diff
          </div>
          <div className="space-y-3 p-4 text-sm">
            {isLegacy ? (
              <div data-testid="dbc-legacy" className="rounded border border-gray-200 bg-gray-50 p-3 text-xs text-gray-600 dark:border-gray-800 dark:bg-gray-800/50 dark:text-gray-300">
                Legacy board - no applied template snapshot.
              </div>
            ) : diff ? (
              <div data-testid="dbc-diff" className="space-y-3">
                <div className={`rounded border p-3 text-xs ${
                  hasOverrides || diff.is_outdated
                    ? 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200'
                    : 'border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-200'
                }`}>
                  <div className="flex items-center gap-1.5 font-medium">
                    {hasOverrides || diff.is_outdated ? <AlertTriangle size={13} /> : <CheckCircle2 size={13} />}
                    {hasOverrides ? `${diff.fields.length} local override${diff.fields.length === 1 ? '' : 's'} detected` : 'Matches applied template'}
                  </div>
                  <div className="mt-1">
                    Applied v{diff.applied_template_version ?? '-'} · active v{diff.active_template_version ?? '-'}{' '}
                    {diff.is_outdated && <span data-testid="dbc-outdated">(outdated)</span>}
                  </div>
                </div>
                {hasOverrides ? (
                  <div data-testid="dbc-diff-fields" className="space-y-2">
                    {diff.fields.map((f) => (
                      <div key={f.field} className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 text-xs">
                        <span className="truncate text-gray-600 dark:text-gray-400">{f.field}</span>
                        <span className="font-medium text-gray-900 dark:text-white">
                          {formatValue(f.template_value)} -&gt; {formatValue(f.current_value)}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p data-testid="dbc-no-overrides" className="text-xs text-gray-500 dark:text-gray-400">
                    No overrides against the applied template.
                  </p>
                )}
              </div>
            ) : (
              <p className="text-xs text-gray-500 dark:text-gray-400">Diff is unavailable.</p>
            )}
          </div>
        </div>

        <div className="space-y-5">
          <div className="rounded-md border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900/60">
            <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3 dark:border-gray-800">
              <h4 className="flex items-center gap-1.5 text-sm font-semibold text-gray-900 dark:text-white">
                <RotateCcw size={14} />
                Version history
              </h4>
            </div>
            {versionCount > 0 ? (
              <>
                <div data-testid="dbc-versions" className="divide-y divide-gray-100 text-xs dark:divide-gray-800">
                  {pagedVersions.map((t: DefaultBoardConfigTemplate) => (
                    <div key={t.id} data-testid={`dbc-version-${t.version}`} className="grid grid-cols-[68px_minmax(0,1fr)_auto] items-center gap-2 px-4 py-3">
                      <span className="font-mono text-gray-900 dark:text-white">v{t.version}</span>
                      <span className={`w-fit rounded px-2 py-1 text-[10px] ${t.is_active ? badgeClass('green') : badgeClass('slate')}`}>
                        {t.status}
                      </span>
                      {!t.is_active && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => runAction(() => apiRef.current.activateDefaultBoardConfigVersion(t.id))}
                          data-testid={`dbc-activate-${t.version}`}
                          className="rounded border border-gray-300 px-2 py-1 text-[10px] disabled:opacity-50 dark:border-gray-700"
                        >
                          Activate
                        </button>
                      )}
                    </div>
                  ))}
                </div>
                {versionCount > VERSION_PAGE_SIZE && (
                  <div
                    data-testid="dbc-versions-pagination"
                    className="flex items-center justify-between border-t border-gray-200 px-4 py-2 text-[11px] text-gray-500 dark:border-gray-800 dark:text-gray-400"
                  >
                    <button
                      type="button"
                      disabled={versionPageSafe === 0}
                      onClick={() => setVersionPage(Math.max(0, versionPageSafe - 1))}
                      data-testid="dbc-versions-prev"
                      className="rounded border border-gray-300 px-2 py-1 disabled:opacity-40 dark:border-gray-700"
                    >
                      Prev
                    </button>
                    <span data-testid="dbc-versions-page">
                      Page {versionPageSafe + 1} of {versionTotalPages}
                    </span>
                    <button
                      type="button"
                      disabled={versionPageSafe >= versionTotalPages - 1}
                      onClick={() => setVersionPage(Math.min(versionTotalPages - 1, versionPageSafe + 1))}
                      data-testid="dbc-versions-next"
                      className="rounded border border-gray-300 px-2 py-1 disabled:opacity-40 dark:border-gray-700"
                    >
                      Next
                    </button>
                  </div>
                )}
              </>
            ) : (
              <p data-testid="dbc-no-versions" className="p-4 text-xs text-gray-500 dark:text-gray-400">
                No template versions yet.
              </p>
            )}
          </div>

          {activeTemplate && (
            <button
              type="button"
              disabled={busy}
              onClick={() => runAction(() => apiRef.current.deactivateDefaultBoardConfigVersion(activeTemplate.id))}
              data-testid="dbc-deactivate"
              className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-xs font-medium text-gray-700 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200"
            >
              Deactivate active template
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
