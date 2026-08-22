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

  it('covers semantic revision, preview, and adoption with exact binding fences', async () => {
    const { api, fetch } = setup();
    const revisionResponse = {
      revision_id: 'revision-2',
      revision: '1.1.0',
      revision_digest: 'f'.repeat(64),
      metrics: [{
        metric_id: 'metric-1',
        code: 'Title.Clarity:v2',
        title: 'Title clarity',
        description: 'Scores how clearly the title communicates intent.',
        evaluation_rubric:
          '0 is unclear; 100 is independently understandable.',
        target_entity_types: ['spec', 'test_scenario'],
        direction: 'minimum',
        default_threshold: 70,
      }],
    };
    fetch.mockResolvedValueOnce(jsonResponse(revisionResponse));

    const created = await api.createGuidelineRevision(
      'board-1',
      'guide-1',
      {
      expected_head_revision: 1,
      version_bump: 'minor',
      content: {
        title: 'Second revision',
        body: 'Require independently understandable titles.',
      },
      metrics: [
        {
          metric_id: 'metric-1',
          code: 'Title.Clarity:v2',
          title: 'Title clarity',
          description: 'Scores how clearly the title communicates intent.',
          evaluation_rubric: '0 is unclear; 100 is independently understandable.',
          target_entity_types: ['spec', 'test_scenario'],
          direction: 'minimum',
          default_threshold: 70,
        },
      ],
      },
    );
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
      proposed_priority: 3,
      proposed_enforcement: 'blocking',
      proposed_minimum_confidence: 80,
      proposed_metric_threshold_overrides: {
        title_clarity: 75,
      },
      idempotency_key: 'impact-1',
      to_revision_id: 'revision-2',
    });
    await api.getGuidelineImpact(
      'board-1',
      'guide-1',
      'preview/1',
    );
    await api.listGuidelineImpactItems(
      'board-1',
      'guide-1',
      'preview/1',
      {
        limit: 50,
        projection: 'summary',
        entityType: 'test_scenario',
        itemKind: 'target',
      },
    );
    await api.adoptGuidelineRevision('board-1', 'guide-1', {
      impact_receipt_id: 'preview-1',
      impact_digest: 'a'.repeat(64),
      idempotency_key: 'adopt-1',
    });

    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      '/boards/board-1/guidelines/guide-1/revisions',
      '/boards/board-1/guidelines/guide-1/revisions/revision%2F2',
      '/boards/board-1/guidelines/guide-1/retire',
      '/boards/board-1/guidelines/guide-1/impact-previews',
      '/boards/board-1/guidelines/guide-1/impact-previews/preview%2F1',
      '/boards/board-1/guidelines/guide-1/impact-previews/preview%2F1/items'
        + '?limit=50&projection=summary'
        + '&entity_type=test_scenario&item_kind=target',
      '/boards/board-1/guidelines/guide-1/adoptions',
    ]);
    expect(created).toEqual(revisionResponse);
    expect(requestBody(fetch, 0)).toEqual({
      expected_head_revision: 1,
      version_bump: 'minor',
      content: {
        title: 'Second revision',
        body: 'Require independently understandable titles.',
      },
      metrics: [
        {
          metric_id: 'metric-1',
          code: 'Title.Clarity:v2',
          title: 'Title clarity',
          description: 'Scores how clearly the title communicates intent.',
          evaluation_rubric:
            '0 is unclear; 100 is independently understandable.',
          target_entity_types: ['spec', 'test_scenario'],
          direction: 'minimum',
          default_threshold: 70,
        },
      ],
    });
    expect(requestBody(fetch, 0)).not.toHaveProperty('board_id');
    expect(requestBody(fetch, 0)).not.toHaveProperty('patch');
    expect(requestBody(fetch, 0)).not.toHaveProperty(
      'declared_semantic_version',
    );
    expect(requestBody(fetch, 0)).not.toHaveProperty('idempotency_key');
    expect(requestBody(fetch, 3)).toEqual({
      proposed_priority: 3,
      proposed_enforcement: 'blocking',
      proposed_minimum_confidence: 80,
      proposed_metric_threshold_overrides: {
        title_clarity: 75,
      },
      idempotency_key: 'impact-1',
      to_revision_id: 'revision-2',
    });
    expect(requestBody(fetch, 6)).toEqual({
      impact_receipt_id: 'preview-1',
      impact_digest: 'a'.repeat(64),
      idempotency_key: 'adopt-1',
    });
  });

  it('round-trips the v3 semantic export/import envelope without stripping null authority', async () => {
    const { api, fetch } = setup();
    const envelope = {
      contract_version: 'guideline-export/v3',
      schema_version: '3',
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

  it('uses the semantic assessment, finding, waiver, and human skip routes exactly', async () => {
    const { api, fetch } = setup();
    const controller = new AbortController();
    const evidence = [{
      source_type: 'spec',
      source_id: 'spec-1',
      source_version: 7,
      content_hash: 'a'.repeat(64),
    }] as const;

    await api.listSemanticGuidelineAssessments('board/1', {
      limit: 25,
      cursor: 'opaque/+==',
      projection: 'detail',
      subjectType: 'spec',
      subjectId: 'spec/1',
      guidelineId: 'guide/1',
      bindingId: 'binding/1',
      outcome: 'metric_threshold_failed',
      currentness: 'stale',
      signal: controller.signal,
    });
    await api.getCurrentSemanticGuidelineAssessment(
      'board/1',
      'spec',
      'spec/1',
      'binding/1',
      'detail',
    );
    await api.getSemanticGuidelineAssessment(
      'board/1',
      'receipt/1',
      'full',
    );
    await api.listSemanticGuidelineFindings('board/1', {
      limit: 10,
      projection: 'detail',
      receiptId: 'receipt/1',
      guidelineId: 'guide/1',
      bindingId: 'binding/1',
      metricId: 'metric/1',
      subjectType: 'spec',
      subjectId: 'spec/1',
      outcome: 'fail',
    });
    await api.listSemanticMetricWaivers('board/1', {
      limit: 10,
      projection: 'detail',
      evaluatedAt: '2026-07-30T12:00:00Z',
      findingId: 'finding/1',
      metricResultId: 'result/1',
      subjectType: 'spec',
      subjectId: 'spec/1',
      status: 'requested',
    });
    await api.requestSemanticMetricWaiver('board/1', {
      metric_result_id: 'result/1',
      finding_id: 'finding/1',
      receipt_id: 'receipt/1',
      justification: 'Temporary migration.',
      evidence_refs: [...evidence],
      expires_at: null,
      idempotency_key: 'waiver-request-1',
    });
    await api.getSemanticMetricWaiver(
      'board/1',
      'waiver/1',
      {
        evaluatedAt: '2026-07-30T12:00:00Z',
        projection: 'detail',
      },
    );
    await api.listSemanticMetricWaiverEvents('board/1', 'waiver/1');
    await api.reviewSemanticMetricWaiver('board/1', 'waiver/1', {
      decision: 'approve',
      reason: 'Evidence is sufficient.',
      evidence_refs: [...evidence],
      expected_waiver_revision: 1,
      idempotency_key: 'waiver-review-1',
    });
    await api.revokeSemanticMetricWaiver('board/1', 'waiver/1', {
      reason: 'Exception no longer applies.',
      evidence_refs: [...evidence],
      expected_waiver_revision: 2,
      idempotency_key: 'waiver-revoke-1',
    });
    await api.revalidateSemanticMetricWaiver('board/1', 'waiver/1', {
      expected_waiver_revision: 3,
      evaluated_at: '2026-07-30T13:00:00Z',
      idempotency_key: 'waiver-revalidate-1',
    });
    await api.listSemanticPolicySkips('board/1', {
      limit: 10,
      projection: 'detail',
      subjectType: 'spec',
      subjectId: 'spec/1',
      bindingId: 'binding/1',
      status: 'active',
      currentness: 'current',
    });
    await api.createSemanticPolicySkip(
      'board/1',
      {
        subject_type: 'spec',
        subject_id: 'spec/1',
        expected_subject_version: 7,
        binding_id: 'binding/1',
        reason: 'Human-owned temporary exception.',
      },
      'skip-create-1',
    );
    await api.revokeSemanticPolicySkip(
      'board/1',
      'skip/1',
      {
        expected_skip_revision: 1,
        reason: 'Exception no longer needed.',
        idempotency_key: 'skip-revoke-1',
      },
    );

    expect(fetch.mock.calls.map((call) => call[0])).toEqual([
      '/boards/board%2F1/semantic-guideline-assessments'
        + '?limit=25&projection=detail&cursor=opaque%2F%2B%3D%3D'
        + '&subject_type=spec&subject_id=spec%2F1'
        + '&guideline_id=guide%2F1&binding_id=binding%2F1'
        + '&outcome=metric_threshold_failed&currentness=stale',
      '/boards/board%2F1/semantic-guideline-assessments/current'
        + '?subject_type=spec&subject_id=spec%2F1'
        + '&binding_id=binding%2F1&projection=detail',
      '/boards/board%2F1/semantic-guideline-assessments/receipt%2F1'
        + '?projection=full',
      '/boards/board%2F1/semantic-guideline-findings'
        + '?limit=10&projection=detail&receipt_id=receipt%2F1'
        + '&guideline_id=guide%2F1&binding_id=binding%2F1'
        + '&metric_id=metric%2F1&subject_type=spec'
        + '&subject_id=spec%2F1&outcome=fail',
      '/boards/board%2F1/policy-waivers'
        + '?limit=10&projection=detail'
        + '&evaluated_at=2026-07-30T12%3A00%3A00Z'
        + '&finding_id=finding%2F1&metric_result_id=result%2F1'
        + '&subject_type=spec&subject_id=spec%2F1&status=requested',
      '/boards/board%2F1/policy-waivers',
      '/boards/board%2F1/policy-waivers/waiver%2F1'
        + '?evaluated_at=2026-07-30T12%3A00%3A00Z&projection=detail',
      '/boards/board%2F1/policy-waivers/waiver%2F1/events',
      '/boards/board%2F1/policy-waivers/waiver%2F1/review',
      '/boards/board%2F1/policy-waivers/waiver%2F1/revoke',
      '/boards/board%2F1/policy-waivers/waiver%2F1/revalidate',
      '/boards/board%2F1/semantic-guideline-skips'
        + '?limit=10&projection=detail&subject_type=spec'
        + '&subject_id=spec%2F1&binding_id=binding%2F1'
        + '&status=active&currentness=current',
      '/boards/board%2F1/semantic-guideline-skips',
      '/boards/board%2F1/semantic-guideline-skips/skip%2F1/revoke',
    ]);
    expect((fetch.mock.calls[0][1] as RequestInit).signal)
      .toBe(controller.signal);
    expect(requestBody(fetch, 5)).toMatchObject({
      metric_result_id: 'result/1',
      finding_id: 'finding/1',
      evidence_refs: evidence,
    });
    expect(
      new Headers((fetch.mock.calls[12][1] as RequestInit).headers)
        .get('Idempotency-Key'),
    ).toBe('skip-create-1');
    expect(requestBody(fetch, 8)).toMatchObject({
      decision: 'approve',
      expected_waiver_revision: 1,
      evidence_refs: evidence,
    });
    expect(requestBody(fetch, 9)).toMatchObject({
      expected_waiver_revision: 2,
      evidence_refs: evidence,
    });
    expect(requestBody(fetch, 10)).toEqual({
      expected_waiver_revision: 3,
      evaluated_at: '2026-07-30T13:00:00Z',
      idempotency_key: 'waiver-revalidate-1',
    });
    expect(requestBody(fetch, 12)).not.toHaveProperty('idempotency_key');
    expect(requestBody(fetch, 13)).toEqual({
      expected_skip_revision: 1,
      reason: 'Exception no longer needed.',
      idempotency_key: 'skip-revoke-1',
    });
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
        expected_head_revision: 1,
        version_bump: 'minor',
        content: {
          title: 'Breaking',
          body: 'Changes established meaning.',
        },
        metrics: [],
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
      .listSemanticGuidelineAssessments('board-1', {
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
      api.listSemanticGuidelineAssessments('board-1', {
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
