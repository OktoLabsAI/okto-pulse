import { describe, expect, it } from 'vitest';

import {
  POLICY_CLASS_BEHAVIOR_NOTE,
  POLICY_CLASS_OPTIONS,
  POLICY_ENTITY_TYPES,
  POLICY_FACT_CATALOG,
  POLICY_FACT_KIND_LABELS,
  POLICY_PREDICATE_CATALOG_VERSION,
  canonicalRuleSet,
  canonicalTags,
  factOptionsForTargets,
  guidelineRuleToDraft,
  isProtectedPolicyClass,
  newRuleDraft,
  operatorsForFact,
  policyClassDescription,
  ruleDraftToInput,
  suggestRuleKey,
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

  it('makes new rules advisory and rejects duplicate IDs or keys in one revision', () => {
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
    expect(validateRuleDrafts([first, second])).toMatch(/keys must be unique/i);
  });

  it('suggests stable rule keys from human titles', () => {
    expect(suggestRuleKey('Require evidência before release')).toBe(
      'require_evidencia_before_release',
    );
    expect(suggestRuleKey('123 checks')).toBe('rule_123_checks');
    expect(suggestRuleKey('   ')).toBe('');
  });

  it('recognizes protected classes and preserves unknown legacy classes', () => {
    expect(isProtectedPolicyClass('Permissions')).toBe(true);
    expect(isProtectedPolicyClass('standard')).toBe(false);
    expect(policyClassDescription('legacy_quality')).toMatch(
      /legacy\/custom/i,
    );

    const legacy = guidelineRuleToDraft({
      rule_id: 'legacy-rule',
      code: 'legacy_rule',
      title: 'Legacy rule',
      description: 'A rule authored before the closed class selector.',
      target_entity_types: ['spec'],
      predicates: [{
        predicate_code: 'exists',
        parameters: [['fact', 'status']],
      }],
      enforcement: 'advisory',
      operator: 'all',
      waivable: true,
      policy_class: 'legacy_quality',
    });
    expect(legacy.policyClass).toBe('legacy_quality');
    expect(ruleDraftToInput(legacy).policy_class).toBe('legacy_quality');
  });

  it('exposes reusable policy-class behavior and complete fact guidance', () => {
    expect(POLICY_CLASS_OPTIONS).toHaveLength(5);
    expect(POLICY_CLASS_BEHAVIOR_NOTE).toMatch(
      /Enforcement controls that/i,
    );
    expect(POLICY_CLASS_OPTIONS.find(
      (option) => option.value === 'coverage',
    )).toMatchObject({
      protected: true,
      waivability: 'Never waivable.',
    });
    expect(POLICY_CLASS_OPTIONS.every(
      (option) =>
        option.effect.length > 0
        && option.whenToUse.length > 0
        && option.waivability.length > 0,
    )).toBe(true);

    expect(POLICY_FACT_CATALOG).toHaveLength(23);
    expect(new Set(POLICY_FACT_CATALOG.map((fact) => fact.code)).size)
      .toBe(POLICY_FACT_CATALOG.length);
    expect(POLICY_FACT_CATALOG.every(
      (fact) =>
        fact.description.length > 0
        && fact.valueGuidance.length > 0
        && fact.example.length > 0
        && POLICY_FACT_KIND_LABELS[fact.kind].length > 0,
    )).toBe(true);
    expect(POLICY_FACT_CATALOG.find(
      (fact) => fact.code === 'ambiguity_score',
    )).toMatchObject({
      kind: 'number',
      minimum: 1,
      maximum: 5,
    });
  });

  it('requires target selection before facts are offered', () => {
    expect(factOptionsForTargets([])).toEqual([]);
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
