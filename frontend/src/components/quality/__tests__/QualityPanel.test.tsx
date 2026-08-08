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

describe('QualityPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMock.getCurrentQualityAssessment.mockResolvedValue(currentAssessment());
    apiMock.listQualityAssessments.mockResolvedValue(page([
      {
        receipt: receipt(),
        is_head: true,
        state: 'current',
        currentness: {
          current: true,
          state: 'current',
          stale_reasons: [],
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

  it('renders currentness and keeps paginated history and pinpoint findings independently collapsible', async () => {
    render(
      <QualityPanel
        subjectType="ideation"
        subjectId="ideation-1"
        subjectVersion={7}
        subjectStatus="evaluating"
        subjectArchived={false}
        canRead
        canAssess={false}
        canProposeQuestions={false}
      />,
    );

    const scoreRing = await screen.findByTestId('quality-score-ring');
    expect(scoreRing).toHaveAccessibleName('Ambiguity score 3 out of 5');
    expect(scoreRing).toHaveClass('h-16', 'w-16', 'rounded-full', 'border-4', 'border-emerald-400');
    expect(screen.getByText('Ambiguity within the allowed limit')).toBeInTheDocument();
    expect(screen.getByText('Maximum tolerated on this board:')).toBeInTheDocument();
    expect(screen.getByTestId('quality-gate-preview-status')).toHaveTextContent('Ready');
    expect(screen.getByTestId('quality-history-toggle')).toHaveAttribute(
      'aria-expanded',
      'false',
    );
    expect(screen.getByTestId('quality-findings-toggle')).toHaveAttribute(
      'aria-expanded',
      'false',
    );
    expect(screen.queryByTestId('quality-history-content')).not.toBeInTheDocument();
    expect(screen.queryByTestId('quality-findings-content')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    expect(screen.getByTestId('quality-findings-toggle')).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getByText('Unclear actor')).toBeInTheDocument();
    expect(screen.getByTestId('quality-read-only')).toHaveTextContent(
      'permissions do not allow',
    );
    expect(screen.queryByRole('button', { name: 'Record assessment' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('quality-history-toggle'));
    expect(screen.getByTestId('quality-history-toggle')).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    const historyPaginator = screen.getByTestId('quality-history-paginator');
    fireEvent.change(within(historyPaginator).getByLabelText('Items per page'), {
      target: { value: '50' },
    });
    await waitFor(() => expect(apiMock.listQualityAssessments).toHaveBeenCalledWith(
      'ideation',
      'ideation-1',
      expect.objectContaining({
        offset: 0,
        limit: 50,
        assessmentKind: 'ambiguity',
      }),
    ));
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
        <QualityPanel
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
      if (subjectType === 'spec') {
        await screen.findByTestId('requirement-lint-summary');
      }
      const historyToggle = screen.getByTestId('quality-history-toggle');
      const findingsToggle = screen.getByTestId('quality-findings-toggle');

      expect(historyToggle).toHaveAttribute('aria-expanded', 'false');
      expect(findingsToggle).toHaveAttribute('aria-expanded', 'false');

      fireEvent.click(historyToggle);
      expect(historyToggle).toHaveAttribute('aria-expanded', 'true');
      expect(screen.getByTestId('quality-history-content')).toBeInTheDocument();
      expect(screen.queryByTestId('quality-findings-content')).not.toBeInTheDocument();

      fireEvent.click(historyToggle);
      expect(historyToggle).toHaveAttribute('aria-expanded', 'false');
      expect(screen.queryByTestId('quality-history-content')).not.toBeInTheDocument();

      fireEvent.click(findingsToggle);
      expect(findingsToggle).toHaveAttribute('aria-expanded', 'true');
      expect(screen.getByTestId('quality-findings-content')).toBeInTheDocument();
      expect(screen.queryByTestId('quality-history-content')).not.toBeInTheDocument();
    },
  );

  it.each([
    {
      caseName: 'blocked',
      assessment: currentAssessment({
        gate_preview: {
          applicable: true,
          enabled: true,
          allowed: false,
          reason_code: 'ambiguity_score_exceeds_threshold',
          threshold: 2,
          score: 3,
          skipped: false,
        },
      }),
      headline: 'Ambiguity exceeds the allowed limit',
      ringClass: 'border-red-400',
      iconState: 'blocked',
    },
    {
      caseName: 'stale',
      assessment: currentAssessment({
        currentness: 'stale',
        stale_reasons: ['subject_version_changed'],
        gate_preview: {
          applicable: true,
          enabled: true,
          allowed: false,
          reason_code: 'ambiguity_assessment_stale',
          threshold: 3,
          score: 3,
          skipped: false,
        },
      }),
      headline: 'Ambiguity assessment is stale',
      ringClass: 'border-amber-400',
      iconState: 'stale',
    },
    {
      caseName: 'skipped',
      assessment: currentAssessment({
        gate_preview: {
          applicable: true,
          enabled: true,
          allowed: true,
          reason_code: 'ambiguity_gate_skipped',
          threshold: 3,
          score: 3,
          skipped: true,
        },
      }),
      headline: 'Ambiguity gate skipped by override',
      ringClass: 'border-amber-400',
      iconState: 'skipped',
    },
    {
      caseName: 'disabled',
      assessment: currentAssessment({
        gate_preview: {
          applicable: true,
          enabled: false,
          allowed: true,
          reason_code: 'ambiguity_gate_disabled',
          threshold: null,
          score: 3,
          skipped: false,
        },
      }),
      headline: 'Ambiguity gate is disabled',
      ringClass: 'border-blue-400',
      iconState: 'neutral',
    },
  ])('keeps the $caseName receipt signal consistent with the server gate reason', async ({
    assessment,
    headline,
    ringClass,
    iconState,
  }) => {
    apiMock.getCurrentQualityAssessment.mockResolvedValueOnce(assessment);

    render(
      <QualityPanel
        subjectType="ideation"
        subjectId="ideation-1"
        subjectVersion={7}
        subjectStatus="evaluating"
        subjectArchived={false}
        canRead
        canAssess={false}
        canProposeQuestions={false}
      />,
    );

    expect(await screen.findByTestId('quality-score-ring')).toHaveClass(ringClass);
    expect(screen.getByText(headline)).toBeInTheDocument();
    expect(screen.getByTestId('quality-receipt-status-icon')).toHaveAttribute(
      'data-state',
      iconState,
    );
  });

  it('uses the current receipt returned by the same refresh when current-only is active', async () => {
    apiMock.getCurrentQualityAssessment
      .mockResolvedValueOnce(currentAssessment())
      .mockResolvedValue(
        currentAssessment({
          receipt: receipt({ id: 'receipt-fresh' }),
          head_revision: 5,
        }),
      );
    render(
      <QualityPanel
        subjectType="ideation"
        subjectId="ideation-1"
        subjectVersion={7}
        subjectStatus="evaluating"
        subjectArchived={false}
        canRead
        canAssess={false}
        canProposeQuestions={false}
      />,
    );

    await screen.findByTestId('quality-score-ring');
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));
    fireEvent.click(screen.getByLabelText('Current receipt only'));

    await waitFor(() => expect(apiMock.listQualityFindings).toHaveBeenLastCalledWith(
      'ideation',
      'ideation-1',
      expect.objectContaining({ receiptId: 'receipt-fresh' }),
    ));
  });

  it.each([
    {
      subjectType: 'ideation' as const,
      subjectStatus: 'review' as const,
      subjectArchived: false,
      reason: 'only while the Ideation is Evaluating',
    },
    {
      subjectType: 'refinement' as const,
      subjectStatus: 'approved' as const,
      subjectArchived: true,
      reason: 'archived subjects cannot receive',
    },
  ])('fails the writer closed outside accepted lifecycle: $reason', async ({
    subjectType,
    subjectStatus,
    subjectArchived,
    reason,
  }) => {
    render(
      <QualityPanel
        subjectType={subjectType}
        subjectId="subject-1"
        subjectVersion={7}
        subjectStatus={subjectStatus}
        subjectArchived={subjectArchived}
        canRead
        canAssess
        canProposeQuestions
      />,
    );

    await screen.findByTestId('quality-score-ring');
    expect(screen.queryByRole('button', { name: 'Record assessment' })).not.toBeInTheDocument();
    expect(screen.getByTestId('quality-read-only')).toHaveTextContent(reason);
  });

  it('omits the question composer and sends no questions without the Q&A ask leaf', async () => {
    render(
      <QualityPanel
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
      <QualityPanel
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
      <QualityPanel
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
      <QualityPanel
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
    apiMock.getCurrentQualityAssessment.mockResolvedValueOnce(currentAssessment({
      receipt: receipt({
        subject_type: 'spec',
        subject_id: 'spec-1',
        subject_version: 9,
        assessment_kind: 'requirement_lint',
        origin: 'semantic_writer',
        channel: 'semantic_writer:bulk_update',
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
      <QualityPanel
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
    ));
    expect(
      screen.queryByRole('tab', { name: 'Spec validation' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Requirement lint' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Ambiguity' })).not.toBeInTheDocument();
    expect(apiMock.listQualityAssessments).toHaveBeenCalledWith(
      'spec',
      'spec-1',
      expect.objectContaining({
        assessmentKind: 'requirement_lint',
      }),
    );
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
    expect(screen.getByTestId('requirement-lint-summary')).toHaveTextContent(
      '2 findings across 13 evaluated rules — lower is better',
    );
    expect(screen.getByTestId('quality-advisory-notice')).toHaveTextContent(
      'never changes transition eligibility',
    );
    expect(screen.getByTestId('quality-advisory-notice')).toHaveTextContent(
      'Checklist and Spec Validation',
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

  it('quotes the anchored requirement text when anchorTexts provides it', async () => {
    render(
      <QualityPanel
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
            'AC-1: Given a legacy board, the move succeeds unchanged.',
        }}
      />,
    );

    await screen.findByTestId('quality-score-ring');
    fireEvent.click(screen.getByTestId('quality-findings-toggle'));

    const quote = await screen.findByTestId('quality-finding-requirement');
    expect(quote).toHaveTextContent(
      'AC-1: Given a legacy board, the move succeeds unchanged.',
    );
  });

});
