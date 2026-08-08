import { describe, expect, it, vi } from 'vitest';

import {
  createResearchDecisionsApi,
  ResearchDecisionApiError,
  type ResearchDecisionTransport,
} from './research-decisions-api';
import type {
  ResearchDecisionContent,
  ResearchDecisionEntry,
  ResearchDecisionPage,
} from '@/types/research-decisions';

const content: ResearchDecisionContent = {
  unknown: 'Which retry policy should be used?',
  status: 'open',
  anchor: {
    anchor_type: 'functional_requirement',
    anchor_ref: 'FR-1',
  },
  evidence_refs: [],
  alternatives: [],
  decision: null,
  rationale: null,
  confidence: null,
  evidence_absence_justification: null,
};

function entry(overrides: Partial<ResearchDecisionEntry> = {}): ResearchDecisionEntry {
  return {
    id: 'entry-1',
    ledger_id: 'ledger/a',
    board_id: 'board-1',
    refinement_id: 'refinement/1',
    refinement_version: 2,
    predecessor_entry_id: null,
    content,
    content_digest: 'a'.repeat(64),
    created_by: 'user-1',
    created_at: '2026-07-28T12:00:00Z',
    ...overrides,
  };
}

function page(
  items: ResearchDecisionEntry[],
  overrides: Partial<ResearchDecisionPage> = {},
): ResearchDecisionPage {
  return {
    items,
    offset: 0,
    limit: 25,
    total_filtered: items.length,
    total_overall: items.length,
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function setup() {
  const fetch = vi.fn();
  const transport: ResearchDecisionTransport = { fetch };
  return { api: createResearchDecisionsApi(transport), fetch };
}

describe('research-decisions-api', () => {
  it('encodes the frozen REST pagination and filters', async () => {
    const { api, fetch } = setup();
    const controller = new AbortController();
    fetch.mockResolvedValue(jsonResponse(page([])));

    await api.listResearchDecisions('refinement/1', {
      offset: 25,
      limit: 50,
      ledgerId: ' ledger/a ',
      status: 'resolved',
      signal: controller.signal,
    });

    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, options] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      '/refinements/refinement%2F1/research-decisions'
        + '?offset=25&limit=50&ledger_id=ledger%2Fa&status=resolved',
    );
    expect(options.signal).toBe(controller.signal);
    expect(new Headers(options.headers).get('Accept')).toBe('application/json');
  });

  it('posts append and supersede with their distinct CAS fences', async () => {
    const { api, fetch } = setup();
    const response = {
      entry_id: 'entry-1',
      thread_id: 'ledger/a',
      sequence: 1,
      head_revision: 1,
      refinement_version: 2,
      replayed: false,
      entry: entry(),
    };
    fetch.mockImplementation(async () => jsonResponse(response));

    await api.appendResearchDecision('refinement/1', {
      operation: 'append',
      expected_head_revision: 0,
      idempotency_key: 'append-key',
      entry: content,
    });
    await api.supersedeResearchDecision('refinement/1', {
      operation: 'supersede',
      expected_head_revision: 3,
      ledger_id: 'ledger/a',
      supersedes_entry_id: 'entry-3',
      idempotency_key: 'supersede-key',
      entry: content,
    });

    const appendOptions = fetch.mock.calls[0][1] as RequestInit;
    const supersedeOptions = fetch.mock.calls[1][1] as RequestInit;
    expect(appendOptions.method).toBe('POST');
    expect(JSON.parse(String(appendOptions.body))).toEqual({
      operation: 'append',
      expected_head_revision: 0,
      idempotency_key: 'append-key',
      entry: content,
    });
    expect(JSON.parse(String(supersedeOptions.body))).toEqual({
      operation: 'supersede',
      expected_head_revision: 3,
      ledger_id: 'ledger/a',
      supersedes_entry_id: 'entry-3',
      idempotency_key: 'supersede-key',
      entry: content,
    });
  });

  it('resolves the authoritative current ledger head for supersede CAS', async () => {
    const { api, fetch } = setup();
    const current = entry({
      id: 'entry-4',
      predecessor_entry_id: 'entry-3',
      refinement_version: 8,
    });
    fetch.mockResolvedValue(
      jsonResponse({ entry: current, head_revision: 4 }),
    );

    await expect(
      api.resolveResearchDecisionHead('refinement/1', 'ledger/a'),
    ).resolves.toEqual({
      entry: current,
      headRevision: 4,
    });
    expect(fetch.mock.calls[0][0]).toBe(
      '/refinements/refinement%2F1/research-decisions'
        + '/head?ledger_id=ledger%2Fa',
    );
  });

  it('preserves the frozen error_code, retryability, and raw reason details', async () => {
    const { api, fetch } = setup();
    fetch.mockResolvedValue(
      jsonResponse(
        {
          detail: {
            error_code: 'version_conflict',
            message: 'version_conflict',
            retryable: true,
            details: {
              reason_code: 'research_decision_head_revision_conflict',
            },
          },
        },
        409,
      ),
    );

    const error = await api
      .listResearchDecisions('refinement-1')
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ResearchDecisionApiError);
    expect(error).toMatchObject({
      status: 409,
      code: 'version_conflict',
      retryable: true,
      message: 'version_conflict',
      details: {
        reason_code: 'research_decision_head_revision_conflict',
      },
    });
  });
});
