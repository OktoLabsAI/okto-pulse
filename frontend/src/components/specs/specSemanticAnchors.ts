import type {
  SemanticAnchorV2,
  SemanticPinpoint,
} from '@/types/policy-governance';
import type { SemanticAnchorResolution } from '@/components/policy-compliance/semanticPolicyModel';

const SPEC_FIELDS = new Set([
  'acceptance_criteria',
  'api_contracts',
  'business_rules',
  'context',
  'decisions',
  'description',
  'functional_requirements',
  'integration_requirements',
  'labels',
  'observability_requirements',
  'status',
  'technical_requirements',
  'test_scenarios',
  'title',
]);

/**
 * Authorize anchors represented by the Spec modal. Stable structured-child
 * identifiers are accepted only when the modal has already loaded their text.
 */
export function resolveSpecSemanticAnchor(
  anchor: SemanticAnchorV2 | SemanticPinpoint,
  anchorTexts?: Readonly<Record<string, string>>,
): SemanticAnchorResolution {
  if (anchor.anchor_type === 'whole_artifact') {
    return { state: 'available', navigationTarget: 'spec:details:root' };
  }
  if (
    anchor.anchor_type === 'field'
    && anchor.anchor_ref !== null
    && SPEC_FIELDS.has(anchor.anchor_ref)
  ) {
    return {
      state: 'available',
      navigationTarget: `spec:field:${anchor.anchor_ref}`,
    };
  }
  if (
    anchor.anchor_type === 'structured_child'
    && anchor.anchor_ref !== null
    && anchorTexts?.[anchor.anchor_ref] !== undefined
  ) {
    return {
      state: 'available',
      navigationTarget: `spec:requirement:${anchor.anchor_ref}`,
    };
  }
  return { state: 'inaccessible' };
}
