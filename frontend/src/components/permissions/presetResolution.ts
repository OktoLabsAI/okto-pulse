import type { PermissionPreset } from '@/types';
import { setAllFlags } from './PermissionFlagsEditor';
import type { FlagsMap } from './PermissionFlagsEditor';

const FULL_CONTROL_KEY = 'fullcontrol';
const FULL_CONTROL_ID_KEYS = new Set([
  FULL_CONTROL_KEY,
  'builtinfullcontrol',
  'permissionpresetfullcontrol',
  'presetfullcontrol',
]);

function identityKey(value: string): string {
  return value.trim().toLocaleLowerCase('en-US').replace(/[^a-z0-9]+/g, '');
}

function hasFlags(preset: PermissionPreset): boolean {
  return (
    preset.flags !== null
    && typeof preset.flags === 'object'
    && !Array.isArray(preset.flags)
  );
}

/**
 * Locate the trusted built-in Full Control preset independently of API order.
 *
 * Stable textual identities are preferred, then the canonical built-in name.
 * A custom preset named "Full Control" is never accepted as the sentinel.
 */
export function findFullControlPreset(
  presets: readonly PermissionPreset[],
): PermissionPreset | null {
  const builtIns = presets.filter((preset) => preset.is_builtin);
  return (
    builtIns.find((preset) => FULL_CONTROL_ID_KEYS.has(identityKey(preset.id)))
    ?? builtIns.find((preset) => identityKey(preset.name) === FULL_CONTROL_KEY)
    ?? null
  );
}

export type PresetLineageState =
  | 'root'
  | 'resolved'
  | 'dangling'
  | 'cycle'
  | 'owner_review';

export interface PresetLineagePresentation {
  directBase: PermissionPreset | null;
  chain: readonly PermissionPreset[];
  state: PresetLineageState;
  stateLabel: string;
  baseLabel: string;
  chainLabel: string;
  ownerReviewRequired: boolean;
  reviewReason: string | null;
  canResetToBase: boolean;
}

function stateFromReviewReason(
  reviewReason: string | null,
): PresetLineageState | null {
  if (reviewReason === 'preset_lineage_cycle') return 'cycle';
  if (
    reviewReason === 'dangling_base_preset'
    || reviewReason === 'unknown_preset'
  ) {
    return 'dangling';
  }
  return reviewReason ? 'owner_review' : null;
}

function stateLabel(state: PresetLineageState): string {
  switch (state) {
    case 'root':
      return 'independent root';
    case 'resolved':
      return 'lineage resolved';
    case 'dangling':
      return 'dangling base';
    case 'cycle':
      return 'lineage cycle';
    case 'owner_review':
      return 'owner review';
  }
}

/** Resolve UI lineage without trusting list order or hiding malformed chains. */
export function resolvePresetLineage(
  preset: PermissionPreset,
  presets: readonly PermissionPreset[],
): PresetLineagePresentation {
  const duplicateIds = new Set<string>();
  const byId = new Map<string, PermissionPreset>();
  for (const candidate of presets) {
    if (byId.has(candidate.id)) duplicateIds.add(candidate.id);
    else byId.set(candidate.id, candidate);
  }

  const directBase = preset.base_preset_id
    ? byId.get(preset.base_preset_id) ?? null
    : null;
  const chain: PermissionPreset[] = [preset];
  const seen = new Set<string>();
  let current = preset;
  let detectedState: PresetLineageState = preset.base_preset_id
    ? 'resolved'
    : 'root';
  let derivedReason: string | null = null;
  let terminalLabel: string | null = null;

  while (current.base_preset_id) {
    if (duplicateIds.has(current.id)) {
      detectedState = 'owner_review';
      derivedReason = 'duplicate_preset_id';
      break;
    }
    if (seen.has(current.id)) {
      detectedState = 'cycle';
      derivedReason = 'preset_lineage_cycle';
      terminalLabel = current.name;
      break;
    }
    seen.add(current.id);
    const next = byId.get(current.base_preset_id);
    if (!next) {
      detectedState = 'dangling';
      derivedReason = 'dangling_base_preset';
      terminalLabel = current.base_preset_id;
      break;
    }
    if (seen.has(next.id)) {
      detectedState = 'cycle';
      derivedReason = 'preset_lineage_cycle';
      terminalLabel = next.name;
      break;
    }
    chain.push(next);
    current = next;
  }

  const inheritedReview = chain.find(
    (candidate) => candidate.owner_review_required,
  );
  const reportedReviewReason = (
    preset.review_reason
    ?? inheritedReview?.review_reason
    ?? null
  );
  const backendState = stateFromReviewReason(reportedReviewReason);
  const state = (
    backendState === 'cycle'
    || backendState === 'dangling'
    || detectedState === 'cycle'
    || detectedState === 'dangling'
  )
    ? (
        backendState === 'cycle' || detectedState === 'cycle'
          ? 'cycle'
          : 'dangling'
      )
    : (
        preset.owner_review_required
        || inheritedReview !== undefined
        || backendState === 'owner_review'
      )
      ? 'owner_review'
      : detectedState;
  const reviewReason = reportedReviewReason ?? derivedReason;
  const ownerReviewRequired = (
    preset.owner_review_required
    || inheritedReview !== undefined
    || state === 'dangling'
    || state === 'cycle'
    || state === 'owner_review'
  );
  const chainNames = chain.map((candidate) => candidate.name);
  if (terminalLabel) {
    chainNames.push(
      state === 'cycle' ? `↻ ${terminalLabel}` : `missing: ${terminalLabel}`,
    );
  }

  return {
    directBase,
    chain,
    state,
    stateLabel: stateLabel(state),
    baseLabel: (
      directBase?.name
      ?? preset.base_preset_id
      ?? 'No base'
    ),
    chainLabel: chainNames.join(' → '),
    ownerReviewRequired,
    reviewReason,
    canResetToBase: directBase !== null
      && state !== 'cycle'
      && state !== 'dangling',
  };
}

export interface AgentPermissionBase {
  preset: PermissionPreset | null;
  flags: FlagsMap | null;
  label: string;
  ownerReviewRequired: boolean;
  reviewReason: string | null;
}

/**
 * Resolve the actual base for an agent.
 *
 * ``presetId=null`` is the trusted Full Control sentinel.  It must resolve the
 * real built-in preset and never fall back to the first list entry.
 */
export function resolveAgentPermissionBase(
  presetId: string | null | undefined,
  presets: readonly PermissionPreset[],
): AgentPermissionBase {
  const preset = presetId
    ? presets.find((candidate) => candidate.id === presetId) ?? null
    : findFullControlPreset(presets);
  if (!preset) {
    return {
      preset: null,
      flags: null,
      label: presetId ? `Unknown preset (${presetId})` : 'Full Control',
      ownerReviewRequired: true,
      reviewReason: presetId
        ? 'unknown_preset'
        : 'full_control_preset_missing',
    };
  }
  const lineage = resolvePresetLineage(preset, presets);
  return {
    preset,
    flags: hasFlags(preset)
      ? preset.flags as unknown as FlagsMap
      : null,
    label: preset.name,
    ownerReviewRequired: lineage.ownerReviewRequired || !hasFlags(preset),
    reviewReason: lineage.reviewReason
      ?? (hasFlags(preset) ? null : 'invalid_preset_flags'),
  };
}

/** Build the disabled new-preset template from the real Full Control shape. */
export function disabledFullControlTemplate(
  presets: readonly PermissionPreset[],
): FlagsMap | null {
  const fullControl = findFullControlPreset(presets);
  if (!fullControl || !hasFlags(fullControl)) return null;
  return setAllFlags(
    fullControl.flags as unknown as FlagsMap,
    false,
  );
}
