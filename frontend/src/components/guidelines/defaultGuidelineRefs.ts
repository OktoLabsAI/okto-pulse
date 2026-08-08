import type {
  DefaultBoardConfigGuidelineRef,
  DefaultGuidelineCandidate,
  DefaultGuidelineRevisionPin,
} from '@/types';

function requireRevisionPin(
  pin: DefaultGuidelineRevisionPin | null | undefined,
): DefaultGuidelineRevisionPin {
  if (
    !pin
    || !pin.revision_id?.trim()
    || !Number.isInteger(pin.revision_number)
    || pin.revision_number < 1
    || !pin.semantic_version?.trim()
    || !/^[0-9a-f]{64}$/.test(pin.revision_digest)
  ) {
    throw new Error('default_guideline_revision_pin_invalid');
  }
  return {
    revision_id: pin.revision_id,
    revision_number: pin.revision_number,
    semantic_version: pin.semantic_version,
    revision_digest: pin.revision_digest,
  };
}

export function canonicalDefaultGuidelineRef(
  ref: DefaultBoardConfigGuidelineRef,
): DefaultBoardConfigGuidelineRef {
  if (
    !ref.guideline_id?.trim()
    || !Number.isInteger(ref.priority)
    || ref.priority < 0
  ) {
    throw new Error('default_guideline_ref_invalid');
  }
  return {
    guideline_id: ref.guideline_id,
    priority: ref.priority,
    ...requireRevisionPin(ref),
  };
}

export function defaultGuidelineRefFromCandidate(
  candidate: DefaultGuidelineCandidate,
  source: 'default' | 'head',
  priority: number,
): DefaultBoardConfigGuidelineRef {
  if (
    source === 'head'
    && (!candidate.eligible || candidate.retired)
  ) {
    throw new Error('default_guideline_candidate_ineligible');
  }
  return canonicalDefaultGuidelineRef({
    guideline_id: candidate.guideline_id,
    priority,
    ...requireRevisionPin(
      source === 'default'
        ? candidate.default_revision
        : candidate.head_revision,
    ),
  });
}

export function currentDefaultGuidelineRefs(
  candidates: readonly DefaultGuidelineCandidate[],
): DefaultBoardConfigGuidelineRef[] {
  return candidates
    .filter((candidate) => candidate.is_default)
    .map((candidate) =>
      defaultGuidelineRefFromCandidate(
        candidate,
        'default',
        candidate.priority ?? 0,
      ),
    )
    .sort((left, right) =>
      left.priority - right.priority
      || left.guideline_id.localeCompare(right.guideline_id),
    );
}

export function canonicalDefaultGuidelineRefs(
  refs: readonly DefaultBoardConfigGuidelineRef[],
): DefaultBoardConfigGuidelineRef[] {
  const normalized = refs.map(canonicalDefaultGuidelineRef);
  if (
    new Set(normalized.map((ref) => ref.guideline_id)).size
    !== normalized.length
  ) {
    throw new Error('default_guideline_duplicate');
  }
  return normalized.sort((left, right) =>
    left.priority - right.priority
    || left.guideline_id.localeCompare(right.guideline_id),
  );
}
