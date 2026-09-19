import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { AuthenticatedFetchError } from '@/lib/authFetch';
import type { ArchitectureClassificationsResponse } from '@/types/architecture-classifications';
import { ArchitectureClassificationsPanel } from '../ArchitectureClassificationsPanel';

const api = vi.hoisted(() => ({ getArchitectureClassifications: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const props = { boardId: 'board', specId: 'spec', specVersion: 9, canRead: true };
function result(changes: Partial<ArchitectureClassificationsResponse> = {}): ArchitectureClassificationsResponse {
  return {
    contract_version: 'architecture-classification-review/v1', board_id: 'board', spec_id: 'spec', spec_edition: 2, spec_version: 9,
    source_complete: true, enumeration_complete: true, classification_complete: false, admission_evaluated: false, semantic_review_evaluated: false, rollout_evaluated: false,
    observed_total: 31, total: 31, counts_scope: 'complete', state_counts: { current: 30, pending: 1, review_required: 0, retired: 0, unavailable: 0, unresolved: 0 },
    issues: [], state_filter: null, profile: 'summary', offset: 0, limit: 25, has_more: true,
    items: [{ candidate_id: 'candidate', root_design_id: 'root', interface_id: 'event', name: 'Orders', state: 'current', current_source_digest: 'a'.repeat(64), analyzed_source_digest: 'a'.repeat(64),
      source_variant_count: 1, source_digests: ['a'.repeat(64)], source_digests_truncated: false, decision_count: 1, dispositions: ['context_only'], issues: [], remainder_state: null }], ...changes,
  };
}
function details(): ArchitectureClassificationsResponse {
  const response = result({ profile: 'detail', has_more: false });
  response.items[0] = { ...response.items[0], state: 'review_required', changed_paths: ['/error_contract/retry'], changed_paths_truncated: true,
    current_contract: { error_contract: { retry: false }, schema_ref: 'http://private.invalid/do-not-fetch' },
    analyzed_contract: { error_contract: { retry: true } },
    decisions: [{ decision_id: 'd', spec_version: 8, actor_id: 'Author', classified_at: '2026-09-19', disposition: 'associate_existing_ir', integration_requirement_refs: ['ir_existing'],
      scope_paths: ['/error_contract'], reason: null, remainder_reason: 'Outside delivery', state: 'review_required', adopted_sources: [{ design_id: 'adopted', revision: 3 }] }] };
  return response;
}
function open() { fireEvent.click(screen.getByRole('button', { name: 'Review classifications' })); }
beforeEach(() => api.getArchitectureClassifications.mockReset());

it('reads only on request and retains off-page pending counts without approval', async () => {
  api.getArchitectureClassifications.mockResolvedValue(result());
  render(<ArchitectureClassificationsPanel {...props} />);
  expect(api.getArchitectureClassifications).not.toHaveBeenCalled();
  open();
  await screen.findByText('Orders · Current');
  expect(within(screen.getByRole('list', { name: 'Global classification counts' })).getByText('Pending: 1')).toBeInTheDocument();
  expect(screen.getByText('Classification is incomplete across the adopted architecture.')).toBeInTheDocument();
  expect(screen.getByText(/does not approve requirements or authorize/)).toBeInTheDocument();
  expect(api.getArchitectureClassifications).toHaveBeenCalledExactlyOnceWith('board', 'spec', expect.any(AbortSignal), { offset: 0, limit: 25 });
});

it('paginates then resets the page when filtering while keeping global counts', async () => {
  api.getArchitectureClassifications.mockResolvedValue(result());
  render(<ArchitectureClassificationsPanel {...props} />); open();
  await screen.findByText('Orders · Current');
  fireEvent.click(screen.getByRole('button', { name: 'Next classifications' }));
  await screen.findByText('Orders · Current');
  expect(api.getArchitectureClassifications).toHaveBeenLastCalledWith('board', 'spec', expect.any(AbortSignal), { offset: 25, limit: 25 });
  fireEvent.change(screen.getByRole('combobox', { name: 'Classification state' }), { target: { value: 'pending' } });
  await screen.findByText('Orders · Current');
  expect(api.getArchitectureClassifications).toHaveBeenLastCalledWith('board', 'spec', expect.any(AbortSignal), { offset: 0, limit: 25, state: 'pending' });
  expect(screen.getByText('Current: 30')).toBeInTheDocument();
});

it('does not represent unavailable sources as a complete zero population', async () => {
  api.getArchitectureClassifications.mockResolvedValue(result({ enumeration_complete: false, source_complete: false, total: null, items: [], counts_scope: 'observed' }));
  render(<ArchitectureClassificationsPanel {...props} />); open();
  expect(await screen.findByRole('alert')).toHaveTextContent('Totals are unknown');
  expect(screen.queryByText(/Current contracts are classified/)).not.toBeInTheDocument();
});

it('loads exact digest details lazily, showing provenance, IRs and contracts as text', async () => {
  api.getArchitectureClassifications.mockResolvedValueOnce(result()).mockResolvedValueOnce(details());
  render(<ArchitectureClassificationsPanel {...props} />); open();
  fireEvent.click(await screen.findByRole('button', { name: 'Review decision and contract' }));
  await screen.findByText('IRs: ir_existing');
  expect(api.getArchitectureClassifications).toHaveBeenLastCalledWith('board', 'spec', expect.any(AbortSignal), { candidateId: 'candidate', sourceDigest: 'a'.repeat(64) });
  expect(screen.getByText('Changed paths: /error_contract/retry')).toBeInTheDocument();
  expect(screen.getByText(/change list is truncated/)).toBeInTheDocument();
  expect(screen.getByText('Analyzed revisions: adopted v3')).toBeInTheDocument();
  expect(screen.getByText('By Author at 2026-09-19 · Spec version 8')).toBeInTheDocument();
  expect(screen.getByText(/private.invalid/)).toBeInTheDocument();
  expect(screen.queryByRole('link')).not.toBeInTheDocument();
});

it('retains the analyzed digest for retired contracts and keeps existing IR obligations visible', async () => {
  const retired = result();
  retired.items[0] = { ...retired.items[0], state: 'retired', current_source_digest: null, source_variant_count: 0, source_digests: [] };
  const history = details(); history.items[0] = { ...history.items[0], ...retired.items[0], current_contract: null };
  api.getArchitectureClassifications.mockResolvedValueOnce(retired).mockResolvedValueOnce(history);
  render(<ArchitectureClassificationsPanel {...props} />); open();
  fireEvent.click(await screen.findByRole('button', { name: 'Review decision and contract' }));
  await screen.findByText('IRs: ir_existing');
  expect(screen.getByText(/Existing IR obligations remain in scope/)).toBeInTheDocument();
  expect(screen.queryByText('Current contract')).not.toBeInTheDocument();
});

it('requires all read permissions and fetches anew after access is restored', async () => {
  api.getArchitectureClassifications.mockResolvedValue(result());
  const { rerender } = render(<ArchitectureClassificationsPanel {...props} canRead={false} />);
  expect(screen.getByText('Spec, architecture and IR read permissions are required.')).toBeInTheDocument();
  expect(api.getArchitectureClassifications).not.toHaveBeenCalled();
  rerender(<ArchitectureClassificationsPanel {...props} />); open();
  await screen.findByText('Orders · Current');
  rerender(<ArchitectureClassificationsPanel {...props} canRead={false} />);
  expect(screen.queryByText('Orders · Current')).not.toBeInTheDocument();
  api.getArchitectureClassifications.mockReturnValueOnce(new Promise(() => {}));
  rerender(<ArchitectureClassificationsPanel {...props} />);
  expect(screen.queryByText('Orders · Current')).not.toBeInTheDocument();
  expect(api.getArchitectureClassifications).toHaveBeenCalledTimes(2);
});

it.each(['permission', 'spec', 'version', 'refresh', 'page', 'close'])(
  'discards late detail responses after %s changes', async change => {
    let finish!: (value: ArchitectureClassificationsResponse) => void;
    api.getArchitectureClassifications.mockResolvedValueOnce(result()).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; })).mockResolvedValue(result({ items: [], spec_id: change === 'spec' ? 'next' : 'spec' }));
    const { rerender } = render(<ArchitectureClassificationsPanel {...props} />); open();
    fireEvent.click(await screen.findByRole('button', { name: 'Review decision and contract' }));
    const signal = api.getArchitectureClassifications.mock.calls[1][2] as AbortSignal;
    if (change === 'permission') rerender(<ArchitectureClassificationsPanel {...props} canRead={false} />);
    else if (change === 'spec') rerender(<ArchitectureClassificationsPanel {...props} specId="next" />);
    else if (change === 'version') rerender(<ArchitectureClassificationsPanel {...props} specVersion={10} />);
    else if (change === 'refresh') fireEvent.click(screen.getByRole('button', { name: 'Refresh classifications' }));
    else if (change === 'page') fireEvent.click(screen.getByRole('button', { name: 'Next classifications' }));
    else open();
    expect(signal.aborted).toBe(true);
    await act(async () => { finish(details()); });
    expect(screen.queryByText('IRs: ir_existing')).not.toBeInTheDocument();
  },
);

it.each(['board', 'spec', 'edition', 'version', 'digest'])(
  'rejects a detail response with a mismatched %s', async mismatch => {
    const invalid = details();
    if (mismatch === 'board') invalid.board_id = 'other';
    if (mismatch === 'spec') invalid.spec_id = 'other';
    if (mismatch === 'edition') invalid.spec_edition = 3;
    if (mismatch === 'version') invalid.spec_version = 10;
    if (mismatch === 'digest') invalid.items[0].source_digests = ['b'.repeat(64)];
    api.getArchitectureClassifications.mockResolvedValueOnce(result()).mockResolvedValueOnce(invalid);
    render(<ArchitectureClassificationsPanel {...props} />); open();
    fireEvent.click(await screen.findByRole('button', { name: 'Review decision and contract' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('completeness is unknown');
    expect(screen.queryByText('IRs: ir_existing')).not.toBeInTheDocument();
  },
);

it.each([403, 409, 500])('handles %i without leaking provider diagnostics', async status => {
  api.getArchitectureClassifications.mockResolvedValueOnce(result()).mockRejectedValueOnce(new AuthenticatedFetchError({ message: 'SECRET provider details', status }));
  render(<ArchitectureClassificationsPanel {...props} />); open();
  await screen.findByText('Orders · Current');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh classifications' }));
  await screen.findByRole('alert');
  expect(screen.queryByText('Orders · Current')).not.toBeInTheDocument();
  expect(screen.queryByText(/SECRET/)).not.toBeInTheDocument();
});
