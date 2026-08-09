import type {
  SemanticAnchorV2,
  SemanticPinpoint,
} from '@/types/policy-governance';
import type { SemanticAnchorResolution } from '@/components/policy-compliance/semanticPolicyModel';

const CARD_DETAILS_FIELDS = new Set([
  'assignee_id',
  'description',
  'details',
  'due_date',
  'labels',
  'priority',
  'status',
  'title',
]);

/**
 * Authorize only anchors whose content is already visible in CardModal's
 * Details tab. Opaque child and Q&A references remain fail-closed until their
 * owning tab supplies a resolver with equivalent read authority.
 */
export function resolveCardSemanticAnchor(
  anchor: SemanticAnchorV2 | SemanticPinpoint,
): SemanticAnchorResolution {
  if (anchor.anchor_type === 'whole_artifact') {
    return { state: 'available', navigationTarget: 'card:details:root' };
  }
  if (
    anchor.anchor_type === 'field'
    && anchor.anchor_ref !== null
    && CARD_DETAILS_FIELDS.has(anchor.anchor_ref)
  ) {
    return {
      state: 'available',
      navigationTarget: `card:details:${anchor.anchor_ref}`,
    };
  }
  return { state: 'inaccessible' };
}

