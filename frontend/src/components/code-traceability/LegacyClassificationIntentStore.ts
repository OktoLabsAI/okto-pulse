import type { LegacyEvidenceClassificationBatchRequest } from '@/types';
import {
  attachLegacyClassificationIdempotencyKey,
  createLegacyClassificationIdempotencyKeyStore,
  type LegacyEvidenceClassificationIntent,
} from './sourceContextPresentation';

export interface ReviewedLegacyClassificationBatch {
  readonly intent: LegacyEvidenceClassificationIntent;
  readonly request: LegacyEvidenceClassificationBatchRequest;
  readonly serializedRequest: string;
}

export interface LegacyClassificationIntentStore {
  review(intent: LegacyEvidenceClassificationIntent): ReviewedLegacyClassificationBatch;
  exactRetry(): ReviewedLegacyClassificationBatch;
  clearReview(): void;
  invalidateReview(): void;
  clear(): void;
}

function freezeRequest(
  request: LegacyEvidenceClassificationBatchRequest,
): LegacyEvidenceClassificationBatchRequest {
  const items = request.items.map((item) => Object.freeze({
    ...item,
    baseline_provenance: Object.freeze({ ...item.baseline_provenance }),
  }));
  return Object.freeze({
    items: Object.freeze(items) as unknown as LegacyEvidenceClassificationBatchRequest['items'],
    justification: request.justification,
    idempotency_key: request.idempotency_key,
  });
}

function freezeIntent(
  intent: LegacyEvidenceClassificationIntent,
): LegacyEvidenceClassificationIntent {
  const items = intent.items.map((item) => Object.freeze({
    ...item,
    baseline_provenance: Object.freeze({ ...item.baseline_provenance }),
  }));
  return Object.freeze({
    items: Object.freeze(items) as unknown as LegacyEvidenceClassificationIntent['items'],
    justification: intent.justification,
  });
}

/**
 * Owns the reviewed transport intention independently from editable form state.
 * An ambiguous retry returns the same frozen request object. Editing after an
 * attempted submission or resolving a typed conflict explicitly invalidates
 * that review and its key before a new review is made.
 */
export function createLegacyClassificationIntentStore(
  createIdempotencyKey: () => string,
): LegacyClassificationIntentStore {
  const keys = createLegacyClassificationIdempotencyKeyStore(createIdempotencyKey);
  let reviewed: ReviewedLegacyClassificationBatch | null = null;

  return {
    review(intent) {
      // Detach the reviewed signature from caller-owned mutable form data. The
      // same immutable intent is used both to allocate and later forget the
      // idempotency key, so an adversarial mutation cannot orphan the original
      // signature or make a different payload reuse its key.
      const immutableIntent = freezeIntent(intent);
      const request = freezeRequest(attachLegacyClassificationIdempotencyKey(
        immutableIntent,
        keys.keyFor(immutableIntent),
      ));
      reviewed = Object.freeze({
        intent: immutableIntent,
        request,
        serializedRequest: JSON.stringify(request),
      });
      return reviewed;
    },
    exactRetry() {
      if (!reviewed) {
        throw new Error('legacy_evidence_classification_review_required');
      }
      return reviewed;
    },
    clearReview() {
      reviewed = null;
    },
    invalidateReview() {
      if (reviewed) keys.forget(reviewed.intent);
      reviewed = null;
    },
    clear() {
      reviewed = null;
      keys.clear();
    },
  };
}
