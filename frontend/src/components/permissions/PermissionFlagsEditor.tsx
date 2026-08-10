/**
 * PermissionFlagsEditor — Reusable component for viewing/editing permission flags.
 *
 * Renders permission trees of arbitrary depth with toggle switches.
 * Supports read-only mode for built-in presets and editable mode for custom.
 * Counters update in real-time on toggle.
 */

import { useState } from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';
import type { PermissionFlags, PermissionFlagTree } from '@/types';

const ENTITY_LABELS: Record<string, string> = {
  board: 'Board',
  story: 'Stories',
  topic: 'Topics',
  spec: 'Specs',
  card: 'Cards',
  ideation: 'Ideations',
  refinement: 'Refinements',
  sprint: 'Sprints',
  profile: 'Profile',
  guidelines: 'Guidelines',
  kg: 'Knowledge Graphs',
  code_traceability: 'Code Traceability',
};

const ENTITY_COLORS: Record<string, string> = {
  board: 'text-blue-600 dark:text-blue-400',
  story: 'text-sky-600 dark:text-sky-300',
  topic: 'text-teal-600 dark:text-teal-300',
  spec: 'text-violet-600 dark:text-violet-400',
  card: 'text-green-600 dark:text-green-400',
  ideation: 'text-amber-600 dark:text-amber-400',
  refinement: 'text-cyan-600 dark:text-cyan-400',
  sprint: 'text-orange-600 dark:text-orange-400',
  profile: 'text-gray-600 dark:text-gray-400',
  guidelines: 'text-pink-600 dark:text-pink-400',
  kg: 'text-indigo-600 dark:text-indigo-400',
  code_traceability: 'text-cyan-700 dark:text-cyan-300',
};

type FlagValue = boolean;
// The canonical registry may introduce groups at any depth (for example
// spec.structured_entity.<type>.<action>). Keep this shape recursive so the
// frontend never needs its own depth-specific permission catalog.
export type FlagTree = PermissionFlagTree;
export type FlagsMap = PermissionFlags;

function isFlagTree(value: unknown): value is FlagTree {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Count total and enabled flags in a nested structure */
function countFlags(obj: FlagTree): { total: number; enabled: number } {
  let total = 0;
  let enabled = 0;
  for (const val of Object.values(obj)) {
    if (typeof val === 'boolean') {
      total++;
      if (val) enabled++;
    } else if (isFlagTree(val)) {
      const nested = countFlags(val);
      total += nested.total;
      enabled += nested.enabled;
    }
  }
  return { total, enabled };
}

function replaceAtPath(
  tree: FlagTree,
  path: string[],
  replace: (value: FlagValue | FlagTree) => FlagValue | FlagTree,
): FlagTree {
  const [key, ...rest] = path;
  if (!key || !(key in tree)) return tree;

  const current = tree[key];
  let next: FlagValue | FlagTree;
  if (rest.length === 0) {
    next = replace(current);
  } else if (isFlagTree(current)) {
    next = replaceAtPath(current, rest, replace);
  } else {
    return tree;
  }

  return next === current ? tree : { ...tree, [key]: next };
}

function setFlagsInTree(tree: FlagTree, value: boolean): FlagTree {
  const updated: FlagTree = {};
  for (const [key, current] of Object.entries(tree)) {
    updated[key] = typeof current === 'boolean'
      ? value
      : setFlagsInTree(current, value);
  }
  return updated;
}

function countBadgeColor(enabled: number, total: number): string {
  if (enabled === total) return 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300';
  if (enabled === 0) return 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';
  return 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300';
}

interface PermissionFlagsEditorProps {
  flags: FlagsMap;
  onChange?: (flags: FlagsMap) => void;
  readOnly?: boolean;
}

export function PermissionFlagsEditor({ flags, onChange, readOnly = false }: PermissionFlagsEditorProps) {
  const [expandedEntity, setExpandedEntity] = useState<string | null>(null);
  const entities = Object.keys(flags);

  // Global counts
  let globalTotal = 0;
  let globalEnabled = 0;
  for (const entity of entities) {
    const c = countFlags(flags[entity]);
    globalTotal += c.total;
    globalEnabled += c.enabled;
  }

  const toggleFlag = (path: string[]) => {
    if (readOnly || !onChange) return;
    const updated = replaceAtPath(flags, path, (current) => (
      typeof current === 'boolean' ? !current : current
    ));
    onChange(updated as FlagsMap);
  };

  const setSubtreeAll = (path: string[], value: boolean) => {
    if (readOnly || !onChange) return;
    const updated = replaceAtPath(flags, path, (current) => (
      isFlagTree(current) ? setFlagsInTree(current, value) : current
    ));
    onChange(updated as FlagsMap);
  };

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      {entities.map((entity) => {
        const entityData = flags[entity];
        const isExpanded = expandedEntity === entity;
        const { total, enabled } = countFlags(entityData);

        return (
          <div key={entity} className="border-b last:border-b-0 border-gray-200 dark:border-gray-700">
            <button
              type="button"
              aria-label={`Edit ${ENTITY_LABELS[entity] || entity} permissions`}
              aria-expanded={isExpanded}
              onClick={() => setExpandedEntity(isExpanded ? null : entity)}
              className="w-full flex items-center justify-between px-4 py-2.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-700/30"
            >
              <div className="flex items-center gap-2">
                {isExpanded ? <ChevronDown size={14} className="text-gray-400" /> : <ChevronRight size={14} className="text-gray-400" />}
                <span className={`font-medium ${ENTITY_COLORS[entity] || 'text-gray-600'}`}>
                  {ENTITY_LABELS[entity] || entity}
                </span>
              </div>
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${countBadgeColor(enabled, total)}`}>
                {enabled}/{total}
              </span>
            </button>
            {isExpanded && (
              <div className="px-4 pb-3">
                <PermissionTreeRows
                  tree={entityData}
                  path={[entity]}
                  onToggle={toggleFlag}
                  onSetSubtree={setSubtreeAll}
                  readOnly={readOnly}
                />
              </div>
            )}
          </div>
        );
      })}

      {/* Global footer */}
      <div className="px-4 py-2 bg-gray-50 dark:bg-gray-700/20 border-t border-gray-200 dark:border-gray-700">
        <span className="text-[10px] text-gray-400">{globalEnabled} of {globalTotal} flags enabled</span>
      </div>
    </div>
  );
}

function PermissionTreeRows({
  tree,
  path,
  onToggle,
  onSetSubtree,
  readOnly,
  depth = 0,
}: {
  tree: FlagTree;
  path: string[];
  onToggle: (path: string[]) => void;
  onSetSubtree: (path: string[], value: boolean) => void;
  readOnly: boolean;
  depth?: number;
}) {
  const leaves = Object.entries(tree).filter((entry): entry is [string, boolean] => (
    typeof entry[1] === 'boolean'
  ));
  const groups = Object.entries(tree).filter((entry): entry is [string, FlagTree] => (
    isFlagTree(entry[1])
  ));

  return (
    <div className="space-y-2">
      {leaves.length > 0 && (
        <div className="space-y-1">
          {leaves.map(([key, enabled]) => {
            const flagPath = [...path, key];
            return (
              <div
                key={key}
                className="flex items-center justify-between py-0.5"
                style={{ paddingLeft: `${(depth + 1) * 1.5}rem` }}
              >
                <span className="text-xs text-gray-700 dark:text-gray-300">{key}</span>
                <ToggleSwitch
                  enabled={enabled}
                  label={flagPath.join('.')}
                  onToggle={() => onToggle(flagPath)}
                  readOnly={readOnly}
                />
              </div>
            );
          })}
        </div>
      )}

      {groups.map(([key, subtree]) => {
        const groupPath = [...path, key];
        const fullPath = groupPath.join('.');
        const { total, enabled } = countFlags(subtree);
        return (
          <div key={key} style={{ marginLeft: `${depth}rem` }}>
            <div className="flex items-center justify-between py-1.5 px-2 rounded bg-gray-50 dark:bg-gray-700/30">
              <span className="text-[10px] font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">{key}</span>
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-gray-400">{enabled}/{total}</span>
                {!readOnly && (
                  <>
                    <button
                      type="button"
                      aria-label={`Turn all on in ${fullPath}`}
                      onClick={() => onSetSubtree(groupPath, true)}
                      className="text-[10px] text-blue-500 hover:text-blue-600"
                    >
                      all on
                    </button>
                    <button
                      type="button"
                      aria-label={`Turn all off in ${fullPath}`}
                      onClick={() => onSetSubtree(groupPath, false)}
                      className="text-[10px] text-red-400 hover:text-red-500"
                    >
                      all off
                    </button>
                  </>
                )}
              </div>
            </div>
            <div className="mt-0.5">
              <PermissionTreeRows
                tree={subtree}
                path={groupPath}
                onToggle={onToggle}
                onSetSubtree={onSetSubtree}
                readOnly={readOnly}
                depth={depth + 1}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** Simple toggle switch */
function ToggleSwitch({
  enabled,
  label,
  onToggle,
  readOnly,
}: {
  enabled: boolean;
  label: string;
  onToggle: () => void;
  readOnly?: boolean;
}) {
  return (
    <button
      type="button"
      aria-label={`Toggle ${label}`}
      aria-pressed={enabled}
      onClick={readOnly ? undefined : onToggle}
      className={`relative w-8 h-4 rounded-full transition-colors shrink-0 ${
        enabled ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'
      } ${readOnly ? 'opacity-50 cursor-default' : 'cursor-pointer'}`}
      disabled={readOnly}
    >
      <span
        className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-transform ${
          enabled ? 'right-0.5' : 'left-0.5'
        }`}
      />
    </button>
  );
}

/** Utility: count total enabled/total in a FlagsMap */
export function countAllFlags(flags: FlagsMap): { total: number; enabled: number } {
  let total = 0;
  let enabled = 0;
  for (const entity of Object.values(flags)) {
    const c = countFlags(entity);
    total += c.total;
    enabled += c.enabled;
  }
  return { total, enabled };
}

/** Utility: count per-entity */
export function countPerEntity(flags: FlagsMap): Record<string, { total: number; enabled: number }> {
  const result: Record<string, { total: number; enabled: number }> = {};
  for (const [entity, data] of Object.entries(flags)) {
    result[entity] = countFlags(data);
  }
  return result;
}

/** Utility: set all flags to a value */
export function setAllFlags(flags: FlagsMap, value: boolean): FlagsMap {
  return Object.fromEntries(
    Object.entries(flags).map(([entity, tree]) => [entity, setFlagsInTree(tree, value)]),
  );
}

export { ENTITY_LABELS, ENTITY_COLORS, countFlags, countBadgeColor };
