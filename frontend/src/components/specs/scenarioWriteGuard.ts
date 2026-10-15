import type { Spec, TestScenario, TestScenarioWrite } from '@/types';
import {
  SCENARIO_TYPES,
  isSupportedScenarioType,
} from './ScenarioTypeBadge';

export function prepareTestScenariosForWrite(
  scenarios: readonly TestScenario[],
): TestScenarioWrite[] {
  return scenarios.map(({ scenario_type: scenarioType, ...scenario }) => {
    if (isSupportedScenarioType(scenarioType)) {
      return { ...scenario, scenario_type: scenarioType };
    }
    throw new Error(
        `Invalid scenario_type ${String(scenarioType)} for scenario ${scenario.id}. `
        + `Choose one of: ${SCENARIO_TYPES.join(', ')}.`,
      );
  });
}

type ScenarioSpecUpdater = (
  specId: string,
  data: { test_scenarios: TestScenarioWrite[]; expected_spec_version: number },
) => Promise<Spec>;

export async function persistTestScenariosWithWriteGuard(
  updateSpec: ScenarioSpecUpdater,
  specId: string,
  scenarios: readonly TestScenario[],
  expectedSpecVersion: number,
): Promise<Spec> {
  const testScenarios = prepareTestScenariosForWrite(
    scenarios,
  );
  return updateSpec(specId, { test_scenarios: testScenarios, expected_spec_version: expectedSpecVersion });
}
