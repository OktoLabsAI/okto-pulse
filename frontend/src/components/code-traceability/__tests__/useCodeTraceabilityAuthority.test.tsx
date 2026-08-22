import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getMyPermissions } from '@/services/permissions-api';
import type { PermissionsResponse } from '@/services/permissions-api';
import { useCodeTraceabilityAuthority } from '../useCodeTraceabilityAuthority';

vi.mock('@/services/permissions-api', async (importOriginal) => {
  const original = await importOriginal<
    typeof import('@/services/permissions-api')
  >();
  return {
    ...original,
    getMyPermissions: vi.fn(),
  };
});

const getMyPermissionsMock = vi.mocked(getMyPermissions);

function response(
  boardId: string,
  flags: Record<string, unknown>,
  ownerReviewRequired = false,
): PermissionsResponse {
  return {
    board_id: boardId,
    preset_name: 'Custom',
    flags,
    owner_review_required: ownerReviewRequired,
    review_reason: ownerReviewRequired ? 'preset_lineage_cycle' : null,
  };
}

function AuthoritySurfaces({ boardId }: { boardId: string }) {
  const authority = useCodeTraceabilityAuthority(boardId);
  return (
    <div>
      <output data-testid="authority-state">
        {authority.isLoading ? 'loading' : authority.error ? 'error' : 'ready'}
      </output>
      {authority.canReadProjection && (
        <div role="tablist" aria-label="Code Traceability tabs">
          <button role="tab">Code Evidence</button>
          <button role="tab">Code Evidence Matrix</button>
          <button role="tab">Implementation Targets</button>
        </div>
      )}
      {authority.canRevokeReceipt && (
        <button type="button">Revoke receipt</button>
      )}
      {authority.canRevokeEvidence && (
        <button type="button">Revoke evidence</button>
      )}
      {authority.canClassifyLegacyEvidence && (
        <button type="button">Review unclassified Evidence</button>
      )}
      {authority.canCreateTarget && (
        <button type="button">Add semantic target</button>
      )}
      {authority.canAcknowledgeOverlap && (
        <button type="button">Acknowledge overlap</button>
      )}
      {authority.canCreateWaiver && (
        <button type="button">Create human waiver</button>
      )}
      {authority.canClearWaiver && (
        <button type="button">Clear waiver</button>
      )}
    </div>
  );
}

function expectSensitiveSurfacesHidden() {
  expect(screen.queryByRole('tab', { name: 'Code Evidence' })).not.toBeInTheDocument();
  expect(screen.queryByRole('tab', { name: 'Code Evidence Matrix' })).not.toBeInTheDocument();
  expect(screen.queryByRole('tab', { name: 'Implementation Targets' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Revoke receipt' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Revoke evidence' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Review unclassified Evidence' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Add semantic target' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Acknowledge overlap' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Create human waiver' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Clear waiver' })).not.toBeInTheDocument();
}

beforeEach(() => {
  getMyPermissionsMock.mockReset();
});

afterEach(() => {
  cleanup();
});

describe('Code Traceability UI authority is fail-closed', () => {
  it('ts_007019f7 — hides every protected surface while permission loading is unresolved', async () => {
    const boardId = 'ct-authority-loading';
    let resolve!: (value: PermissionsResponse) => void;
    getMyPermissionsMock.mockReturnValueOnce(new Promise((done) => {
      resolve = done;
    }));

    render(<AuthoritySurfaces boardId={boardId} />);

    expect(screen.getByTestId('authority-state')).toHaveTextContent('loading');
    expectSensitiveSurfacesHidden();

    await act(async () => resolve(response(boardId, {})));
    await waitFor(() => expect(screen.getByTestId('authority-state')).toHaveTextContent('ready'));
  });

  it('ts_007019f7 — hides every protected surface after the permission request errors', async () => {
    getMyPermissionsMock.mockRejectedValueOnce(new Error('authority unavailable'));
    render(<AuthoritySurfaces boardId="ct-authority-error" />);

    await waitFor(() => expect(screen.getByTestId('authority-state')).toHaveTextContent('error'));
    expectSensitiveSurfacesHidden();
  });

  it('ts_007019f7 — hides every protected surface when Code Traceability leaves are denied or omitted', async () => {
    const boardId = 'ct-authority-omitted';
    getMyPermissionsMock.mockResolvedValueOnce(response(boardId, {
      code_traceability: {},
    }));
    render(<AuthoritySurfaces boardId={boardId} />);

    await waitFor(() => expect(screen.getByTestId('authority-state')).toHaveTextContent('ready'));
    expectSensitiveSurfacesHidden();
  });

  it('shows read and mutation surfaces only after every exact leaf is explicitly granted', async () => {
    const boardId = 'ct-authority-granted';
    getMyPermissionsMock.mockResolvedValueOnce(response(boardId, {
      code_traceability: {
        investigation: { read: true, revoke: true },
        evidence: { read: true, revoke: true, classify_legacy: true },
        target: { read: true, create: true },
        overlap: { read: true, acknowledge: true },
        waiver: { create: true, clear: true },
      },
      spec: { entity: { edit_fields: true } },
    }));
    render(<AuthoritySurfaces boardId={boardId} />);

    expect(await screen.findByRole('tab', { name: 'Code Evidence' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Code Evidence Matrix' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Implementation Targets' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Revoke receipt' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Revoke evidence' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Review unclassified Evidence' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add semantic target' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Acknowledge overlap' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create human waiver' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Clear waiver' })).toBeInTheDocument();
  });

  it('does not infer target or overlap mutation authority from read leaves', async () => {
    const boardId = 'ct-authority-read-only';
    getMyPermissionsMock.mockResolvedValueOnce(response(boardId, {
      code_traceability: {
        investigation: { read: true },
        evidence: { read: true },
        target: { read: true },
        overlap: { read: true },
      },
    }));
    render(<AuthoritySurfaces boardId={boardId} />);

    expect(await screen.findByRole('tab', { name: 'Implementation Targets' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Revoke receipt' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Revoke evidence' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review unclassified Evidence' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add semantic target' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Acknowledge overlap' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create human waiver' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Clear waiver' })).not.toBeInTheDocument();
  });

  it('keeps read separate from legacy classification and preserves an explicit deny', async () => {
    const boardId = 'ct-authority-classify-denied';
    getMyPermissionsMock.mockResolvedValueOnce(response(boardId, {
      code_traceability: {
        investigation: { read: true },
        evidence: { read: true, classify_legacy: false },
        target: { read: true },
        overlap: { read: true },
      },
      spec: { entity: { edit_fields: true } },
    }));
    render(<AuthoritySurfaces boardId={boardId} />);

    expect(await screen.findByRole('tab', { name: 'Code Evidence' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review unclassified Evidence' }))
      .not.toBeInTheDocument();
  });

  it('requires the historical edit authority in addition to the classify leaf', async () => {
    const boardId = 'ct-authority-classify-no-history';
    getMyPermissionsMock.mockResolvedValueOnce(response(boardId, {
      code_traceability: {
        investigation: { read: true },
        evidence: { read: true, classify_legacy: true },
        target: { read: true },
        overlap: { read: true },
      },
    }));
    render(<AuthoritySurfaces boardId={boardId} />);

    expect(await screen.findByRole('tab', { name: 'Code Evidence' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review unclassified Evidence' }))
      .not.toBeInTheDocument();
  });

  it('fails every Code Evidence surface closed for owner-review lineages', async () => {
    const boardId = 'ct-authority-owner-review';
    getMyPermissionsMock.mockResolvedValueOnce(response(boardId, {
      code_traceability: {
        investigation: { read: true },
        evidence: { read: true, classify_legacy: true },
        target: { read: true },
        overlap: { read: true },
      },
      spec: { entity: { edit_fields: true } },
    }, true));
    render(<AuthoritySurfaces boardId={boardId} />);

    await waitFor(() => expect(screen.getByTestId('authority-state')).toHaveTextContent('ready'));
    expectSensitiveSurfacesHidden();
  });
});
