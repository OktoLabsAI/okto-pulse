import { describe, expect, it } from 'vitest';

import {
  POLICY_ENTITY_TYPES,
  POLICY_PREDICATE_CATALOG_VERSION,
  canonicalRuleSet,
  canonicalTags,
  factOptionsForTargets,
  newRuleDraft,
  operatorsForFact,
  ruleDraftToInput,
  validateRuleDraft,
  validateRuleDrafts,
  type GuidelineRuleDraft,
} from '../policyEditorModel';

function validRule(): GuidelineRuleDraft {
  return {
    ...newRuleDraft(),
    ruleId: 'rule-1',
    code: 'require_coverage',
    title: 'Require coverage',
    description: 'Coverage must be present.',
    targetEntityTypes: ['spec'],
    predicates: [{
      localId: 'predicate-1',
      fact: 'coverage_percent',
      operator: 'gte',
      rawValue: '80',
    }],
  };
}

describe('policyEditorModel policy/v1 closed authoring', () => {
  it('pins the UI to policy/v1 and the six closed executable targets', () => {
    expect(POLICY_PREDICATE_CATALOG_VERSION).toBe('policy/v1');
    expect(POLICY_ENTITY_TYPES).toEqual([
      'ideation',
      'refinement',
      'spec',
      'sprint',
      'card',
      'test_scenario',
    ]);
  });

  it('intersects target-specific status values for multi-target rules', () => {
    const status = factOptionsForTargets(['ideation', 'refinement'])
      .find((fact) => fact.code === 'status');
    expect(status?.allowedValues).toEqual([
      'draft',
      'review',
      'approved',
      'done',
      'cancelled',
    ]);
    expect(
      factOptionsForTargets(['ideation', 'spec'])
        .some((fact) => fact.code === 'ambiguity_score'),
    ).toBe(false);
  });

  it('exposes only operator families compatible with each fact type', () => {
    const cardFacts = factOptionsForTargets(['card']);
    const labels = cardFacts.find((fact) => fact.code === 'labels');
    const priority = cardFacts.find((fact) => fact.code === 'priority');
    const dependencies = cardFacts.find(
      (fact) => fact.code === 'dependency_open_count',
    );
    expect(labels && operatorsForFact(labels)).toEqual([
      'exists',
      'not_exists',
      'count_eq',
      'count_ne',
      'count_gt',
      'count_gte',
      'count_lt',
      'count_lte',
      'contains',
      'not_contains',
    ]);
    expect(priority && operatorsForFact(priority)).not.toContain('gt');
    expect(dependencies && operatorsForFact(dependencies)).toContain('gte');
  });

  it('enforces enum values, integer/range bounds, and non-negative count parameters', () => {
    const enumRule = validRule();
    enumRule.targetEntityTypes = ['card'];
    enumRule.predicates = [{
      localId: 'predicate-priority',
      fact: 'priority',
      operator: 'eq',
      rawValue: 'urgent',
    }];
    expect(validateRuleDraft(enumRule)).toMatch(/allowed value/i);

    const rangeRule = validRule();
    rangeRule.predicates[0].rawValue = '101';
    expect(validateRuleDraft(rangeRule)).toMatch(/at most 100/i);

    const countRule = validRule();
    countRule.targetEntityTypes = ['card'];
    countRule.predicates = [{
      localId: 'predicate-count',
      fact: 'labels',
      operator: 'count_gte',
      rawValue: '-1',
    }];
    expect(validateRuleDraft(countRule)).toMatch(/non-negative integer/i);
    countRule.predicates[0].rawValue = '1.5';
    expect(validateRuleDraft(countRule)).toMatch(/non-negative integer/i);
  });

  it('makes new rules advisory and rejects duplicate IDs or codes in one revision', () => {
    expect(newRuleDraft().enforcement).toBe('advisory');
    const first = validRule();
    const second = {
      ...validRule(),
      localId: 'rule-local-2',
      code: 'another_code',
    };
    expect(validateRuleDrafts([first, second])).toMatch(/IDs must be unique/i);
    second.ruleId = 'rule-2';
    second.code = first.code;
    expect(validateRuleDrafts([first, second])).toMatch(/codes must be unique/i);
  });

  it('canonicalizes tags, targets, membership values, predicates, and rule order', () => {
    const first = validRule();
    first.targetEntityTypes = ['spec', 'card'];
    first.predicates = [{
      localId: 'predicate-membership',
      fact: 'test_scenario_count',
      operator: 'in',
      rawValue: '2, 1, 2',
    }];
    const firstInput = ruleDraftToInput(first);
    const secondInput = {
      ...firstInput,
      rule_id: 'rule-2',
      code: 'z_rule',
    };
    expect(firstInput.predicates[0].parameters?.values).toEqual([1, 2]);
    expect(canonicalRuleSet([secondInput, firstInput])).toEqual(
      canonicalRuleSet([firstInput, secondInput]),
    );
    expect(canonicalTags(['beta', 'alpha', 'beta', ' '])).toEqual([
      'alpha',
      'beta',
    ]);
  });
});
