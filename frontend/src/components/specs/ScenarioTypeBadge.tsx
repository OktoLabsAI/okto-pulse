/**
 * Single source for the frontend's supported scenario_type runtime list and the
 * type badge (spec ac16b3c9, card bf52c32f). Mirrors the backend authoritative
 * enum VALID_SCENARIO_TYPES.
 *
 * A persisted value OUTSIDE the supported enum (historical/legacy data, e.g.
 * `regression`) is rendered EXPLICITLY as `<value> (unsupported)` with a warning
 * style and tooltip — it is never shown as a plain supported-looking label. This
 * surfaces stale data for deliberate remediation instead of hiding it (FR5/AC5);
 * new writes already fail closed on the backend (card 58844a26).
 */

export const SCENARIO_TYPES = ['unit', 'integration', 'e2e', 'manual'] as const;

const SCENARIO_TYPE_COLORS: Record<string, string> = {
  unit: 'bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-300',
  integration: 'bg-violet-50 text-violet-600 dark:bg-violet-900/30 dark:text-violet-300',
  e2e: 'bg-amber-50 text-amber-600 dark:bg-amber-900/30 dark:text-amber-300',
  manual: 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400',
};

const UNSUPPORTED_CLASS =
  'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300';

export function isSupportedScenarioType(value: string): boolean {
  return (SCENARIO_TYPES as readonly string[]).includes(value);
}

export function ScenarioTypeBadge({ scenarioType }: { scenarioType: string }) {
  const known = isSupportedScenarioType(scenarioType);
  return (
    <span
      data-testid="scenario-type-badge"
      data-unsupported={known ? undefined : 'true'}
      className={`text-[10px] px-1.5 py-0.5 rounded ${
        known ? SCENARIO_TYPE_COLORS[scenarioType] : UNSUPPORTED_CLASS
      }`}
      title={
        known
          ? undefined
          : `Unsupported scenario_type — supported: ${SCENARIO_TYPES.join(', ')}`
      }
    >
      {known ? scenarioType : `${scenarioType} (unsupported)`}
    </span>
  );
}
