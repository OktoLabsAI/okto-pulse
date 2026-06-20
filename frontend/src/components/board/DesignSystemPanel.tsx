// Design System menu (spec 3a006f65 / card 1392f59d / FR2). Mirrors the Guidelines
// modal pattern: left nav with "Global Catalog" + "Board Design System" tabs, a small
// default/effective info box, and card-based lists with a per-row action group
// (set/unset default, edit, use/stop-using on the board, delete).
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Edit3,
  FileText,
  Globe,
  HelpCircle,
  Link,
  Palette,
  Plus,
  Search,
  Trash2,
  Unlink,
  X,
} from 'lucide-react';

import { useDashboardApi } from '@/services/api';
import type {
  BoardDesignSystemEffectiveResponse,
  DefaultBoardConfigActiveResponse,
  DesignSystem,
} from '@/types';

type Tab = 'global' | 'board';
type GateMode = 'off' | 'advisory' | 'blocking';

const gateModes: GateMode[] = ['off', 'advisory', 'blocking'];

function normalizeGateMode(value: unknown): GateMode {
  return gateModes.includes(value as GateMode) ? (value as GateMode) : 'off';
}

function contentFromPayload(payload: Record<string, unknown> | null | undefined) {
  if (!payload) return '';
  if (typeof payload.content === 'string') return payload.content;
  return JSON.stringify(payload, null, 2);
}

function buildPayload(content: string): Record<string, unknown> | null {
  const trimmed = content.trim();
  if (!trimmed) return null;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // Plain text is the expected path for assistant context snippets.
  }
  return { content: trimmed };
}

function payloadSummary(payload: Record<string, unknown> | null | undefined) {
  const content = contentFromPayload(payload).trim();
  if (!content) return 'No content yet';
  const oneLine = content.replace(/\s+/g, ' ');
  return oneLine.length > 150 ? `${oneLine.slice(0, 150)}...` : oneLine;
}

export function DesignSystemPanel({ boardId, onClose }: { boardId: string; onClose: () => void }) {
  const api = useDashboardApi();
  const apiRef = useRef(api);
  apiRef.current = api;

  const [activeTab, setActiveTab] = useState<Tab>('global');
  const [globals, setGlobals] = useState<DesignSystem[]>([]);
  const [inlines, setInlines] = useState<DesignSystem[]>([]);
  const [effective, setEffective] = useState<BoardDesignSystemEffectiveResponse | null>(null);
  const [defaultConfig, setDefaultConfig] = useState<DefaultBoardConfigActiveResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [globalSearch, setGlobalSearch] = useState('');
  const [showInlineForm, setShowInlineForm] = useState(false);
  const [showGlobalForm, setShowGlobalForm] = useState(false);
  const [editing, setEditing] = useState<DesignSystem | null>(null);
  const [showHelp, setShowHelp] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [g, i, e, cfg] = await Promise.all([
        apiRef.current.listDesignSystems('global'),
        apiRef.current.listDesignSystems('inline', boardId),
        apiRef.current.getBoardDesignSystem(boardId),
        apiRef.current.getActiveDefaultBoardConfig(),
      ]);
      setGlobals(g);
      setInlines(i);
      setEffective(e);
      setDefaultConfig(cfg);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load Design Systems.');
    } finally {
      setLoading(false);
    }
  }, [boardId]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = useCallback(
    async (fn: () => Promise<unknown>) => {
      setBusy(true);
      setError(null);
      try {
        await fn();
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Action failed.');
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  const resetForm = () => {
    setTitle('');
    setContent('');
    setShowInlineForm(false);
    setShowGlobalForm(false);
    setEditing(null);
  };

  const openCreateGlobal = () => {
    setTitle('');
    setContent('');
    setEditing(null);
    setShowGlobalForm(!showGlobalForm);
    setShowInlineForm(false);
  };

  const openCreateInline = () => {
    setTitle('');
    setContent('');
    setEditing(null);
    setShowInlineForm(!showInlineForm);
    setShowGlobalForm(false);
  };

  const openEdit = (designSystem: DesignSystem) => {
    setEditing(designSystem);
    setTitle(designSystem.title);
    setContent(contentFromPayload(designSystem.payload));
    setShowGlobalForm(false);
    setShowInlineForm(false);
    setActiveTab(designSystem.scope === 'inline' ? 'board' : 'global');
  };

  const createGlobal = () => run(async () => {
    await apiRef.current.createDesignSystem({
      title: title.trim(),
      scope: 'global',
      payload: buildPayload(content),
    });
    resetForm();
  });

  const createInline = () => run(async () => {
    await apiRef.current.createDesignSystem({
      title: title.trim(),
      scope: 'inline',
      board_id: boardId,
      payload: buildPayload(content),
    });
    resetForm();
  });

  const saveEdit = () => run(async () => {
    if (!editing) return;
    await apiRef.current.updateDesignSystem(editing.id, {
      title: title.trim(),
      payload: buildPayload(content),
    });
    resetForm();
  });

  const ensureTemplateId = useCallback(async (): Promise<string> => {
    if (defaultConfig?.active?.id) return defaultConfig.active.id;
    const created = await apiRef.current.createDefaultBoardConfigVersion({ activate: true });
    return created.id;
  }, [defaultConfig?.active?.id]);

  const effectiveId = effective?.effective?.design_system_id ?? null;
  const effectiveTitle = effective?.effective?.title ?? effectiveId;
  const defaultRef = defaultConfig?.active?.design_system_default_ref ?? null;
  const defaultId = defaultRef?.design_system_id ?? null;
  const resolvedDefaultGateMode = normalizeGateMode(
    defaultConfig?.active?.settings_payload?.design_system_gate_mode
      ?? defaultRef?.gate_mode
      ?? effective?.effective?.gate_mode
      ?? 'off',
  );
  const filteredGlobals = globals.filter((d) =>
    !globalSearch || d.title.toLowerCase().includes(globalSearch.toLowerCase()),
  );

  const setAsDefault = (designSystem: DesignSystem) => run(async () => {
    const templateId = await ensureTemplateId();
    await apiRef.current.setDefaultDesignSystem(templateId, {
      design_system_id: designSystem.id,
      version: designSystem.version,
      gate_mode: resolvedDefaultGateMode,
    });
  });

  // The backend has no "clear default" endpoint (set_template_design_system requires a
  // real design_system_id). To UNSET, copy-on-write a new active template version that
  // is identical to the current one but with the Design System default cleared.
  const unsetDefault = () => run(async () => {
    const tpl = defaultConfig?.active;
    if (!tpl) return;
    await apiRef.current.createDefaultBoardConfigVersion({
      settings_payload: tpl.settings_payload ?? {},
      guideline_default_refs: tpl.guideline_default_refs ?? [],
      design_system_default_ref: null,
      activate: true,
    });
  });

  // Toggle: a non-default global becomes the default; the current default is cleared.
  const toggleDefault = (designSystem: DesignSystem) =>
    defaultId === designSystem.id ? unsetDefault() : setAsDefault(designSystem);

  const handleDelete = (designSystem: DesignSystem) => {
    const warnings: string[] = [];
    if (effectiveId === designSystem.id) warnings.push("the board's effective Design System");
    if (defaultId === designSystem.id) warnings.push('the global default');
    const suffix = warnings.length
      ? `\n\nWarning: this is ${warnings.join(' and ')}. Existing references will be left dangling.`
      : '';
    if (!window.confirm(`Delete Design System "${designSystem.title}"? This cannot be undone.${suffix}`)) return;
    void run(() => apiRef.current.deleteDesignSystem(designSystem.id));
  };

  const createOrEditForm = (saveLabel: string, onSave: () => void, testId: string) => (
    <div className="space-y-3 rounded-md border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900/70">
      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Design System title"
        data-testid="dsp-new-title"
        className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none focus:ring-2 focus:ring-blue-300 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
      />
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        rows={9}
        placeholder="Content for assistant context: tokens, components, layout rules, accessibility expectations, evidence required for mockups."
        data-testid="dsp-new-content"
        className="w-full rounded-md border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 outline-none focus:ring-2 focus:ring-blue-300 dark:border-gray-700 dark:bg-gray-950 dark:text-white"
      />
      <div className="flex gap-2">
        <button
          type="button"
          disabled={busy || !title.trim()}
          onClick={onSave}
          data-testid={testId}
          className="rounded-md bg-gray-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-gray-900"
        >
          {saveLabel}
        </button>
        <button type="button" onClick={resetForm} className="rounded-md border border-gray-300 px-3 py-2 text-sm dark:border-gray-700">
          Cancel
        </button>
      </div>
    </div>
  );

  const helpPanel = showHelp && (
    <section
      data-testid="dsp-help-examples"
      className="mb-4 rounded-md border border-blue-200 bg-blue-50 p-4 text-sm text-blue-950 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-100"
    >
      <div className="mb-2 flex items-center gap-2 font-semibold">
        <HelpCircle size={15} />
        Assistant context examples
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Visual language</div>
          <p className="mt-1 text-xs leading-5">
            Use restrained operational UI, 8px max card radius, lucide icons for toolbar actions,
            neutral surfaces, and semantic status colors only for decisions or validation state.
          </p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Mockup evidence</div>
          <p className="mt-1 text-xs leading-5">
            When producing a mockup, cite the Design System version, list consumed tokens/components,
            and explain any intentional deviation before submission.
          </p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Structured JSON</div>
          <p className="mt-1 text-xs leading-5">
            JSON payloads are accepted for tokens, typography, spacing, component rules, and accessibility constraints.
          </p>
        </div>
        <div>
          <div className="text-xs font-semibold uppercase opacity-75">Assistant instruction</div>
          <p className="mt-1 text-xs leading-5">
            Prefer dense, repeatable product workflows over marketing composition; validate text fit on desktop and mobile.
          </p>
        </div>
      </div>
    </section>
  );

  const badge = (testId: string, label: string, tone: 'green' | 'blue') => (
    <span
      data-testid={testId}
      className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${
        tone === 'green'
          ? 'bg-green-100 text-green-700 dark:bg-green-500/15 dark:text-green-200'
          : 'bg-blue-100 text-blue-700 dark:bg-blue-500/15 dark:text-blue-200'
      }`}
    >
      {label}
    </span>
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" data-testid="design-system-panel">
      <div className="flex h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-lg border border-gray-200 bg-white shadow-2xl dark:border-gray-700 dark:bg-gray-900">
        <div className="flex shrink-0 items-center justify-between border-b border-gray-200 px-6 py-4 dark:border-gray-700">
          <div className="flex items-center gap-2">
            <Palette size={20} className="text-blue-500" />
            <div>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Design System</h2>
              <p className="text-xs text-gray-500 dark:text-gray-400">Catalog, board design system and assistant context</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setShowHelp((value) => !value)}
              data-testid="dsp-help-toggle"
              className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
            >
              <HelpCircle size={14} />
              Help
            </button>
            <button
              onClick={onClose}
              data-testid="dsp-close"
              className="rounded-md p-1.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-800 dark:hover:text-gray-300"
              aria-label="Close Design System"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="grid min-h-0 flex-1 grid-cols-[240px_minmax(0,1fr)]">
          <aside className="border-r border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-950/30">
            <nav className="space-y-1 text-sm">
              {([
                { id: 'global' as Tab, label: 'Global Catalog', icon: <Globe size={14} />, count: globals.length, testId: 'dsp-tab-global' },
                { id: 'board' as Tab, label: 'Board Design System', icon: <FileText size={14} />, count: inlines.length, testId: 'dsp-tab-board' },
              ]).map((tab) => (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setActiveTab(tab.id)}
                  data-testid={tab.testId}
                  className={`flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 font-medium transition-colors ${
                    activeTab === tab.id
                      ? 'bg-white text-gray-900 shadow-sm ring-1 ring-gray-200 dark:bg-gray-800 dark:text-white dark:ring-gray-700'
                      : 'text-gray-600 hover:bg-white/70 dark:text-gray-400 dark:hover:bg-gray-800/60'
                  }`}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    {tab.icon}
                    <span className="truncate">{tab.label}</span>
                  </span>
                  <span className="shrink-0 rounded bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-600 dark:bg-gray-700 dark:text-gray-300">
                    {tab.count}
                  </span>
                </button>
              ))}
              <div className="mt-3 rounded-md border border-gray-200 bg-white px-3 py-2 text-xs text-gray-500 dark:border-gray-800 dark:bg-gray-900 dark:text-gray-400" data-testid="dsp-effective-summary">
                <div className="font-medium text-gray-700 dark:text-gray-200">Board effective</div>
                <div className="mt-0.5 truncate">{effective?.effective ? effectiveTitle : 'none'}</div>
                <div className="mt-1 font-medium text-gray-700 dark:text-gray-200">Default template</div>
                <div className="mt-0.5">
                  {defaultConfig?.active ? `v${defaultConfig.active.version}` : 'No active template'}
                </div>
              </div>
            </nav>
          </aside>

          <main className="min-w-0 overflow-y-auto p-6 text-sm">
            {loading ? (
              <div data-testid="dsp-loading" className="text-center text-gray-400">Loading Design Systems...</div>
            ) : error ? (
              <div data-testid="dsp-error" className="rounded-md border border-red-200 bg-red-50 p-3 text-red-600 dark:border-red-900/50 dark:bg-red-900/20 dark:text-red-300">{error}</div>
            ) : (
              <div className="space-y-4">
                {helpPanel}

                {editing && (
                  <section>
                    {createOrEditForm('Save changes', saveEdit, 'dsp-save-edit')}
                  </section>
                )}

                {/* ==================== GLOBAL CATALOG ==================== */}
                {activeTab === 'global' && (
                  <section className="space-y-4">
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={openCreateGlobal}
                        className="inline-flex items-center gap-1 rounded-md bg-gray-900 px-3 py-1.5 text-xs font-medium text-white dark:bg-white dark:text-gray-900"
                      >
                        <Plus size={13} /> New design system
                      </button>
                      <div className="relative flex-1">
                        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
                        <input
                          type="text"
                          value={globalSearch}
                          onChange={(e) => setGlobalSearch(e.target.value)}
                          placeholder="Search"
                          className="w-full rounded-md border border-gray-300 bg-white py-1.5 pl-9 pr-3 text-xs text-gray-900 outline-none dark:border-gray-700 dark:bg-gray-950 dark:text-white"
                        />
                      </div>
                    </div>

                    {showGlobalForm && createOrEditForm('Create Global', createGlobal, 'dsp-create-global')}

                    {filteredGlobals.length === 0 ? (
                      <div data-testid="dsp-no-globals" className="py-10 text-center text-gray-400">
                        <Globe size={36} className="mx-auto mb-2 opacity-40" />
                        <p className="text-sm">{globalSearch ? 'No matching Design Systems' : 'No global Design Systems yet.'}</p>
                      </div>
                    ) : (
                      <div data-testid="dsp-globals" className="space-y-2">
                        {filteredGlobals.map((d) => {
                          // "linked" = an explicit per-board selection (board_link). Being the
                          // effective DS via the default fallback is NOT a link — it shows the
                          // "default" badge, and selection/deselection stays reversible.
                          const isBoardLinked = effective?.effective?.source === 'board_link' && effectiveId === d.id;
                          const isDefault = defaultId === d.id;
                          return (
                            <div key={d.id} data-testid={`dsp-global-${d.id}`} className="rounded-lg border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800/50">
                              <div className="flex items-start justify-between gap-3">
                                <div className="min-w-0 flex-1">
                                  <div className="mb-1 flex items-center gap-2">
                                    <Globe size={14} className="shrink-0 text-blue-500" />
                                    <h3 className="truncate text-sm font-medium text-gray-900 dark:text-white">{d.title}</h3>
                                    <span className="shrink-0 text-[10px] text-gray-400">v{d.version}</span>
                                    {isBoardLinked && badge(`dsp-linked-${d.id}`, 'linked', 'green')}
                                    {isDefault && badge(`dsp-default-${d.id}`, 'default', 'blue')}
                                  </div>
                                  <p className="line-clamp-2 text-xs text-gray-500 dark:text-gray-400">{payloadSummary(d.payload)}</p>
                                </div>
                                <div className="flex shrink-0 items-center gap-1">
                                  <button
                                    type="button"
                                    disabled={busy || d.scope !== 'global' || d.status !== 'active'}
                                    onClick={() => toggleDefault(d)}
                                    data-testid={`dsp-set-default-${d.id}`}
                                    title={isDefault ? 'Remove as the default for new boards' : 'Set as the default for new boards'}
                                    className={`rounded border px-2 py-1 text-[10px] disabled:cursor-not-allowed disabled:opacity-40 ${
                                      isDefault
                                        ? 'border-blue-500 bg-blue-500 text-white'
                                        : 'border-gray-300 text-gray-600 dark:border-gray-600 dark:text-gray-300'
                                    }`}
                                  >
                                    {isDefault ? 'Default' : 'Set default'}
                                  </button>
                                  {isBoardLinked ? (
                                    <button
                                      type="button"
                                      disabled={busy}
                                      onClick={() => run(() => apiRef.current.unlinkBoardDesignSystem(boardId))}
                                      data-testid={`dsp-unlink-${d.id}`}
                                      title="Stop using on this board (fall back to the default)"
                                      className="rounded border border-gray-300 px-2 py-1 text-[10px] disabled:opacity-50 dark:border-gray-600"
                                    >
                                      <Unlink size={10} className="mr-1 inline" /> Stop using
                                    </button>
                                  ) : (
                                    <button
                                      type="button"
                                      disabled={busy}
                                      onClick={() => run(() => apiRef.current.linkBoardDesignSystem(boardId, d.id))}
                                      data-testid={`dsp-link-${d.id}`}
                                      title="Use on this board"
                                      className="rounded border border-gray-300 px-2 py-1 text-[10px] disabled:opacity-50 dark:border-gray-600"
                                    >
                                      <Link size={10} className="mr-1 inline" /> Use
                                    </button>
                                  )}
                                  <button
                                    type="button"
                                    disabled={busy}
                                    onClick={() => openEdit(d)}
                                    data-testid={`dsp-edit-${d.id}`}
                                    title="Edit"
                                    className="rounded p-1.5 text-gray-400 hover:bg-blue-50 hover:text-blue-500 disabled:opacity-50 dark:hover:bg-blue-900/20"
                                  >
                                    <Edit3 size={14} />
                                  </button>
                                  <button
                                    type="button"
                                    disabled={busy}
                                    onClick={() => handleDelete(d)}
                                    data-testid={`dsp-delete-${d.id}`}
                                    title="Delete this Design System"
                                    className="rounded p-1.5 text-gray-400 hover:bg-red-50 hover:text-red-500 disabled:opacity-50 dark:hover:bg-red-900/20"
                                  >
                                    <Trash2 size={14} />
                                  </button>
                                </div>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </section>
                )}

                {/* ==================== BOARD DESIGN SYSTEM ==================== */}
                {activeTab === 'board' && (
                  <section className="space-y-4">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <h3 className="font-semibold text-gray-900 dark:text-white">Board Design System</h3>
                        <p className="text-xs text-gray-500 dark:text-gray-400">The board's single effective Design System and its own inline systems.</p>
                      </div>
                      <button
                        type="button"
                        onClick={openCreateInline}
                        className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-3 py-1.5 text-xs font-medium dark:border-gray-700"
                      >
                        <Plus size={13} /> Create Inline
                      </button>
                    </div>

                    {showInlineForm && createOrEditForm('Create Inline', createInline, 'dsp-create-inline')}

                    <div data-testid="dsp-effective" className="rounded-md border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-gray-900/60">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="text-xs uppercase text-gray-500 dark:text-gray-400">Effective Design System</div>
                          {effective?.effective ? (
                            <>
                              <div className="mt-2 font-semibold text-gray-900 dark:text-white">{effectiveTitle}</div>
                              <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                                {effective.effective.source} · v{effective.effective.version ?? '-'} · {effective.effective.gate_mode ?? resolvedDefaultGateMode}
                              </div>
                              {effective.effective.source === 'default_snapshot' && (
                                <div data-testid="dsp-effective-default-note" className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                                  No board-specific selection — falling back to the default. Pick &ldquo;Use&rdquo; on a Design System to override it for this board.
                                </div>
                              )}
                            </>
                          ) : (
                            <div data-testid="dsp-effective-none" className="mt-2 text-gray-500">none</div>
                          )}
                        </div>
                        {effective?.effective?.source === 'board_link' && (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => run(() => apiRef.current.unlinkBoardDesignSystem(boardId))}
                            data-testid="dsp-unlink"
                            title="Remove this board's selection and fall back to the default"
                            className="rounded border border-gray-300 px-2 py-1 text-xs disabled:opacity-50 dark:border-gray-700"
                          >
                            <Unlink size={12} className="mr-1 inline" /> Unlink
                          </button>
                        )}
                      </div>
                    </div>

                    <div data-testid="dsp-inlines" className="rounded-md border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900/60">
                      <div className="border-b border-gray-200 px-4 py-3 text-sm font-semibold text-gray-900 dark:border-gray-800 dark:text-white">Board inline systems</div>
                      {inlines.length > 0 ? (
                        <div className="divide-y divide-gray-100 dark:divide-gray-800">
                          {inlines.map((d) => {
                            const linked = effective?.effective?.source === 'board_link' && effectiveId === d.id;
                            return (
                              <div key={d.id} data-testid={`dsp-inline-${d.id}`} className="flex items-start justify-between gap-3 px-4 py-3">
                                <div className="min-w-0">
                                  <div className="flex items-center gap-2">
                                    <FileText size={14} className="shrink-0 text-gray-400" />
                                    <span className="truncate font-medium text-gray-900 dark:text-white">{d.title}</span>
                                    {linked && badge(`dsp-linked-${d.id}`, 'linked', 'green')}
                                  </div>
                                  <div className="text-xs text-gray-500 dark:text-gray-400">inline · v{d.version} · {d.status}</div>
                                  <div className="mt-1 truncate text-xs text-gray-500 dark:text-gray-400">{payloadSummary(d.payload)}</div>
                                </div>
                                <div className="flex shrink-0 items-center gap-1">
                                  {linked ? (
                                    <button
                                      type="button"
                                      disabled={busy}
                                      onClick={() => run(() => apiRef.current.unlinkBoardDesignSystem(boardId))}
                                      data-testid={`dsp-unlink-${d.id}`}
                                      title="Stop using on this board"
                                      className="rounded border border-gray-300 px-2 py-1 text-[10px] disabled:opacity-50 dark:border-gray-600"
                                    >
                                      <Unlink size={10} className="mr-1 inline" /> Stop using
                                    </button>
                                  ) : (
                                    <button
                                      type="button"
                                      disabled={busy}
                                      onClick={() => run(() => apiRef.current.linkBoardDesignSystem(boardId, d.id))}
                                      data-testid={`dsp-link-${d.id}`}
                                      title="Use on this board"
                                      className="rounded border border-gray-300 px-2 py-1 text-[10px] disabled:opacity-50 dark:border-gray-600"
                                    >
                                      <Link size={10} className="mr-1 inline" /> Use on board
                                    </button>
                                  )}
                                  <button
                                    type="button"
                                    disabled={busy}
                                    onClick={() => openEdit(d)}
                                    data-testid={`dsp-edit-${d.id}`}
                                    title="Edit"
                                    className="rounded p-1.5 text-gray-400 hover:bg-blue-50 hover:text-blue-500 disabled:opacity-50 dark:hover:bg-blue-900/20"
                                  >
                                    <Edit3 size={14} />
                                  </button>
                                  <button
                                    type="button"
                                    disabled={busy}
                                    onClick={() => handleDelete(d)}
                                    data-testid={`dsp-delete-${d.id}`}
                                    title="Delete this inline Design System"
                                    className="rounded p-1.5 text-gray-400 hover:bg-red-50 hover:text-red-500 disabled:opacity-50 dark:hover:bg-red-900/20"
                                  >
                                    <Trash2 size={14} />
                                  </button>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <div data-testid="dsp-no-inlines" className="py-10 text-center text-gray-400">
                          <FileText size={36} className="mx-auto mb-2 opacity-40" />
                          <p className="text-sm">No inline Design Systems on this board.</p>
                        </div>
                      )}
                    </div>
                  </section>
                )}
              </div>
            )}
          </main>
        </div>
      </div>
    </div>
  );
}
