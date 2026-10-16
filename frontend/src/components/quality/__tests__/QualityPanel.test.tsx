import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  CurrentQualityAssessment,
  QualityAssessmentReceipt,
} from '@/types';
import { QualityGatePreviewCard } from '../QualityGatePreview';
import { QualityPanel } from '../QualityPanel';

const apiMock = vi.hoisted(() => ({
  getValidationCycle: vi.fn(),
  getValidationTechnicalAudit: vi.fn(),
  getCurrentQualityAssessment: vi.fn(),
  listQualityAssessments: vi.fn(),
  listQualityFindings: vi.fn(),
  recordAmbiguityAssessment: vi.fn(),
}));

const toastMock = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
}));

vi.mock('@/services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api')>();
  return {
    ...actual,
    useDashboardApi: () => apiMock,
  };
});
vi.mock('react-hot-toast', () => ({ default: toastMock }));

function receipt(
  overrides: Partial<QualityAssessmentReceipt> = {},
): QualityAssessmentReceipt {
  return {
    id: 'receipt-1',
    board_id: 'board-1',
    subject_type: 'ideation',
    subject_id: 'ideation-1',
    subject_version: 7,
    subject_edition: 1,
    assessment_kind: 'ambiguity',
    origin: 'human_or_agent',
    source: 'native',
    channel: 'rest',
    outcome: 'recorded',
    scale: {
      kind: 'ambiguity_score',
      minimum: 1,
      maximum: 5,
      direction: 'lower_better',
    },
    score: 3,
    justification: 'Pinpointed ambiguity',
    digests: {
      content_digest: 'a',
      clarification_digest: 'b',
      ruleset_digest: 'c',
      taxonomy_digest: 'd',
      policy_digest: 'e',
      input_digest: 'f',
      canonicalization_version: 'v1',
    },
    versions: {
      ruleset_version: 'v1',
      taxonomy_version: 'v1',
      analyzer_version: 'v1',
      policy_version: 'v1',
    },
    run_identity_digest: 'g',
    authority_digest: 'h',
    idempotency_key: 'idem',
    request_digest: 'i',
    created_by: 'agent-1',
    created_at: '2026-07-28T12:00:00Z',
    predecessor_receipt_id: null,
    contract_version: 'quality-assessment/v1',
    ...overrides,
  };
}

function currentAssessment(
  overrides: Partial<CurrentQualityAssessment> = {},
): CurrentQualityAssessment {
  return {
    receipt: receipt(),
    head_revision: 4,
    currentness: 'current',
    lifecycle_state: 'current',
    edition: 1,
    stale_reasons: [],
    gate_preview: {
      applicable: true,
      enabled: true,
      allowed: true,
      reason_code: 'ambiguity_gate_ready',
      threshold: 3,
      score: 3,
      skipped: false,
    },
    ...overrides,
  };
}

function page<T>(items: T[]) {
  return {
    items,
    total_filtered: items.length,
    total_overall: items.length,
    offset: 0,
    limit: 25,
  };
}

function validationCycle() {
  return {
      subject_type: 'ideation',
      subject_id: 'ideation-1',
      edition: 1,
      subject_status: 'evaluating',
      cycle_state: 'in_progress',
      current_result: {
        result_id: 'receipt-1',
        result_type: 'ambiguity_assessment',
        subject_edition: 1,
        status: 'passed',
        summary: {
          score: 3,
          threshold: 3,
          created_at: '2026-07-28T12:00:00Z',
          created_by: 'agent-1',
          justification: 'Pinpointed ambiguity',
        },
      },
      previous_result_count: 0,
      previous_results: [],
      submission_fence: {
        expected_validation_edition: 1,
        expected_subject_version: 7,
        expected_head_revision: 4,
      },
    };
}

describe('QualityPanel', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    apiMock.getValidationCycle.mockImplementation(async (subjectType, subjectId) => ({
      ...validationCycle(),
      subject_type: subjectType,
      subject_id: subjectId,
    }));
    apiMock.getValidationTechnicalAudit.mockResolvedValue({
      subject_type: 'ideation',
      subject_id: 'ideation-1',
      result_id: 'receipt-1',
      result_type: 'ambiguity_assessment',
      subject_edition: 1,
      technical_audit: {
        receipt_id: 'receipt-1',
        subject_version: 7,
        head_revision: 4,
        digests: {},
        visible_exception_types: [],
        exceptions: [],
      },
    });
    apiMock.getCurrentQualityAssessment.mockResolvedValue(currentAssessment({
      receipt: receipt({ subject_type: 'spec', subject_id: 'spec-1', assessment_kind: 'requirement_lint' }),
    }));
    apiMock.listQualityAssessments.mockResolvedValue(page([
      {
        receipt: receipt({ subject_type: 'spec', subject_id: 'spec-1', assessment_kind: 'requirement_lint' }),
        is_head: false,
        state: 'previous',
        currentness: {
          current: false,
          state: 'previous',
          stale_reasons: ['subject_edition_changed'],
        },
      },
    ]));
    apiMock.listQualityFindings.mockResolvedValue(page([
      {
        id: 'finding-1',
        receipt_id: 'receipt-1',
        assessment_kind: 'ambiguity',
        finding_key: 'finding-key-1',
        category_code: 'functional_scope_behavior',
        taxonomy_version: 'v1',
        severity: 'medium',
        confidence: 1,
        deterministic: false,
        blocking_eligible: true,
        title: 'Unclear actor',
        detail: 'The primary actor is not identified.',
        anchor: {
          board_id: 'board-1',
          subject_type: 'ideation',
          subject_id: 'ideation-1',
          subject_version: 7,
          input_digest: 'digest',
          anchor_type: 'field',
          anchor_ref: 'problem_statement',
          excerpt_hash: null,
        },
        evidence_refs: [],
        lifecycle: 'open',
        created_at: '2026-07-28T12:00:00Z',
        remediation: 'Name the actor.',
        rule_code: null,
      },
    ]));
    apiMock.recordAmbiguityAssessment.mockResolvedValue({
      outcome: 'success',
      replayed: false,
      receipt_id: 'receipt-2',
      head_revision: 5,
      qa_id_map: {},
    });
  });

  it('renders Gate preview only for assessments backed by a real gate', () => {
    const { rerender } = render(
      <QualityGatePreviewCard assessment={currentAssessment()} />,
    );
    expect(screen.getByTestId('quality-gate-preview')).toBeInTheDocument();

    rerender(
      <QualityGatePreviewCard
        assessment={currentAssessment({
          gate_preview: {
            applicable: false,
            enabled: false,
            allowed: true,
            reason_code: 'not_applicable',
            threshold: null,
            score: 2,
            skipped: false,
          },
        })}
      />,
    );

    expect(screen.queryByTestId('quality-gate-preview')).not.toBeInTheDocument();
    expect(screen.queryByText('Not applicable')).not.toBeInTheDocument();
  });

  it('presents the legacy ambiguity-stale reason as an edition-based Previous result', () => {
    render(
      <QualityGatePreviewCard
        assessment={currentAssessment({
          currentness: 'previous',
          stale_reasons: ['subject_edition_changed'],
          gate_preview: {
            applicable: true,
            enabled: true,
            allowed: false,
            reason_code: 'ambiguity_assessment_stale',
            threshold: 3,
            score: 3,
            skipped: false,
          },
        })}
      />,
    );

    expect(screen.getByTestId('quality-gate-preview-status')).toHaveTextContent(
      'current-edition assessment required',
    );
    expect(screen.getByText('Previous')).toBeInTheDocument();
    expect(screen.getByTestId('quality-previous-result-guidance')).toHaveTextContent(
      'available under Previous',
    );
    expect(screen.queryByText(/stale/i)).not.toBeInTheDocument();
  });

  it('keeps current-edition findings filterable and paginated without a compatibility mode', async () => {
    const findings = await apiMock.listQualityFindings();
    apiMock.listQualityFindings.mockClear();
    apiMock.listQualityFindings.mockResolvedValue({ ...findings, total_filtered: 60, total_overall: 60 });
    render(
      <QualityPanel subjectEdition={1} subjectType="ideation" subjectId="ideation-1"
        subjectVersion={7} subjectStatus="evaluating" subjectArchived={false}
        canRead canAssess={false} canProposeQuestions={false} />,
    );
    expect(await screen.findByRole('img', { name: 'Ambiguity score 3 out of 5' })).toBeInTheDocument();
    expect(screen.getByTestId('quality-current-status')).toHaveTextContent('Passed');
    expect(screen.getByTestId('quality-read-only')).toHaveTextContent('permissions do not allow');
    expect(screen.queryByRole('button', { name: 'Record assessment' })).not.toBeInTheDocument();
    expect(screen.getByTestId('quality-previous-results-toggle')).toHaveAttribute('aria-expanded', 'false');
    expect(apiMock.listQualityFindings).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    expect(await screen.findByText('Unclear actor')).toBeInTheDocument();
    const findingsPanel = screen.getByTestId('quality-findings-content');
    fireEvent.change(within(findingsPanel).getByLabelText('Severity'), { target: { value: 'high' } });
    fireEvent.change(within(findingsPanel).getByLabelText('Category'), { target: { value: 'domain_data_model' } });
    await waitFor(() => expect(apiMock.listQualityFindings).toHaveBeenLastCalledWith(
      'ideation', 'ideation-1', expect.objectContaining({
        subjectEdition: 1, receiptId: 'receipt-1', severity: 'high', categoryCode: 'domain_data_model', offset: 0,
      }),
    ));
    const paginator = screen.getByTestId('quality-findings-paginator');
    const next = within(paginator).getByRole('button', { name: 'Next page' });
    await waitFor(() => expect(next).toBeEnabled());
    fireEvent.click(next);
    await waitFor(() => expect(apiMock.listQualityFindings).toHaveBeenLastCalledWith(
      'ideation', 'ideation-1', expect.objectContaining({ offset: 25, limit: 25, receiptId: 'receipt-1' }),
    ));
    fireEvent.change(within(paginator).getByLabelText('Items per page'), { target: { value: '50' } });
    await waitFor(() => expect(apiMock.listQualityFindings).toHaveBeenLastCalledWith(
      'ideation', 'ideation-1', expect.objectContaining({ offset: 0, limit: 50 }),
    ));
    expect(apiMock.getCurrentQualityAssessment).not.toHaveBeenCalled();
    expect(apiMock.listQualityAssessments).not.toHaveBeenCalled();
  });

  it('loads lifecycle details without consulting the legacy receipt-state list', async () => {
    apiMock.listQualityAssessments.mockRejectedValue(
      new Error('assessment_receipt_state_mismatch'),
    );
    const lifecycleCycle = {
      subject_type: 'ideation',
      subject_id: 'ideation-1',
      edition: 2,
      subject_status: 'evaluating',
      cycle_state: 'in_progress',
      current_result: {
        result_id: 'receipt-edition-2',
        result_type: 'ambiguity_assessment',
        subject_edition: 2,
        status: 'passed',
        summary: { score: 3, threshold: 3 },
      },
      previous_result_count: 26,
      previous_results: [],
      submission_fence: {
        expected_validation_edition: 2,
        expected_subject_version: 7,
        expected_head_revision: 4,
      },
    } as const;
    apiMock.getValidationCycle
      .mockResolvedValueOnce(lifecycleCycle)
      .mockResolvedValue({
        ...lifecycleCycle,
        previous_results: [{
          result_id: 'receipt-edition-1',
          result_type: 'ambiguity_assessment',
          subject_edition: 1,
          status: 'completed',
          summary: {
            score: 2,
            scale_maximum: 5,
            created_at: '2026-07-27T12:00:00Z',
            created_by: 'agent-1',
            justification: 'Earlier accepted assessment',
          },
        }],
      });
    apiMock.getValidationTechnicalAudit.mockResolvedValue({
      subject_type: 'ideation',
      subject_id: 'ideation-1',
      result_id: 'receipt-edition-2',
      result_type: 'ambiguity_assessment',
      subject_edition: 2,
      technical_audit: {
        receipt_id: 'receipt-edition-2',
        subject_version: 7,
        head_revision: 4,
        digests: {},
        visible_exception_types: [],
        exceptions: [],
      },
    });

    render(
      <QualityPanel
        subjectType="ideation"
        subjectId="ideation-1"
        subjectVersion={7}
        subjectEdition={2}
        subjectStatus="evaluating"
        subjectArchived={false}
        canRead
        canAssess={false}
        canProposeQuestions={false}
      />,
    );

    expect(await screen.findByText('Edition 2')).toBeInTheDocument();
    expect(screen.getByText(
      'One current ambiguity result is kept for each lifecycle edition.',
    )).toBeInTheDocument();
    expect(apiMock.listQualityFindings).not.toHaveBeenCalled();
    expect(apiMock.listQualityAssessments).not.toHaveBeenCalled();
    expect(apiMock.getValidationTechnicalAudit).not.toHaveBeenCalled();
    expect(apiMock.getCurrentQualityAssessment).not.toHaveBeenCalled();
    expect(apiMock.getValidationCycle).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/stale/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/receipt-edition-2/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/head r/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/subject r/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/subject v/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    await waitFor(() => expect(apiMock.listQualityFindings).toHaveBeenCalledWith(
      'ideation',
      'ideation-1',
      expect.objectContaining({
        assessmentKind: 'ambiguity',
        receiptId: 'receipt-edition-2',
        subjectEdition: 2,
      }),
    ));
    expect(apiMock.listQualityFindings).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    expect(apiMock.listQualityFindings).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('quality-previous-results-toggle'));
    await waitFor(() => expect(apiMock.getValidationCycle).toHaveBeenCalledTimes(2));
    expect(apiMock.getValidationCycle).toHaveBeenLastCalledWith(
      'ideation',
      'ideation-1',
      expect.objectContaining({
        includePrevious: true,
        offset: 0,
        limit: 25,
      }),
    );
    expect(apiMock.listQualityAssessments).not.toHaveBeenCalled();
    expect(screen.getByTestId('quality-previous-results')).toHaveTextContent('Edition 1');
    expect(screen.getByTestId('quality-previous-results')).toHaveTextContent(
      'Earlier accepted assessment',
    );
    expect(screen.queryByText(/assessment_receipt_state_mismatch/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('quality-previous-results-toggle'));
    fireEvent.click(screen.getByTestId('quality-previous-results-toggle'));
    expect(apiMock.getValidationCycle).toHaveBeenCalledTimes(2);

    const nextPage = within(
      screen.getByTestId('quality-previous-results-paginator'),
    ).getByRole('button', { name: 'Next page' });
    await waitFor(() => expect(nextPage).toBeEnabled());
    fireEvent.click(nextPage);
    await waitFor(() => expect(apiMock.getValidationCycle).toHaveBeenCalledTimes(3));
    expect(apiMock.getValidationCycle).toHaveBeenLastCalledWith(
      'ideation',
      'ideation-1',
      expect.objectContaining({
        includePrevious: true,
        offset: 25,
        limit: 25,
      }),
    );

    fireEvent.click(screen.getByTestId('technical-audit-toggle'));
    await waitFor(() => expect(
      apiMock.getValidationTechnicalAudit,
    ).toHaveBeenCalledTimes(1));
    expect(screen.getAllByText('receipt-edition-2')).toHaveLength(2);
    expect(screen.getByText('subject r7 · head r4')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('technical-audit-toggle'));
    fireEvent.click(screen.getByTestId('technical-audit-toggle'));
    expect(apiMock.getValidationTechnicalAudit).toHaveBeenCalledTimes(1);
  });

  it('renders the current edition as failed when ambiguity exceeds its threshold', async () => {
    apiMock.getValidationCycle.mockResolvedValue({
      subject_type: 'ideation',
      subject_id: 'ideation-1',
      edition: 2,
      subject_status: 'evaluating',
      cycle_state: 'completed',
      current_result: {
        result_id: 'receipt-above-threshold',
        result_type: 'ambiguity_assessment',
        subject_edition: 2,
        status: 'failed',
        summary: {
          score: 4,
          threshold: 2,
          skipped: false,
          enabled: true,
          allowed: false,
          reason_code: 'ambiguity_score_exceeds_threshold',
          headline: 'Ambiguity exceeds the allowed limit',
        },
      },
      previous_result_count: 1,
      previous_results: [],
      submission_fence: {
        expected_validation_edition: 2,
        expected_subject_version: 7,
        expected_head_revision: 2,
      },
    });

    render(
      <QualityPanel
        subjectType="ideation"
        subjectId="ideation-1"
        subjectVersion={7}
        subjectEdition={2}
        subjectStatus="evaluating"
        subjectArchived={false}
        canRead
        canAssess={false}
        canProposeQuestions={false}
      />,
    );

    const current = await screen.findByTestId('quality-current-result');
    expect(current).toHaveTextContent('Ambiguity exceeds the allowed limit');
    expect(screen.getByTestId('quality-current-status')).toHaveTextContent('Failed');
    expect(
      within(current).getByRole('img', { name: 'Ambiguity score 4 out of 5' }),
    ).toHaveClass('border-red-400');
    expect(current).toHaveTextContent('Maximum accepted score 2');
    expect(current).not.toHaveTextContent(/stale/i);
  });

  it('refuses an editionless result instead of rendering imported history', async () => {
    const cycle = validationCycle();
    apiMock.getValidationCycle.mockResolvedValue({
      ...cycle, current_result: { ...cycle.current_result, subject_edition: null },
    });
    render(
      <QualityPanel subjectEdition={1} subjectType="ideation" subjectId="ideation-1"
        subjectVersion={7} subjectStatus="evaluating" subjectArchived={false}
        canRead canAssess canProposeQuestions={false} />,
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('does not match this subject edition');
    expect(screen.queryByText('Legacy')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Record assessment' })).toBeDisabled();
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    expect(apiMock.listQualityFindings).not.toHaveBeenCalled();
  });

  it('does not request lifecycle findings without a current edition result', async () => {
    apiMock.getCurrentQualityAssessment.mockResolvedValue(null);

    render(
      <QualityPanel
        subjectType="spec"
        subjectId="spec-1"
        subjectVersion={9}
        subjectEdition={3}
        subjectStatus="approved"
        subjectArchived={false}
        canRead
        canAssess={false}
        canProposeQuestions={false}
      />,
    );

    expect(await screen.findByText('No result for Edition 3')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    await waitFor(() => expect(
      screen.getByText('No findings were recorded for this edition.'),
    ).toBeInTheDocument());
    expect(apiMock.listQualityFindings).not.toHaveBeenCalled();
  });

  it.each([
    {
      subjectType: 'ideation' as const,
      subjectStatus: 'evaluating' as const,
    },
    {
      subjectType: 'refinement' as const,
      subjectStatus: 'approved' as const,
    },
    {
      subjectType: 'spec' as const,
      subjectStatus: 'review' as const,
    },
  ])(
    'shares independent collapsed sections with $subjectType',
    async ({ subjectType, subjectStatus }) => {
      render(
        <QualityPanel subjectEdition={1}
          subjectType={subjectType}
          subjectId={`${subjectType}-1`}
          subjectVersion={7}
          subjectStatus={subjectStatus}
          subjectArchived={false}
          canRead
          canAssess={false}
          canProposeQuestions={false}
        />,
      );

      await screen.findByTestId('quality-score-ring');
      const historyToggle = screen.getByTestId('quality-previous-results-toggle');
      const findingsToggle = screen.getByTestId('quality-findings-toggle');

      expect(historyToggle).toHaveAttribute('aria-expanded', 'false');
      expect(findingsToggle).toHaveAttribute('aria-expanded', 'false');

      fireEvent.click(historyToggle);
      expect(historyToggle).toHaveAttribute('aria-expanded', 'true');
      expect(screen.getByTestId('quality-previous-results-content')).toBeInTheDocument();
      expect(screen.queryByTestId('quality-findings-content')).not.toBeInTheDocument();

      fireEvent.click(historyToggle);
      expect(historyToggle).toHaveAttribute('aria-expanded', 'false');
      expect(screen.getByTestId('quality-previous-results-content')).not.toBeVisible();

      fireEvent.click(findingsToggle);
      expect(findingsToggle).toHaveAttribute('aria-expanded', 'true');
      expect(screen.getByTestId('quality-findings-content')).toBeInTheDocument();
      expect(screen.getByTestId('quality-previous-results-content')).not.toBeVisible();
    },
  );

  it.each([
    { status: 'failed', headline: 'Ambiguity exceeds the allowed limit', label: 'Failed', ringClass: 'border-red-400' },
    { status: 'passed', headline: 'Ambiguity gate skipped by override', label: 'Passed', ringClass: 'border-emerald-400' },
    { status: 'passed', headline: 'Ambiguity gate is disabled', label: 'Passed', ringClass: 'border-emerald-400' },
  ])('keeps the current result consistent with the server summary: $headline', async ({ status, headline, label, ringClass }) => {
    const cycle = validationCycle();
    apiMock.getValidationCycle.mockResolvedValue({
      ...cycle,
      current_result: { ...cycle.current_result, status, summary: { score: 3, threshold: 2, headline } },
    });
    render(
      <QualityPanel subjectEdition={1} subjectType="ideation" subjectId="ideation-1"
        subjectVersion={7} subjectStatus="evaluating" subjectArchived={false}
        canRead canAssess={false} canProposeQuestions={false} />,
    );
    expect(await screen.findByTestId('quality-score-ring')).toHaveClass(ringClass);
    expect(screen.getByText(headline)).toBeInTheDocument();
    expect(screen.getByTestId('quality-current-status')).toHaveTextContent(label);
  });

  it('refreshes findings against the new current result in the same cycle response', async () => {
    const cycle = validationCycle();
    apiMock.getValidationCycle.mockResolvedValueOnce(cycle).mockResolvedValue({
      ...cycle, current_result: { ...cycle.current_result, result_id: 'receipt-fresh' },
    });
    render(
      <QualityPanel subjectEdition={1} subjectType="ideation" subjectId="ideation-1"
        subjectVersion={7} subjectStatus="evaluating" subjectArchived={false}
        canRead canAssess={false} canProposeQuestions={false} />,
    );
    await screen.findByTestId('quality-score-ring');
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    await waitFor(() => expect(apiMock.listQualityFindings).toHaveBeenCalledWith(
      'ideation', 'ideation-1', expect.objectContaining({ receiptId: 'receipt-1' }),
    ));
    const refresh = screen.getByRole('button', { name: 'Refresh' });
    await waitFor(() => expect(refresh).toBeEnabled());
    fireEvent.click(refresh);
    await waitFor(() => expect(apiMock.listQualityFindings).toHaveBeenLastCalledWith(
      'ideation', 'ideation-1', expect.objectContaining({ receiptId: 'receipt-fresh', subjectEdition: 1 }),
    ));
  });

  it('keeps manual ambiguity authoring unavailable outside its lifecycle without an inline warning', async () => {
    render(
      <QualityPanel subjectEdition={1}
        subjectType="ideation"
        subjectId="subject-1"
        subjectVersion={7}
        subjectStatus="review"
        subjectArchived={false}
        canRead
        canAssess
        canProposeQuestions
      />,
    );

    await screen.findByTestId('quality-score-ring');
    expect(screen.queryByRole('button', { name: 'Record assessment' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('quality-read-only')).not.toBeInTheDocument();
    expect(screen.queryByText(/manual ambiguity assessment is available only/i))
      .not.toBeInTheDocument();
  });

  it('retains the actionable archive explanation when ambiguity authoring is unavailable', async () => {
    render(
      <QualityPanel subjectEdition={1}
        subjectType="refinement"
        subjectId="subject-1"
        subjectVersion={7}
        subjectStatus="approved"
        subjectArchived
        canRead
        canAssess
        canProposeQuestions
      />,
    );

    await screen.findByTestId('quality-score-ring');
    expect(screen.queryByRole('button', { name: 'Record assessment' })).not.toBeInTheDocument();
    expect(screen.getByTestId('quality-read-only')).toHaveTextContent(
      'archived subjects cannot receive',
    );
  });

  it('omits the question composer and sends no questions without the Q&A ask leaf', async () => {
    render(
      <QualityPanel subjectEdition={1}
        subjectType="ideation"
        subjectId="ideation-1"
        subjectVersion={7}
        subjectStatus="evaluating"
        subjectArchived={false}
        canRead
        canAssess
        canProposeQuestions={false}
      />,
    );

    await screen.findByTestId('quality-score-ring');
    fireEvent.click(screen.getByRole('button', { name: 'Record assessment' }));
    expect(screen.queryByRole('button', { name: 'Add question' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Record governed assessment' }));

    await waitFor(() => expect(apiMock.recordAmbiguityAssessment).toHaveBeenCalled());
    expect(apiMock.recordAmbiguityAssessment.mock.calls[0][2]).toMatchObject({
      proposed_questions: [],
    });
  });

  it('records a governed assessment with score, pinpoint finding and optional question', async () => {
    const onAssessmentRecorded = vi.fn();
    render(
      <QualityPanel subjectEdition={1}
        subjectType="refinement"
        subjectId="refinement-1"
        subjectVersion={7}
        subjectStatus="approved"
        subjectArchived={false}
        canRead
        canAssess
        canProposeQuestions
        onAssessmentRecorded={onAssessmentRecorded}
      />,
    );

    await screen.findByTestId('quality-score-ring');
    fireEvent.click(screen.getByRole('button', { name: 'Record assessment' }));
    fireEvent.change(screen.getByLabelText('Ambiguity score'), {
      target: { value: '3' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Record governed assessment' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Scores above 1 require at least one pinpoint finding',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Add finding' }));
    fireEvent.change(screen.getByLabelText('Title'), {
      target: { value: 'Unclear retry behavior' },
    });
    fireEvent.change(screen.getByLabelText('Detail'), {
      target: { value: 'The refinement does not say when retries stop.' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add question' }));
    fireEvent.change(screen.getByLabelText('Question 1'), {
      target: { value: 'How many retry attempts are allowed?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Record governed assessment' }));

    await waitFor(() => expect(apiMock.recordAmbiguityAssessment).toHaveBeenCalledTimes(1));
    expect(apiMock.recordAmbiguityAssessment).toHaveBeenCalledWith(
      'refinement',
      'refinement-1',
      expect.objectContaining({
        idempotency_key: expect.any(String),
        expected_subject_version: 7,
        expected_head_revision: 4,
        score: 3,
        findings: [
          expect.objectContaining({
            finding_key: expect.any(String),
            category_code: 'functional_scope_behavior',
            severity: 'medium',
            deterministic: false,
            confidence: 1,
            title: 'Unclear retry behavior',
            detail: 'The refinement does not say when retries stop.',
            anchor: {
              anchor_type: 'whole_artifact',
              anchor_ref: null,
              excerpt_hash: null,
            },
          }),
        ],
        proposed_questions: [
          expect.objectContaining({
            question: 'How many retry attempts are allowed?',
            question_type: 'text',
            allow_free_text: true,
            choices: [],
          }),
        ],
      }),
    );
    expect(onAssessmentRecorded).toHaveBeenCalled();
  });

  it('reuses a Quality idempotency key for the same failed intent and rotates on change and success', async () => {
    apiMock.recordAmbiguityAssessment
      .mockRejectedValueOnce(new Error('temporary outage'))
      .mockRejectedValueOnce(new Error('temporary outage'))
      .mockResolvedValueOnce({
        outcome: 'success',
        replayed: false,
        receipt_id: 'receipt-2',
        head_revision: 5,
        qa_id_map: {},
      })
      .mockResolvedValueOnce({
        outcome: 'success',
        replayed: false,
        receipt_id: 'receipt-3',
        head_revision: 6,
        qa_id_map: {},
      });
    render(
      <QualityPanel subjectEdition={1}
        subjectType="ideation"
        subjectId="ideation-1"
        subjectVersion={7}
        subjectStatus="evaluating"
        subjectArchived={false}
        canRead
        canAssess
        canProposeQuestions={false}
      />,
    );

    await screen.findByTestId('quality-score-ring');
    fireEvent.click(screen.getByRole('button', { name: 'Record assessment' }));
    const submit = screen.getByRole('button', { name: 'Record governed assessment' });
    fireEvent.click(submit);
    await waitFor(() => expect(apiMock.recordAmbiguityAssessment).toHaveBeenCalledTimes(1));
    const firstKey = apiMock.recordAmbiguityAssessment.mock.calls[0][2].idempotency_key;

    fireEvent.click(submit);
    await waitFor(() => expect(apiMock.recordAmbiguityAssessment).toHaveBeenCalledTimes(2));
    expect(apiMock.recordAmbiguityAssessment.mock.calls[1][2].idempotency_key).toBe(firstKey);

    fireEvent.change(screen.getByLabelText('Ambiguity score'), {
      target: { value: '2' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add finding' }));
    fireEvent.change(screen.getByLabelText('Title'), {
      target: { value: 'Unclear timeout' },
    });
    fireEvent.change(screen.getByLabelText('Detail'), {
      target: { value: 'The timeout is not specified.' },
    });
    fireEvent.click(submit);
    await waitFor(() => expect(apiMock.recordAmbiguityAssessment).toHaveBeenCalledTimes(3));
    const changedKey = apiMock.recordAmbiguityAssessment.mock.calls[2][2].idempotency_key;
    expect(changedKey).not.toBe(firstKey);

    const reopen = await screen.findByRole('button', { name: 'Record assessment' });
    await waitFor(() => expect(reopen).not.toBeDisabled());
    fireEvent.click(reopen);
    fireEvent.click(screen.getByRole('button', { name: 'Record governed assessment' }));
    await waitFor(() => expect(apiMock.recordAmbiguityAssessment).toHaveBeenCalledTimes(4));
    expect(apiMock.recordAmbiguityAssessment.mock.calls[3][2].idempotency_key)
      .not.toBe(firstKey);
  });

  it('clears question links when their finding is removed', async () => {
    render(
      <QualityPanel subjectEdition={1}
        subjectType="refinement"
        subjectId="refinement-1"
        subjectVersion={7}
        subjectStatus="approved"
        subjectArchived={false}
        canRead
        canAssess
        canProposeQuestions
      />,
    );

    await screen.findByTestId('quality-score-ring');
    fireEvent.click(screen.getByRole('button', { name: 'Record assessment' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add finding' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add question' }));
    fireEvent.change(screen.getByLabelText('Question 1'), {
      target: { value: 'Which requirement should be clarified?' },
    });
    const linkedFinding = screen.getByLabelText('Linked finding') as HTMLSelectElement;
    const findingOption = linkedFinding.options[1];
    fireEvent.change(linkedFinding, { target: { value: findingOption.value } });
    expect(linkedFinding.value).toBe(findingOption.value);

    fireEvent.click(screen.getByRole('button', { name: 'Remove finding' }));
    expect(linkedFinding).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Record governed assessment' }));

    await waitFor(() => expect(apiMock.recordAmbiguityAssessment).toHaveBeenCalled());
    expect(apiMock.recordAmbiguityAssessment.mock.calls[0][2].proposed_questions[0])
      .toMatchObject({ finding_keys: [] });
  });

  it('keeps spec quality read-only and exposes only native requirement lint', async () => {
    const onOpenHelp = vi.fn();
    apiMock.getCurrentQualityAssessment.mockResolvedValue(currentAssessment({
      receipt: receipt({
        subject_type: 'spec',
        subject_id: 'spec-1',
        subject_version: 9,
        assessment_kind: 'requirement_lint',
        origin: 'human_or_agent',
        channel: 'mcp',
        outcome: 'advisory',
        scale: {
          kind: 'finding_count',
          minimum: 0,
          maximum: 13,
          direction: 'lower_better',
        },
        score: 2,
      }),
      gate_preview: {
        applicable: false,
        enabled: false,
        allowed: true,
        reason_code: 'not_applicable',
        threshold: null,
        score: 2,
        skipped: false,
      },
    }));
    render(
      <QualityPanel subjectEdition={1}
        subjectType="spec"
        subjectId="spec-1"
        subjectVersion={9}
        subjectStatus="review"
        subjectArchived={false}
        canRead
        canAssess
        canProposeQuestions={false}
        onOpenHelp={onOpenHelp}
      />,
    );

    expect(
      await screen.findByRole('heading', { name: 'Requirement lint' }),
    ).toBeInTheDocument();
    await waitFor(() => expect(
      apiMock.getCurrentQualityAssessment,
    ).toHaveBeenCalledWith(
      'spec',
      'spec-1',
      'requirement_lint',
      expect.any(AbortSignal),
      1,
    ));
    expect(
      screen.queryByRole('tab', { name: 'Spec validation' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Requirement lint' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Ambiguity' })).not.toBeInTheDocument();
    expect(apiMock.listQualityAssessments).not.toHaveBeenCalled();
    expect(apiMock.listQualityFindings).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('quality-previous-results-toggle'));
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    await waitFor(() => expect(apiMock.listQualityAssessments).toHaveBeenCalledWith(
      'spec',
      'spec-1',
      expect.objectContaining({
        assessmentKind: 'requirement_lint',
      }),
    ));
    expect(apiMock.listQualityFindings).toHaveBeenCalledWith(
      'spec',
      'spec-1',
      expect.objectContaining({
        assessmentKind: 'requirement_lint',
      }),
    );
    expect(apiMock.getCurrentQualityAssessment).not.toHaveBeenCalledWith(
      'spec',
      'spec-1',
      'spec_validation',
      expect.anything(),
    );
    expect(apiMock.listQualityAssessments).not.toHaveBeenCalledWith(
      'spec',
      'spec-1',
      expect.objectContaining({ assessmentKind: 'spec_validation' }),
    );
    expect(apiMock.listQualityFindings).not.toHaveBeenCalledWith(
      'spec',
      'spec-1',
      expect.objectContaining({ assessmentKind: 'spec_validation' }),
    );
    expect(screen.queryByTestId('quality-gate-preview')).not.toBeInTheDocument();
    expect(screen.queryByText('Gate preview')).not.toBeInTheDocument();
    expect(screen.queryByText('Not applicable')).not.toBeInTheDocument();
    const scoreRing = screen.getByTestId('quality-score-ring');
    expect(scoreRing).toHaveAccessibleName(
      'Requirement lint score 2 out of 13',
    );
    expect(scoreRing).toHaveClass(
      'h-16',
      'w-16',
      'rounded-full',
      'border-4',
      'border-blue-400',
    );
    expect(screen.getByTestId('quality-current-result')).toHaveTextContent('2 lint findings');
    expect(screen.getByTestId('quality-current-result')).toHaveTextContent('13 rules evaluated · lower is better');
    expect(screen.getByTestId('quality-advisory-notice')).toHaveTextContent(
      'An accepted result for the current edition is required to continue',
    );
    expect(screen.getByTestId('quality-advisory-notice')).toHaveTextContent(
      'findings remain advisory and do not block by count or severity',
    );
    fireEvent.click(
      screen.getByRole('button', {
        name: 'How is requirement lint calculated?',
      }),
    );
    expect(onOpenHelp).toHaveBeenCalledOnce();
    expect(screen.queryByTestId('quality-read-only')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Record assessment' })).not.toBeInTheDocument();
  });

  it('shows and paginates native lint history independently of current-edition findings', async () => {
    apiMock.getCurrentQualityAssessment.mockResolvedValue(null);
    const previous = {
      receipt: receipt({
        id: 'lint-edition-1', subject_type: 'spec', subject_id: 'spec-1',
        assessment_kind: 'requirement_lint', subject_edition: 1,
        score: 2, justification: 'Earlier lint observations',
        scale: { kind: 'finding_count', minimum: 0, maximum: 13, direction: 'lower_better' },
      }),
      state: 'previous', is_head: false,
      currentness: { current: false, state: 'previous', stale_reasons: ['subject_edition_changed'] },
    };
    apiMock.listQualityAssessments.mockResolvedValue({
      ...page([previous]), total_filtered: 26, total_overall: 27,
    });
    render(
      <QualityPanel subjectEdition={2} subjectType="spec" subjectId="spec-1"
        subjectVersion={9} subjectStatus="review" subjectArchived={false}
        canRead canAssess={false} canProposeQuestions={false} />,
    );
    expect(await screen.findByText('No result for Edition 2')).toBeInTheDocument();
    expect(apiMock.listQualityAssessments).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('quality-previous-results-toggle'));
    expect(await screen.findByText('Earlier lint observations')).toBeInTheDocument();
    const history = screen.getByTestId('quality-previous-results');
    expect(history).toHaveTextContent('Edition 1');
    expect(history).toHaveTextContent('Score 2 of 13');
    expect(history).not.toHaveTextContent('Legacy');
    expect(apiMock.listQualityAssessments).toHaveBeenLastCalledWith(
      'spec', 'spec-1', expect.objectContaining({ state: 'previous', assessmentKind: 'requirement_lint', offset: 0, limit: 25 }),
    );
    const next = within(screen.getByTestId('quality-previous-results-paginator'))
      .getByRole('button', { name: 'Next page' });
    await waitFor(() => expect(next).toBeEnabled());
    fireEvent.click(next);
    await waitFor(() => expect(apiMock.listQualityAssessments).toHaveBeenLastCalledWith(
      'spec', 'spec-1', expect.objectContaining({ state: 'previous', offset: 25, limit: 25 }),
    ));
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    expect(apiMock.listQualityFindings).not.toHaveBeenCalled();
  });

  it('refuses editionless previous results without a Legacy display path', async () => {
    const cycle = validationCycle();
    apiMock.getValidationCycle.mockImplementation(async (_type, _id, options) => ({
      ...cycle,
      previous_results: options.includePrevious
        ? [{ ...cycle.current_result, result_id: 'invalid-result', subject_edition: null }]
        : [],
    }));
    render(
      <QualityPanel subjectEdition={1} subjectType="ideation" subjectId="ideation-1"
        subjectVersion={7} subjectStatus="evaluating" subjectArchived={false}
        canRead canAssess={false} canProposeQuestions={false} />,
    );
    await screen.findByTestId('quality-score-ring');
    fireEvent.click(screen.getByTestId('quality-previous-results-toggle'));
    expect(await screen.findByRole('alert')).toHaveTextContent('history does not match this subject edition');
    expect(screen.queryByText('Legacy')).not.toBeInTheDocument();
    expect(screen.queryByText('invalid-result')).not.toBeInTheDocument();
  });

  it('makes no reads without permission and reloads after permission is restored', async () => {
    const props = {
      subjectEdition: 1, subjectType: 'ideation' as const, subjectId: 'ideation-1',
      subjectVersion: 7, subjectStatus: 'evaluating' as const, subjectArchived: false,
      canAssess: false, canProposeQuestions: false,
    };
    const { rerender } = render(<QualityPanel {...props} canRead={false} />);
    expect(screen.queryByTestId('quality-panel')).not.toBeInTheDocument();
    expect(apiMock.getValidationCycle).not.toHaveBeenCalled();
    rerender(<QualityPanel {...props} canRead />);
    await screen.findByTestId('quality-score-ring');
    expect(apiMock.getValidationCycle).toHaveBeenCalledTimes(1);
    rerender(<QualityPanel {...props} canRead={false} />);
    expect(screen.queryByTestId('quality-panel')).not.toBeInTheDocument();
    rerender(<QualityPanel {...props} canRead />);
    await screen.findByTestId('quality-score-ring');
    expect(apiMock.getValidationCycle).toHaveBeenCalledTimes(2);
  });

  it('quotes the anchored requirement text when anchorTexts provides it', async () => {
    render(
      <QualityPanel subjectEdition={1}
        subjectType="ideation"
        subjectId="ideation-1"
        subjectVersion={7}
        subjectStatus="evaluating"
        subjectArchived={false}
        canRead
        canAssess={false}
        canProposeQuestions={false}
        anchorTexts={{
          problem_statement:
            'AC-1: Given an authorized board, the move succeeds.',
        }}
      />,
    );

    await screen.findByTestId('quality-score-ring');
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));

    const quote = await screen.findByTestId('quality-finding-requirement');
    expect(quote).toHaveTextContent(
      'AC-1: Given an authorized board, the move succeeds.',
    );
  });

});
