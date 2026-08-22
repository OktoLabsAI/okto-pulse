import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CodeTraceabilityProjection } from '@/types';
import { CodeEvidencePanel } from '../CodeEvidencePanel';
import { sanitizeCodeEvidenceProjectionForAuthority } from '../codeEvidenceAuthority';

const apiMock = vi.hoisted(() => ({
  getCodeTraceabilityProjection: vi.fn(),
  revokeCodeEvidence: vi.fn(),
}));

const authorityState = vi.hoisted(() => ({
  canReadProjection: true,
  canClassifyLegacyEvidence: false,
  canRevokeEvidence: false,
  isLoading: false,
  error: null as Error | null,
}));

const PROTECTED_PAYLOAD_SHA = 'f'.repeat(64);
const PROTECTED_WORKSPACE_STATE = 'protected-workspace-state-sentinel';
const PROTECTED_PERMISSION_DIAGNOSTIC = 'protected-permission-diagnostic-sentinel';

vi.mock('@/services/api', () => ({
  useDashboardApi: () => apiMock,
}));

vi.mock('../useCodeTraceabilityAuthority', () => ({
  useCodeTraceabilityAuthority: () => authorityState,
}));

function projection(): CodeTraceabilityProjection {
  return {
    subject_type: 'refinement',
    subject_id: 'refinement-1',
    subject_version: 2,
    profile: 'detail',
    context_scope: 'default',
    source_context_classification_inputs: [{
      evidence_id: 'evidence-1',
      expected_evidence_payload_sha256: PROTECTED_PAYLOAD_SHA,
      expected_classification_revision: 0,
      baseline_provenance: {
        presence: 'committed_snapshot',
        workspace_state_id: PROTECTED_WORKSPACE_STATE,
        provenance_note: null,
        provenance_note_required: false,
      },
    }],
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
  };
}

function panel() {
  return (
    <CodeEvidencePanel
      boardId="board-1"
      subjectId="refinement-1"
      subjectVersion={2}
    />
  );
}

beforeEach(() => {
  Object.assign(authorityState, {
    canReadProjection: true,
    canClassifyLegacyEvidence: false,
    canRevokeEvidence: false,
    isLoading: false,
    error: null,
  });
  apiMock.getCodeTraceabilityProjection.mockReset();
  apiMock.revokeCodeEvidence.mockReset();
  apiMock.getCodeTraceabilityProjection.mockResolvedValue(projection());
});

afterEach(() => {
  cleanup();
});

describe('CodeEvidencePanel authority boundary', () => {
  it('ts_007019f7 — shows an accessible skeleton without fetching or leaking protected data while authority loads', () => {
    authorityState.canReadProjection = false;
    authorityState.isLoading = true;

    render(panel());

    const loading = screen.getByRole('status');
    expect(loading).toHaveTextContent('Loading code evidence…');
    expect(loading).toHaveClass('sm:grid-cols-2');
    expect(apiMock.getCodeTraceabilityProjection).not.toHaveBeenCalled();
    expect(screen.queryByTestId('refinement-code-evidence-panel')).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(PROTECTED_PAYLOAD_SHA);
    expect(document.body).not.toHaveTextContent(PROTECTED_WORKSPACE_STATE);
  });

  it('ts_007019f7 — renders nothing and hides protected diagnostics after a permission failure', () => {
    authorityState.canReadProjection = false;
    authorityState.error = new Error(PROTECTED_PERMISSION_DIAGNOSTIC);

    render(panel());

    expect(apiMock.getCodeTraceabilityProjection).not.toHaveBeenCalled();
    expect(screen.queryByTestId('refinement-code-evidence-panel')).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(PROTECTED_PERMISSION_DIAGNOSTIC);
    expect(document.body).not.toHaveTextContent(PROTECTED_PAYLOAD_SHA);
    expect(document.body).not.toHaveTextContent(PROTECTED_WORKSPACE_STATE);
  });

  it('ts_007019f7 — renders nothing and never fetches when projection read is denied or omitted', () => {
    authorityState.canReadProjection = false;

    render(panel());

    expect(apiMock.getCodeTraceabilityProjection).not.toHaveBeenCalled();
    expect(screen.queryByTestId('refinement-code-evidence-panel')).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(PROTECTED_PAYLOAD_SHA);
    expect(document.body).not.toHaveTextContent(PROTECTED_WORKSPACE_STATE);
  });

  it('ts_007019f7 — preserves readable context while redacting protected CAS inputs without classify authority', async () => {
    const serverProjection = projection();
    apiMock.getCodeTraceabilityProjection.mockResolvedValueOnce(serverProjection);

    render(panel());

    await waitFor(() => expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId('refinement-code-evidence-panel')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(PROTECTED_PAYLOAD_SHA);
    expect(document.body).not.toHaveTextContent(PROTECTED_WORKSPACE_STATE);

    const sanitized = sanitizeCodeEvidenceProjectionForAuthority(
      serverProjection,
      false,
    );
    expect(sanitized).not.toBe(serverProjection);
    expect(sanitized.source_context_classification_inputs).toEqual([]);
    expect(serverProjection.source_context_classification_inputs).toHaveLength(1);
    expect(sanitizeCodeEvidenceProjectionForAuthority(serverProjection, true))
      .toBe(serverProjection);
  });

  it('ts_007019f7 — removes a stale authorized projection immediately when read authority is lost', async () => {
    authorityState.canClassifyLegacyEvidence = true;
    const view = render(panel());
    expect(await screen.findByTestId('refinement-code-evidence-panel')).toBeInTheDocument();
    await waitFor(() => expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(1));

    authorityState.canReadProjection = false;
    authorityState.canClassifyLegacyEvidence = false;
    view.rerender(panel());

    expect(screen.queryByTestId('refinement-code-evidence-panel')).not.toBeInTheDocument();
    expect(apiMock.getCodeTraceabilityProjection).toHaveBeenCalledTimes(1);
    expect(document.body).not.toHaveTextContent(PROTECTED_PAYLOAD_SHA);
    expect(document.body).not.toHaveTextContent(PROTECTED_WORKSPACE_STATE);
  });
});
