import type { FlagsMap } from './PermissionFlagsEditor';

export interface PermissionIntroductionManifest {
  readonly version: string;
  readonly leaves: readonly string[];
  readonly historicalAuthorities: Readonly<Record<string, string>>;
}

export interface ComposedPermissionIntroductions {
  readonly leaves: readonly string[];
  readonly historicalAuthorities: Readonly<Record<string, string>>;
}

/**
 * Compose versioned introductions in declared order and reject ambiguous
 * authority before any permission document is evaluated.
 *
 * Historical authorities must point to pre-manifest leaves. The current
 * resolver deliberately performs one authority check, so introduced-to-
 * introduced chains are rejected instead of being partially authorized.
 */
export function composePermissionIntroductionManifests(
  manifests: readonly PermissionIntroductionManifest[],
): ComposedPermissionIntroductions {
  const versions = new Set<string>();
  const allLeaves = new Set<string>();

  for (const manifest of manifests) {
    const version = manifest.version.trim();
    if (!version || versions.has(version)) {
      throw new Error('permission_introduction_manifest_version_invalid');
    }
    versions.add(version);
    for (const leaf of manifest.leaves) {
      if (!leaf.trim() || allLeaves.has(leaf)) {
        throw new Error('permission_introduction_leaf_collision');
      }
      allLeaves.add(leaf);
    }
  }

  const leaves: string[] = [];
  const historicalAuthorities: Record<string, string> = {};
  for (const manifest of manifests) {
    const leafSet = new Set(manifest.leaves);
    const authorityKeys = Object.keys(manifest.historicalAuthorities);
    if (
      authorityKeys.length !== leafSet.size
      || authorityKeys.some((leaf) => !leafSet.has(leaf))
    ) {
      throw new Error('permission_introduction_authority_set_mismatch');
    }
    for (const leaf of manifest.leaves) {
      const authority = manifest.historicalAuthorities[leaf]?.trim();
      if (
        !authority
        || authority === leaf
        || allLeaves.has(authority)
      ) {
        throw new Error('permission_introduction_authority_invalid');
      }
      leaves.push(leaf);
      historicalAuthorities[leaf] = authority;
    }
  }

  return Object.freeze({
    leaves: Object.freeze(leaves),
    historicalAuthorities: Object.freeze(historicalAuthorities),
  });
}

export const SKA_PERMISSION_INTRODUCTION_V1 = {
  version: 'SK-A/v1',
  leaves: [
    'ideation.quality.read',
    'ideation.quality.assess',
    'refinement.quality.read',
    'refinement.quality.assess',
    'spec.quality.read',
    'spec.quality.assess',
    'refinement.research_decisions.read',
    'refinement.research_decisions.append',
    'spec.checklist.read',
    'spec.checklist.execute',
  ],
  historicalAuthorities: {
    'ideation.quality.read': 'ideation.entity.read',
    'ideation.quality.assess': 'spec.entity.edit_fields',
    'refinement.quality.read': 'refinement.entity.read',
    'refinement.quality.assess': 'spec.entity.edit_fields',
    'spec.quality.read': 'spec.entity.read',
    'spec.quality.assess': 'spec.validation.submit',
    'refinement.research_decisions.read': 'refinement.entity.read',
    'refinement.research_decisions.append': 'spec.entity.edit_fields',
    'spec.checklist.read': 'spec.entity.read',
    'spec.checklist.execute': 'spec.entity.edit_fields',
  },
} as const satisfies PermissionIntroductionManifest;

export const SKB_PERMISSION_INTRODUCTION_V1 = {
  version: 'SK-B3/v1',
  leaves: [
    'guidelines.revisions.read',
    'guidelines.revisions.create',
    'guidelines.revisions.retire',
    'guidelines.metrics.author',
    'guidelines.impact.preview',
    'guidelines.adoption.manage',
    'guidelines.assessments.read',
    'guidelines.assessments.record',
    'guidelines.waiver.read',
    'guidelines.waiver.request',
    'guidelines.waiver.review',
    'guidelines.waiver.revoke',
    'guidelines.waiver.revalidate',
  ],
  historicalAuthorities: {
    'guidelines.revisions.read': 'guidelines.read',
    'guidelines.revisions.create': 'spec.entity.edit_fields',
    'guidelines.revisions.retire': 'guidelines.delete',
    'guidelines.metrics.author': 'spec.entity.edit_fields',
    'guidelines.impact.preview': 'guidelines.read',
    'guidelines.adoption.manage': 'spec.entity.edit_fields',
    'guidelines.assessments.read': 'guidelines.read',
    'guidelines.assessments.record': 'guidelines.read',
    'guidelines.waiver.read': 'guidelines.read',
    'guidelines.waiver.request': 'guidelines.read',
    'guidelines.waiver.review': 'spec.validation.submit',
    'guidelines.waiver.revoke': 'guidelines.delete',
    'guidelines.waiver.revalidate': 'spec.validation.submit',
  },
} as const satisfies PermissionIntroductionManifest;

/**
 * Separate introduction for the human-only legacy Evidence classification
 * action.  It intentionally does not extend CODE-TRACEABILITY/v1: that
 * manifest remains pinned to its original 22 explicit-grant leaves.
 */
export const CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1 = {
  version: 'CODE-EVIDENCE-LEGACY-CLASSIFICATION/v1',
  leaves: [
    'code_traceability.evidence.classify_legacy',
  ],
  historicalAuthorities: {
    'code_traceability.evidence.classify_legacy': 'spec.entity.edit_fields',
  },
} as const satisfies PermissionIntroductionManifest;

export const PERMISSION_INTRODUCTION_MANIFESTS = [
  SKA_PERMISSION_INTRODUCTION_V1,
  SKB_PERMISSION_INTRODUCTION_V1,
  CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1,
] as const satisfies readonly PermissionIntroductionManifest[];

export const SKA_PERMISSION_INTRODUCTION_V1_LEAVES =
  SKA_PERMISSION_INTRODUCTION_V1.leaves;
export const SKB_PERMISSION_INTRODUCTION_V1_LEAVES =
  SKB_PERMISSION_INTRODUCTION_V1.leaves;
export const CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1_LEAVES =
  CODE_EVIDENCE_LEGACY_CLASSIFICATION_PERMISSION_INTRODUCTION_V1.leaves;

/**
 * Exact explicit-grant leaves introduced by Core's CODE-TRACEABILITY/v1
 * manifest. Unlike SK-A/SK-B migration leaves, these have no historical
 * fallback authority: absence must remain denied for every preset lineage.
 */
export const CODE_TRACEABILITY_PERMISSION_INTRODUCTION_V1_LEAVES = [
  'code_traceability.investigation.start',
  'code_traceability.investigation.read',
  'code_traceability.investigation.receipt_submit',
  'code_traceability.investigation.revoke',
  'code_traceability.evidence.read',
  'code_traceability.evidence.submit',
  'code_traceability.evidence.supersede',
  'code_traceability.evidence.revoke',
  'code_traceability.spec_link.create',
  'code_traceability.spec_link.delete',
  'code_traceability.spec_link.set_disposition',
  'code_traceability.spec_link.rebase',
  'code_traceability.target.read',
  'code_traceability.target.suggest',
  'code_traceability.target.create',
  'code_traceability.target.edit',
  'code_traceability.target.resolution_submit',
  'code_traceability.target.execution_submit',
  'code_traceability.overlap.read',
  'code_traceability.overlap.acknowledge',
  'code_traceability.waiver.create',
  'code_traceability.waiver.clear',
] as const;

const COMPOSED_PERMISSION_INTRODUCTIONS =
  composePermissionIntroductionManifests(
    PERMISSION_INTRODUCTION_MANIFESTS,
  );

export const INTRODUCED_PERMISSION_LEAVES: readonly string[] =
  COMPOSED_PERMISSION_INTRODUCTIONS.leaves;

export const INTRODUCED_PERMISSION_HISTORICAL_AUTHORITIES:
Readonly<Record<string, string>> = (
  COMPOSED_PERMISSION_INTRODUCTIONS.historicalAuthorities
);

const STATIC_INTRODUCED_PERMISSION_LEAVES = new Set(
  INTRODUCED_PERMISSION_LEAVES,
);

const POST_SKB_INTRODUCED_PREFIXES = [
  'agent.',
  'board.admin.',
  'board.share.',
  'permission_preset.',
  'default_board_config.',
  'design_system.',
  'runtime.',
  'metrics.',
  'amendment.',
  'kg.operations.',
  'ideation.knowledge.',
  'story.mockups.',
  'test_scenario.interact_in.',
  // Core introduces this namespace through an explicit-grant manifest. Keep
  // future leaves fail-closed even before the frontend consumes them.
  'code_traceability.',
] as const;

const POST_SKB_INTRODUCED_EXACT_LEAVES = new Set([
  'ideation.qa.delete',
  'refinement.qa.delete',
  'spec.qa.delete',
  'sprint.qa.delete',
  'spec.tests.execute',
  'spec.tests.edit',
  'spec.tests.delete',
  'spec.entity.manage_dependencies',
  'sprint.tasks.assign',
  'ideation.interact_in.review',
  'ideation.interact_in.approved',
  'card.interact_in.rejected',
]);

const SDLC_TRANSITION_ENTITIES = new Set([
  'story',
  'ideation',
  'refinement',
  'spec',
  'card',
  'sprint',
  'test_scenario',
]);

// Exact transition leaves that predate the SDLC-registry projection. Core
// deliberately keeps these outside the fail-closed introduction manifest so
// historical snapshots and board ceilings retain their original semantics.
const PRE_REGISTRY_TRANSITION_PERMISSION_LEAVES = new Set([
  'card.move.in_progress_to_done',
  'card.move.in_progress_to_on_hold',
  'card.move.in_progress_to_validation',
  'card.move.not_started_to_started',
  'card.move.on_hold_to_in_progress',
  'card.move.started_to_in_progress',
  'card.move.validation_to_cancelled',
  'card.move.validation_to_done',
  'card.move.validation_to_on_hold',
  'refinement.move.approved_to_done',
  'refinement.move.review_to_approved',
  'spec.move.approved_to_draft',
  'spec.move.approved_to_validated',
  'spec.move.draft_to_review',
  'spec.move.in_progress_to_done',
  'spec.move.review_to_approved',
  'spec.move.validated_to_draft',
  'spec.move.validated_to_in_progress',
  'sprint.move.active_to_review',
  'sprint.move.draft_to_active',
  'sprint.move.review_to_closed',
  'story.move.draft_to_ready',
  'story.move.draft_to_triage',
  'story.move.ready_to_triage',
  'story.move.triage_to_draft',
  'story.move.triage_to_ready',
]);

/** Keep the client fail-closed for every post-SK-B manifest generation. */
export function isIntroducedPermissionLeaf(path: string): boolean {
  if (
    STATIC_INTRODUCED_PERMISSION_LEAVES.has(path)
    || POST_SKB_INTRODUCED_EXACT_LEAVES.has(path)
    || POST_SKB_INTRODUCED_PREFIXES.some((prefix) => path.startsWith(prefix))
  ) {
    return true;
  }
  const [entity, branch] = path.split('.', 3);
  return branch === 'move'
    && SDLC_TRANSITION_ENTITIES.has(entity)
    && !PRE_REGISTRY_TRANSITION_PERMISSION_LEAVES.has(path);
}

type PermissionDocument = Record<string, unknown>;

function isDocument(value: unknown): value is PermissionDocument {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function cloneDocument<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function isStrictPermissionDocument(value: unknown): value is PermissionDocument {
  if (!isDocument(value)) return false;
  return Object.values(value).every(
    (child) => typeof child === 'boolean'
      || (isDocument(child) && isStrictPermissionDocument(child)),
  );
}

function setNested(
  document: PermissionDocument,
  path: string,
  value: boolean,
): void {
  const parts = path.split('.');
  let current = document;
  for (const part of parts.slice(0, -1)) {
    const child = current[part];
    if (!isDocument(child)) {
      current[part] = {};
    }
    current = current[part] as PermissionDocument;
  }
  current[parts.at(-1)!] = value;
}

function getNested(
  document: PermissionDocument,
  path: string,
): { present: boolean; value: unknown } {
  let current: unknown = document;
  for (const part of path.split('.')) {
    if (!isDocument(current) || !(part in current)) {
      return { present: false, value: undefined };
    }
    current = current[part];
  }
  return { present: true, value: current };
}

function booleanLeaves(
  document: PermissionDocument,
  prefix = '',
): Array<[string, boolean]> {
  const leaves: Array<[string, boolean]> = [];
  for (const [key, value] of Object.entries(document)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === 'boolean') {
      leaves.push([path, value]);
    } else if (isDocument(value)) {
      leaves.push(...booleanLeaves(value, path));
    }
  }
  return leaves;
}

export function allPermissionsDenied(
  source: PermissionDocument,
): PermissionDocument {
  const denied: PermissionDocument = {};
  for (const [path] of booleanLeaves(source)) {
    setNested(denied, path, false);
  }
  return denied;
}

function overlayInto(
  target: PermissionDocument,
  overrides: PermissionDocument,
): void {
  for (const [key, value] of Object.entries(overrides)) {
    if (typeof value === 'boolean') {
      target[key] = value;
    } else if (isDocument(value)) {
      const current = isDocument(target[key]) ? target[key] : {};
      target[key] = current;
      overlayInto(current, value);
    }
  }
}

/** Apply a sparse direct agent delta over its resolved preset/Full Control base. */
export function applyPermissionDelta(
  base: PermissionDocument,
  delta: unknown,
): FlagsMap {
  if (!isStrictPermissionDocument(base)) return {} as FlagsMap;
  if (delta !== null && delta !== undefined && !isStrictPermissionDocument(delta)) {
    return allPermissionsDenied(base) as FlagsMap;
  }
  const effective = cloneDocument(base);
  if (isStrictPermissionDocument(delta)) overlayInto(effective, delta);
  return effective as FlagsMap;
}

/** Compute the sparse direct agent delta for a desired effective document. */
export function permissionDelta(
  base: PermissionDocument,
  desired: PermissionDocument,
): PermissionDocument {
  const result: PermissionDocument = {};
  for (const [key, desiredValue] of Object.entries(desired)) {
    const baseValue = base[key];
    if (typeof desiredValue === 'boolean') {
      if (desiredValue !== baseValue) result[key] = desiredValue;
    } else if (isDocument(desiredValue)) {
      const nested = permissionDelta(
        isDocument(baseValue) ? baseValue : {},
        desiredValue,
      );
      if (Object.keys(nested).length > 0) result[key] = nested;
    }
  }
  return result;
}

/** Apply the raw board ceiling with the same missing-introduced-leaf rule as Core. */
export function applyBoardCeiling(
  base: PermissionDocument,
  ceiling: unknown,
): FlagsMap {
  if (ceiling === null || ceiling === undefined) {
    return cloneDocument(base) as FlagsMap;
  }
  if (!isStrictPermissionDocument(ceiling)) {
    return allPermissionsDenied(base) as FlagsMap;
  }

  const effective = cloneDocument(base);
  for (const [path, value] of booleanLeaves(ceiling)) {
    if (value === false) setNested(effective, path, false);
  }
  for (const [path] of booleanLeaves(base)) {
    if (!isIntroducedPermissionLeaf(path)) continue;
    if (
      getNested(base, path).present
      && getNested(ceiling, path).value !== true
    ) {
      setNested(effective, path, false);
    }
  }
  return effective as FlagsMap;
}

/**
 * Convert a desired board-effective document to the minimal ceiling.
 *
 * A null ceiling means no restriction. Once a ceiling exists, introduced
 * leaves that stay enabled must be explicitly admitted with True because
 * Core treats an absent introduced ceiling leaf as denied.
 */
export function boardCeilingDelta(
  base: PermissionDocument,
  desired: PermissionDocument,
): PermissionDocument | null {
  const restrictions: string[] = [];
  for (const [path, baseValue] of booleanLeaves(base)) {
    const desiredValue = getNested(desired, path);
    if (baseValue === true && desiredValue.value !== true) {
      restrictions.push(path);
    }
  }
  if (restrictions.length === 0) return null;

  const ceiling: PermissionDocument = {};
  for (const path of restrictions) setNested(ceiling, path, false);
  for (const [path] of booleanLeaves(base)) {
    if (!isIntroducedPermissionLeaf(path)) continue;
    const baseValue = getNested(base, path).value;
    const desiredValue = getNested(desired, path).value;
    if (baseValue === true && desiredValue === true) {
      setNested(ceiling, path, true);
    }
  }
  return ceiling;
}
