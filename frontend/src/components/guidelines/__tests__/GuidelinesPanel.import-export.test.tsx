import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  GuidelineExportEnvelopeV3,
  GuidelineExportMetricV3,
} from '@/types/policy-governance';

const dashboardApiMock = vi.hoisted(() => ({
  getBoardGuidelines: vi.fn(),
  listDefaultGuidelineCandidates: vi.fn(),
  listGuidelines: vi.fn(),
}));
const policyApiMock = vi.hoisted(() => ({
  exportGuidelinePolicy: vi.fn(),
  importGuidelinePolicy: vi.fn(),
}));
const toastMock = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));
const permissionState = vi.hoisted(() => ({
  allowed: new Set<string>(),
}));

vi.mock('@/services/api', () => ({
  useDashboardApi: () => dashboardApiMock,
}));
vi.mock('@/services/policy-governance-api', () => ({
  PolicyGovernanceApiError: class extends Error {},
  usePolicyGovernanceApi: () => policyApiMock,
}));
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    preset: 'Full Control',
    isLoading: false,
    error: null,
    ownerReviewRequired: false,
    has: (permission: string) => permissionState.allowed.has(permission),
  }),
}));
vi.mock('react-hot-toast', () => ({ default: toastMock }));

import { GuidelinesPanel } from '../GuidelinesPanel';
import { GuidelinePolicyExportButton } from '../GuidelinePolicyTransfer';

const ENVELOPE = {
  contract_version: 'guideline-export/v3' as const,
  schema_version: '3' as const,
  kind: 'guidelines' as const,
  exported_at: '2026-07-29T00:00:00Z',
  source_board_id: 'b1',
  content_digest: 'a'.repeat(64),
  guidelines: [],
} satisfies GuidelineExportEnvelopeV3;

const METRIC = {
  metric_id: 'segregation',
  code: 'architecture.segregation',
  title: 'Segregation',
  description: 'Business logic stays independent from adapters.',
  evaluation_rubric: 'Score observable separation from 0 to 100.',
  target_entity_types: ['spec'] as const,
  direction: 'minimum' as const,
  default_threshold: 80,
} satisfies GuidelineExportMetricV3;

function envelopeWithMetrics(
  metrics: GuidelineExportMetricV3[],
): GuidelineExportEnvelopeV3 {
  return {
    ...ENVELOPE,
    guidelines: [{
      identity: {
        guideline_id: 'g1',
        owner_id: 'u1',
        scope: 'global',
        board_id: null,
        context_scope: 'all',
        created_at: '2026-07-29T00:00:00Z',
      },
      revisions: [{
        revision_id: 'r1',
        guideline_id: 'g1',
        revision_number: 1,
        semantic_version: '1.0.0',
        title: 'Hexagonal architecture',
        content: 'Use hexagonal architecture.',
        revision_digest: 'b'.repeat(64),
        metrics,
        created_by: 'u1',
        created_at: '2026-07-29T00:00:00Z',
        parent_revision_id: null,
        tags: [],
        published_head_revision: 1,
        published_head_updated_at: '2026-07-29T00:00:00Z',
        legacy_version: null,
        legacy_version_unresolvable: false,
        legacy_tags: null,
      }],
      head: {
        guideline_id: 'g1',
        revision_id: 'r1',
        revision_number: 1,
        semantic_version: '1.0.0',
        head_revision: 1,
        updated_at: '2026-07-29T00:00:00Z',
      },
      retirement: null,
      bindings: [],
      history_status: 'complete',
      migration_notes: [],
    }],
  };
}

function envelopeWithBinding(): GuidelineExportEnvelopeV3 {
  const envelope = envelopeWithMetrics([METRIC]);
  const aggregate = envelope.guidelines[0];
  return {
    ...envelope,
    guidelines: [{
      ...aggregate,
      bindings: [{
        binding: {
          binding_id: 'binding-1',
          board_id: 'b1',
          guideline_id: 'g1',
          revision_id: 'r1',
          semantic_version: '1.0.0',
          revision_digest: 'b'.repeat(64),
          priority: 0,
          binding_revision: 1,
          adopted_by: 'u1',
          adopted_at: '2026-07-29T01:00:00Z',
          enforcement: 'blocking',
          minimum_confidence: 80,
          metric_threshold_overrides: {
            'architecture.segregation': 85,
          },
          configuration_digest: 'c'.repeat(64),
          state: 'active',
          source_kind: 'native',
        },
        physical_source_kind: 'native',
        binding_origin: 'native',
        materialization: 'live',
        legacy_source_id: null,
        legacy_guideline_version: null,
        legacy_template_id: null,
        legacy_template_version: null,
        legacy_version_unresolvable: false,
        evidence_refs: [['source', 'kb:architecture']],
        binding_digest: 'd'.repeat(64),
      }],
    }],
  };
}

async function expectEnvelopeRejected(
  envelope: unknown,
  expectedPath: string,
): Promise<void> {
  render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
  fireEvent.change(await screen.findByTestId('guidelines-import-input'), {
    target: {
      files: [new File(
        [JSON.stringify(envelope)],
        'malformed.json',
        { type: 'application/json' },
      )],
    },
  });

  await waitFor(() => {
    expect(toastMock.error).toHaveBeenCalledWith(
      `Guideline v3 envelope is invalid at ${expectedPath}.`,
    );
  });
  expect(policyApiMock.importGuidelinePolicy).not.toHaveBeenCalled();
}

describe('GuidelinesPanel immutable policy import/export', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.allowed = new Set([
      'guidelines.revisions.read',
      'guidelines.revisions.create',
      'guidelines.metrics.author',
    ]);
    dashboardApiMock.getBoardGuidelines.mockResolvedValue([]);
    dashboardApiMock.listDefaultGuidelineCandidates.mockResolvedValue({
      scope: 'global',
      template_id: null,
      template_version: null,
      candidates: [],
    });
    dashboardApiMock.listGuidelines.mockResolvedValue([]);
    URL.createObjectURL = vi.fn(() => 'blob:policy-export');
    URL.revokeObjectURL = vi.fn();
  });

  it('exports guideline-export/v3 semantic metrics without a legacy envelope', async () => {
    policyApiMock.exportGuidelinePolicy.mockResolvedValue(ENVELOPE);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('guidelines-export'));

    await waitFor(() => {
      expect(policyApiMock.exportGuidelinePolicy).toHaveBeenCalledWith('b1');
      expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    });
    const anchor = clickSpy.mock.instances[0] as HTMLAnchorElement;
    expect(anchor.download).toBe('guideline-policy-b1.json');
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Exported 0 guideline aggregate(s).',
    );
    clickSpy.mockRestore();
  });

  it('exports one complete guideline aggregate by stable id', async () => {
    policyApiMock.exportGuidelinePolicy.mockResolvedValue(ENVELOPE);
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    render(
      <GuidelinePolicyExportButton
        boardId="b1"
        guidelineId="g1"
        guidelineTitle="Architecture policy"
      />,
    );
    fireEvent.click(screen.getByTestId('guidelines-export-g1'));

    await waitFor(() => {
      expect(policyApiMock.exportGuidelinePolicy).toHaveBeenCalledWith(
        'b1',
        { guidelineIds: ['g1'] },
      );
    });
    const anchor = clickSpy.mock.instances[0] as HTMLAnchorElement;
    expect(anchor.download).toBe('guideline-policy-Architecture-policy.json');
    clickSpy.mockRestore();
  });

  it('fails closed when export returns an unknown v3 envelope shape', async () => {
    policyApiMock.exportGuidelinePolicy.mockResolvedValue({
      ...ENVELOPE,
      unexpected: true,
    });

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.click(await screen.findByTestId('guidelines-export'));

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'Guideline export returned an invalid v3 envelope.',
      );
    });
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('dry-runs a v3 import before committing and refreshes policy lists', async () => {
    policyApiMock.importGuidelinePolicy
      .mockResolvedValueOnce({
        transaction_status: 'dry_run',
        created_count: 2,
        skip_identical_count: 1,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: true,
      })
      .mockResolvedValueOnce({
        transaction_status: 'committed',
        created_count: 2,
        skip_identical_count: 1,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: false,
      });

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    const boardReadsBefore = dashboardApiMock.getBoardGuidelines.mock.calls.length;
    const file = new File(
      [JSON.stringify(ENVELOPE)],
      'guideline-policy.json',
      { type: 'application/json' },
    );
    fireEvent.change(
      await screen.findByTestId('guidelines-import-input'),
      { target: { files: [file] } },
    );

    await waitFor(() => {
      expect(policyApiMock.importGuidelinePolicy).toHaveBeenNthCalledWith(
        1,
        'b1',
        ENVELOPE,
        { dryRun: true },
      );
      expect(policyApiMock.importGuidelinePolicy).toHaveBeenNthCalledWith(
        2,
        'b1',
        ENVELOPE,
      );
    });
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Imported 2; skipped 1 identical aggregate(s).',
    );
    await waitFor(() => {
      expect(
        dashboardApiMock.getBoardGuidelines.mock.calls.length,
      ).toBeGreaterThan(boardReadsBefore);
    });
  });

  it('accepts multiple v3 files and dry-runs all before the first commit', async () => {
    const secondEnvelope = {
      ...ENVELOPE,
      content_digest: 'b'.repeat(64),
    };
    policyApiMock.importGuidelinePolicy
      .mockResolvedValueOnce({
        transaction_status: 'dry_run',
        created_count: 1,
        skip_identical_count: 0,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: true,
      })
      .mockResolvedValueOnce({
        transaction_status: 'dry_run',
        created_count: 2,
        skip_identical_count: 0,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: true,
      })
      .mockResolvedValueOnce({
        transaction_status: 'committed',
        created_count: 1,
        skip_identical_count: 0,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: false,
      })
      .mockResolvedValueOnce({
        transaction_status: 'committed',
        created_count: 2,
        skip_identical_count: 0,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: false,
      });

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.change(await screen.findByTestId('guidelines-import-input'), {
      target: {
        files: [
          new File([JSON.stringify(ENVELOPE)], 'one.json', { type: 'application/json' }),
          new File([JSON.stringify(secondEnvelope)], 'two.json', { type: 'application/json' }),
        ],
      },
    });

    await waitFor(() => {
      expect(policyApiMock.importGuidelinePolicy).toHaveBeenCalledTimes(4);
    });
    expect(policyApiMock.importGuidelinePolicy.mock.calls.map((call) => call[2])).toEqual([
      { dryRun: true },
      { dryRun: true },
      undefined,
      undefined,
    ]);
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Imported 3; skipped 0 identical aggregate(s). Processed 2 files.',
    );
  });

  it('stops after a conflicting dry-run and never overwrites history', async () => {
    policyApiMock.importGuidelinePolicy.mockResolvedValueOnce({
      transaction_status: 'rolled_back',
      created_count: 0,
      skip_identical_count: 0,
      conflict_count: 1,
      overwritten_row_count: 0,
      dry_run: true,
      error_code: 'guideline_import_conflict',
    });
    const file = new File(
      [JSON.stringify(ENVELOPE)],
      'guideline-policy.json',
      { type: 'application/json' },
    );
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.change(
      await screen.findByTestId('guidelines-import-input'),
      { target: { files: [file] } },
    );

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'guideline_import_conflict',
      );
    });
    expect(policyApiMock.importGuidelinePolicy).toHaveBeenCalledTimes(1);
  });

  it('does not commit after a partial dry-run response', async () => {
    policyApiMock.importGuidelinePolicy.mockResolvedValueOnce({
      transaction_status: 'dry_run',
      created_count: 0,
      skip_identical_count: 0,
      conflict_count: 0,
      dry_run: true,
    });
    const file = new File(
      [JSON.stringify(ENVELOPE)],
      'guideline-policy.json',
      { type: 'application/json' },
    );
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.change(
      await screen.findByTestId('guidelines-import-input'),
      { target: { files: [file] } },
    );

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'Guideline import returned an invalid result.',
      );
    });
    expect(policyApiMock.importGuidelinePolicy).toHaveBeenCalledTimes(1);
  });

  it('imports a metric-free policy with revision authority alone', async () => {
    permissionState.allowed = new Set(['guidelines.revisions.create']);
    const metricFreeEnvelope = envelopeWithMetrics([]);
    policyApiMock.importGuidelinePolicy
      .mockResolvedValueOnce({
        transaction_status: 'dry_run',
        created_count: 1,
        skip_identical_count: 0,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: true,
      })
      .mockResolvedValueOnce({
        transaction_status: 'committed',
        created_count: 1,
        skip_identical_count: 0,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: false,
      });

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    expect(await screen.findByTestId('guidelines-import')).not.toBeDisabled();
    fireEvent.change(screen.getByTestId('guidelines-import-input'), {
      target: {
        files: [new File(
          [JSON.stringify(metricFreeEnvelope)],
          'metric-free.json',
          { type: 'application/json' },
        )],
      },
    });

    await waitFor(() => {
      expect(policyApiMock.importGuidelinePolicy).toHaveBeenCalledTimes(2);
    });
  });

  it('requires metric-author authority only when the envelope contains semantic metrics', async () => {
    permissionState.allowed = new Set(['guidelines.revisions.create']);
    const semanticEnvelope = envelopeWithMetrics([METRIC]);

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.change(await screen.findByTestId('guidelines-import-input'), {
      target: {
        files: [new File(
          [JSON.stringify(semanticEnvelope)],
          'semantic.json',
          { type: 'application/json' },
        )],
      },
    });

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'Importing semantic metrics requires guidelines.metrics.author.',
      );
    });
    expect(policyApiMock.importGuidelinePolicy).not.toHaveBeenCalled();
  });

  it('imports semantic metrics when metric-author authority is granted', async () => {
    const semanticEnvelope = envelopeWithMetrics([METRIC]);
    policyApiMock.importGuidelinePolicy
      .mockResolvedValueOnce({
        transaction_status: 'dry_run',
        created_count: 1,
        skip_identical_count: 0,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: true,
      })
      .mockResolvedValueOnce({
        transaction_status: 'committed',
        created_count: 1,
        skip_identical_count: 0,
        conflict_count: 0,
        overwritten_row_count: 0,
        dry_run: false,
      });

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.change(await screen.findByTestId('guidelines-import-input'), {
      target: {
        files: [new File(
          [JSON.stringify(semanticEnvelope)],
          'semantic.json',
          { type: 'application/json' },
        )],
      },
    });

    await waitFor(() => {
      expect(policyApiMock.importGuidelinePolicy).toHaveBeenNthCalledWith(
        1,
        'b1',
        semanticEnvelope,
        { dryRun: true },
      );
      expect(policyApiMock.importGuidelinePolicy).toHaveBeenNthCalledWith(
        2,
        'b1',
        semanticEnvelope,
      );
    });
  });

  it('fails closed when an imported semantic metric cannot be classified', async () => {
    permissionState.allowed = new Set(['guidelines.revisions.create']);
    const malformedEnvelope = envelopeWithMetrics([{
      ...METRIC,
      direction: 'mystery',
    } as unknown as GuidelineExportMetricV3]);

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.change(await screen.findByTestId('guidelines-import-input'), {
      target: {
        files: [new File(
          [JSON.stringify(malformedEnvelope)],
          'malformed.json',
          { type: 'application/json' },
        )],
      },
    });

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'Guideline v3 envelope is invalid at '
        + 'guidelines[0].revisions[0].metrics[0].direction.',
      );
    });
    expect(policyApiMock.importGuidelinePolicy).not.toHaveBeenCalled();
  });

  it.each([
    [
      'an unknown identity field',
      () => {
        const envelope = envelopeWithMetrics([METRIC]);
        const aggregate = envelope.guidelines[0];
        return {
          ...envelope,
          guidelines: [{
            ...aggregate,
            identity: {
              ...aggregate.identity,
              unexpected: true,
            },
          }],
        };
      },
      'guidelines[0].identity',
    ],
    [
      'an unknown revision field',
      () => {
        const envelope = envelopeWithMetrics([METRIC]);
        const aggregate = envelope.guidelines[0];
        return {
          ...envelope,
          guidelines: [{
            ...aggregate,
            revisions: [{
              ...aggregate.revisions[0],
              unexpected: true,
            }],
          }],
        };
      },
      'guidelines[0].revisions[0]',
    ],
    [
      'a malformed revision timestamp',
      () => {
        const envelope = envelopeWithMetrics([METRIC]);
        const aggregate = envelope.guidelines[0];
        return {
          ...envelope,
          guidelines: [{
            ...aggregate,
            revisions: [{
              ...aggregate.revisions[0],
              created_at: '2026-07-29T00:00:00',
            }],
          }],
        };
      },
      'guidelines[0].revisions[0].created_at',
    ],
    [
      'a malformed revision digest',
      () => {
        const envelope = envelopeWithMetrics([METRIC]);
        const aggregate = envelope.guidelines[0];
        return {
          ...envelope,
          guidelines: [{
            ...aggregate,
            revisions: [{
              ...aggregate.revisions[0],
              revision_digest: 'not-a-sha256',
            }],
          }],
        };
      },
      'guidelines[0].revisions[0].revision_digest',
    ],
    [
      'a head that does not identify the latest revision',
      () => {
        const envelope = envelopeWithMetrics([METRIC]);
        const aggregate = envelope.guidelines[0];
        return {
          ...envelope,
          guidelines: [{
            ...aggregate,
            head: {
              ...aggregate.head,
              revision_id: 'r-other',
            },
          }],
        };
      },
      'guidelines[0].head',
    ],
    [
      'an unknown retirement field',
      () => {
        const envelope = envelopeWithMetrics([METRIC]);
        const aggregate = envelope.guidelines[0];
        return {
          ...envelope,
          guidelines: [{
            ...aggregate,
            retirement: {
              retirement_id: 'retirement-1',
              guideline_id: 'g1',
              status: 'retired',
              retired_revision_id: 'r1',
              retired_revision_number: 1,
              retired_semantic_version: '1.0.0',
              retired_revision_digest: 'b'.repeat(64),
              retired_head_revision: 1,
              reason: 'No longer applicable.',
              retired_by: 'u1',
              retired_at: '2026-07-30T00:00:00Z',
              superseded_by_guideline_id: null,
              unexpected: true,
            },
          }],
        };
      },
      'guidelines[0].retirement',
    ],
    [
      'an unknown logical binding field',
      () => {
        const envelope = envelopeWithBinding();
        const aggregate = envelope.guidelines[0];
        const exportedBinding = aggregate.bindings[0];
        return {
          ...envelope,
          guidelines: [{
            ...aggregate,
            bindings: [{
              ...exportedBinding,
              binding: {
                ...exportedBinding.binding,
                unexpected: true,
              },
            }],
          }],
        };
      },
      'guidelines[0].bindings[0].binding',
    ],
    [
      'a binding revision that is absent from revision history',
      () => {
        const envelope = envelopeWithBinding();
        const aggregate = envelope.guidelines[0];
        const exportedBinding = aggregate.bindings[0];
        return {
          ...envelope,
          guidelines: [{
            ...aggregate,
            bindings: [{
              ...exportedBinding,
              binding: {
                ...exportedBinding.binding,
                revision_id: 'r-other',
              },
            }],
          }],
        };
      },
      'guidelines[0].bindings[0].binding.revision_id',
    ],
    [
      'a malformed evidence reference',
      () => {
        const envelope = envelopeWithBinding();
        const aggregate = envelope.guidelines[0];
        const exportedBinding = aggregate.bindings[0];
        return {
          ...envelope,
          guidelines: [{
            ...aggregate,
            bindings: [{
              ...exportedBinding,
              evidence_refs: [['source']],
            }],
          }],
        };
      },
      'guidelines[0].bindings[0].evidence_refs[0]',
    ],
  ])('rejects %s before the dry-run', async (
    _description,
    buildEnvelope,
    expectedPath,
  ) => {
    await expectEnvelopeRejected(buildEnvelope(), expectedPath);
  });

  it('rejects legacy and malformed envelopes before any API mutation', async () => {
    const legacyFile = new File(
      [JSON.stringify({ schema_version: '1', kind: 'guidelines', items: [] })],
      'legacy.json',
      { type: 'application/json' },
    );
    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.change(
      await screen.findByTestId('guidelines-import-input'),
      { target: { files: [legacyFile] } },
    );

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'Select a guideline-export/v3 JSON file.',
      );
    });
    expect(policyApiMock.importGuidelinePolicy).not.toHaveBeenCalled();
  });
});
