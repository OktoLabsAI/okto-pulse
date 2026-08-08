// Shared impact-evidence editor (SK-B2-S1, FR-7/AC-10/AC-16).
// Mounted by BOTH human conclusion surfaces — the CardModal inline prompt and
// the KanbanBoard drag-and-drop "Execution Report Required" modal — so the
// two never drift. Collapsible optional section, default zero rows, last-row
// removal allowed, enum selects + mono inputs, Add/Remove per section. The
// editor renders collapsed with its OWN internal scroll so the DnD modal
// keeps max-w-lg (AC-16). The UI never infers nor pre-populates evidence
// content (FR-7): every row starts empty and is typed by the author.
import { useMemo } from 'react';
import { ChevronRight, Plus, X } from 'lucide-react';
import type {
  ImpactEvidenceChangeKind,
  ImpactEvidenceRepo,
  ImpactEvidenceSurfaceKind,
  ImpactEvidenceSymbolAction,
  ImpactEvidenceSymbolKind,
  ImpactEvidenceTestAction,
} from '@/types';
import {
  impactDraftRowCount,
  nextImpactRepo,
  type ImpactEvidenceDraft,
} from './impactEvidenceModel';

const REPOS: ImpactEvidenceRepo[] = ['core', 'community'];

const CHANGE_KINDS: ImpactEvidenceChangeKind[] = [
  'created',
  'modified',
  'deleted',
  'renamed',
];
const SYMBOL_KINDS: ImpactEvidenceSymbolKind[] = [
  'function',
  'class',
  'method',
  'component',
  'port',
  'other',
];
const SYMBOL_ACTIONS: ImpactEvidenceSymbolAction[] = [
  'created',
  'modified',
  'deleted',
];
const SURFACE_KINDS: ImpactEvidenceSurfaceKind[] = [
  'rest_route',
  'mcp_tool',
  'mcp_resource',
  'ui_component',
  'table',
  'cli_command',
  'event',
  'migration',
  'other',
];
const TEST_ACTIONS: ImpactEvidenceTestAction[] = ['added', 'updated'];

const inputCls =
  'w-full rounded border border-gray-300 px-2 py-1 font-mono text-[11px] '
  + 'dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100';
const selectCls =
  'rounded border border-gray-300 px-1 py-1 text-[11px] '
  + 'dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100';

function SectionHeader({
  title,
  onAdd,
  disabled,
  testId,
}: {
  title: string;
  onAdd: () => void;
  disabled?: boolean;
  testId: string;
}) {
  return (
    <div className="flex items-center justify-between">
      <p className="text-[11px] font-semibold text-gray-600 dark:text-gray-300">
        {title}
      </p>
      <button
        type="button"
        onClick={onAdd}
        disabled={disabled}
        data-testid={testId}
        className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-violet-700 hover:bg-violet-50 disabled:opacity-50 dark:text-violet-300 dark:hover:bg-violet-900/20"
      >
        <Plus size={11} /> Add
      </button>
    </div>
  );
}

function RemoveButton({
  onClick,
  disabled,
  label,
}: {
  onClick: () => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      className="shrink-0 rounded p-1 text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-50 dark:hover:bg-red-900/20"
    >
      <X size={12} />
    </button>
  );
}

export interface ImpactEvidenceEditorProps {
  draft: ImpactEvidenceDraft;
  onChange: (draft: ImpactEvidenceDraft) => void;
  disabled?: boolean;
}

export function ImpactEvidenceEditor({
  draft,
  onChange,
  disabled = false,
}: ImpactEvidenceEditorProps) {
  const rowCount = useMemo(() => impactDraftRowCount(draft), [draft]);

  const patch = (partial: Partial<ImpactEvidenceDraft>) =>
    onChange({ ...draft, ...partial });

  return (
    <details
      className="mt-3 rounded-lg border border-gray-200 dark:border-gray-700"
      data-testid="impact-evidence-editor"
    >
      <summary className="flex cursor-pointer select-none items-center gap-1.5 px-3 py-2 text-xs font-medium text-gray-700 dark:text-gray-300">
        <ChevronRight size={12} className="shrink-0" />
        Impact evidence (optional)
        <span
          className="ml-1 rounded-full bg-gray-100 px-1.5 text-[10px] text-gray-500 dark:bg-gray-700 dark:text-gray-400"
          data-testid="impact-evidence-row-count"
        >
          {rowCount}
        </span>
        <span className="ml-auto text-[10px] font-normal text-gray-400">
          declared claim — validators diff reality
        </span>
      </summary>
      <fieldset
        disabled={disabled}
        className="max-h-64 space-y-3 overflow-y-auto border-t border-gray-100 p-3 dark:border-gray-700/50"
        data-testid="impact-evidence-sections"
      >
        <div className="space-y-1.5">
          <SectionHeader
            title="Files"
            testId="impact-add-file"
            disabled={disabled}
            onAdd={() =>
              patch({
                files: [
                  ...draft.files,
                  {
                    repo: nextImpactRepo(draft.files),
                    path: '',
                    change_kind: 'modified',
                    previous_path: '',
                    note: '',
                  },
                ],
              })
            }
          />
          {draft.files.map((row, i) => (
            <div
              key={`file-${i}`}
              className="flex items-center gap-1.5"
              data-testid={`impact-file-row-${i}`}
            >
              <select
                className={selectCls}
                value={row.repo}
                aria-label={`file ${i} repo`}
                onChange={(e) => {
                  const files = [...draft.files];
                  files[i] = {
                    ...row,
                    repo: e.target.value as ImpactEvidenceRepo,
                  };
                  patch({ files });
                }}
              >
                {REPOS.map((r) => (
                  <option key={r}>{r}</option>
                ))}
              </select>
              <select
                className={selectCls}
                value={row.change_kind}
                aria-label={`file ${i} change kind`}
                onChange={(e) => {
                  const files = [...draft.files];
                  files[i] = {
                    ...row,
                    change_kind: e.target
                      .value as ImpactEvidenceChangeKind,
                  };
                  patch({ files });
                }}
              >
                {CHANGE_KINDS.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
              <input
                className={inputCls}
                placeholder="src/path/file.py"
                value={row.path}
                aria-label={`file ${i} path`}
                onChange={(e) => {
                  const files = [...draft.files];
                  files[i] = { ...row, path: e.target.value };
                  patch({ files });
                }}
              />
              {row.change_kind === 'renamed' && (
                <input
                  className={inputCls}
                  placeholder="previous/path.py"
                  value={row.previous_path}
                  aria-label={`file ${i} previous path`}
                  onChange={(e) => {
                    const files = [...draft.files];
                    files[i] = { ...row, previous_path: e.target.value };
                    patch({ files });
                  }}
                />
              )}
              <input
                className={inputCls}
                placeholder="why (optional)"
                value={row.note}
                aria-label={`file ${i} note`}
                onChange={(e) => {
                  const files = [...draft.files];
                  files[i] = { ...row, note: e.target.value };
                  patch({ files });
                }}
              />
              <RemoveButton
                label={`remove file ${i}`}
                disabled={disabled}
                onClick={() =>
                  patch({ files: draft.files.filter((_, j) => j !== i) })
                }
              />
            </div>
          ))}
        </div>

        <div className="space-y-1.5">
          <SectionHeader
            title="Symbols"
            testId="impact-add-symbol"
            disabled={disabled}
            onAdd={() =>
              patch({
                symbols: [
                  ...draft.symbols,
                  {
                    name: '',
                    kind: 'function',
                    action: 'created',
                    repo: nextImpactRepo(draft.symbols),
                    file: '',
                  },
                ],
              })
            }
          />
          {draft.symbols.map((row, i) => (
            <div
              key={`symbol-${i}`}
              className="flex items-center gap-1.5"
              data-testid={`impact-symbol-row-${i}`}
            >
              <input
                className={inputCls}
                placeholder="SymbolName"
                value={row.name}
                aria-label={`symbol ${i} name`}
                onChange={(e) => {
                  const symbols = [...draft.symbols];
                  symbols[i] = { ...row, name: e.target.value };
                  patch({ symbols });
                }}
              />
              <select
                className={selectCls}
                value={row.kind}
                aria-label={`symbol ${i} kind`}
                onChange={(e) => {
                  const symbols = [...draft.symbols];
                  symbols[i] = {
                    ...row,
                    kind: e.target.value as ImpactEvidenceSymbolKind,
                  };
                  patch({ symbols });
                }}
              >
                {SYMBOL_KINDS.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
              <select
                className={selectCls}
                value={row.action}
                aria-label={`symbol ${i} action`}
                onChange={(e) => {
                  const symbols = [...draft.symbols];
                  symbols[i] = {
                    ...row,
                    action: e.target.value as ImpactEvidenceSymbolAction,
                  };
                  patch({ symbols });
                }}
              >
                {SYMBOL_ACTIONS.map((a) => (
                  <option key={a}>{a}</option>
                ))}
              </select>
              <select
                className={selectCls}
                value={row.repo}
                aria-label={`symbol ${i} repo`}
                onChange={(e) => {
                  const symbols = [...draft.symbols];
                  symbols[i] = {
                    ...row,
                    repo: e.target.value as ImpactEvidenceRepo,
                  };
                  patch({ symbols });
                }}
              >
                {REPOS.map((r) => (
                  <option key={r}>{r}</option>
                ))}
              </select>
              <input
                className={inputCls}
                placeholder="src/file.py"
                value={row.file}
                aria-label={`symbol ${i} file`}
                onChange={(e) => {
                  const symbols = [...draft.symbols];
                  symbols[i] = { ...row, file: e.target.value };
                  patch({ symbols });
                }}
              />
              <RemoveButton
                label={`remove symbol ${i}`}
                disabled={disabled}
                onClick={() =>
                  patch({
                    symbols: draft.symbols.filter((_, j) => j !== i),
                  })
                }
              />
            </div>
          ))}
        </div>

        <div className="space-y-1.5">
          <SectionHeader
            title="Surfaces"
            testId="impact-add-surface"
            disabled={disabled}
            onAdd={() =>
              patch({
                surfaces: [
                  ...draft.surfaces,
                  { kind: 'rest_route', identifier: '' },
                ],
              })
            }
          />
          {draft.surfaces.map((row, i) => (
            <div
              key={`surface-${i}`}
              className="flex items-center gap-1.5"
              data-testid={`impact-surface-row-${i}`}
            >
              <select
                className={selectCls}
                value={row.kind}
                aria-label={`surface ${i} kind`}
                onChange={(e) => {
                  const surfaces = [...draft.surfaces];
                  surfaces[i] = {
                    ...row,
                    kind: e.target.value as ImpactEvidenceSurfaceKind,
                  };
                  patch({ surfaces });
                }}
              >
                {SURFACE_KINDS.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
              <input
                className={inputCls}
                placeholder="identifier"
                value={row.identifier}
                aria-label={`surface ${i} identifier`}
                onChange={(e) => {
                  const surfaces = [...draft.surfaces];
                  surfaces[i] = { ...row, identifier: e.target.value };
                  patch({ surfaces });
                }}
              />
              <RemoveButton
                label={`remove surface ${i}`}
                disabled={disabled}
                onClick={() =>
                  patch({
                    surfaces: draft.surfaces.filter((_, j) => j !== i),
                  })
                }
              />
            </div>
          ))}
        </div>

        <div className="space-y-1.5">
          <SectionHeader
            title="Tests"
            testId="impact-add-test"
            disabled={disabled}
            onAdd={() =>
              patch({
                tests: [
                  ...draft.tests,
                  {
                    action: 'added',
                    repo: nextImpactRepo(draft.tests),
                    test_file_path: '',
                    test_function: '',
                    scenario_id: '',
                  },
                ],
              })
            }
          />
          {draft.tests.map((row, i) => (
            <div
              key={`test-${i}`}
              className="flex items-center gap-1.5"
              data-testid={`impact-test-row-${i}`}
            >
              <select
                className={selectCls}
                value={row.action}
                aria-label={`test ${i} action`}
                onChange={(e) => {
                  const tests = [...draft.tests];
                  tests[i] = {
                    ...row,
                    action: e.target.value as ImpactEvidenceTestAction,
                  };
                  patch({ tests });
                }}
              >
                {TEST_ACTIONS.map((a) => (
                  <option key={a}>{a}</option>
                ))}
              </select>
              <select
                className={selectCls}
                value={row.repo}
                aria-label={`test ${i} repo`}
                onChange={(e) => {
                  const tests = [...draft.tests];
                  tests[i] = {
                    ...row,
                    repo: e.target.value as ImpactEvidenceRepo,
                  };
                  patch({ tests });
                }}
              >
                {REPOS.map((r) => (
                  <option key={r}>{r}</option>
                ))}
              </select>
              <input
                className={inputCls}
                placeholder="tests/test_x.py"
                value={row.test_file_path}
                aria-label={`test ${i} file path`}
                onChange={(e) => {
                  const tests = [...draft.tests];
                  tests[i] = { ...row, test_file_path: e.target.value };
                  patch({ tests });
                }}
              />
              <input
                className={inputCls}
                placeholder="test_function (optional)"
                value={row.test_function}
                aria-label={`test ${i} function`}
                onChange={(e) => {
                  const tests = [...draft.tests];
                  tests[i] = { ...row, test_function: e.target.value };
                  patch({ tests });
                }}
              />
              <input
                className={inputCls}
                placeholder="ts_<id>"
                value={row.scenario_id}
                aria-label={`test ${i} scenario id`}
                onChange={(e) => {
                  const tests = [...draft.tests];
                  tests[i] = { ...row, scenario_id: e.target.value };
                  patch({ tests });
                }}
              />
              <RemoveButton
                label={`remove test ${i}`}
                disabled={disabled}
                onClick={() =>
                  patch({ tests: draft.tests.filter((_, j) => j !== i) })
                }
              />
            </div>
          ))}
        </div>

        <div className="space-y-1.5">
          <SectionHeader
            title="Evidence refs"
            testId="impact-add-evidence-ref"
            disabled={disabled}
            onAdd={() =>
              patch({ evidence_refs: [...draft.evidence_refs, ''] })
            }
          />
          {draft.evidence_refs.map((ref, i) => (
            <div
              key={`ref-${i}`}
              className="flex items-center gap-1.5"
              data-testid={`impact-evidence-ref-row-${i}`}
            >
              <input
                className={inputCls}
                placeholder="ts_<id> | tests/test_x.py::test_y"
                value={ref}
                aria-label={`evidence ref ${i}`}
                onChange={(e) => {
                  const evidence_refs = [...draft.evidence_refs];
                  evidence_refs[i] = e.target.value;
                  patch({ evidence_refs });
                }}
              />
              <RemoveButton
                label={`remove evidence ref ${i}`}
                disabled={disabled}
                onClick={() =>
                  patch({
                    evidence_refs: draft.evidence_refs.filter(
                      (_, j) => j !== i,
                    ),
                  })
                }
              />
            </div>
          ))}
        </div>
      </fieldset>
    </details>
  );
}
