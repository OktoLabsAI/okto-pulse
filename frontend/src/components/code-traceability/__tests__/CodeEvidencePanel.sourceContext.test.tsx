import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type {
  CodeTraceabilityEvidence,
  CodeTraceabilityProjection,
  SourceContextEvidenceItemV2,
  SourceContextSummaryV2,
} from '@/types';
import { CodeEvidencePanel } from '../CodeEvidencePanel';

const apiMock = vi.hoisted(() => ({
  getCodeTraceabilityProjection: vi.fn(),
  revokeCodeEvidence: vi.fn(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('../useCodeTraceabilityAuthority', () => ({
  useCodeTraceabilityAuthority: () => ({
    canReadProjection: true,
    canClassifyLegacyEvidence: false,
    canRevokeEvidence: false,
    isLoading: false,
    error: null,
  }),
}));

const evidence: CodeTraceabilityEvidence = {
  id: 'evidence-1',
  investigation_receipt_id: 'receipt-1',
  source_ref: 'repository:payments',
  parent_type: 'refinement',
  parent_id: 'refinement-1',
  parent_version: 4,
  evidence_type: 'code_observation',
  claim: 'The agent observed a reusable authorization hook.',
  workspace_state: {
    workspace_state_id: 'sha256:workspace-1',
    declared_revision: 'revision-1',
    declared_dirty: false,
    observed_at: '2026-08-22T12:00:00Z',
    reproducibility_claim: 'committed_snapshot',
    fingerprint_algorithm: 'agent-manifest-v1',
    manifest_digest: 'sha256:manifest',
    manifest_entry_count: 12,
  },
  selector_kind: 'qualified_symbol',
  relative_path: 'src/payments/authorize.ts',
  language: 'typescript',
  symbol_kind: 'function',
  qualified_symbol: 'authorizePayment',
  snapshot_line_start: 18,
  snapshot_line_end: 31,
  attestation_state: 'agent_attested',
  lifecycle_status: 'active',
  supersedes_evidence_id: null,
};

const emptyRoleCounts = {
  current_implementation_count: 0,
  existing_scaffold_count: 0,
  existing_constraint_count: 0,
  reference_pattern_count: 0,
  uncategorized_legacy_count: 0,
};

function sourceContext(
  overrides: Partial<SourceContextSummaryV2> = {},
): SourceContextSummaryV2 {
  return {
    delivery_context: 'brownfield',
    delivery_context_provenance: {
      value: 'brownfield',
      source_refinement_id: 'refinement-1',
      source_refinement_version: 4,
    },
    investigation_outcome: 'evidence_applicable',
    role_counts: { ...emptyRoleCounts, current_implementation_count: 1 },
    classification_state: { classified_count: 1, uncategorized_legacy_count: 0 },
    evidence_applicable: true,
    interpretation_rule: 'Only current implementation may support implementation coverage.',
    items_not_current_implementation_count: 0,
    technical_details_available: true,
    ...overrides,
  };
}

function contextItem(
  overrides: Partial<SourceContextEvidenceItemV2> = {},
): SourceContextEvidenceItemV2 {
  return {
    evidence_id: evidence.id,
    source_role: 'current_implementation',
    relevance_summary: 'Existing authorization behavior relevant to this delivery.',
    scope_relation: 'The refinement extends this authorization path.',
    source_origin: 'Payments service baseline.',
    interpretation_limit: null,
    baseline_provenance: {
      presence: 'committed_snapshot',
      workspace_state_id: 'sha256:workspace-1',
      provenance_note: null,
    },
    context_origin: 'authored',
    context_contract_version: 2,
    evidence_applicable: true,
    ...overrides,
  };
}

function projection(overrides: Partial<CodeTraceabilityProjection> = {}): CodeTraceabilityProjection {
  return {
    subject_type: 'refinement',
    subject_id: 'refinement-1',
    subject_version: 4,
    profile: 'detail',
    context_scope: 'default',
    evidence: [],
    inherited_evidence_ids: [],
    direct_evidence_ids: [],
    referenced_evidence_ids: [],
    links: [],
    dispositions: [],
    targets: [],
    resolutions: [],
    overlaps: [],
    waivers: [],
    heads: [],
    counts: {},
    coverage: {
      total: 0,
      linked: 0,
      dispositioned: 0,
      pending: 0,
      pending_ids: [],
      coverage_pct: 0,
    },
    resolution_freshness: {},
    gate_readiness: {
      mode: 'advisory',
      allowed: true,
      passed: true,
      blockers: [],
      receipt_currentness: {},
      resolution_freshness: {},
    },
    ...overrides,
  };
}

function renderPanel() {
  return render(
    <CodeEvidencePanel
      boardId="board-1"
      subjectId="refinement-1"
      subjectVersion={4}
    />,
  );
}

function expectBefore(left: Element, right: Element) {
  expect(
    left.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
}

describe('Code Evidence source context presentation', () => {
  beforeEach(() => {
    apiMock.getCodeTraceabilityProjection.mockReset();
    apiMock.revokeCodeEvidence.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it('ts_02e08e5b — orders context, outcome/counts, groups, and applicability in the DOM', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      source_context: sourceContext(),
      source_context_items: [contextItem({
        interpretation_limit: 'Do not treat the helper as the complete authorization policy.',
      })],
      evidence: [evidence],
      inherited_evidence_ids: [evidence.id],
      heads: [{
        source_ref: evidence.source_ref,
        generation: 1,
        current_receipt_id: evidence.investigation_receipt_id,
        state: 'current',
      }],
      gate_readiness: {
        mode: 'advisory',
        allowed: true,
        passed: true,
        blockers: [],
        receipt_currentness: { [evidence.investigation_receipt_id]: 'current' },
        resolution_freshness: {},
      },
    }));

    renderPanel();

    const contextHeading = await screen.findByRole('heading', { name: 'Source context' });
    expect(screen.getByText('Brownfield')).toBeInTheDocument();
    const outcome = screen.getByText('Existing implementation found');
    const overview = screen.getByTestId('source-context-overview');
    const countsHeading = within(overview).getByRole('heading', { name: 'Source roles' });
    const countLabel = within(overview).getByText('Existing implementation');
    expect(within(countLabel.parentElement as HTMLElement).getByText('1')).toBeInTheDocument();
    const groupHeading = screen.getByRole('heading', {
      level: 4,
      name: 'Existing implementation',
    });
    const applicability = screen.getAllByText('Implementation evidence')
      .find((item) => !item.closest('details'));
    expect(applicability).toBeDefined();
    expectBefore(contextHeading, outcome);
    expectBefore(outcome, countsHeading);
    expectBefore(countsHeading, groupHeading);
    expectBefore(groupHeading, applicability as HTMLElement);
    expect(screen.getByRole('heading', {
      name: 'The agent observed a reusable authorization hook.',
    })).toBeInTheDocument();
    expect(screen.getAllByText('Existing authorization behavior relevant to this delivery.')
      .some((item) => !item.closest('details'))).toBe(true);
    expect(screen.getAllByText('Payments service baseline.')
      .some((item) => !item.closest('details'))).toBe(true);
    expect(screen.getAllByText('Do not treat the helper as the complete authorization policy.')
      .some((item) => !item.closest('details'))).toBe(true);
    expect(screen.getAllByText('Implementation evidence')
      .some((item) => !item.closest('details'))).toBe(true);

    const relation = screen.getByText('The refinement extends this authorization path.');
    expect(relation.closest('details')).not.toBeNull();
    expect(relation).not.toBeVisible();
  });

  it('ts_18205fb0 — controls technical disclosure with explicit expansion and preserved focus', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      source_context: sourceContext(),
      source_context_items: [contextItem()],
      evidence: [{
        ...evidence,
        symbol_signature: 'authorizePayment(input: Payment): Promise<Result>',
        payload_sha256: 'sha256:evidence-payload',
      }],
    }));

    renderPanel();

    const detailsSummary = await screen.findByText('Technical evidence details');
    const details = detailsSummary.closest('details');
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute('open');
    expect(detailsSummary).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('Agent-attested').closest('details')).toBe(details);
    expect(screen.getByText('evidence-1').closest('details')).toBe(details);
    expect(screen.getByText('authorizePayment(input: Payment): Promise<Result>').closest('details'))
      .toBe(details);
    expect(screen.getByText('sha256:evidence-payload').closest('details')).toBe(details);
    detailsSummary.focus();
    fireEvent.click(detailsSummary);
    expect(details).toHaveAttribute('open');
    await waitFor(() => expect(detailsSummary).toHaveAttribute('aria-expanded', 'true'));
    expect(detailsSummary).toHaveFocus();
    expect(within(details as HTMLElement).getByText('src/payments/authorize.ts')).toBeInTheDocument();
    expect(within(details as HTMLElement).getByText('Committed snapshot')).toBeInTheDocument();
  });

  it('ts_f6b0f9f7 — presents pure Greenfield absence without synthetic progress or warning states', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      source_context: sourceContext({
        delivery_context: 'greenfield',
        delivery_context_provenance: {
          value: 'greenfield',
          source_refinement_id: 'refinement-1',
          source_refinement_version: 4,
        },
        investigation_outcome: 'no_relevant_existing_implementation',
        role_counts: emptyRoleCounts,
        classification_state: { classified_count: 0, uncategorized_legacy_count: 0 },
        evidence_applicable: false,
        interpretation_rule: 'No existing implementation is required for this greenfield scope.',
      }),
      source_context_items: [],
      contextual_evidence_coverage: {
        total: 0,
        linked: 0,
        dispositioned: 0,
        pending: 0,
        pending_ids: [],
        unresolved_applicability_count: 0,
        coverage_pct: null,
        projection_complete: true,
      },
    }));

    renderPanel();

    expect(await screen.findByText('Greenfield')).toBeInTheDocument();
    expect(screen.getByText('No relevant existing implementation')).toBeInTheDocument();
    expect(screen.getByText('Code evidence is not applicable')).toBeInTheDocument();
    expect(screen.getByText('No existing implementation was found')).toBeInTheDocument();
    expect(screen.getByText(/expected result for this delivery context/i)).toBeInTheDocument();
    expect(screen.queryByText('No code evidence submitted')).not.toBeInTheDocument();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/warning|waiting|waiver|skip|unavailable/i);
  });

  it('keeps reported Greenfield absence indeterminate without a complete zero-total aggregate', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      source_context: sourceContext({
        delivery_context: 'greenfield',
        investigation_outcome: 'no_relevant_existing_implementation',
        role_counts: emptyRoleCounts,
        classification_state: { classified_count: 0, uncategorized_legacy_count: 0 },
        evidence_applicable: false,
      }),
      source_context_items: [],
      contextual_evidence_coverage: {
        total: 0,
        linked: 0,
        dispositioned: 0,
        pending: 0,
        pending_ids: [],
        unresolved_applicability_count: 0,
        coverage_pct: null,
        projection_complete: false,
      },
    }));

    renderPanel();

    expect(await screen.findByText('Applicability not finalized')).toBeInTheDocument();
    expect(screen.getByText(/authoritative contextual projection does not yet confirm this outcome/i)).toBeInTheDocument();
    expect(screen.queryByText('Code evidence is not applicable')).not.toBeInTheDocument();
  });

  it('ts_75dfe647 — renders only Greenfield scaffold, constraint, and reference groups with human context', async () => {
    const roles = [
      {
        role: 'existing_scaffold' as const,
        claim: 'Generated service shell is already present.',
        origin: 'Generated payments module.',
        relevance: 'Shows where the new handler belongs.',
        limit: 'The shell does not prove runtime behavior.',
      },
      {
        role: 'existing_constraint' as const,
        claim: 'Deployment policy constrains the runtime.',
        origin: 'Platform deployment policy.',
        relevance: 'Limits the supported execution environment.',
        limit: 'The policy does not implement the requested behavior.',
      },
      {
        role: 'reference_pattern' as const,
        claim: 'A sibling module demonstrates the approved pattern.',
        origin: 'Approved sibling service.',
        relevance: 'Provides a placement and naming pattern.',
        limit: 'The sibling behavior must not be copied as current implementation.',
      },
    ];
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      source_context: sourceContext({
        delivery_context: 'greenfield',
        delivery_context_provenance: {
          value: 'greenfield',
          source_refinement_id: 'refinement-1',
          source_refinement_version: 4,
        },
        investigation_outcome: 'no_relevant_existing_implementation',
        role_counts: {
          ...emptyRoleCounts,
          existing_scaffold_count: 1,
          existing_constraint_count: 1,
          reference_pattern_count: 1,
        },
        classification_state: { classified_count: 3, uncategorized_legacy_count: 0 },
        evidence_applicable: false,
        items_not_current_implementation_count: 3,
      }),
      source_context_items: roles.map((item, index) => contextItem({
        evidence_id: `evidence-greenfield-${index + 1}`,
        source_role: item.role,
        relevance_summary: item.relevance,
        source_origin: item.origin,
        interpretation_limit: item.limit,
        evidence_applicable: false,
      })),
      evidence: roles.map((item, index) => ({
        ...evidence,
        id: `evidence-greenfield-${index + 1}`,
        investigation_receipt_id: `receipt-greenfield-${index + 1}`,
        claim: item.claim,
      })),
      contextual_evidence_coverage: {
        total: 0,
        linked: 0,
        dispositioned: 0,
        pending: 0,
        pending_ids: [],
        unresolved_applicability_count: 0,
        coverage_pct: null,
        projection_complete: true,
      },
    }));

    renderPanel();

    expect(await screen.findByText('Greenfield')).toBeInTheDocument();
    const overview = screen.getByTestId('source-context-overview');
    expect(within(overview).getByText('3 recorded items')).toBeInTheDocument();
    for (const label of ['Existing scaffold', 'Existing constraint', 'Reference pattern']) {
      const roleCount = within(overview).getByText(label);
      expect(within(roleCount.parentElement as HTMLElement).getByText('1')).toBeInTheDocument();
    }
    expect(within(overview).queryByText('Existing implementation')).not.toBeInTheDocument();
    expect(within(overview).queryByText('Needs classification')).not.toBeInTheDocument();

    const groupHeadings = screen.getAllByRole('heading', { level: 4 })
      .map((heading) => heading.textContent)
      .filter((heading) => [
        'Existing scaffold',
        'Existing constraint',
        'Reference pattern',
      ].includes(heading ?? ''));
    expect(groupHeadings).toEqual([
      'Existing scaffold',
      'Existing constraint',
      'Reference pattern',
    ]);
    for (const item of roles) {
      expect(screen.getByRole('heading', { level: 3, name: item.claim })).toBeInTheDocument();
      for (const copy of [item.origin, item.relevance, item.limit]) {
        expect(screen.getAllByText(copy).some((node) => !node.closest('details'))).toBe(true);
      }
    }
  });

  it('ts_b8b58cba — renders partial receipt omissions with deterministic human remediation', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      source_context: sourceContext({
        delivery_context: 'greenfield',
        delivery_context_provenance: {
          value: 'greenfield',
          source_refinement_id: 'refinement-1',
          source_refinement_version: 4,
        },
        investigation_outcome: 'partial',
        role_counts: emptyRoleCounts,
        classification_state: { classified_count: 0, uncategorized_legacy_count: 0 },
        evidence_applicable: false,
        technical_details_available: false,
      }),
      current_receipts: [{
        id: 'receipt-partial',
        outcome: 'partial',
        source_ref: 'repository:payments',
        omission_manifest: [{
          reason_code: 'permission_denied',
          affected_scope_digest: 'a'.repeat(64),
          count: 2,
        }],
      }],
    }));

    renderPanel();

    expect(await screen.findByText('Investigation partially available')).toBeInTheDocument();
    expect(screen.getByText('Only partial source context was available')).toBeInTheDocument();
    expect(screen.getByText('Source access denied · 2 affected source items')).toBeInTheDocument();
    expect(screen.getByText(/Grant the investigating agent access.*run the investigation again/i))
      .toBeInTheDocument();
    expect(screen.queryByText('Greenfield')).not.toBeInTheDocument();
    expect(screen.queryByText(/not applicable/i)).not.toBeInTheDocument();
    expect(screen.queryByText('permission_denied')).not.toBeInTheDocument();
  });

  it('ts_c23cc546 — renders unavailable receipt omissions without implying context or applicability', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      source_context: sourceContext({
        delivery_context: 'greenfield',
        delivery_context_provenance: {
          value: 'greenfield',
          source_refinement_id: 'refinement-1',
          source_refinement_version: 4,
        },
        investigation_outcome: 'unavailable',
        role_counts: emptyRoleCounts,
        classification_state: { classified_count: 0, uncategorized_legacy_count: 0 },
        evidence_applicable: false,
        technical_details_available: false,
      }),
      current_receipts: [{
        id: 'receipt-unavailable',
        outcome: 'unavailable',
        source_ref: 'repository:payments',
        omission_manifest: [{
          reason_code: 'timeout',
          affected_scope_digest: 'b'.repeat(64),
          count: 1,
        }],
      }],
    }));

    renderPanel();

    expect(await screen.findByText('Investigation unavailable')).toBeInTheDocument();
    expect(screen.getByText('The source investigation was unavailable')).toBeInTheDocument();
    expect(screen.getByText('No code context or applicability is implied.')).toBeInTheDocument();
    expect(screen.getByText('Investigation timed out · 1 affected source item')).toBeInTheDocument();
    expect(screen.getByText(/Reduce the source scope.*run the investigation again/i))
      .toBeInTheDocument();
    expect(screen.queryByText('Greenfield')).not.toBeInTheDocument();
    expect(screen.queryByText(/not applicable/i)).not.toBeInTheDocument();
    expect(screen.queryByText('timeout')).not.toBeInTheDocument();
  });

  it('ts_e6933912 — does not infer role or applicability for unclassified legacy evidence', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      source_context: sourceContext({
        investigation_outcome: 'partial',
        role_counts: { ...emptyRoleCounts, uncategorized_legacy_count: 1 },
        classification_state: { classified_count: 0, uncategorized_legacy_count: 1 },
        evidence_applicable: null,
        items_not_current_implementation_count: 1,
      }),
      source_context_items: [contextItem({
        source_role: 'uncategorized_legacy',
        relevance_summary: null,
        scope_relation: null,
        source_origin: null,
        context_origin: 'unclassified_legacy',
        context_contract_version: null,
        evidence_applicable: null,
      })],
      evidence: [{ ...evidence, source_role: 'current_implementation' }],
    }));

    renderPanel();

    expect((await screen.findAllByText('Needs classification')).length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { name: 'Classification not provided' })).toBeInTheDocument();
    expect(screen.getByText('Applicability unresolved')).toBeInTheDocument();
    expect(screen.getByText(/Applicability remains unresolved/i)).toBeInTheDocument();
    expect(screen.queryByText('Implementation evidence')).not.toBeInTheDocument();
    expect(screen.queryByText('Existing implementation')).not.toBeInTheDocument();
  });

  it('keeps legacy Evidence readable as audit-only when the contextual contract is absent', async () => {
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      evidence: [evidence],
    }));

    renderPanel();

    expect(await screen.findByText('Source context unavailable')).toBeInTheDocument();
    expect(screen.getByText(/remains available below for audit/i)).toBeInTheDocument();
    expect(screen.getByText('The agent observed a reusable authorization hook.')).toBeInTheDocument();
    expect(screen.getByText('Context not projected')).toBeInTheDocument();
    expect(screen.getByText(/role and applicability are not inferred/i)).toBeInTheDocument();
    expect(screen.getByText('Technical evidence details')).toBeInTheDocument();
    expect(screen.getByText('Agent-attested')).toBeInTheDocument();
  });

  it('ts_30354e51 — renders one heading for two same-role items and omits empty Hybrid groups', async () => {
    const hybridEvidence = [
      {
        ...evidence,
        id: 'evidence-scaffold-a',
        investigation_receipt_id: 'receipt-scaffold-a',
        claim: 'Scaffold alpha',
      },
      {
        ...evidence,
        id: 'evidence-scaffold-b',
        investigation_receipt_id: 'receipt-scaffold-b',
        claim: 'Scaffold beta',
      },
      {
        ...evidence,
        id: 'evidence-reference',
        investigation_receipt_id: 'receipt-reference',
        claim: 'Reference gamma',
      },
    ];
    const hybridItems = [
      contextItem({
        evidence_id: 'evidence-scaffold-a',
        source_role: 'existing_scaffold',
        relevance_summary: 'First existing scaffold item.',
        evidence_applicable: false,
      }),
      contextItem({
        evidence_id: 'evidence-scaffold-b',
        source_role: 'existing_scaffold',
        relevance_summary: 'Second existing scaffold item.',
        evidence_applicable: false,
      }),
      contextItem({
        evidence_id: 'evidence-reference',
        source_role: 'reference_pattern',
        relevance_summary: 'Reference pattern for the hybrid delivery.',
        evidence_applicable: false,
      }),
    ];
    apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection({
      source_context: sourceContext({
        delivery_context: 'hybrid',
        delivery_context_provenance: {
          value: 'hybrid',
          inherited_value: 'brownfield',
          source_refinement_id: 'refinement-1',
          source_refinement_version: 4,
          override_reason: 'The new module is greenfield while the integration remains brownfield.',
        },
        investigation_outcome: 'no_relevant_existing_implementation',
        role_counts: {
          ...emptyRoleCounts,
          existing_scaffold_count: 2,
          reference_pattern_count: 1,
        },
        classification_state: { classified_count: 3, uncategorized_legacy_count: 0 },
        evidence_applicable: false,
        items_not_current_implementation_count: 3,
      }),
      source_context_items: [...hybridItems].reverse(),
      evidence: [...hybridEvidence].reverse(),
    }));

    renderPanel();

    expect(await screen.findByText('Hybrid')).toBeInTheDocument();
    const override = await screen.findByText('The new module is greenfield while the integration remains brownfield.');
    expect(override.closest('details')).toBeNull();
    expect(screen.getAllByRole('heading', {
      level: 4,
      name: 'Existing scaffold',
    })).toHaveLength(1);
    expect(screen.queryByRole('heading', {
      level: 4,
      name: 'Existing constraint',
    })).not.toBeInTheDocument();
    const groupHeadings = screen.getAllByRole('heading', { level: 4 })
      .map((heading) => heading.textContent)
      .filter((heading): heading is string => Boolean(
        heading && ['Existing scaffold', 'Reference pattern'].includes(heading),
      ));
    expect(groupHeadings).toEqual(['Existing scaffold', 'Reference pattern']);

    const scaffoldHeading = screen.getByRole('heading', {
      level: 4,
      name: 'Existing scaffold',
    });
    const scaffoldGroup = scaffoldHeading.closest('section');
    expect(scaffoldGroup).not.toBeNull();
    expect(within(scaffoldGroup as HTMLElement).getAllByRole('heading', { level: 3 })
      .map((heading) => heading.textContent)).toEqual(['Scaffold alpha', 'Scaffold beta']);
  });

  it('ts_007019f7 — offers a deterministic Retry after a recoverable projection error', async () => {
    apiMock.getCodeTraceabilityProjection
      .mockRejectedValueOnce(new Error('Context projection could not be loaded.'))
      .mockResolvedValueOnce(projection({
        source_context: sourceContext({
          role_counts: emptyRoleCounts,
          classification_state: { classified_count: 0, uncategorized_legacy_count: 0 },
          evidence_applicable: null,
        }),
      }));

    renderPanel();

    expect(await screen.findByRole('alert')).toHaveTextContent('Context projection could not be loaded.');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('heading', { name: 'Source context' })).toBeInTheDocument();
    await waitFor(() => expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(2));
  });
});
