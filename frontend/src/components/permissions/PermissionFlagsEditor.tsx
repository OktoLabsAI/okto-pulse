/**
 * PermissionFlagsEditor — Reusable component for viewing/editing permission flags.
 *
 * Renders hierarchical flags (entity → level → action) with toggle switches.
 * Supports read-only mode for built-in presets and editable mode for custom.
 * Counters update in real-time on toggle.
 */

import { useState } from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';

const ENTITY_LABELS: Record<string, string> = {
  board: 'Board',
  spec: 'Specs',
  card: 'Cards',
  ideation: 'Ideations',
  refinement: 'Refinements',
  sprint: 'Sprints',
  profile: 'Profile',
  guidelines: 'Guidelines',
  kg: 'Knowledge Graphs',
};

const ENTITY_COLORS: Record<string, string> = {
  board: 'text-blue-600 dark:text-blue-400',
  spec: 'text-violet-600 dark:text-violet-400',
  card: 'text-green-600 dark:text-green-400',
  ideation: 'text-amber-600 dark:text-amber-400',
  refinement: 'text-cyan-600 dark:text-cyan-400',
  sprint: 'text-orange-600 dark:text-orange-400',
  profile: 'text-gray-600 dark:text-gray-400',
  guidelines: 'text-pink-600 dark:text-pink-400',
  kg: 'text-indigo-600 dark:text-indigo-400',
};

type FlagValue = boolean;
type FlagLevel = Record<string, FlagValue>;
type FlagEntity = Record<string, FlagLevel | FlagValue>;
export type FlagsMap = Record<string, FlagEntity>;

/** Count total and enabled flags in a nested structure */
function countFlags(obj: FlagEntity): { total: number; enabled: number } {
  let total = 0;
  let enabled = 0;
  for (const val of Object.values(obj)) {
    if (typeof val === 'boolean') {
      total++;
      if (val) enabled++;
    } else if (typeof val === 'object' && val !== null) {
      for (const v of Object.values(val)) {
        if (typeof v === 'boolean') {
          total++;
          if (v) enabled++;
        }
      }
    }
  }
  return { total, enabled };
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

  const toggleFlag = (entity: string, level: string, action: string) => {
    if (readOnly || !onChange) return;
    const updated = JSON.parse(JSON.stringify(flags)) as FlagsMap;
    const target = updated[entity]?.[level];
    if (typeof target === 'object' && target !== null && action in target) {
      (target as FlagLevel)[action] = !(target as FlagLevel)[action];
    } else if (typeof updated[entity]?.[level] === 'boolean') {
      // flat flag at entity level (e.g. profile.update, guidelines.read)
      (updated[entity] as Record<string, FlagValue>)[level] = !(updated[entity] as Record<string, FlagValue>)[level];
    }
    onChange(updated);
  };

  const setLevelAll = (entity: string, level: string, value: boolean) => {
    if (readOnly || !onChange) return;
    const updated = JSON.parse(JSON.stringify(flags)) as FlagsMap;
    const target = updated[entity]?.[level];
    if (typeof target === 'object' && target !== null) {
      for (const key of Object.keys(target)) {
        (target as FlagLevel)[key] = value;
      }
    }
    onChange(updated);
  };

  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
      {entities.map((entity) => {
        const entityData = flags[entity];
        const isExpanded = expandedEntity === entity;
        const { total, enabled } = countFlags(entityData);

        // Separate flat flags (boolean at entity level) vs nested levels (objects)
        const flatFlags: [string, boolean][] = [];
        const nestedLevels: [string, Record<string, boolean>][] = [];
        for (const [key, val] of Object.entries(entityData)) {
          if (typeof val === 'boolean') {
            flatFlags.push([key, val]);
          } else if (typeof val === 'object' && val !== null) {
            nestedLevels.push([key, val as Record<string, boolean>]);
          }
        }

        return (
          <div key={entity} className="border-b last:border-b-0 border-gray-200 dark:border-gray-700">
            <button
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
              <div className="px-4 pb-3 space-y-2">
                {/* Flat flags (e.g. profile.update, guidelines.read) */}
                {flatFlags.length > 0 && (
                  <div className="space-y-1">
                    {flatFlags.map(([key, val]) => (
                      <div key={key} className="flex items-center justify-between py-0.5 pl-6">
                        <span className="text-xs text-gray-700 dark:text-gray-300">{key}</span>
                        <ToggleSwitch enabled={val} onToggle={() => toggleFlag(entity, key, '')} readOnly={readOnly} />
                      </div>
                    ))}
                  </div>
                )}

                {/* Nested levels (entity, move, interact_in, qa, etc.) */}
                {nestedLevels.map(([level, actions]) => {
                  const levelEnabled = Object.values(actions).filter(Boolean).length;
                  const levelTotal = Object.keys(actions).length;
                  return (
                    <div key={level}>
                      <div className="flex items-center justify-between py-1.5 px-2 rounded bg-gray-50 dark:bg-gray-700/30">
                        <span className="text-[10px] font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">{level}</span>
                        <div className="flex items-center gap-2">
                          <span className="text-[10px] text-gray-400">{levelEnabled}/{levelTotal}</span>
                          {!readOnly && (
                            <>
                              <button onClick={() => setLevelAll(entity, level, true)} className="text-[10px] text-blue-500 hover:text-blue-600">all on</button>
                              <button onClick={() => setLevelAll(entity, level, false)} className="text-[10px] text-red-400 hover:text-red-500">all off</button>
                            </>
                          )}
                        </div>
                      </div>
                      <div className="pl-4 space-y-0.5 mt-0.5">
                        {Object.entries(actions).map(([action, val]) => (
                          <div key={action} className="flex items-center justify-between py-0.5">
                            <span className="text-xs text-gray-700 dark:text-gray-300">{action}</span>
                            <ToggleSwitch enabled={val} onToggle={() => toggleFlag(entity, level, action)} readOnly={readOnly} />
                          </div>
                        ))}
                      </div>
                    </div>
                  );
                })}
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

/** Simple toggle switch */
function ToggleSwitch({ enabled, onToggle, readOnly }: { enabled: boolean; onToggle: () => void; readOnly?: boolean }) {
  return (
    <button
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
  const updated = JSON.parse(JSON.stringify(flags)) as FlagsMap;
  for (const entity of Object.values(updated)) {
    for (const [key, val] of Object.entries(entity)) {
      if (typeof val === 'boolean') {
        (entity as Record<string, boolean>)[key] = value;
      } else if (typeof val === 'object' && val !== null) {
        for (const k of Object.keys(val)) {
          (val as Record<string, boolean>)[k] = value;
        }
      }
    }
  }
  return updated;
}

export { ENTITY_LABELS, ENTITY_COLORS, countFlags, countBadgeColor };
