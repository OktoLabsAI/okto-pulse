import type {
  GuidelinePredicate,
  GuidelinePredicateInput,
  GuidelineRule,
  GuidelineRuleInput,
  PolicyEntityType,
  PolicyParameterValue,
  PolicyScalar,
} from '@/types/policy-governance';

export const POLICY_ENTITY_TYPES: readonly PolicyEntityType[] = [
  'ideation',
  'refinement',
  'spec',
  'sprint',
  'card',
  'test_scenario',
];

export const POLICY_PREDICATE_CATALOG_VERSION = 'policy/v1';

export type PolicyPredicateOperator =
  | 'exists'
  | 'not_exists'
  | 'eq'
  | 'ne'
  | 'in'
  | 'not_in'
  | 'gt'
  | 'gte'
  | 'lt'
  | 'lte'
  | 'count_eq'
  | 'count_ne'
  | 'count_gt'
  | 'count_gte'
  | 'count_lt'
  | 'count_lte'
  | 'contains'
  | 'not_contains';

type PolicyFactKind = 'boolean' | 'enum' | 'integer' | 'number' | 'string_set';

export interface PolicyFactOption {
  code: string;
  label: string;
  kind: PolicyFactKind;
  targets: readonly PolicyEntityType[] | 'all';
  allowedValues?: readonly string[];
  minimum?: number;
  maximum?: number;
}

const STATUS_VALUES: Readonly<Record<PolicyEntityType, readonly string[]>> = {
  ideation: ['draft', 'review', 'approved', 'evaluating', 'done', 'cancelled'],
  refinement: ['draft', 'review', 'approved', 'done', 'cancelled'],
  spec: [
    'draft',
    'review',
    'approved',
    'validated',
    'in_progress',
    'done',
    'cancelled',
  ],
  sprint: ['draft', 'active', 'review', 'closed', 'cancelled'],
  card: [
    'not_started',
    'started',
    'in_progress',
    'validation',
    'on_hold',
    'done',
    'cancelled',
  ],
  test_scenario: ['draft', 'ready', 'automated', 'passed', 'failed'],
};

const COMMON_FACTS: readonly PolicyFactOption[] = [
  { code: 'status', label: 'Status', kind: 'enum', targets: 'all' },
  { code: 'labels', label: 'Labels', kind: 'string_set', targets: 'all' },
  {
    code: 'resource_gate_ready',
    label: 'Resource gate ready',
    kind: 'boolean',
    targets: 'all',
  },
];

const TARGET_FACTS: readonly PolicyFactOption[] = [
  {
    code: 'complexity',
    label: 'Complexity',
    kind: 'enum',
    targets: ['ideation'],
    allowedValues: ['small', 'medium', 'large'],
  },
  {
    code: 'qa_open_count',
    label: 'Open Q&A count',
    kind: 'integer',
    targets: ['ideation'],
    minimum: 0,
  },
  {
    code: 'ambiguity_score',
    label: 'Ambiguity score',
    kind: 'number',
    targets: ['ideation', 'refinement'],
    minimum: 1,
    maximum: 5,
  },
  {
    code: 'research_open_count',
    label: 'Open research count',
    kind: 'integer',
    targets: ['refinement'],
    minimum: 0,
  },
  {
    code: 'research_resolved_count',
    label: 'Resolved research count',
    kind: 'integer',
    targets: ['refinement'],
    minimum: 0,
  },
  {
    code: 'fr_count',
    label: 'Functional requirement count',
    kind: 'integer',
    targets: ['spec'],
    minimum: 0,
  },
  {
    code: 'ac_count',
    label: 'Acceptance criterion count',
    kind: 'integer',
    targets: ['spec'],
    minimum: 0,
  },
  {
    code: 'tr_count',
    label: 'Technical requirement count',
    kind: 'integer',
    targets: ['spec'],
    minimum: 0,
  },
  {
    code: 'coverage_percent',
    label: 'Coverage percent',
    kind: 'number',
    targets: ['spec'],
    minimum: 0,
    maximum: 100,
  },
  {
    code: 'validation_state',
    label: 'Validation state',
    kind: 'enum',
    targets: ['spec'],
  },
  {
    code: 'card_count',
    label: 'Card count',
    kind: 'integer',
    targets: ['sprint'],
    minimum: 0,
  },
  {
    code: 'open_card_count',
    label: 'Open card count',
    kind: 'integer',
    targets: ['sprint'],
    minimum: 0,
  },
  {
    code: 'passed_scenario_count',
    label: 'Passed scenario count',
    kind: 'integer',
    targets: ['sprint'],
    minimum: 0,
  },
  {
    code: 'card_type',
    label: 'Card type',
    kind: 'enum',
    targets: ['card'],
    allowedValues: ['normal', 'bug', 'test'],
  },
  {
    code: 'priority',
    label: 'Priority',
    kind: 'enum',
    targets: ['card'],
    allowedValues: ['critical', 'very_high', 'high', 'medium', 'low', 'none'],
  },
  {
    code: 'dependency_open_count',
    label: 'Open dependency count',
    kind: 'integer',
    targets: ['card'],
    minimum: 0,
  },
  {
    code: 'scenario_type',
    label: 'Scenario type',
    kind: 'enum',
    targets: ['test_scenario'],
    allowedValues: ['unit', 'integration', 'e2e', 'manual', 'negative'],
  },
  {
    code: 'linked_test_card_count',
    label: 'Linked test card count',
    kind: 'integer',
    targets: ['test_scenario'],
    minimum: 0,
  },
  {
    code: 'test_scenario_count',
    label: 'Test scenario count',
    kind: 'integer',
    targets: ['spec', 'sprint', 'card'],
    minimum: 0,
  },
  {
    code: 'evidence_count',
    label: 'Evidence count',
    kind: 'integer',
    targets: ['card', 'test_scenario'],
    minimum: 0,
  },
];

const PRESENCE_OPERATORS: readonly PolicyPredicateOperator[] = [
  'exists',
  'not_exists',
];
const EQUALITY_OPERATORS: readonly PolicyPredicateOperator[] = ['eq', 'ne'];
const MEMBERSHIP_OPERATORS: readonly PolicyPredicateOperator[] = ['in', 'not_in'];
const NUMERIC_OPERATORS: readonly PolicyPredicateOperator[] = [
  'gt',
  'gte',
  'lt',
  'lte',
];
const COUNT_OPERATORS: readonly PolicyPredicateOperator[] = [
  'count_eq',
  'count_ne',
  'count_gt',
  'count_gte',
  'count_lt',
  'count_lte',
];
const CONTAINS_OPERATORS: readonly PolicyPredicateOperator[] = [
  'contains',
  'not_contains',
];

export interface PolicyPredicateDraft {
  localId: string;
  fact: string;
  operator: PolicyPredicateOperator;
  rawValue: string;
}

export interface GuidelineRuleDraft {
  localId: string;
  ruleId: string;
  code: string;
  title: string;
  description: string;
  targetEntityTypes: PolicyEntityType[];
  predicates: PolicyPredicateDraft[];
  enforcement: 'advisory' | 'blocking';
  operator: 'all' | 'any';
  waivable: boolean;
  policyClass: string;
}

let fallbackId = 0;

export function createPolicyClientId(prefix: string): string {
  const randomUuid = globalThis.crypto?.randomUUID?.();
  if (randomUuid) return `${prefix}-${randomUuid}`;
  fallbackId += 1;
  return `${prefix}-${Date.now()}-${fallbackId}`;
}

export function factOptionsForTargets(
  targets: readonly PolicyEntityType[],
): PolicyFactOption[] {
  const selectedTargets =
    targets.length === 0 ? POLICY_ENTITY_TYPES : targets;
  return [...COMMON_FACTS, ...TARGET_FACTS].filter(
    (fact) =>
      fact.targets === 'all'
      || selectedTargets.every((target) => fact.targets.includes(target)),
  ).map((fact) => {
    if (fact.code !== 'status') return fact;
    const [firstTarget, ...remainingTargets] = selectedTargets;
    if (!firstTarget) return { ...fact, allowedValues: [] };
    const allowedValues = remainingTargets.reduce<string[]>(
      (current, target) =>
        current.filter((value) => STATUS_VALUES[target].includes(value)),
      [...STATUS_VALUES[firstTarget]],
    );
    return { ...fact, allowedValues };
  });
}

export function operatorsForFact(
  fact: PolicyFactOption,
): readonly PolicyPredicateOperator[] {
  if (fact.kind === 'boolean') {
    return [...PRESENCE_OPERATORS, ...EQUALITY_OPERATORS];
  }
  if (fact.kind === 'enum') {
    return [
      ...PRESENCE_OPERATORS,
      ...EQUALITY_OPERATORS,
      ...MEMBERSHIP_OPERATORS,
    ];
  }
  if (fact.kind === 'integer' || fact.kind === 'number') {
    return [
      ...PRESENCE_OPERATORS,
      ...EQUALITY_OPERATORS,
      ...MEMBERSHIP_OPERATORS,
      ...NUMERIC_OPERATORS,
    ];
  }
  return [
    ...PRESENCE_OPERATORS,
    ...COUNT_OPERATORS,
    ...CONTAINS_OPERATORS,
  ];
}

export function operatorNeedsValue(operator: PolicyPredicateOperator): boolean {
  return operator !== 'exists' && operator !== 'not_exists';
}

function factForRule(
  rule: GuidelineRuleDraft,
  factCode: string,
): PolicyFactOption | undefined {
  return factOptionsForTargets(rule.targetEntityTypes).find(
    (fact) => fact.code === factCode,
  );
}

function scalarValue(kind: PolicyFactKind, rawValue: string): PolicyScalar {
  const trimmed = rawValue.trim();
  if (kind === 'boolean') {
    if (trimmed !== 'true' && trimmed !== 'false') {
      throw new Error('policy_rule_boolean_value_invalid');
    }
    return trimmed === 'true';
  }
  if (kind === 'integer' || kind === 'number') {
    const value = Number(trimmed);
    if (!Number.isFinite(value)) {
      throw new Error('policy_rule_numeric_value_invalid');
    }
    if (kind === 'integer' && !Number.isInteger(value)) {
      throw new Error('policy_rule_integer_value_invalid');
    }
    return value;
  }
  return trimmed;
}

function predicateInput(
  rule: GuidelineRuleDraft,
  predicate: PolicyPredicateDraft,
): GuidelinePredicateInput {
  const fact = factForRule(rule, predicate.fact);
  if (!fact) throw new Error('policy_rule_fact_target_mismatch');

  const parameters: Record<string, PolicyParameterValue> = {
    fact: predicate.fact,
  };
  if (operatorNeedsValue(predicate.operator)) {
    if (predicate.operator === 'in' || predicate.operator === 'not_in') {
      const rawValues = predicate.rawValue.split(',').map((value) => value.trim());
      if (rawValues.some((value) => value === '')) {
        throw new Error('policy_rule_membership_value_invalid');
      }
      parameters.values = Array.from(
        new Set(
          rawValues.map((value) =>
            scalarValue(fact.kind, value),
          ),
        ),
      ).sort((left, right) =>
        JSON.stringify(left).localeCompare(JSON.stringify(right)),
      );
    } else if (predicate.operator.startsWith('count_')) {
      const value = Number(predicate.rawValue.trim());
      if (!Number.isInteger(value) || value < 0) {
        throw new Error('policy_rule_count_value_invalid');
      }
      parameters.value = value;
    } else {
      parameters.value = scalarValue(fact.kind, predicate.rawValue);
    }
  }
  return {
    predicate_code: predicate.operator,
    parameters,
  };
}

export function validateRuleDraft(rule: GuidelineRuleDraft): string | null {
  if (!rule.ruleId.trim()) return 'Rule ID is required.';
  if (rule.ruleId.trim().length > 64) return 'Rule ID cannot exceed 64 characters.';
  if (!rule.code.trim()) return 'Rule code is required.';
  if (rule.code.trim().length > 200) {
    return 'Rule code cannot exceed 200 characters.';
  }
  if (!/^[A-Za-z][A-Za-z0-9_.:-]*$/.test(rule.code.trim())) {
    return 'Rule code must start with a letter and use only letters, numbers, _, ., :, or -.';
  }
  if (!rule.title.trim()) return 'Rule title is required.';
  if (rule.title.trim().length > 500) {
    return 'Rule title cannot exceed 500 characters.';
  }
  if (!rule.description.trim()) return 'Rule description is required.';
  if (rule.targetEntityTypes.length === 0) {
    return 'Select at least one executable target.';
  }
  if (rule.predicates.length === 0) return 'Add at least one predicate.';
  if (!rule.policyClass.trim()) return 'Policy class is required.';
  if (rule.policyClass.trim().length > 200) {
    return 'Policy class cannot exceed 200 characters.';
  }
  if (
    ['coverage', 'permissions', 'reviewer_separation', 'lineage'].includes(
      rule.policyClass.trim().toLowerCase(),
    )
    && rule.waivable
  ) {
    return `${rule.policyClass.trim()} is a protected non-waivable policy class.`;
  }

  for (const predicate of rule.predicates) {
    const fact = factForRule(rule, predicate.fact);
    if (!fact) {
      return `Fact "${predicate.fact}" is not available for every selected target.`;
    }
    if (!operatorsForFact(fact).includes(predicate.operator)) {
      return `Operator "${predicate.operator}" is not valid for ${fact.label}.`;
    }
    if (
      operatorNeedsValue(predicate.operator)
      && predicate.rawValue.trim() === ''
    ) {
      return `A value is required for ${fact.label}.`;
    }
    const rawValues =
      predicate.operator === 'in' || predicate.operator === 'not_in'
        ? predicate.rawValue.split(',').map((value) => value.trim())
        : [predicate.rawValue.trim()];
    if (
      operatorNeedsValue(predicate.operator)
      && rawValues.some((value) => value === '')
    ) {
      return `${fact.label} cannot contain an empty value.`;
    }
    if (
      fact.kind === 'boolean'
      && operatorNeedsValue(predicate.operator)
      && rawValues.some((value) => value !== 'true' && value !== 'false')
    ) {
      return `${fact.label} requires true or false.`;
    }
    if (
      (fact.kind === 'integer' || fact.kind === 'number'
        || predicate.operator.startsWith('count_'))
      && operatorNeedsValue(predicate.operator)
      && rawValues.some((value) => !Number.isFinite(Number(value)))
    ) {
      return `${fact.label} requires a numeric value.`;
    }
    if (
      fact.kind === 'integer'
      && operatorNeedsValue(predicate.operator)
      && rawValues.some((value) => !Number.isInteger(Number(value)))
    ) {
      return `${fact.label} requires an integer value.`;
    }
    if (
      predicate.operator.startsWith('count_')
      && operatorNeedsValue(predicate.operator)
      && rawValues.some(
        (value) =>
          !Number.isInteger(Number(value))
          || Number(value) < 0,
      )
    ) {
      return `${fact.label} count requires a non-negative integer.`;
    }
    if (
      fact.minimum !== undefined
      && operatorNeedsValue(predicate.operator)
      && rawValues.some((value) => Number(value) < fact.minimum!)
    ) {
      return `${fact.label} must be at least ${fact.minimum}.`;
    }
    if (
      fact.maximum !== undefined
      && operatorNeedsValue(predicate.operator)
      && rawValues.some((value) => Number(value) > fact.maximum!)
    ) {
      return `${fact.label} must be at most ${fact.maximum}.`;
    }
    if (
      fact.allowedValues
      && fact.allowedValues.length > 0
      && operatorNeedsValue(predicate.operator)
      && rawValues.some((value) => !fact.allowedValues!.includes(value))
    ) {
      return `${fact.label} must use an allowed value: ${fact.allowedValues.join(', ')}.`;
    }
  }
  return null;
}

export function validateRuleDrafts(
  rules: readonly GuidelineRuleDraft[],
): string | null {
  const individualError = rules
    .map(validateRuleDraft)
    .find((candidate): candidate is string => candidate !== null);
  if (individualError) return individualError;

  const ruleIds = rules.map((rule) => rule.ruleId.trim());
  if (new Set(ruleIds).size !== ruleIds.length) {
    return 'Rule IDs must be unique within a revision.';
  }
  const ruleCodes = rules.map((rule) => rule.code.trim());
  if (new Set(ruleCodes).size !== ruleCodes.length) {
    return 'Rule codes must be unique within a revision.';
  }
  return null;
}

export function ruleDraftToInput(
  rule: GuidelineRuleDraft,
): GuidelineRuleInput {
  const validationError = validateRuleDraft(rule);
  if (validationError) throw new Error(validationError);
  const [firstTarget, ...remainingTargets] = rule.targetEntityTypes;
  const predicateInputs = rule.predicates.map((predicate) =>
    predicateInput(rule, predicate),
  );
  const [firstPredicate, ...remainingPredicates] = predicateInputs;
  return {
    rule_id: rule.ruleId.trim(),
    code: rule.code.trim(),
    title: rule.title.trim(),
    description: rule.description.trim(),
    target_entity_types: [firstTarget, ...remainingTargets],
    predicates: [firstPredicate, ...remainingPredicates],
    enforcement: rule.enforcement,
    operator: rule.operator,
    waivable: rule.waivable,
    policy_class: rule.policyClass.trim(),
  };
}

function parametersRecord(
  predicate: GuidelinePredicate,
): Record<string, PolicyParameterValue> {
  return Object.fromEntries(predicate.parameters);
}

function rawPredicateValue(parameters: Record<string, PolicyParameterValue>): string {
  if (Array.isArray(parameters.values)) return parameters.values.join(', ');
  if (parameters.value === undefined || parameters.value === null) return '';
  return String(parameters.value);
}

export function guidelineRuleToDraft(rule: GuidelineRule): GuidelineRuleDraft {
  return {
    localId: createPolicyClientId('rule-draft'),
    ruleId: rule.rule_id,
    code: rule.code,
    title: rule.title,
    description: rule.description,
    targetEntityTypes: [...rule.target_entity_types],
    predicates: rule.predicates.map((predicate) => {
      const parameters = parametersRecord(predicate);
      return {
        localId: createPolicyClientId('predicate-draft'),
        fact:
          typeof parameters.fact === 'string' ? parameters.fact : 'status',
        operator: predicate.predicate_code as PolicyPredicateOperator,
        rawValue: rawPredicateValue(parameters),
      };
    }),
    enforcement: rule.enforcement,
    operator: rule.operator,
    waivable: rule.waivable,
    policyClass: rule.policy_class,
  };
}

export function newRuleDraft(): GuidelineRuleDraft {
  return {
    localId: createPolicyClientId('rule-draft'),
    ruleId: createPolicyClientId('rule'),
    code: '',
    title: '',
    description: '',
    targetEntityTypes: [],
    predicates: [
      {
        localId: createPolicyClientId('predicate-draft'),
        fact: 'status',
        operator: 'exists',
        rawValue: '',
      },
    ],
    // Advisory is an explicit product invariant, not merely a visual default.
    enforcement: 'advisory',
    operator: 'all',
    waivable: false,
    policyClass: 'standard',
  };
}

export function guidelineRuleInputComparable(rule: GuidelineRuleInput): object {
  return {
    ...rule,
    policy_class: (rule.policy_class ?? 'standard').toLowerCase(),
    target_entity_types: [...rule.target_entity_types].sort(),
    predicates: rule.predicates
      .map((predicate) => ({
        predicate_code: predicate.predicate_code,
        parameters: Object.fromEntries(
          Object.entries(predicate.parameters ?? {}).sort(([left], [right]) =>
            left.localeCompare(right),
          ),
        ),
      }))
      .sort((left, right) =>
        JSON.stringify(left).localeCompare(JSON.stringify(right)),
      ),
  };
}

export function canonicalRuleSet(
  rules: readonly GuidelineRuleInput[],
): object[] {
  return [...rules]
    .sort((left, right) => left.code.localeCompare(right.code))
    .map(guidelineRuleInputComparable);
}

export function canonicalTags(tags: readonly string[]): string[] {
  return [...new Set(tags.map((tag) => tag.trim()).filter(Boolean))]
    .sort((left, right) => left.localeCompare(right));
}
