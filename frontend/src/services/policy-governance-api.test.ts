import { describe, expect, it, vi } from 'vitest';

import {
  createPolicyGovernanceApi,
  PolicyGovernanceApiError,
  type PolicyGovernanceTransport,
} from './policy-governance-api';
import {
  isPolicyActionEnabled,
  type PolicyActionState,
  type PolicyEntityType,
  type PolicyErrorDetail,
} from '@/types/policy-governance';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function setup() {
  const fetch = vi.fn();
  const transport: PolicyGovernanceTransport = { fetch };
  fetch.mockImplementation(async () => jsonResponse({}));
  return { api: createPolicyGovernanceApi(transport), fetch };
}

function requestBody(fetch: ReturnType<typeof vi.fn>, index: number): unknown {
  return JSON.parse(String((fetch.mock.calls[index][1] as RequestInit).body));
}

describe('policy-governance-api', () => {
  it('keeps keyset cursors opaque and binds them to projection and filters', async () => {
    const { api, fetch } = setup();
    const controller = new AbortController();
    fetch.mockResolvedValue(
      jsonResponse({
        items: [],
        limit: 25,
        has_more: false,
        next_cursor: null,
      }),
    );

    await api.listGuidelineRevisions('board/one', 'guide/one', {
      limit: 25,
      cursor: 'opaque/+== cursor',
      projection: 'detail',
      signal: controller.signal,
    });

    const [url, options] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      '/boards/board%2Fone/guidelines/guide%2Fone/revisions'
        + '?limit=25&projection=detail&cursor=opaque%2F%2B%3D%3D+cursor',
    );
    expect(options.signal).toBe(controller.signal);
    expect(new Headers(options.headers).get('Accept')).toBe(
      'application/json',
    );
  });

  it('covers revision, retirement, impact, and adoption without client-owned fences', async () => {
    const { api, fetch } = setup();

    await api.createGuidelineRevision('board-1', 'guide-1', {
      idempotency_key: 'revision-2',
      patch: {
        title: 'Second revision',
        rules: [
          {
            rule_id: 'rule-1',
            code: 'require_title',
            title: 'Require title',
            description: 'Requires a title.',
            target_entity_types: ['spec', 'test_scenario'],
            predicates: [
              {
                predicate_code: 'field_present',
                parameters: { field: 'title' },
              },
            ],
          },
        ],
      },
    });
    await api.getGuidelineRevision(
      'board-1',
      'guide-1',
      'revision/2',
    );
    await api.retireGuideline('board-1', 'guide-1', {
      retirement_id: 'retirement-1',
      reason: 'Superseded',
      status: 'superseded',
      superseded_by_guideline_id: 'guide-2',
      idempotency_key: 'retire-1',
    });
    await api.previewGuidelineImpact('board-1', 'guide-1', {
      proposed_priority: 7,
      proposed_default_enforcement: 'blocking',
      to_revision_id: 'revision-2',
      idempotency_key: 'impact-1',
    });
    await api.getGuidelineImpact(
      'board-1',
      'guide-1',
      'receipt/1',
    );
    await api.listGuidelineImpactItems(
      'board-1',
      'guide-1',
      'receipt/1',
      {
        limit: 50,
        projection: 'summary',
        entityType: 'test_scenario',
        itemKind: 'target',
      },
    );
    await api.adoptGuidelineRevision('board-1', 'guide-1', {
      impact_receipt_id: 'receipt-1',
      impact_digest: 'a'.repeat(64),
      idempotency_key: 'adopt-1',
    });

    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      '/boards/board-1/guidelines/guide-1/revisions',
      '/boards/board-1/guidelines/guide-1/revisions/revision%2F2',
      '/boards/board-1/guidelines/guide-1/retire',
      '/boards/board-1/guidelines/guide-1/impact-previews',
      '/boards/board-1/guidelines/guide-1/impact-previews/receipt%2F1',
      '/boards/board-1/guidelines/guide-1/impact-previews/receipt%2F1/items'
        + '?limit=50&projection=summary'
        + '&entity_type=test_scenario&item_kind=target',
      '/boards/board-1/guidelines/guide-1/adoptions',
    ]);
    expect(requestBody(fetch, 0)).not.toHaveProperty(
      'expected_head_revision',
    );
    expect(requestBody(fetch, 3)).not.toHaveProperty(
      'expected_binding_revision',
    );
    expect(requestBody(fetch, 6)).toEqual({
      impact_receipt_id: 'receipt-1',
      impact_digest: 'a'.repeat(64),
      idempotency_key: 'adopt-1',
    });
  });

  it('uses the server-owned subject version and digest contract for test scenarios', async () => {
    const { api, fetch } = setup();
    fetch.mockResolvedValue(
      jsonResponse({
        evaluation: {
          evaluation_id: 'evaluation-1',
          input_digest: 'b'.repeat(64),
          receipt: {
            subject: {
              board_id: 'board-1',
              entity_type: 'test_scenario',
              subject_id: 'scenario-1',
              subject_version: 4,
            },
            subject_content_digest: 'c'.repeat(64),
          },
        },
      }),
    );

    const response = await api.evaluatePolicyCompliance('board-1', {
      entity_type: 'test_scenario',
      subject_id: 'scenario-1',
      idempotency_key: 'evaluate-1',
    });

    expect(requestBody(fetch, 0)).toEqual({
      entity_type: 'test_scenario',
      subject_id: 'scenario-1',
      idempotency_key: 'evaluate-1',
    });
    expect(requestBody(fetch, 0)).not.toHaveProperty('subject_version');
    expect(requestBody(fetch, 0)).not.toHaveProperty(
      'subject_content_digest',
    );
    expect(response.evaluation.receipt.subject.subject_version).toBe(4);
    expect(response.evaluation.receipt.subject_content_digest).toBe(
      'c'.repeat(64),
    );
  });

  it('encodes compliance receipt, currentness, and finding queries exactly', async () => {
    const { api, fetch } = setup();

    await api.listPolicyComplianceReceipts('board-1', {
      cursor: 'receipt-cursor',
      entityType: 'spec',
      subjectId: 'spec/1',
      outcome: 'fail',
      currentness: 'stale',
      projection: 'detail',
    });
    await api.getCurrentPolicyComplianceReceipt(
      'board-1',
      'spec',
      'spec/1',
    );
    await api.getPolicyComplianceReceipt(
      'board-1',
      'receipt/1',
    );
    await api.listPolicyComplianceFindings('board-1', {
      limit: 200,
      receiptId: 'receipt/1',
      guidelineId: 'guide/1',
      ruleId: 'rule/1',
      subjectId: 'spec/1',
      outcome: 'error',
      projection: 'summary',
    });

    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      '/boards/board-1/policy-compliance/receipts'
        + '?limit=50&projection=detail&cursor=receipt-cursor'
        + '&entity_type=spec&subject_id=spec%2F1&outcome=fail'
        + '&currentness=stale',
      '/boards/board-1/policy-compliance/receipts/current'
        + '?entity_type=spec&subject_id=spec%2F1',
      '/boards/board-1/policy-compliance/receipts/receipt%2F1',
      '/boards/board-1/policy-compliance/findings'
        + '?limit=200&projection=summary&receipt_id=receipt%2F1'
        + '&guideline_id=guide%2F1&rule_id=rule%2F1'
        + '&subject_id=spec%2F1&outcome=error',
    ]);
  });

  it('covers the complete governed waiver lifecycle and keyset filters', async () => {
    const { api, fetch } = setup();
    const evidence: [string, ...string[]] = ['card:proof'];

    await api.listPolicyWaivers('board-1', {
      evaluatedAt: '2026-07-30T00:00:00Z',
      cursor: 'waiver-cursor',
      limit: 25,
      findingId: 'finding-1',
      receiptId: 'receipt-1',
      guidelineId: 'guideline-1',
      revisionId: 'revision-1',
      ruleId: 'rule-1',
      entityType: 'card',
      subjectId: 'card-1',
      subjectVersion: 3,
      status: 'approved',
      projection: 'detail',
    });
    await api.requestPolicyWaiver('board-1', {
      finding_id: 'finding-1',
      justification: 'Temporary exception',
      evidence_refs: evidence,
      expires_at: '2026-08-30T00:00:00Z',
      idempotency_key: 'request-1',
    });
    await api.listPolicyWaiverEvents('board-1', 'waiver/1');
    await api.reviewPolicyWaiver('board-1', 'waiver/1', {
      decision: 'approve',
      reason: 'Approved temporarily',
      evidence_refs: evidence,
      expected_waiver_revision: 1,
      idempotency_key: 'review-1',
    });
    await api.revokePolicyWaiver('board-1', 'waiver/1', {
      reason: 'No longer needed',
      evidence_refs: evidence,
      expected_waiver_revision: 2,
      idempotency_key: 'revoke-1',
    });
    await api.revalidatePolicyWaiver('board-1', 'waiver/1', {
      reason: 'Scope is current',
      evidence_refs: evidence,
      expected_waiver_revision: 3,
      new_expires_at: '2026-09-30T00:00:00Z',
      idempotency_key: 'revalidate-1',
    });
    await api.getPolicyWaiver('board-1', 'waiver/1');

    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      '/boards/board-1/policy-waivers'
        + '?limit=25&projection=detail&cursor=waiver-cursor'
        + '&evaluated_at=2026-07-30T00%3A00%3A00Z'
        + '&finding_id=finding-1&receipt_id=receipt-1'
        + '&guideline_id=guideline-1&revision_id=revision-1&rule_id=rule-1'
        + '&entity_type=card&subject_id=card-1&subject_version=3'
        + '&status=approved',
      '/boards/board-1/policy-waivers',
      '/boards/board-1/policy-waivers/waiver%2F1/events',
      '/boards/board-1/policy-waivers/waiver%2F1/review',
      '/boards/board-1/policy-waivers/waiver%2F1/revoke',
      '/boards/board-1/policy-waivers/waiver%2F1/revalidate',
      '/boards/board-1/policy-waivers/waiver%2F1',
    ]);
    expect(requestBody(fetch, 1)).toEqual({
      finding_id: 'finding-1',
      justification: 'Temporary exception',
      evidence_refs: evidence,
      expires_at: '2026-08-30T00:00:00Z',
      idempotency_key: 'request-1',
    });
    expect(requestBody(fetch, 3)).toEqual({
      decision: 'approve',
      reason: 'Approved temporarily',
      evidence_refs: evidence,
      expected_waiver_revision: 1,
      idempotency_key: 'review-1',
    });
    expect(requestBody(fetch, 4)).toEqual({
      reason: 'No longer needed',
      evidence_refs: evidence,
      expected_waiver_revision: 2,
      idempotency_key: 'revoke-1',
    });
    expect(requestBody(fetch, 5)).toEqual({
      reason: 'Scope is current',
      evidence_refs: evidence,
      expected_waiver_revision: 3,
      new_expires_at: '2026-09-30T00:00:00Z',
      idempotency_key: 'revalidate-1',
    });
    for (const mutationIndex of [1, 3, 4, 5]) {
      expect(requestBody(fetch, mutationIndex)).not.toHaveProperty('waiver_id');
      expect(requestBody(fetch, mutationIndex)).not.toHaveProperty('event_id');
    }
  });

  it('round-trips the v2 export/import envelope without stripping null authority', async () => {
    const { api, fetch } = setup();
    const envelope = {
      contract_version: 'guideline-export/v2',
      schema_version: '2',
      kind: 'guidelines',
      exported_at: '2026-07-30T00:00:00Z',
      source_board_id: null,
      content_digest: 'd'.repeat(64),
      guidelines: [],
    } as const;
    fetch.mockResolvedValueOnce(jsonResponse(envelope));
    fetch.mockResolvedValueOnce(
      jsonResponse({
        transaction_status: 'dry_run',
        created_count: 0,
        skip_identical_count: 0,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: true,
        error_code: null,
      }),
    );

    const exported = await api.exportGuidelinePolicy('board/1', {
      guidelineIds: ['guide/1', 'guide-2'],
      includeBindingHistory: false,
    });
    await api.importGuidelinePolicy('board/2', exported, {
      dryRun: true,
    });

    expect(fetch.mock.calls[0][0]).toBe(
      '/boards/board%2F1/guidelines/export'
        + '?guideline_ids=guide%2F1&guideline_ids=guide-2'
        + '&include_binding_history=false',
    );
    expect(fetch.mock.calls[1][0]).toBe(
      '/boards/board%2F2/guidelines/import?dry_run=true',
    );
    expect(requestBody(fetch, 1)).toEqual(envelope);
    expect(requestBody(fetch, 1)).toHaveProperty(
      'source_board_id',
      null,
    );
  });

  it('preserves the canonical structured error and remediation fields', async () => {
    const { api, fetch } = setup();
    const detail: PolicyErrorDetail = {
      outcome: 'error',
      error: 'under_bump',
      code: 'under_bump',
      error_code: 'under_bump',
      message: 'The declared semantic version is below the required minimum.',
      category: 'invalid_argument',
      status_category: 'invalid_argument',
      http_status: 400,
      retryable: false,
      next_action: 'increase_semantic_version',
      details: {
        minimum_bump: 'major',
        minimum_semantic_version: '2.0.0',
      },
    };
    fetch.mockResolvedValue(jsonResponse({ detail }, 400));

    const error = await api
      .createGuidelineRevision('board-1', 'guide-1', {
        idempotency_key: 'revision-2',
        declared_semantic_version: '1.1.0',
        patch: { title: 'Breaking' },
      })
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(PolicyGovernanceApiError);
    expect(error).toMatchObject({
      status: 400,
      httpStatus: 400,
      kind: 'under_bump',
      code: 'under_bump',
      category: 'invalid_argument',
      statusCategory: 'invalid_argument',
      retryable: false,
      nextAction: 'increase_semantic_version',
      details: {
        minimum_bump: 'major',
        minimum_semantic_version: '2.0.0',
      },
    });
  });

  it('preserves canonical remediation through the SaaS BFF envelope', async () => {
    const { api, fetch } = setup();
    fetch.mockResolvedValue(
      jsonResponse(
        {
          backend_error: {
            detail: {
              outcome: 'error',
              error: 'invalid_cursor',
              code: 'invalid_cursor',
              error_code: 'invalid_cursor',
              message: 'The cursor is invalid.',
              category: 'invalid_argument',
              status_category: 'invalid_argument',
              http_status: 400,
              retryable: false,
              next_action: 'restart_pagination',
              details: { reason_code: 'invalid_cursor' },
            },
          },
        },
        400,
      ),
    );

    const error = await api
      .listPolicyComplianceReceipts('board-1', {
        cursor: 'stale-cursor',
      })
      .catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      kind: 'invalid_cursor',
      code: 'invalid_cursor',
      nextAction: 'restart_pagination',
      details: { reason_code: 'invalid_cursor' },
    });
  });

  it('rejects invalid limits locally without decoding or sending a cursor', () => {
    const { api, fetch } = setup();

    expect(() =>
      api.listPolicyComplianceReceipts('board-1', {
        limit: 201,
        cursor: 'opaque',
      }),
    ).toThrow('policy_page_limit_invalid');
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe('closed policy UI state', () => {
  const entityLabels = {
    ideation: 'Ideation',
    refinement: 'Refinement',
    spec: 'Spec',
    sprint: 'Sprint',
    card: 'Card',
    test_scenario: 'Test scenario',
  } satisfies Record<PolicyEntityType, string>;

  it('keeps controls disabled until both data and authority are ready', () => {
    const loading: PolicyActionState<{ id: string }> = {
      status: 'loading',
      data: null,
      error: null,
      authorization: 'unknown',
      controls_enabled: false,
    };
    const failed: PolicyActionState<{ id: string }> = {
      status: 'error',
      data: null,
      error: {
        status: 503,
        kind: 'service_unavailable',
        code: 'service_unavailable',
        category: 'service_unavailable',
        statusCategory: 'service_unavailable',
        retryable: true,
        nextAction: 'retry_or_report',
        details: {},
      },
      authorization: 'unknown',
      controls_enabled: false,
    };
    const denied: PolicyActionState<{ id: string }> = {
      status: 'ready',
      data: { id: 'receipt-1' },
      error: null,
      authorization: 'denied',
      controls_enabled: false,
    };
    const allowed: PolicyActionState<{ id: string }> = {
      status: 'ready',
      data: { id: 'receipt-1' },
      error: null,
      authorization: 'allowed',
      controls_enabled: true,
    };

    expect(isPolicyActionEnabled(loading)).toBe(false);
    expect(isPolicyActionEnabled(failed)).toBe(false);
    expect(isPolicyActionEnabled(denied)).toBe(false);
    expect(isPolicyActionEnabled(allowed)).toBe(true);
    expect(entityLabels.test_scenario).toBe('Test scenario');
  });
});
