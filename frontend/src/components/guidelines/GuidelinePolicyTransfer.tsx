import { useRef, useState } from 'react';
import { Download, Upload } from 'lucide-react';
import toast from 'react-hot-toast';

import { usePermissions } from '@/hooks/usePermissions';
import {
  PolicyGovernanceApiError,
  usePolicyGovernanceApi,
} from '@/services/policy-governance-api';
import type {
  GuidelineExportEnvelopeV3,
  GuidelineImportResult,
} from '@/types/policy-governance';

interface GuidelinePolicyTransferProps {
  boardId: string;
  onImported: () => void | Promise<void>;
}

const ENVELOPE_KEYS = [
  'contract_version',
  'schema_version',
  'kind',
  'exported_at',
  'source_board_id',
  'content_digest',
  'guidelines',
] as const;

const AGGREGATE_KEYS = [
  'identity',
  'revisions',
  'head',
  'retirement',
  'bindings',
  'history_status',
  'migration_notes',
] as const;

const IDENTITY_KEYS = [
  'guideline_id',
  'owner_id',
  'scope',
  'board_id',
  'context_scope',
  'created_at',
] as const;

const REVISION_KEYS = [
  'revision_id',
  'guideline_id',
  'revision_number',
  'semantic_version',
  'title',
  'content',
  'revision_digest',
  'metrics',
  'created_by',
  'created_at',
  'parent_revision_id',
  'tags',
  'published_head_revision',
  'published_head_updated_at',
  'legacy_version',
  'legacy_version_unresolvable',
  'legacy_tags',
] as const;

const HEAD_KEYS = [
  'guideline_id',
  'revision_id',
  'revision_number',
  'semantic_version',
  'head_revision',
  'updated_at',
] as const;

const RETIREMENT_KEYS = [
  'retirement_id',
  'guideline_id',
  'status',
  'retired_revision_id',
  'retired_revision_number',
  'retired_semantic_version',
  'retired_revision_digest',
  'retired_head_revision',
  'reason',
  'retired_by',
  'retired_at',
  'superseded_by_guideline_id',
] as const;

const METRIC_KEYS = [
  'metric_id',
  'code',
  'title',
  'description',
  'evaluation_rubric',
  'target_entity_types',
  'direction',
  'default_threshold',
] as const;

const EXPORTED_BINDING_KEYS = [
  'binding',
  'physical_source_kind',
  'binding_origin',
  'materialization',
  'legacy_source_id',
  'legacy_guideline_version',
  'legacy_template_id',
  'legacy_template_version',
  'legacy_version_unresolvable',
  'evidence_refs',
  'binding_digest',
] as const;

const LOGICAL_BINDING_KEYS = [
  'binding_id',
  'board_id',
  'guideline_id',
  'revision_id',
  'semantic_version',
  'revision_digest',
  'priority',
  'binding_revision',
  'adopted_by',
  'adopted_at',
  'enforcement',
  'minimum_confidence',
  'metric_threshold_overrides',
  'configuration_digest',
  'state',
  'source_kind',
] as const;

const IMPORT_RESULT_KEYS = [
  'transaction_status',
  'created_count',
  'skip_identical_count',
  'conflict_count',
  'overwritten_row_count',
  'dry_run',
] as const;

const ENTITY_TYPES = new Set([
  'ideation',
  'refinement',
  'spec',
  'sprint',
  'card',
  'test_scenario',
]);

const METRIC_CODE_PATTERN = /^[A-Za-z][A-Za-z0-9_.:-]*$/u;
const SHA256_PATTERN = /^[0-9a-f]{64}$/u;
const SEMVER_PATTERN =
  /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/u;

function policyTransferError(error: unknown): string {
  if (error instanceof PolicyGovernanceApiError) return error.message;
  return error instanceof Error
    ? error.message
    : 'Unexpected policy transfer error.';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasOnlyRequiredKeys(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const allowed = new Set([...required, ...optional]);
  return (
    required.every((key) => (
      Object.prototype.hasOwnProperty.call(value, key)
    ))
    && Object.keys(value).every((key) => allowed.has(key))
  );
}

interface SemanticVersionValue {
  core: [number, number, number];
  prerelease: string[];
}

interface ValidatedMetric {
  metricId: string;
  code: string;
}

interface ValidatedRevision {
  revisionId: string;
  guidelineId: string;
  revisionNumber: number;
  semanticVersion: string;
  semanticVersionValue: SemanticVersionValue;
  revisionDigest: string;
  createdAt: number;
  publishedHeadUpdatedAt: number;
  parentRevisionId: string | null;
  metricCodes: Set<string>;
  metricCount: number;
  legacyVersion: string | null;
  legacyVersionUnresolvable: boolean;
}

interface ValidatedIdentity {
  guidelineId: string;
  scope: 'global' | 'inline';
  boardId: string | null;
  createdAt: number;
}

interface ValidatedHead {
  guidelineId: string;
  revisionId: string;
  revisionNumber: number;
  semanticVersion: string;
  headRevision: number;
  updatedAt: number;
}

interface ValidatedRetirement {
  guidelineId: string;
  status: 'retired' | 'superseded';
  retiredRevisionId: string;
  retiredRevisionNumber: number;
  retiredSemanticVersion: string;
  retiredRevisionDigest: string;
  retiredHeadRevision: number;
  retiredAt: number;
  successorId: string | null;
}

interface ValidatedBinding {
  boardId: string;
  bindingId: string;
  guidelineId: string;
  revisionId: string;
  bindingRevision: number;
  adoptedAt: number;
  sourceKind: 'native' | 'default_materialization';
  state: 'active' | 'unlinked';
  snapshotSignature: string;
}

interface ValidatedAggregate {
  guidelineId: string;
  containsSemanticMetrics: boolean;
  successorId: string | null;
}

function invalidEnvelope(path: string): never {
  throw new Error(`Guideline v3 envelope is invalid at ${path}.`);
}

function recordWithExactKeys(
  value: unknown,
  keys: readonly string[],
  path: string,
): Record<string, unknown> {
  if (!isRecord(value) || !hasOnlyRequiredKeys(value, keys)) {
    invalidEnvelope(path);
  }
  return value;
}

function canonicalText(value: unknown, path: string): string {
  if (
    typeof value !== 'string'
    || value.length === 0
    || value !== value.trim()
  ) {
    invalidEnvelope(path);
  }
  return value;
}

function nullableCanonicalText(
  value: unknown,
  path: string,
): string | null {
  return value === null ? null : canonicalText(value, path);
}

function strictInteger(
  value: unknown,
  path: string,
  minimum = 0,
): number {
  if (
    typeof value !== 'number'
    || !Number.isSafeInteger(value)
    || value < minimum
  ) {
    invalidEnvelope(path);
  }
  return value;
}

function score(value: unknown, path: string): number {
  const result = strictInteger(value, path);
  if (result > 100) invalidEnvelope(path);
  return result;
}

function digest(value: unknown, path: string): string {
  const result = canonicalText(value, path);
  if (!SHA256_PATTERN.test(result)) invalidEnvelope(path);
  return result;
}

function timestamp(value: unknown, path: string): number {
  const raw = canonicalText(value, path);
  if (!/(?:Z|[+-][0-9]{2}:[0-9]{2})$/u.test(raw)) {
    invalidEnvelope(path);
  }
  const parsed = Date.parse(raw);
  if (!Number.isFinite(parsed)) invalidEnvelope(path);
  return parsed;
}

function semanticVersion(
  value: unknown,
  path: string,
): {
  raw: string;
  parsed: SemanticVersionValue;
} {
  const raw = canonicalText(value, path);
  const match = SEMVER_PATTERN.exec(raw);
  if (!match) invalidEnvelope(path);
  const core = [Number(match[1]), Number(match[2]), Number(match[3])] as const;
  if (core.some((item) => !Number.isSafeInteger(item))) {
    invalidEnvelope(path);
  }
  const prerelease = match[4]?.split('.') ?? [];
  if (prerelease.some((item) =>
    /^[0-9]+$/u.test(item) && item.length > 1 && item.startsWith('0')
  )) {
    invalidEnvelope(path);
  }
  return {
    raw,
    parsed: {
      core: [core[0], core[1], core[2]],
      prerelease,
    },
  };
}

function compareSemanticVersions(
  left: SemanticVersionValue,
  right: SemanticVersionValue,
): number {
  for (let index = 0; index < 3; index += 1) {
    if (left.core[index] !== right.core[index]) {
      return left.core[index] < right.core[index] ? -1 : 1;
    }
  }
  if (left.prerelease.length === 0 || right.prerelease.length === 0) {
    if (left.prerelease.length === right.prerelease.length) return 0;
    return left.prerelease.length === 0 ? 1 : -1;
  }
  const length = Math.max(left.prerelease.length, right.prerelease.length);
  for (let index = 0; index < length; index += 1) {
    const leftPart = left.prerelease[index];
    const rightPart = right.prerelease[index];
    if (leftPart === undefined || rightPart === undefined) {
      return leftPart === undefined ? -1 : 1;
    }
    if (leftPart === rightPart) continue;
    const leftNumeric = /^[0-9]+$/u.test(leftPart);
    const rightNumeric = /^[0-9]+$/u.test(rightPart);
    if (leftNumeric && rightNumeric) {
      return Number(leftPart) < Number(rightPart) ? -1 : 1;
    }
    if (leftNumeric !== rightNumeric) return leftNumeric ? -1 : 1;
    return leftPart < rightPart ? -1 : 1;
  }
  return 0;
}

function uniqueTextList(value: unknown, path: string): string[] {
  if (!Array.isArray(value)) invalidEnvelope(path);
  const items = value.map((item, index) =>
    canonicalText(item, `${path}[${index}]`)
  );
  if (new Set(items).size !== items.length) invalidEnvelope(path);
  return items;
}

function validateMetric(value: unknown, path: string): ValidatedMetric {
  const metric = recordWithExactKeys(value, METRIC_KEYS, path);
  const metricId = canonicalText(metric.metric_id, `${path}.metric_id`);
  const code = canonicalText(metric.code, `${path}.code`);
  const title = canonicalText(metric.title, `${path}.title`);
  if (
    !METRIC_CODE_PATTERN.test(code)
    || metricId.toLowerCase() === 'confidence'
    || code.toLowerCase() === 'confidence'
    || title.toLowerCase() === 'confidence'
  ) {
    invalidEnvelope(path);
  }
  canonicalText(metric.description, `${path}.description`);
  canonicalText(metric.evaluation_rubric, `${path}.evaluation_rubric`);
  if (
    !Array.isArray(metric.target_entity_types)
    || metric.target_entity_types.length === 0
  ) {
    invalidEnvelope(`${path}.target_entity_types`);
  }
  const targets = metric.target_entity_types.map((target, index) => {
    const result = canonicalText(
      target,
      `${path}.target_entity_types[${index}]`,
    );
    if (!ENTITY_TYPES.has(result)) {
      invalidEnvelope(`${path}.target_entity_types[${index}]`);
    }
    return result;
  });
  if (new Set(targets).size !== targets.length) {
    invalidEnvelope(`${path}.target_entity_types`);
  }
  if (metric.direction !== 'minimum' && metric.direction !== 'maximum') {
    invalidEnvelope(`${path}.direction`);
  }
  score(metric.default_threshold, `${path}.default_threshold`);
  return { metricId, code };
}

function validateRevision(
  value: unknown,
  path: string,
): ValidatedRevision {
  const revision = recordWithExactKeys(value, REVISION_KEYS, path);
  const revisionId = canonicalText(
    revision.revision_id,
    `${path}.revision_id`,
  );
  const guidelineId = canonicalText(
    revision.guideline_id,
    `${path}.guideline_id`,
  );
  const revisionNumber = strictInteger(
    revision.revision_number,
    `${path}.revision_number`,
    1,
  );
  const version = semanticVersion(
    revision.semantic_version,
    `${path}.semantic_version`,
  );
  canonicalText(revision.title, `${path}.title`);
  canonicalText(revision.content, `${path}.content`);
  const revisionDigest = digest(
    revision.revision_digest,
    `${path}.revision_digest`,
  );
  if (!Array.isArray(revision.metrics)) {
    invalidEnvelope(`${path}.metrics`);
  }
  const metrics = revision.metrics.map((metric, index) =>
    validateMetric(metric, `${path}.metrics[${index}]`)
  );
  if (
    new Set(metrics.map((metric) => metric.metricId)).size !== metrics.length
    || new Set(metrics.map((metric) => metric.code.toLowerCase())).size
      !== metrics.length
  ) {
    invalidEnvelope(`${path}.metrics`);
  }
  canonicalText(revision.created_by, `${path}.created_by`);
  const createdAt = timestamp(revision.created_at, `${path}.created_at`);
  const parentRevisionId = nullableCanonicalText(
    revision.parent_revision_id,
    `${path}.parent_revision_id`,
  );
  uniqueTextList(revision.tags, `${path}.tags`);
  const publishedHeadRevision = strictInteger(
    revision.published_head_revision,
    `${path}.published_head_revision`,
    1,
  );
  if (publishedHeadRevision !== revisionNumber) {
    invalidEnvelope(`${path}.published_head_revision`);
  }
  const publishedHeadUpdatedAt = timestamp(
    revision.published_head_updated_at,
    `${path}.published_head_updated_at`,
  );
  if (publishedHeadUpdatedAt < createdAt) {
    invalidEnvelope(`${path}.published_head_updated_at`);
  }
  const legacyVersion = revision.legacy_version === null
    ? null
    : semanticVersion(
        revision.legacy_version,
        `${path}.legacy_version`,
      ).raw;
  if (typeof revision.legacy_version_unresolvable !== 'boolean') {
    invalidEnvelope(`${path}.legacy_version_unresolvable`);
  }
  if (
    revision.legacy_version_unresolvable !== (legacyVersion !== null)
  ) {
    invalidEnvelope(`${path}.legacy_version`);
  }
  if (revision.legacy_tags !== null) {
    uniqueTextList(revision.legacy_tags, `${path}.legacy_tags`);
    if (!revision.legacy_version_unresolvable) {
      invalidEnvelope(`${path}.legacy_tags`);
    }
  }
  return {
    revisionId,
    guidelineId,
    revisionNumber,
    semanticVersion: version.raw,
    semanticVersionValue: version.parsed,
    revisionDigest,
    createdAt,
    publishedHeadUpdatedAt,
    parentRevisionId,
    metricCodes: new Set(metrics.map((metric) => metric.code)),
    metricCount: metrics.length,
    legacyVersion,
    legacyVersionUnresolvable: revision.legacy_version_unresolvable,
  };
}

function validateIdentity(
  value: unknown,
  path: string,
): ValidatedIdentity {
  const identity = recordWithExactKeys(value, IDENTITY_KEYS, path);
  const guidelineId = canonicalText(
    identity.guideline_id,
    `${path}.guideline_id`,
  );
  canonicalText(identity.owner_id, `${path}.owner_id`);
  if (identity.scope !== 'global' && identity.scope !== 'inline') {
    invalidEnvelope(`${path}.scope`);
  }
  const boardId = nullableCanonicalText(identity.board_id, `${path}.board_id`);
  if (
    (identity.scope === 'inline' && boardId === null)
    || (identity.scope === 'global' && boardId !== null)
  ) {
    invalidEnvelope(`${path}.board_id`);
  }
  if (identity.context_scope !== 'all') {
    invalidEnvelope(`${path}.context_scope`);
  }
  return {
    guidelineId,
    scope: identity.scope,
    boardId,
    createdAt: timestamp(identity.created_at, `${path}.created_at`),
  };
}

function validateHead(value: unknown, path: string): ValidatedHead {
  const head = recordWithExactKeys(value, HEAD_KEYS, path);
  return {
    guidelineId: canonicalText(head.guideline_id, `${path}.guideline_id`),
    revisionId: canonicalText(head.revision_id, `${path}.revision_id`),
    revisionNumber: strictInteger(
      head.revision_number,
      `${path}.revision_number`,
      1,
    ),
    semanticVersion: semanticVersion(
      head.semantic_version,
      `${path}.semantic_version`,
    ).raw,
    headRevision: strictInteger(
      head.head_revision,
      `${path}.head_revision`,
      1,
    ),
    updatedAt: timestamp(head.updated_at, `${path}.updated_at`),
  };
}

function validateRetirement(
  value: unknown,
  path: string,
): ValidatedRetirement | null {
  if (value === null) return null;
  const retirement = recordWithExactKeys(value, RETIREMENT_KEYS, path);
  canonicalText(retirement.retirement_id, `${path}.retirement_id`);
  const guidelineId = canonicalText(
    retirement.guideline_id,
    `${path}.guideline_id`,
  );
  if (
    retirement.status !== 'retired'
    && retirement.status !== 'superseded'
  ) {
    invalidEnvelope(`${path}.status`);
  }
  const successorId = nullableCanonicalText(
    retirement.superseded_by_guideline_id,
    `${path}.superseded_by_guideline_id`,
  );
  if (
    (retirement.status === 'superseded'
      && (successorId === null || successorId === guidelineId))
    || (retirement.status === 'retired' && successorId !== null)
  ) {
    invalidEnvelope(`${path}.superseded_by_guideline_id`);
  }
  canonicalText(retirement.reason, `${path}.reason`);
  canonicalText(retirement.retired_by, `${path}.retired_by`);
  return {
    guidelineId,
    status: retirement.status,
    retiredRevisionId: canonicalText(
      retirement.retired_revision_id,
      `${path}.retired_revision_id`,
    ),
    retiredRevisionNumber: strictInteger(
      retirement.retired_revision_number,
      `${path}.retired_revision_number`,
      1,
    ),
    retiredSemanticVersion: semanticVersion(
      retirement.retired_semantic_version,
      `${path}.retired_semantic_version`,
    ).raw,
    retiredRevisionDigest: digest(
      retirement.retired_revision_digest,
      `${path}.retired_revision_digest`,
    ),
    retiredHeadRevision: strictInteger(
      retirement.retired_head_revision,
      `${path}.retired_head_revision`,
      1,
    ),
    retiredAt: timestamp(retirement.retired_at, `${path}.retired_at`),
    successorId,
  };
}

function validateBinding(
  value: unknown,
  path: string,
  revisionsById: ReadonlyMap<string, ValidatedRevision>,
): ValidatedBinding {
  const exported = recordWithExactKeys(value, EXPORTED_BINDING_KEYS, path);
  const binding = recordWithExactKeys(
    exported.binding,
    LOGICAL_BINDING_KEYS,
    `${path}.binding`,
  );
  const bindingId = canonicalText(
    binding.binding_id,
    `${path}.binding.binding_id`,
  );
  const boardId = canonicalText(
    binding.board_id,
    `${path}.binding.board_id`,
  );
  const guidelineId = canonicalText(
    binding.guideline_id,
    `${path}.binding.guideline_id`,
  );
  const revisionId = canonicalText(
    binding.revision_id,
    `${path}.binding.revision_id`,
  );
  const version = semanticVersion(
    binding.semantic_version,
    `${path}.binding.semantic_version`,
  ).raw;
  const revisionDigest = digest(
    binding.revision_digest,
    `${path}.binding.revision_digest`,
  );
  const revision = revisionsById.get(revisionId);
  if (
    !revision
    || revision.semanticVersion !== version
    || revision.revisionDigest !== revisionDigest
  ) {
    invalidEnvelope(`${path}.binding.revision_id`);
  }
  const priority = strictInteger(
    binding.priority,
    `${path}.binding.priority`,
  );
  const bindingRevision = strictInteger(
    binding.binding_revision,
    `${path}.binding.binding_revision`,
    1,
  );
  canonicalText(binding.adopted_by, `${path}.binding.adopted_by`);
  const adoptedAt = timestamp(
    binding.adopted_at,
    `${path}.binding.adopted_at`,
  );
  if (adoptedAt < revision.createdAt) {
    invalidEnvelope(`${path}.binding.adopted_at`);
  }
  if (binding.enforcement !== 'advisory' && binding.enforcement !== 'blocking') {
    invalidEnvelope(`${path}.binding.enforcement`);
  }
  const minimumConfidence = score(
    binding.minimum_confidence,
    `${path}.binding.minimum_confidence`,
  );
  if (
    !isRecord(binding.metric_threshold_overrides)
  ) {
    invalidEnvelope(`${path}.binding.metric_threshold_overrides`);
  }
  const overrideEntries = Object.entries(binding.metric_threshold_overrides)
    .map(([metricCode, threshold]) => {
      if (
        metricCode !== metricCode.trim()
        || !METRIC_CODE_PATTERN.test(metricCode)
        || metricCode.toLowerCase() === 'confidence'
        || !revision.metricCodes.has(metricCode)
      ) {
        invalidEnvelope(
          `${path}.binding.metric_threshold_overrides.${metricCode}`,
        );
      }
      return [
        metricCode,
        score(
          threshold,
          `${path}.binding.metric_threshold_overrides.${metricCode}`,
        ),
      ] as const;
    })
    .sort(([left], [right]) => left.localeCompare(right));
  if (
    new Set(overrideEntries.map(([code]) => code.toLowerCase())).size
    !== overrideEntries.length
  ) {
    invalidEnvelope(`${path}.binding.metric_threshold_overrides`);
  }
  digest(
    binding.configuration_digest,
    `${path}.binding.configuration_digest`,
  );
  if (binding.state !== 'active' && binding.state !== 'unlinked') {
    invalidEnvelope(`${path}.binding.state`);
  }
  if (
    binding.source_kind !== 'native'
    && binding.source_kind !== 'default_materialization'
  ) {
    invalidEnvelope(`${path}.binding.source_kind`);
  }

  canonicalText(
    exported.physical_source_kind,
    `${path}.physical_source_kind`,
  );
  canonicalText(exported.binding_origin, `${path}.binding_origin`);
  if (exported.materialization !== 'live'
    && exported.materialization !== 'candidate') {
    invalidEnvelope(`${path}.materialization`);
  }
  for (const key of [
    'legacy_source_id',
    'legacy_guideline_version',
    'legacy_template_id',
    'legacy_template_version',
  ] as const) {
    nullableCanonicalText(exported[key], `${path}.${key}`);
  }
  if (typeof exported.legacy_version_unresolvable !== 'boolean') {
    invalidEnvelope(`${path}.legacy_version_unresolvable`);
  }
  if (!Array.isArray(exported.evidence_refs)) {
    invalidEnvelope(`${path}.evidence_refs`);
  }
  const evidenceKinds = new Set<string>();
  for (const [index, evidence] of exported.evidence_refs.entries()) {
    if (!Array.isArray(evidence) || evidence.length !== 2) {
      invalidEnvelope(`${path}.evidence_refs[${index}]`);
    }
    const kind = canonicalText(
      evidence[0],
      `${path}.evidence_refs[${index}][0]`,
    );
    canonicalText(
      evidence[1],
      `${path}.evidence_refs[${index}][1]`,
    );
    if (evidenceKinds.has(kind)) {
      invalidEnvelope(`${path}.evidence_refs[${index}][0]`);
    }
    evidenceKinds.add(kind);
  }
  digest(exported.binding_digest, `${path}.binding_digest`);

  return {
    boardId,
    bindingId,
    guidelineId,
    revisionId,
    bindingRevision,
    adoptedAt,
    sourceKind: binding.source_kind,
    state: binding.state,
    snapshotSignature: JSON.stringify([
      revisionId,
      version,
      revisionDigest,
      priority,
      binding.enforcement,
      minimumConfidence,
      overrideEntries,
    ]),
  };
}

function validateBindingHistories(
  bindings: ValidatedBinding[],
  path: string,
): void {
  const bindingIdByBoard = new Map<string, string>();
  const histories = new Map<string, ValidatedBinding[]>();
  const candidateKeys = new Set<string>();
  for (const binding of bindings) {
    const stableId = bindingIdByBoard.get(binding.boardId);
    if (stableId !== undefined && stableId !== binding.bindingId) {
      invalidEnvelope(`${path}.binding.binding_id`);
    }
    bindingIdByBoard.set(binding.boardId, binding.bindingId);
    const candidateKey = [
      binding.boardId,
      binding.guidelineId,
      binding.bindingId,
      binding.bindingRevision,
    ].join('\u0000');
    if (candidateKeys.has(candidateKey)) {
      invalidEnvelope(`${path}.binding.binding_revision`);
    }
    candidateKeys.add(candidateKey);
    const historyKey = [binding.boardId, binding.bindingId].join('\u0000');
    const history = histories.get(historyKey) ?? [];
    history.push(binding);
    histories.set(historyKey, history);
  }

  for (const history of histories.values()) {
    history.sort((left, right) =>
      left.bindingRevision - right.bindingRevision
    );
    for (let index = 0; index < history.length; index += 1) {
      const current = history[index];
      if (current.bindingRevision !== index + 1) {
        invalidEnvelope(`${path}.binding.binding_revision`);
      }
      if (index === 0) {
        if (current.state !== 'active') {
          invalidEnvelope(`${path}.binding.state`);
        }
        continue;
      }
      const previous = history[index - 1];
      if (
        current.adoptedAt <= previous.adoptedAt
        || current.sourceKind !== previous.sourceKind
      ) {
        invalidEnvelope(`${path}.binding.adopted_at`);
      }
      const snapshotChanged =
        current.snapshotSignature !== previous.snapshotSignature;
      if (
        current.state === 'unlinked'
        && (previous.state === 'unlinked' || snapshotChanged)
      ) {
        invalidEnvelope(`${path}.binding.state`);
      }
      if (current.state === previous.state && !snapshotChanged) {
        invalidEnvelope(`${path}.binding.state`);
      }
    }
  }
}

function validateAggregate(
  value: unknown,
  path: string,
  sourceBoardId: string | null,
): ValidatedAggregate {
  const aggregate = recordWithExactKeys(value, AGGREGATE_KEYS, path);
  const identity = validateIdentity(aggregate.identity, `${path}.identity`);
  if (!Array.isArray(aggregate.revisions) || aggregate.revisions.length === 0) {
    invalidEnvelope(`${path}.revisions`);
  }
  const revisions = aggregate.revisions.map((revision, index) =>
    validateRevision(revision, `${path}.revisions[${index}]`)
  );
  const revisionIds = new Set<string>();
  const semanticVersions = new Set<string>();
  for (let index = 0; index < revisions.length; index += 1) {
    const revision = revisions[index];
    const previous = revisions[index - 1];
    if (
      revision.guidelineId !== identity.guidelineId
      || revision.revisionNumber !== index + 1
      || revisionIds.has(revision.revisionId)
      || semanticVersions.has(revision.semanticVersion)
      || revision.parentRevisionId
        !== (previous?.revisionId ?? null)
      || (previous !== undefined && revision.createdAt <= previous.createdAt)
      || (
        previous !== undefined
        && revision.publishedHeadUpdatedAt
          < previous.publishedHeadUpdatedAt
      )
      || (
        previous !== undefined
        && compareSemanticVersions(
          revision.semanticVersionValue,
          previous.semanticVersionValue,
        ) <= 0
      )
    ) {
      invalidEnvelope(`${path}.revisions[${index}]`);
    }
    revisionIds.add(revision.revisionId);
    semanticVersions.add(revision.semanticVersion);
  }
  if (
    revisions[0].semanticVersion !== '1.0.0'
    || identity.createdAt > revisions[0].createdAt
  ) {
    invalidEnvelope(`${path}.revisions[0]`);
  }

  const latest = revisions[revisions.length - 1];
  const head = validateHead(aggregate.head, `${path}.head`);
  if (
    head.guidelineId !== identity.guidelineId
    || head.revisionId !== latest.revisionId
    || head.revisionNumber !== latest.revisionNumber
    || head.headRevision !== latest.revisionNumber
    || head.semanticVersion !== latest.semanticVersion
    || head.updatedAt !== latest.publishedHeadUpdatedAt
  ) {
    invalidEnvelope(`${path}.head`);
  }
  const retirement = validateRetirement(
    aggregate.retirement,
    `${path}.retirement`,
  );
  if (
    retirement !== null
    && (
      retirement.guidelineId !== identity.guidelineId
      || retirement.retiredRevisionId !== latest.revisionId
      || retirement.retiredRevisionNumber !== latest.revisionNumber
      || retirement.retiredSemanticVersion !== latest.semanticVersion
      || retirement.retiredRevisionDigest !== latest.revisionDigest
      || retirement.retiredHeadRevision !== head.headRevision
      || retirement.retiredAt <= head.updatedAt
    )
  ) {
    invalidEnvelope(`${path}.retirement`);
  }

  if (!Array.isArray(aggregate.bindings)) {
    invalidEnvelope(`${path}.bindings`);
  }
  const revisionsById = new Map(
    revisions.map((revision) => [revision.revisionId, revision]),
  );
  const bindings = aggregate.bindings.map((binding, index) =>
    validateBinding(
      binding,
      `${path}.bindings[${index}]`,
      revisionsById,
    )
  );
  for (const [index, binding] of bindings.entries()) {
    if (
      binding.guidelineId !== identity.guidelineId
      || (
        identity.scope === 'inline'
        && binding.boardId !== identity.boardId
      )
      || (sourceBoardId !== null && binding.boardId !== sourceBoardId)
    ) {
      invalidEnvelope(`${path}.bindings[${index}].binding`);
    }
  }
  validateBindingHistories(bindings, `${path}.bindings`);

  if (
    aggregate.history_status !== 'complete'
    && aggregate.history_status !== 'baseline_only'
  ) {
    invalidEnvelope(`${path}.history_status`);
  }
  uniqueTextList(aggregate.migration_notes, `${path}.migration_notes`);
  if (aggregate.history_status === 'baseline_only') {
    if (
      revisions.length !== 1
      || !revisions[0].legacyVersionUnresolvable
      || revisions[0].metricCount !== 0
      || bindings.length !== 0
    ) {
      invalidEnvelope(`${path}.history_status`);
    }
  } else if (revisions.some((revision) => revision.legacyVersion !== null)) {
    invalidEnvelope(`${path}.revisions`);
  }
  if (
    sourceBoardId !== null
    && identity.scope === 'inline'
    && identity.boardId !== sourceBoardId
  ) {
    invalidEnvelope(`${path}.identity.board_id`);
  }
  return {
    guidelineId: identity.guidelineId,
    containsSemanticMetrics: revisions.some(
      (revision) => revision.metricCount > 0,
    ),
    successorId: retirement?.status === 'superseded'
      ? retirement.successorId
      : null,
  };
}

function validateSuccessorGraph(
  aggregates: ValidatedAggregate[],
): void {
  const byId = new Map(
    aggregates.map((aggregate) => [aggregate.guidelineId, aggregate]),
  );
  for (const aggregate of aggregates) {
    const seen = new Set<string>();
    let cursor: ValidatedAggregate | undefined = aggregate;
    while (cursor) {
      if (seen.has(cursor.guidelineId)) {
        invalidEnvelope('guidelines.retirement.superseded_by_guideline_id');
      }
      seen.add(cursor.guidelineId);
      cursor = cursor.successorId
        ? byId.get(cursor.successorId)
        : undefined;
    }
  }
}

function validateEnvelope(
  value: unknown,
): {
  envelope: GuidelineExportEnvelopeV3;
  containsSemanticMetrics: boolean;
} {
  if (
    !isRecord(value)
    || !hasOnlyRequiredKeys(value, ENVELOPE_KEYS)
    || value.contract_version !== 'guideline-export/v3'
    || value.schema_version !== '3'
    || value.kind !== 'guidelines'
    || !Array.isArray(value.guidelines)
  ) {
    throw new Error('Select a guideline-export/v3 JSON file.');
  }
  timestamp(value.exported_at, 'exported_at');
  const sourceBoardId = nullableCanonicalText(
    value.source_board_id,
    'source_board_id',
  );
  digest(value.content_digest, 'content_digest');
  const aggregates = value.guidelines.map((aggregate, index) =>
    validateAggregate(
      aggregate,
      `guidelines[${index}]`,
      sourceBoardId,
    )
  );
  if (
    new Set(aggregates.map((aggregate) => aggregate.guidelineId)).size
    !== aggregates.length
  ) {
    invalidEnvelope('guidelines');
  }
  validateSuccessorGraph(aggregates);
  const envelope = value as unknown as GuidelineExportEnvelopeV3;
  return {
    envelope,
    containsSemanticMetrics: aggregates.some(
      (aggregate) => aggregate.containsSemanticMetrics,
    ),
  };
}

function parseEnvelope(raw: string): {
  envelope: GuidelineExportEnvelopeV3;
  containsSemanticMetrics: boolean;
} {
  try {
    return validateEnvelope(JSON.parse(raw) as unknown);
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error('Select a guideline-export/v3 JSON file.');
    }
    throw error;
  }
}

function validateImportResult(value: unknown): GuidelineImportResult {
  if (
    !isRecord(value)
    || !hasOnlyRequiredKeys(value, IMPORT_RESULT_KEYS, ['error_code'])
    || ![
      'planned',
      'dry_run',
      'committed',
      'rolled_back',
    ].includes(String(value.transaction_status))
    || ![
      value.created_count,
      value.skip_identical_count,
      value.conflict_count,
      value.overwritten_row_count,
    ].every((count) => Number.isInteger(count) && Number(count) >= 0)
    || typeof value.dry_run !== 'boolean'
    || (
      Object.prototype.hasOwnProperty.call(value, 'error_code')
      && value.error_code !== null
      && typeof value.error_code !== 'string'
    )
  ) {
    throw new Error('Guideline import returned an invalid result.');
  }
  return value as unknown as GuidelineImportResult;
}

function validateDryRun(value: unknown): GuidelineImportResult {
  const result = validateImportResult(value);
  if (!result.dry_run) {
    throw new Error('Guideline import preview did not run as a dry-run.');
  }
  if (
    result.transaction_status === 'rolled_back'
    || result.conflict_count > 0
    || result.overwritten_row_count > 0
  ) {
    throw new Error(
      result.error_code
      ?? `Import preview found ${result.conflict_count} conflict(s).`,
    );
  }
  if (result.transaction_status !== 'dry_run') {
    throw new Error('Guideline import preview returned an invalid status.');
  }
  return result;
}

function validateCommittedImport(value: unknown): GuidelineImportResult {
  const result = validateImportResult(value);
  if (
    result.dry_run
    || result.transaction_status !== 'committed'
    || result.conflict_count !== 0
    || result.overwritten_row_count !== 0
  ) {
    throw new Error(result.error_code ?? 'Guideline import did not commit.');
  }
  return result;
}

function validateExport(value: unknown): GuidelineExportEnvelopeV3 {
  try {
    return validateEnvelope(value).envelope;
  } catch {
    throw new Error('Guideline export returned an invalid v3 envelope.');
  }
}

function downloadEnvelope(
  boardId: string,
  envelope: GuidelineExportEnvelopeV3,
): void {
  const blob = new Blob(
    [JSON.stringify(envelope, null, 2)],
    { type: 'application/json' },
  );
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `guideline-policy-${boardId}.json`;
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}

export function GuidelinePolicyTransfer({
  boardId,
  onImported,
}: GuidelinePolicyTransferProps) {
  const api = usePolicyGovernanceApi();
  const permissions = usePermissions(boardId);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<'export' | 'import' | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const authorityReady =
    !permissions.isLoading
    && !permissions.error
    && !permissions.ownerReviewRequired;
  const canExport =
    authorityReady && permissions.has('guidelines.revisions.read');
  const canImport =
    authorityReady && permissions.has('guidelines.revisions.create');
  const canAuthorMetrics =
    authorityReady && permissions.has('guidelines.metrics.author');

  const exportPolicy = async () => {
    if (!canExport || busy) return;
    setBusy('export');
    setStatus(null);
    try {
      const envelope = validateExport(
        await api.exportGuidelinePolicy(boardId),
      );
      downloadEnvelope(boardId, envelope);
      setStatus(
        `Exported ${envelope.guidelines.length} guideline aggregate(s).`,
      );
    } catch (error) {
      toast.error(policyTransferError(error));
    } finally {
      setBusy(null);
    }
  };

  const importPolicy = async (file: File) => {
    if (!canImport || busy) return;
    setBusy('import');
    setStatus(null);
    try {
      const parsed = parseEnvelope(await file.text());
      if (parsed.containsSemanticMetrics && !canAuthorMetrics) {
        throw new Error(
          'Importing semantic metrics requires guidelines.metrics.author.',
        );
      }
      validateDryRun(await api.importGuidelinePolicy(
        boardId,
        parsed.envelope,
        { dryRun: true },
      ));
      const result = validateCommittedImport(
        await api.importGuidelinePolicy(boardId, parsed.envelope),
      );
      setStatus(
        `Imported ${result.created_count}; skipped `
        + `${result.skip_identical_count} identical aggregate(s).`,
      );
      await onImported();
    } catch (error) {
      toast.error(policyTransferError(error));
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = '';
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        disabled={!canExport || busy !== null}
        data-testid="guidelines-export"
        onClick={() => void exportPolicy()}
        title={
          canExport
            ? 'Export immutable semantic guideline policy'
            : 'Requires guidelines.revisions.read'
        }
        className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
      >
        <Download size={13} />
        {busy === 'export' ? 'Exporting…' : 'Export v3'}
      </button>
      <button
        type="button"
        disabled={!canImport || busy !== null}
        data-testid="guidelines-import"
        onClick={() => fileInputRef.current?.click()}
        title={
          canImport
            ? 'Dry-run and import immutable semantic guideline policy'
            : 'Requires guidelines.revisions.create'
        }
        className="inline-flex items-center gap-1 rounded-md border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-600 hover:bg-gray-50 disabled:opacity-40 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
      >
        <Upload size={13} />
        {busy === 'import' ? 'Importing…' : 'Import v3'}
      </button>
      <input
        ref={fileInputRef}
        type="file"
        accept="application/json,.json"
        data-testid="guidelines-import-input"
        className="hidden"
        aria-label="Import semantic guideline policy v3"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void importPolicy(file);
        }}
      />
      {status && (
        <span className="sr-only" role="status">
          {status}
        </span>
      )}
    </div>
  );
}
