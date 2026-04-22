/**
 * PermissionDiffView — Shows diff between base agent permissions and effective board permissions.
 */

import { countPerEntity, countAllFlags, ENTITY_LABELS, ENTITY_COLORS } from './PermissionFlagsEditor';
import type { FlagsMap } from './PermissionFlagsEditor';

interface PermissionDiffViewProps {
  baseFlags: FlagsMap;
  effectiveFlags: FlagsMap;
}

export function PermissionDiffView({ baseFlags, effectiveFlags }: PermissionDiffViewProps) {
  const baseCounts = countPerEntity(baseFlags);
  const effectiveCounts = countPerEntity(effectiveFlags);
  const baseTotal = countAllFlags(baseFlags);
  const effectiveTotal = countAllFlags(effectiveFlags);
  const restricted = baseTotal.enabled - effectiveTotal.enabled;
  const pct = baseTotal.total > 0 ? Math.round((effectiveTotal.enabled / baseTotal.total) * 100) : 0;

  return (
    <div className="space-y-2">
      {/* Summary bar */}
      <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-3">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-gray-600 dark:text-gray-300">Effective Permissions</span>
          <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium ${
            restricted === 0
              ? 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300'
              : 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300'
          }`}>
            {effectiveTotal.enabled}/{baseTotal.total}
          </span>
        </div>
        <div className="h-2 bg-gray-100 dark:bg-gray-700 rounded-full overflow-hidden mb-2">
          <div
            className={`h-full rounded-full transition-all duration-500 ${restricted === 0 ? 'bg-green-500' : 'bg-amber-500'}`}
            style={{ width: `${pct}%` }}
          />
        </div>
        {restricted > 0 && (
          <p className="text-[10px] text-gray-400">{restricted} flag{restricted !== 1 ? 's' : ''} restricted by board override</p>
        )}
      </div>

      {/* Per-entity diff rows */}
      <div className="space-y-1">
        {Object.keys(baseCounts).map((entity) => {
          const base = baseCounts[entity];
          const eff = effectiveCounts[entity] || { total: base.total, enabled: 0 };
          const diff = base.enabled - eff.enabled;
          const noChange = diff === 0;

          return (
            <div key={entity} className="flex items-center justify-between px-2 py-1.5 rounded hover:bg-white dark:hover:bg-gray-700/30">
              <span className={`text-xs font-medium ${ENTITY_COLORS[entity] || 'text-gray-600'}`}>
                {ENTITY_LABELS[entity] || entity}
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
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-100 text-red-600 dark:bg-red-900/40 dark:text-red-300">
                      {eff.enabled}/{eff.total}
                    </span>
                    <span className="text-[10px] text-red-500 font-medium w-20 text-right">-{diff} restricted</span>
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
