import type { Spec, TestScenario, TestScenarioWrite } from '@/types';
import {
  SCENARIO_TYPES,
  isSupportedScenarioType,
} from './ScenarioTypeBadge';

export function prepareTestScenariosForWrite(
  scenarios: readonly TestScenario[],
  persistedScenarios: readonly TestScenario[],
): TestScenarioWrite[] {
  const persistedIds = new Set(persistedScenarios.map((scenario) => scenario.id));
  return scenarios.map(({ scenario_type: scenarioType, ...scenario }) => {
    if (isSupportedScenarioType(scenarioType)) {
      return { ...scenario, scenario_type: scenarioType };
    }
    if (!persistedIds.has(scenario.id)) {
      throw new Error(
        `Invalid scenario_type ${String(scenarioType)} for new scenario ${scenario.id}. `
        + `Choose one of: ${SCENARIO_TYPES.join(', ')}.`,
      );
    }
    return scenario;
  });
}

type ScenarioSpecUpdater = (
  specId: string,
  data: { test_scenarios: TestScenarioWrite[] },
) => Promise<Spec>;

export async function persistTestScenariosWithWriteGuard(
  updateSpec: ScenarioSpecUpdater,
  specId: string,
  persistedScenarios: readonly TestScenario[],
  scenarios: readonly TestScenario[],
): Promise<Spec> {
  const testScenarios = prepareTestScenariosForWrite(
    scenarios,
    persistedScenarios,
  );
  return updateSpec(specId, { test_scenarios: testScenarios });
}
