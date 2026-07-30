import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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

const ENVELOPE = {
  contract_version: 'guideline-export/v2' as const,
  schema_version: '2' as const,
  kind: 'guidelines' as const,
  exported_at: '2026-07-29T00:00:00Z',
  source_board_id: 'b1',
  content_digest: 'a'.repeat(64),
  guidelines: [],
};

function envelopeWithRule(enforcement: unknown) {
  return {
    ...ENVELOPE,
    guidelines: [{
      revisions: [{
        rules: [{ enforcement }],
      }],
    }],
  };
}

describe('GuidelinesPanel immutable policy import/export', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    permissionState.allowed = new Set([
      'guidelines.revisions.read',
      'guidelines.revisions.create',
      'guidelines.rules.author_blocking',
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

  it('exports guideline-export/v2 without using the lossy legacy envelope', async () => {
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

  it('dry-runs a v2 import before committing and refreshes policy lists', async () => {
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

  it('imports advisory-only policy with revision authority alone', async () => {
    permissionState.allowed = new Set(['guidelines.revisions.create']);
    const advisoryEnvelope = envelopeWithRule('advisory');
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
          [JSON.stringify(advisoryEnvelope)],
          'advisory.json',
          { type: 'application/json' },
        )],
      },
    });

    await waitFor(() => {
      expect(policyApiMock.importGuidelinePolicy).toHaveBeenCalledTimes(2);
    });
  });

  it('requires blocking-rule authority only when the envelope contains blocking rules', async () => {
    permissionState.allowed = new Set(['guidelines.revisions.create']);
    const blockingEnvelope = envelopeWithRule('blocking');

    render(<GuidelinesPanel boardId="b1" onClose={() => {}} />);
    fireEvent.change(await screen.findByTestId('guidelines-import-input'), {
      target: {
        files: [new File(
          [JSON.stringify(blockingEnvelope)],
          'blocking.json',
          { type: 'application/json' },
        )],
      },
    });

    await waitFor(() => {
      expect(toastMock.error).toHaveBeenCalledWith(
        'Importing blocking rules requires guidelines.rules.author_blocking.',
      );
    });
    expect(policyApiMock.importGuidelinePolicy).not.toHaveBeenCalled();
  });

  it('fails closed when imported rule enforcement cannot be classified', async () => {
    permissionState.allowed = new Set(['guidelines.revisions.create']);
    const malformedEnvelope = envelopeWithRule('mystery');

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
        'Unable to classify policy rules in this import.',
      );
    });
    expect(policyApiMock.importGuidelinePolicy).not.toHaveBeenCalled();
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
        'Select a guideline-export/v2 JSON file.',
      );
    });
    expect(policyApiMock.importGuidelinePolicy).not.toHaveBeenCalled();
  });
});
