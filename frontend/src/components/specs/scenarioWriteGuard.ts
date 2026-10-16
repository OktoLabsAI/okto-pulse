import type { Spec, TestScenario, TestScenarioWrite } from '@/types';
import {
  SCENARIO_TYPES,
  isSupportedScenarioType,
} from './ScenarioTypeBadge';

export type ScenarioWriteAttempt = {
  next: TestScenario[];
  expectedVersion: number;
  expectedSnapshot: string;
  kind: 'create' | 'remove';
  scenarioId: string;
};

export class ScenarioAuthoringInputError extends Error {}

/** Observe the requested state; this does not claim authorship or replay a write. */
export function inspectScenarioWrite(attempt: ScenarioWriteAttempt, current: Spec): 'observed' | 'unchanged' | 'changed' {
  const found = current.test_scenarios?.find(item => item.id === attempt.scenarioId);
  const expected = attempt.next.find(item => item.id === attempt.scenarioId);
  const fields = ['id', 'title', 'scenario_type', 'verification_method', 'given', 'when', 'then',
    'notes', 'status', 'linked_criteria', 'linked_task_ids', 'evidence'] as const;
  if (attempt.kind === 'remove' ? !found : found && expected && fields.every(
    field => JSON.stringify(found[field] ?? null) === JSON.stringify(expected[field] ?? null),
  )) return 'observed';
  return current.version === attempt.expectedVersion && current.scenario_snapshot === attempt.expectedSnapshot
    ? 'unchanged' : 'changed';
}

export function prepareTestScenariosForWrite(
  scenarios: readonly TestScenario[],
): TestScenarioWrite[] {
  return scenarios.map(({ scenario_type: scenarioType, ...scenario }) => {
    if (isSupportedScenarioType(scenarioType)) {
      return { ...scenario, scenario_type: scenarioType };
    }
    throw new ScenarioAuthoringInputError(
        `Invalid scenario_type ${String(scenarioType)} for scenario ${scenario.id}. `
        + `Choose one of: ${SCENARIO_TYPES.join(', ')}.`,
      );
  });
}

type ScenarioSpecUpdater = (
  specId: string,
  data: { test_scenarios: TestScenarioWrite[]; expected_spec_version: number; expected_scenario_snapshot: string },
) => Promise<Spec>;

export async function persistTestScenariosWithWriteGuard(
  updateSpec: ScenarioSpecUpdater,
  specId: string,
  scenarios: readonly TestScenario[],
  expectedSpecVersion: number,
  expectedScenarioSnapshot: string,
): Promise<Spec> {
  const testScenarios = prepareTestScenariosForWrite(
    scenarios,
  );
  if (!/^[0-9a-f]{64}$/.test(expectedScenarioSnapshot)) {
    throw new ScenarioAuthoringInputError('Reload scenarios before saving: the authoring snapshot is unavailable.');
  }
  return updateSpec(specId, { test_scenarios: testScenarios, expected_spec_version: expectedSpecVersion,
    expected_scenario_snapshot: expectedScenarioSnapshot });
}
