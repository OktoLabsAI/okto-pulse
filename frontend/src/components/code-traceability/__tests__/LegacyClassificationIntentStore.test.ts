import { describe, expect, it, vi } from 'vitest';
import type { LegacyEvidenceClassificationIntent } from '../sourceContextPresentation';
import { createLegacyClassificationIntentStore } from '../LegacyClassificationIntentStore';

function intent(): LegacyEvidenceClassificationIntent {
  return {
    items: [{
      evidence_id: 'evidence-1',
      expected_evidence_payload_sha256: 'a'.repeat(64),
      expected_classification_revision: 2,
      source_role: 'existing_scaffold',
      relevance_summary: 'The shell establishes the intended module boundary.',
      scope_relation: 'The delivery extends the generated service shell.',
      source_origin: 'Committed application scaffold.',
      interpretation_limit: 'The shell does not describe runtime behavior.',
      baseline_provenance: {
        presence: 'committed_snapshot',
        workspace_state_id: 'sha256:workspace-1',
        provenance_note: null,
      },
    }],
    justification: 'Reviewed against the frozen baseline.',
  };
}

describe('LegacyClassificationIntentStore', () => {
  it('ts_8b303869 — replays the exact frozen request after an ambiguous outcome', () => {
    const createKey = vi.fn()
      .mockReturnValueOnce('classification-key-1');
    const store = createLegacyClassificationIntentStore(createKey);

    const reviewed = store.review(intent());
    const retry = store.exactRetry();

    expect(retry).toBe(reviewed);
    expect(retry.request).toBe(reviewed.request);
    expect(JSON.stringify(retry.request)).toBe(reviewed.serializedRequest);
    expect(Object.isFrozen(retry.request)).toBe(true);
    expect(Object.isFrozen(retry.request.items)).toBe(true);
    expect(Object.isFrozen(retry.request.items[0])).toBe(true);
    expect(Object.isFrozen(retry.intent)).toBe(true);
    expect(Object.isFrozen(retry.intent.items)).toBe(true);
    expect(Object.isFrozen(retry.intent.items[0])).toBe(true);
    expect(Object.isFrozen(retry.intent.items[0].baseline_provenance)).toBe(true);
    expect(createKey).toHaveBeenCalledTimes(1);
  });

  it('ts_8b303869 — protects the original signature from caller and reviewed-intent mutation', () => {
    const createKey = vi.fn()
      .mockReturnValueOnce('classification-key-1')
      .mockReturnValueOnce('classification-key-2');
    const store = createLegacyClassificationIntentStore(createKey);
    const callerOwned = intent();
    const reviewed = store.review(callerOwned);

    callerOwned.items[0].relevance_summary = 'Caller mutation after review.';
    expect(() => {
      (reviewed.intent.items[0] as { relevance_summary: string }).relevance_summary =
        'Adversarial reviewed-intent mutation.';
    }).toThrow(TypeError);
    expect(store.exactRetry().serializedRequest).toBe(reviewed.serializedRequest);

    store.invalidateReview();
    const rereviewed = store.review(intent());
    expect(rereviewed.request.idempotency_key).toBe('classification-key-2');
    expect(createKey).toHaveBeenCalledTimes(2);
  });

  it('ts_8b303869 — keeps the same key across exact retry and rotates it after an edit or conflict', () => {
    const createKey = vi.fn()
      .mockReturnValueOnce('classification-key-1')
      .mockReturnValueOnce('classification-key-2')
      .mockReturnValueOnce('classification-key-3');
    const store = createLegacyClassificationIntentStore(createKey);

    const first = store.review(intent());
    store.clearReview();
    const ordinaryRereview = store.review(intent());
    expect(ordinaryRereview.request.idempotency_key).toBe('classification-key-1');

    store.invalidateReview();
    const afterConflict = store.review(intent());
    expect(afterConflict.request.idempotency_key).toBe('classification-key-2');

    store.clear();
    const afterClear = store.review(intent());
    expect(afterClear.request.idempotency_key).toBe('classification-key-3');
    expect(first.request.idempotency_key).toBe('classification-key-1');
  });

  it('ts_4822298b — requires a completed immutable review before retry', () => {
    const store = createLegacyClassificationIntentStore(() => 'classification-key-1');
    expect(() => store.exactRetry()).toThrow(
      'legacy_evidence_classification_review_required',
    );
  });
});
