import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { ArchitectureCandidatesResponse } from '@/types/architecture-candidates';
import { ArchitectureCandidatesPanel } from '../ArchitectureCandidatesPanel';

const api = vi.hoisted(() => ({ getArchitectureCandidates: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));

function population(overrides: Partial<ArchitectureCandidatesResponse> = {}): ArchitectureCandidatesResponse {
  return {
    contract_version: 'architecture-candidates/v1', board_id: 'board', spec_id: 'spec',
    spec_version: 1, spec_edition: 2, source_complete: true, population_state: 'complete', total: 1,
    total_variants: 1, offset: 0, limit: 25, has_more: false, profile: 'summary', issue_counts: {}, issues_truncated: false,
    candidates: [{
      id: 'candidate', root_design_id: 'root', interface_id: 'boundary', source_digest: 'a'.repeat(64),
      name: 'Order event', contract_type: 'event', direction: null, protocol: null,
      signals: ['unrestricted_schema'], adopted_sources: [{ design_id: 'adopted', revision: 3 }],
    }], issues: [], ...overrides,
  };
}

const props = { boardId: 'board', specId: 'spec', specVersion: 1, canRead: true };
beforeEach(() => api.getArchitectureCandidates.mockReset());

it('does not request contracts without read permission', () => {
  render(<ArchitectureCandidatesPanel {...props} canRead={false} />);
  expect(screen.getByText('Architecture read permission is required.')).toBeInTheDocument();
  expect(api.getArchitectureCandidates).not.toHaveBeenCalled();
});

it('shows complete adopted contracts and treats external refs as text', async () => {
  const details = population({ profile: 'detail', candidates: [{ ...population().candidates[0],
    contract: { event_schema: {}, schema_ref: 'http://private.invalid/schema' },
  }] });
  api.getArchitectureCandidates.mockResolvedValueOnce(population()).mockResolvedValueOnce(details);
  render(<ArchitectureCandidatesPanel {...props} />);
  await screen.findByText('Order event');
  expect(api.getArchitectureCandidates).toHaveBeenCalledTimes(1);
  expect(screen.queryByText(/private.invalid/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByText('Order event'));
  expect(screen.getByText('Contains an unrestricted schema')).toBeInTheDocument();
  expect(screen.getByText(/adopted v3/)).toBeInTheDocument();
  expect(await screen.findByText(/"event_schema": \{\}/)).toBeInTheDocument();
  expect(screen.queryByRole('link')).not.toBeInTheDocument();
  expect(api.getArchitectureCandidates).toHaveBeenLastCalledWith('board', 'spec', expect.any(AbortSignal), {
    candidateId: 'candidate', sourceDigest: 'a'.repeat(64),
  });
});

it('distinguishes confirmed empty from unavailable instead of showing 100 percent', async () => {
  api.getArchitectureCandidates.mockResolvedValueOnce(population({ total: 0, candidates: [] }))
    .mockResolvedValueOnce(population({ total: null, candidates: [], source_complete: false, population_state: 'unavailable' }));
  render(<ArchitectureCandidatesPanel {...props} />);
  await screen.findByText('No declared contracts in the effective architecture.');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh candidates' }));
  await screen.findByText('Sources are unavailable or incomplete. The candidate total is unknown.');
  expect(screen.queryByText('No declared contracts in the effective architecture.')).not.toBeInTheDocument();
  expect(screen.queryByText(/100%/)).not.toBeInTheDocument();
});

it('keeps both conflicting variants visible', async () => {
  const first = population().candidates[0];
  api.getArchitectureCandidates.mockResolvedValue(population({
    population_state: 'unresolved', candidates: [first, { ...first, source_digest: 'b'.repeat(64), name: 'New order event' }],
    issues: [{ code: 'architecture_contract_revision_conflict', candidate_id: first.id, design_id: null, interface_index: null }],
  }));
  render(<ArchitectureCandidatesPanel {...props} />);
  await screen.findByText('Order event');
  expect(screen.getByText('New order event')).toBeInTheDocument();
  expect(screen.getByText(/Adopted copies contain conflicting/)).toBeInTheDocument();
});

it('clears cached contracts when read permission is revoked', async () => {
  api.getArchitectureCandidates.mockResolvedValue(population());
  const { rerender } = render(<ArchitectureCandidatesPanel {...props} />);
  await screen.findByText('Order event');
  rerender(<ArchitectureCandidatesPanel {...props} canRead={false} />);
  expect(screen.queryByText('Order event')).not.toBeInTheDocument();
  expect(api.getArchitectureCandidates).toHaveBeenCalledTimes(1);
});

it('shows server permission denial on refresh without retaining old content', async () => {
  api.getArchitectureCandidates.mockResolvedValueOnce(population()).mockRejectedValueOnce(
    new AuthenticatedFetchError({ status: 403, message: 'private implementation detail' }),
  );
  render(<ArchitectureCandidatesPanel {...props} />);
  await screen.findByText('Order event');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh candidates' }));
  await screen.findByText('You do not have permission to read architecture candidates.');
  expect(screen.queryByText('Order event')).not.toBeInTheDocument();
  expect(screen.queryByText('private implementation detail')).not.toBeInTheDocument();
});

it('cancels the old request and ignores its late response after switching Spec', async () => {
  let finishOld!: (value: ArchitectureCandidatesResponse) => void;
  api.getArchitectureCandidates.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }))
    .mockResolvedValueOnce(population({ spec_id: 'next', total: 0, candidates: [] }));
  const { rerender } = render(<ArchitectureCandidatesPanel {...props} />);
  const oldSignal = api.getArchitectureCandidates.mock.calls[0][2] as AbortSignal;
  rerender(<ArchitectureCandidatesPanel {...props} specId="next" />);
  await screen.findByText('No declared contracts in the effective architecture.');
  await act(async () => finishOld(population()));
  expect(oldSignal.aborted).toBe(true);
  expect(screen.queryByText('Order event')).not.toBeInTheDocument();
});

it('rejects a response carrying a different scope', async () => {
  api.getArchitectureCandidates.mockResolvedValue(population({ board_id: 'another-board' }));
  render(<ArchitectureCandidatesPanel {...props} />);
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('population is unknown'));
  expect(screen.queryByText('Order event')).not.toBeInTheDocument();
});

it('pages summaries without treating the page count as the whole population', async () => {
  api.getArchitectureCandidates.mockResolvedValueOnce(population({ total: 30, total_variants: 30, has_more: true }))
    .mockResolvedValueOnce(population({ total: 30, total_variants: 30, offset: 25, candidates: [{ ...population().candidates[0], id: 'last', name: 'Last candidate' }] }));
  render(<ArchitectureCandidatesPanel {...props} />);
  await screen.findByText('Order event');
  expect(screen.getByText('30 candidates across all adopted sources · 30 contract variants')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Next candidates' }));
  await screen.findByText('Last candidate');
  expect(api.getArchitectureCandidates).toHaveBeenLastCalledWith('board', 'spec', expect.any(AbortSignal), { offset: 25, limit: 25 });
  expect(screen.queryByText('Order event')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Next candidates' })).toBeDisabled();
});

it('reports a stale detail digest without showing another contract revision', async () => {
  api.getArchitectureCandidates.mockResolvedValueOnce(population()).mockRejectedValueOnce(
    new AuthenticatedFetchError({ status: 409, message: 'source changed' }),
  );
  render(<ArchitectureCandidatesPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Order event' }));
  await screen.findByText('The adopted contract changed. Refresh candidates before continuing.');
  expect(screen.queryByText(/private.invalid/)).not.toBeInTheDocument();
});

it('replaces the whole adopted population after a Spec revision instead of retaining unselected roots', async () => {
  const first = population().candidates[0];
  api.getArchitectureCandidates.mockResolvedValueOnce(population({ total: 2, total_variants: 2,
    candidates: [first, { ...first, id: 'unselected', root_design_id: 'other-root', name: 'Other contract' }],
  })).mockResolvedValueOnce(population({ spec_version: 2 }));
  const { rerender } = render(<ArchitectureCandidatesPanel {...props} />);
  await screen.findByText('Other contract');
  rerender(<ArchitectureCandidatesPanel {...props} specVersion={2} />);
  expect(screen.queryByText('Other contract')).not.toBeInTheDocument();
  await screen.findByText('1 candidates across all adopted sources · 1 contract variants');
  expect(screen.getByText('Order event')).toBeInTheDocument();
  expect(screen.queryByText('Other contract')).not.toBeInTheDocument();
});

it.each(['Spec', 'board', 'revision', 'permission', 'refresh', 'page'] as const)(
  'discards a late contract detail after changing %s', async (change) => {
    let finishDetail!: (value: ArchitectureCandidatesResponse) => void;
    api.getArchitectureCandidates.mockResolvedValueOnce(population({ has_more: true }))
      .mockImplementationOnce(() => new Promise(resolve => { finishDetail = resolve; }))
      .mockResolvedValueOnce(population({
        spec_id: change === 'Spec' ? 'next' : 'spec',
        board_id: change === 'board' ? 'next-board' : 'board',
        total: 0, candidates: [],
      }));
    const { rerender } = render(<ArchitectureCandidatesPanel {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Order event' }));
    await screen.findByText('Loading contract…');
    const detailSignal = api.getArchitectureCandidates.mock.calls[1][2] as AbortSignal;

    if (change === 'refresh') fireEvent.click(screen.getByRole('button', { name: 'Refresh candidates' }));
    else if (change === 'page') fireEvent.click(screen.getByRole('button', { name: 'Next candidates' }));
    else rerender(<ArchitectureCandidatesPanel {...props}
      specId={change === 'Spec' ? 'next' : props.specId}
      boardId={change === 'board' ? 'next-board' : props.boardId}
      specVersion={change === 'revision' ? 2 : props.specVersion}
      canRead={change !== 'permission'}
    />);

    expect(detailSignal.aborted).toBe(true);
    await act(async () => finishDetail(population({ profile: 'detail', candidates: [{
      ...population().candidates[0], contract: { description: 'Obsolete confidential contract' },
    }] })));
    expect(screen.queryByText(/Obsolete confidential contract/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Order event' })).not.toBeInTheDocument();
    if (change === 'permission') {
      expect(screen.getByText('Architecture read permission is required.')).toBeInTheDocument();
      expect(api.getArchitectureCandidates).toHaveBeenCalledTimes(2);
    } else await screen.findByText('No declared contracts in the effective architecture.');
  },
);

it.each(['board', 'Spec', 'candidate', 'digest'] as const)(
  'withholds a detail response with a different %s', async (mismatch) => {
    api.getArchitectureCandidates.mockResolvedValueOnce(population()).mockResolvedValueOnce(population({
      profile: 'detail',
      board_id: mismatch === 'board' ? 'other' : 'board',
      spec_id: mismatch === 'Spec' ? 'other' : 'spec',
      candidates: [{ ...population().candidates[0],
        id: mismatch === 'candidate' ? 'other' : 'candidate',
        source_digest: mismatch === 'digest' ? 'b'.repeat(64) : 'a'.repeat(64),
        contract: { description: 'Wrong contract body' },
      }],
    }));
    render(<ArchitectureCandidatesPanel {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Order event' }));
    await screen.findByText('The contract could not be loaded or access is no longer available.');
    expect(screen.queryByText(/Wrong contract body/)).not.toBeInTheDocument();
  },
);

it('reloads contracts after permission is restored instead of reusing previously visible details', async () => {
  api.getArchitectureCandidates.mockResolvedValueOnce(population()).mockResolvedValueOnce(population({
    profile: 'detail', candidates: [{ ...population().candidates[0], contract: { description: 'Old protected body' } }],
  })).mockResolvedValueOnce(population({ total: 0, candidates: [] }));
  const { rerender } = render(<ArchitectureCandidatesPanel {...props} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Order event' }));
  await screen.findByText(/Old protected body/);
  rerender(<ArchitectureCandidatesPanel {...props} canRead={false} />);
  expect(screen.queryByText(/Old protected body/)).not.toBeInTheDocument();
  rerender(<ArchitectureCandidatesPanel {...props} />);
  await screen.findByText('No declared contracts in the effective architecture.');
  expect(screen.queryByText(/Old protected body/)).not.toBeInTheDocument();
  expect(api.getArchitectureCandidates).toHaveBeenCalledTimes(3);
});

it('recovers from unavailable sources through an explicit refresh', async () => {
  api.getArchitectureCandidates.mockRejectedValueOnce(new Error('private transport diagnostic'))
    .mockResolvedValueOnce(population());
  render(<ArchitectureCandidatesPanel {...props} />);
  await screen.findByText('Architecture candidates could not be loaded. The population is unknown.');
  expect(screen.queryByText('private transport diagnostic')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh candidates' }));
  await screen.findByText('Order event');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});
