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

export const PROTECTED_POLICY_CLASSES = [
  'coverage',
  'permissions',
  'reviewer_separation',
  'lineage',
] as const;

export type ProtectedPolicyClass = (typeof PROTECTED_POLICY_CLASSES)[number];

export interface PolicyClassOption {
  value: 'standard' | ProtectedPolicyClass;
  label: string;
  description: string;
  effect: string;
  whenToUse: string;
  waivability: string;
  protected: boolean;
}

export const POLICY_CLASS_BEHAVIOR_NOTE =
  'Policy class records governance intent. Protected classes only make findings non-waivable: they do not run specialized coverage, permission, reviewer-separation, or lineage logic. Policy class never makes a rule blocking (Enforcement controls that) and never changes its Facts or operators.';

export const POLICY_CLASS_OPTIONS: readonly PolicyClassOption[] = [
  {
    value: 'standard',
    label: 'Standard',
    description:
      'A general-purpose policy. It can be advisory or blocking and may be waivable.',
    effect:
      'Records general intent without a protected-class waiver restriction. The selected conditions and Enforcement define all executable behavior.',
    whenToUse:
      'Use for ordinary delivery, quality, or process rules that do not belong to a protected governance category.',
    waivability: 'May be waivable when Waivable is enabled.',
    protected: false,
  },
  {
    value: 'coverage',
    label: 'Coverage',
    description:
      'Labels a rule as coverage governance and makes its findings non-waivable.',
    effect:
      'Records coverage intent in policy results and forces non-waivable findings. It adds no coverage calculation or check.',
    whenToUse:
      'Choose this label only when the configured Facts and conditions already express the required delivery-coverage threshold.',
    waivability: 'Never waivable.',
    protected: true,
  },
  {
    value: 'permissions',
    label: 'Permissions',
    description:
      'Labels a rule as permissions governance and makes its findings non-waivable.',
    effect:
      'Records permissions intent in policy results and forces non-waivable findings. It runs no ACL check and grants or revokes nothing.',
    whenToUse:
      'Choose this label only when the configured Facts already express the access boundary. policy/v1 currently has no direct ACL or capability Fact.',
    waivability: 'Never waivable.',
    protected: true,
  },
  {
    value: 'reviewer_separation',
    label: 'Reviewer separation',
    description:
      'Labels a rule as reviewer-separation governance and makes its findings non-waivable.',
    effect:
      'Records separation-of-duties intent in policy results and forces non-waivable findings. It performs no identity or reviewer lookup.',
    whenToUse:
      'Choose this label only when the configured Facts already prove independent review. policy/v1 currently has no author or reviewer identity Fact.',
    waivability: 'Never waivable.',
    protected: true,
  },
  {
    value: 'lineage',
    label: 'Lineage',
    description:
      'Labels a rule as lineage governance and makes its findings non-waivable.',
    effect:
      'Records lineage intent in policy results and forces non-waivable findings. It performs no KG traversal and creates no links.',
    whenToUse:
      'Use with available link or evidence-count Facts. policy/v1 has no direct KG traversal Fact.',
    waivability: 'Never waivable.',
    protected: true,
  },
] as const;

export function isProtectedPolicyClass(value: string): boolean {
  return PROTECTED_POLICY_CLASSES.includes(
    value.trim().toLowerCase() as ProtectedPolicyClass,
  );
}

export function isKnownPolicyClass(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  return POLICY_CLASS_OPTIONS.some((option) => option.value === normalized);
}

export function policyClassDescription(value: string): string {
  const normalized = value.trim().toLowerCase();
  return POLICY_CLASS_OPTIONS.find((option) => option.value === normalized)
    ?.description
    ?? 'Legacy/custom classification retained for compatibility. Choose a supported class to normalize it.';
}

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

export type PolicyFactKind =
  | 'boolean'
  | 'enum'
  | 'integer'
  | 'number'
  | 'string_set';

export interface PolicyFactOption {
  code: string;
  label: string;
  kind: PolicyFactKind;
  targets: readonly PolicyEntityType[] | 'all';
  description: string;
  valueGuidance: string;
  example: string;
  allowedValues?: readonly string[];
  minimum?: number;
  maximum?: number;
}

export const POLICY_FACT_KIND_LABELS: Readonly<Record<PolicyFactKind, string>> = {
  boolean: 'True / false',
  enum: 'Named value',
  integer: 'Whole number',
  number: 'Number',
  string_set: 'Set of text values',
};

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

export const POLICY_FACT_CATALOG: readonly PolicyFactOption[] = [
  {
    code: 'status',
    label: 'Status',
    kind: 'enum',
    targets: 'all',
    description:
      'The entity’s current lifecycle status. Available values are narrowed to the selected executable targets.',
    valueGuidance:
      'Choose a presence operator without a value, or compare with one of the statuses offered for every selected target.',
    example: 'Status — Is present',
  },
  {
    code: 'labels',
    label: 'Labels',
    kind: 'string_set',
    targets: 'all',
    description:
      'The normalized labels currently assigned to the entity. The Fact is absent when the entity has no labels.',
    valueGuidance:
      'Use Contains or Does not contain for one label, a Count operator for the number of labels, or a presence operator without a value.',
    example: 'Labels — Contains — security',
  },
  {
    code: 'resource_gate_ready',
    label: 'Resource gate ready',
    kind: 'boolean',
    targets: 'all',
    description:
      'Whether the entity currently satisfies its resource-readiness gate. Sprint and test-scenario snapshots always expose false because those types do not support this gate.',
    valueGuidance:
      'Use Equals true to require readiness. Is present only checks that the field exists; it does not mean the gate is ready.',
    example: 'Resource gate ready — Equals — true',
  },
  {
    code: 'complexity',
    label: 'Complexity',
    kind: 'enum',
    targets: ['ideation'],
    description:
      'The complexity classification assigned to an ideation. The Fact is absent until complexity is defined.',
    valueGuidance:
      'Choose Equals/Does not equal for one level, or Is one of/Is none of with comma-separated levels.',
    example: 'Complexity — Equals — large',
    allowedValues: ['small', 'medium', 'large'],
  },
  {
    code: 'qa_open_count',
    label: 'Open Q&A count',
    kind: 'integer',
    targets: ['ideation'],
    description:
      'The number of active ideation Q&A items that do not have an answer yet.',
    valueGuidance:
      'Compare the whole-number count with Equals, At least, At most, or another numeric operator.',
    example: 'Open Q&A count — Equals — 0',
    minimum: 0,
  },
  {
    code: 'ambiguity_score',
    label: 'Ambiguity score',
    kind: 'number',
    targets: ['ideation', 'refinement'],
    description:
      'The current ambiguity assessment score for the ideation or refinement, from 1 to 5. The fact is absent when no current assessment exists.',
    valueGuidance:
      'Compare with a number from 1 to 5. Lower thresholds are commonly expressed with Is at most.',
    example: 'Ambiguity score — Is at most — 2',
    minimum: 1,
    maximum: 5,
  },
  {
    code: 'research_open_count',
    label: 'Unresolved research decisions',
    kind: 'integer',
    targets: ['refinement'],
    description:
      'The number of research decisions in the refinement that are not resolved, including open, investigating, and deferred decisions.',
    valueGuidance:
      'Compare the whole-number count; use Equals 0 when all research decisions must be resolved.',
    example: 'Unresolved research decisions — Equals — 0',
    minimum: 0,
  },
  {
    code: 'research_resolved_count',
    label: 'Resolved research count',
    kind: 'integer',
    targets: ['refinement'],
    description:
      'The number of research decisions in the refinement that are resolved.',
    valueGuidance:
      'Compare the whole-number count with the minimum or exact amount of resolved research required.',
    example: 'Resolved research count — Is at least — 1',
    minimum: 0,
  },
  {
    code: 'fr_count',
    label: 'Functional requirement count',
    kind: 'integer',
    targets: ['spec'],
    description:
      'The number of functional requirements currently defined in the spec.',
    valueGuidance:
      'Compare the whole-number count with the minimum or exact number required.',
    example: 'Functional requirement count — Is at least — 1',
    minimum: 0,
  },
  {
    code: 'ac_count',
    label: 'Acceptance criterion count',
    kind: 'integer',
    targets: ['spec'],
    description:
      'The number of acceptance criteria currently defined in the spec.',
    valueGuidance:
      'Compare the whole-number count with the minimum or exact number required.',
    example: 'Acceptance criterion count — Is at least — 1',
    minimum: 0,
  },
  {
    code: 'tr_count',
    label: 'Technical requirement count',
    kind: 'integer',
    targets: ['spec'],
    description:
      'The number of technical requirements currently defined in the spec.',
    valueGuidance:
      'Compare the whole-number count with the minimum or exact number required.',
    example: 'Technical requirement count — Is at least — 1',
    minimum: 0,
  },
  {
    code: 'coverage_percent',
    label: 'Acceptance criteria coverage (%)',
    kind: 'number',
    targets: ['spec'],
    description:
      'The spec’s current acceptance-criterion coverage percentage, from 0 to 100. It is 100 when the spec has no acceptance criteria.',
    valueGuidance:
      'Use Is at least for a minimum threshold. Add Acceptance criterion count > 0 when an empty spec must not pass.',
    example: 'Acceptance criteria coverage (%) — Is at least — 80',
    minimum: 0,
    maximum: 100,
  },
  {
    code: 'validation_state',
    label: 'Validation state',
    kind: 'enum',
    targets: ['spec'],
    description:
      'The outcome of the spec’s current validation, or a server-owned state such as not_validated or validation_unavailable.',
    valueGuidance:
      'Enter the exact server-owned outcome, such as success, failed, not_validated, or validation_unavailable. Values remain open because providers may add outcomes.',
    example: 'Validation state — Equals — success',
  },
  {
    code: 'card_count',
    label: 'Card count',
    kind: 'integer',
    targets: ['sprint'],
    description:
      'The number of non-archived cards assigned to the sprint.',
    valueGuidance:
      'Compare the whole-number count with the minimum, maximum, or exact number required.',
    example: 'Card count — Is at least — 1',
    minimum: 0,
  },
  {
    code: 'open_card_count',
    label: 'Open card count',
    kind: 'integer',
    targets: ['sprint'],
    description:
      'The number of non-archived sprint cards whose status is neither done nor cancelled.',
    valueGuidance:
      'Compare the whole-number count; use Equals 0 when no unfinished cards may remain.',
    example: 'Open card count — Equals — 0',
    minimum: 0,
  },
  {
    code: 'passed_scenario_count',
    label: 'Passed scenario count',
    kind: 'integer',
    targets: ['sprint'],
    description:
      'The number of test scenarios in the sprint scope whose current status is passed.',
    valueGuidance:
      'Compare the whole-number count with the minimum or exact number of passed scenarios required.',
    example: 'Passed scenario count — Is at least — 1',
    minimum: 0,
  },
  {
    code: 'card_type',
    label: 'Card type',
    kind: 'enum',
    targets: ['card'],
    description:
      'The card classification: normal work, bug, or test.',
    valueGuidance:
      'Choose Equals/Does not equal for one type, or Is one of/Is none of with comma-separated types.',
    example: 'Card type — Equals — bug',
    allowedValues: ['normal', 'bug', 'test'],
  },
  {
    code: 'priority',
    label: 'Priority',
    kind: 'enum',
    targets: ['card'],
    description:
      'The priority currently assigned to the card.',
    valueGuidance:
      'Choose Equals/Does not equal for one priority, or Is one of/Is none of with comma-separated priorities.',
    example: 'Priority — Is one of — critical, very_high',
    allowedValues: ['critical', 'very_high', 'high', 'medium', 'low', 'none'],
  },
  {
    code: 'dependency_open_count',
    label: 'Open dependency count',
    kind: 'integer',
    targets: ['card'],
    description:
      'The number of upstream card dependencies that are neither done nor cancelled.',
    valueGuidance:
      'Compare the whole-number count; use Equals 0 when all dependencies must be resolved.',
    example: 'Open dependency count — Equals — 0',
    minimum: 0,
  },
  {
    code: 'scenario_type',
    label: 'Scenario type',
    kind: 'enum',
    targets: ['test_scenario'],
    description:
      'The test scenario classification. Negative covers invalid, prohibited, or denial paths that the system is expected to reject.',
    valueGuidance:
      'Choose Equals/Does not equal for one type, or Is one of/Is none of with comma-separated types.',
    example: 'Scenario type — Equals — e2e',
    allowedValues: ['unit', 'integration', 'e2e', 'manual', 'negative'],
  },
  {
    code: 'linked_test_card_count',
    label: 'Linked test card count',
    kind: 'integer',
    targets: ['test_scenario'],
    description:
      'The number of non-archived test cards linked to the test scenario.',
    valueGuidance:
      'Compare the whole-number count with the minimum or exact number of linked test cards required.',
    example: 'Linked test card count — Is at least — 1',
    minimum: 0,
  },
  {
    code: 'test_scenario_count',
    label: 'Test scenario count',
    kind: 'integer',
    targets: ['spec', 'sprint', 'card'],
    description:
      'The number of test scenarios in scope for the selected spec, sprint, or card.',
    valueGuidance:
      'Compare the whole-number count with the minimum or exact number of scenarios required.',
    example: 'Test scenario count — Is at least — 1',
    minimum: 0,
  },
  {
    code: 'evidence_count',
    label: 'Evidence count',
    kind: 'integer',
    targets: ['card', 'test_scenario'],
    description:
      'The count of current, authenticated evidence. A test scenario contributes 0 or 1; a card sums that value across its linked scenarios.',
    valueGuidance:
      'Compare the whole-number count with the minimum or exact number of evidence records required.',
    example: 'Evidence count — Is at least — 1',
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
  originalCode: string | null;
  title: string;
  description: string;
  targetEntityTypes: PolicyEntityType[];
  predicates: PolicyPredicateDraft[];
  enforcement: 'advisory' | 'blocking';
  operator: 'all' | 'any';
  waivable: boolean;
  policyClass: string;
  originalPolicyClass: string | null;
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
  if (targets.length === 0) return [];
  const selectedTargets = targets;
  return POLICY_FACT_CATALOG.filter(
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

export const POLICY_OPERATOR_LABELS: Readonly<
  Record<PolicyPredicateOperator, string>
> = {
  exists: 'Is present',
  not_exists: 'Is not present',
  eq: 'Equals',
  ne: 'Does not equal',
  in: 'Is one of',
  not_in: 'Is none of',
  gt: 'Is greater than',
  gte: 'Is at least',
  lt: 'Is less than',
  lte: 'Is at most',
  count_eq: 'Count equals',
  count_ne: 'Count does not equal',
  count_gt: 'Count is greater than',
  count_gte: 'Count is at least',
  count_lt: 'Count is less than',
  count_lte: 'Count is at most',
  contains: 'Contains',
  not_contains: 'Does not contain',
};

export function suggestRuleKey(title: string): string {
  const normalized = title
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '');
  if (!normalized) return '';
  const withLetterPrefix = /^[a-z]/.test(normalized)
    ? normalized
    : `rule_${normalized}`;
  return withLetterPrefix.slice(0, 200).replace(/_+$/g, '');
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
  if (!rule.code.trim()) return 'Rule key is required.';
  if (rule.code.trim().length > 200) {
    return 'Rule key cannot exceed 200 characters.';
  }
  if (!/^[A-Za-z][A-Za-z0-9_.:-]*$/.test(rule.code.trim())) {
    return 'Rule key must start with a letter and use only letters, numbers, _, ., :, or -.';
  }
  if (!rule.title.trim()) return 'Rule title is required.';
  if (rule.title.trim().length > 500) {
    return 'Rule title cannot exceed 500 characters.';
  }
  if (!rule.description.trim()) return 'Rule description is required.';
  if (rule.targetEntityTypes.length === 0) {
    return 'Select at least one executable target.';
  }
  if (rule.predicates.length === 0) return 'Add at least one condition.';
  if (!rule.policyClass.trim()) return 'Policy class is required.';
  if (rule.policyClass.trim().length > 200) {
    return 'Policy class cannot exceed 200 characters.';
  }
  if (
    isProtectedPolicyClass(rule.policyClass)
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
    return 'Rule keys must be unique within a revision.';
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
    originalCode: rule.code,
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
    waivable: isProtectedPolicyClass(rule.policy_class)
      ? false
      : rule.waivable,
    policyClass: rule.policy_class,
    originalPolicyClass: rule.policy_class,
  };
}

export function newRuleDraft(): GuidelineRuleDraft {
  return {
    localId: createPolicyClientId('rule-draft'),
    ruleId: createPolicyClientId('rule'),
    code: '',
    originalCode: null,
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
    originalPolicyClass: null,
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
