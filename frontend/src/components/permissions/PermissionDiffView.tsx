/**
 * PermissionDiffView — Shows diff between base agent permissions and effective board permissions.
 */

import { countPerEntity, countAllFlags } from './PermissionFlagsEditor';
import { getEntityLabel, getEntityTextClasses } from './permissionLabels';
import type { FlagsMap } from './PermissionFlagsEditor';

interface PermissionDiffViewProps {
  baseFlags: FlagsMap;
  effectiveFlags: FlagsMap;
  baseLabel?: string;
  restrictionLabel?: string;
}

type FlatFlags = Map<string, boolean>;

function flattenFlags(
  value: unknown,
  prefix = '',
  target: FlatFlags = new Map(),
): FlatFlags {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return target;
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof child === 'boolean') target.set(path, child);
    else flattenFlags(child, path, target);
  }
  return target;
}

function entityChanges(
  entity: string,
  base: FlatFlags,
  effective: FlatFlags,
): { enabled: number; restricted: number } {
  const prefix = `${entity}.`;
  const paths = new Set([
    ...[...base.keys()].filter((path) => path.startsWith(prefix)),
    ...[...effective.keys()].filter((path) => path.startsWith(prefix)),
  ]);
  let enabled = 0;
  let restricted = 0;
  for (const path of paths) {
    const before = base.get(path) === true;
    const after = effective.get(path) === true;
    if (!before && after) enabled += 1;
    if (before && !after) restricted += 1;
  }
  return { enabled, restricted };
}

export function PermissionDiffView({
  baseFlags,
  effectiveFlags,
  baseLabel = 'Base permissions',
  restrictionLabel = 'board override',
}: PermissionDiffViewProps) {
  const baseCounts = countPerEntity(baseFlags);
  const effectiveCounts = countPerEntity(effectiveFlags);
  const baseFlat = flattenFlags(baseFlags);
  const effectiveFlat = flattenFlags(effectiveFlags);
  const paths = new Set([...baseFlat.keys(), ...effectiveFlat.keys()]);
  const restricted = [...paths].filter(
    (path) => baseFlat.get(path) === true && effectiveFlat.get(path) !== true,
  ).length;
  const enabled = [...paths].filter(
    (path) => baseFlat.get(path) !== true && effectiveFlat.get(path) === true,
  ).length;
  const changed = restricted + enabled;
  const effectiveTotal = countAllFlags(effectiveFlags);
  const pct = paths.size > 0
    ? Math.round((effectiveTotal.enabled / paths.size) * 100)
    : 0;
  const entities = Array.from(
    new Set([...Object.keys(baseCounts), ...Object.keys(effectiveCounts)]),
  );

  return (
    <div className="space-y-2">
      {/* Summary bar */}
      <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-3">
        <div className="flex items-start justify-between mb-2">
          <div>
            <span className="text-xs font-semibold text-gray-600 dark:text-gray-300">Effective Permissions</span>
            <p className="text-[10px] text-gray-400" data-testid="permission-diff-base">
              Base: {baseLabel}
            </p>
          </div>
          <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
            changed === 0
              ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
              : 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
          }`}>
            {effectiveTotal.enabled}/{paths.size}
          </span>
        </div>
        <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden mb-2">
          <div
            className={`h-full rounded-full transition-all duration-500 ${changed === 0 ? 'bg-green-500' : 'bg-amber-500'}`}
            style={{ width: `${pct}%` }}
          />
        </div>
        <p className="text-[10px] text-gray-400" data-testid="permission-diff-summary">
          {changed === 0
            ? 'No effective changes from the resolved base.'
            : [
                restricted > 0
                  ? `${restricted} flag${restricted !== 1 ? 's' : ''} restricted by ${restrictionLabel}`
                  : null,
                enabled > 0
                  ? `${enabled} flag${enabled !== 1 ? 's' : ''} enabled by direct customization`
                  : null,
              ].filter(Boolean).join(' · ')}
        </p>
      </div>

      {/* Per-entity diff rows */}
      <div className="space-y-1">
        {entities.map((entity) => {
          const base = baseCounts[entity] || { total: 0, enabled: 0 };
          const eff = effectiveCounts[entity] || { total: 0, enabled: 0 };
          const diff = entityChanges(entity, baseFlat, effectiveFlat);
          const noChange = diff.restricted === 0 && diff.enabled === 0;

          return (
            <div key={entity} className="flex items-center justify-between px-2 py-1.5 rounded hover:bg-white dark:hover:bg-gray-700/30">
              <span className={`text-xs font-medium ${getEntityTextClasses(entity)}`}>
                {getEntityLabel(entity)}
              </span>
              <div className="flex items-center gap-2">
                <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300">
                  {base.enabled}/{base.total}
                </span>
                {noChange ? (
                  <>
                    <span className="text-[10px] text-gray-300">=</span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300">
                      {eff.enabled}/{eff.total}
                    </span>
                    <span className="text-[10px] text-green-600 dark:text-green-400 w-20 text-right">no change</span>
                  </>
                ) : (
                  <>
                    <span className="text-[10px] text-gray-300">&rarr;</span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                      {eff.enabled}/{eff.total}
                    </span>
                    <span className="text-[10px] text-amber-600 dark:text-amber-400 font-medium w-24 text-right">
                      {diff.restricted > 0 ? `-${diff.restricted}` : ''}
                      {diff.restricted > 0 && diff.enabled > 0 ? ' / ' : ''}
                      {diff.enabled > 0 ? `+${diff.enabled}` : ''}
                      {' changed'}
                    </span>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
