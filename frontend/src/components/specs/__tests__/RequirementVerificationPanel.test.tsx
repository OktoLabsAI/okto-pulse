import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { RequirementVerificationPanel } from '../RequirementVerificationPanel';
import type { RequirementVerificationResponse, RequirementVerificationRow } from '@/types/requirement-verification';

const api = vi.hoisted(() => ({ getRequirementVerification: vi.fn(), updateSpecEntity: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const scope = { boardId: 'board', specId: 'spec', version: 8, edition: 2 };
const onSaved = vi.fn();
function row(extra: Partial<RequirementVerificationRow> = {}): RequirementVerificationRow {
  return { requirement_type: 'functional_requirement', requirement_id: 'fr', title: 'Five failed attempts block access',
    source_digest: 'a'.repeat(64), verification: null, qualification_origin: 'absent_or_invalid', qualification_resolved: false,
    default_proposal: { version: 'requirement-verification-defaults/v1', origin: 'product', requires_author_acceptance: true,
      verification: { mode: 'explicit', required_profiles: ['functional'], inheritance: [], evidence_policy_ref: 'pulse-verification/v1' } },
    criteria_paths: [], blockers: [{ code: 'verification_profile_required' }], blocker_count: 1, blockers_truncated: false,
    paths_total: 0, paths_offset: 0, paths_has_more: false, next_paths_offset: null, paths_unavailable: false, ...extra };
}
function response(extra: Partial<RequirementVerificationResponse> = {}): RequirementVerificationResponse {
  return { contract_version: 'requirement-verification/v1', board_id: 'board', spec_id: 'spec', spec_version: 8, spec_edition: 2,
    spec_status: 'draft', archived: false, population_complete: true, criteria_resolution_complete: false,
    total: 1, population_total: 1, resolved_count: 0, counts_scope: 'complete', offset: 0, limit: 25,
    has_more: false, next_offset: null, items: [row()], issues: [], issue_count: 0, issues_truncated: false,
    methods_evaluated: false, execution_evaluated: false, semantic_review_evaluated: false, delivery_evaluated: false, rollout_evaluated: false, ...extra };
}
const options = [
  { type: 'functional_requirement' as const, id: 'fr', title: 'Lock access' },
  { type: 'business_rule' as const, id: 'br', title: 'Blocking policy' },
];
function props(extra = {}) { return { scope, canRead: true, canEdit: () => true, options, criteria: [{ id: 'ac-lock', text: 'Five failures lock access' }], onSaved, ...extra }; }
function open() { fireEvent.click(screen.getByRole('button', { name: 'Review requirement qualification' })); }
async function edit() { open(); fireEvent.click(await screen.findByRole('button', { name: 'Edit qualification fr' })); }
function useDefault() { fireEvent.click(screen.getByRole('button', { name: /Use proposed default/ })); }
function save() { fireEvent.click(screen.getByRole('button', { name: 'Save qualification' })); }

describe('requirement qualification', () => {
  beforeEach(() => { vi.clearAllMocks(); api.getRequirementVerification.mockResolvedValue(response()); api.updateSpecEntity.mockResolvedValue({ success: true }); onSaved.mockResolvedValue(undefined); });
  it('loads only when requested and materializes a default only after deliberate versioned save', async () => {
    render(<RequirementVerificationPanel {...props()} />);
    expect(api.getRequirementVerification).not.toHaveBeenCalled();
    await edit();
    expect(screen.getByLabelText('functional')).not.toBeChecked();
    expect(api.updateSpecEntity).not.toHaveBeenCalled();
    useDefault(); save();
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(api.updateSpecEntity).toHaveBeenCalledExactlyOnceWith('spec', 'functional_requirement', 'fr', { verification: row().default_proposal!.verification }, 8);
    expect(screen.getByText(/Methods, work assignments, semantic review and evidence/)).toBeInTheDocument();
  });
  it('does not fetch without all read permissions', () => {
    render(<RequirementVerificationPanel {...props({ canRead: false })} />);
    expect(screen.getByText(/needs Spec, IR and OR read permissions/)).toBeInTheDocument();
    expect(api.getRequirementVerification).not.toHaveBeenCalled();
  });
  it('keeps read access without authoring authority', async () => {
    render(<RequirementVerificationPanel {...props({ canEdit: () => false })} />); open();
    expect(await screen.findByText(/Profiles not defined/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Edit qualification/ })).not.toBeInTheDocument();
  });
  it('discards the editor when authoring permission is lost', async () => {
    const view = render(<RequirementVerificationPanel {...props()} />); await edit();
    view.rerender(<RequirementVerificationPanel {...props({ canEdit: () => false })} />);
    expect(screen.queryByLabelText('Qualification mode')).not.toBeInTheDocument();
  });
  it.each([{ spec_version: 9 }, { spec_edition: 3 }, { board_id: 'other' }, { spec_id: 'other' }])('rejects a response from another scope or revision: %j', async mismatch => {
    api.getRequirementVerification.mockResolvedValue(response(mismatch));
    render(<RequirementVerificationPanel {...props()} />); open();
    expect(await screen.findByRole('alert')).toHaveTextContent('completeness is unknown');
    expect(screen.queryByRole('button', { name: /Edit qualification/ })).not.toBeInTheDocument();
  });
  it('preserves global pending counts while paging and marks incomplete populations', async () => {
    api.getRequirementVerification.mockResolvedValueOnce(response({ population_total: 27, total: 27, next_offset: 25, has_more: true, issue_count: 3 }))
      .mockResolvedValueOnce(response({ items: [], population_complete: false, total: null, population_total: null, counts_scope: 'observed', offset: 25 }));
    render(<RequirementVerificationPanel {...props()} />); open();
    expect(await screen.findByText(/27 requirements in scope · 3/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Next requirements' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('population is incomplete');
    expect(api.getRequirementVerification.mock.calls[1][3]).toEqual({ offset: 25, limit: 25 });
  });
  it('sends the selected terminal criterion, source digest and covered aspect only', async () => {
    const source = row({ requirement_type: 'business_rule', requirement_id: 'br', qualification_resolved: true,
      criteria_paths: ['ac-lock', 'ac-unrelated'].map(id => ({ criterion_id: id, profile: 'functional', path: [{ requirement_type: 'business_rule', requirement_id: 'br' }], aspects: [null], source_digests: [] })) });
    api.getRequirementVerification.mockResolvedValueOnce(response()).mockResolvedValueOnce(response({ items: [source] }));
    render(<RequirementVerificationPanel {...props()} />); await edit();
    fireEvent.change(screen.getByLabelText('Qualification mode'), { target: { value: 'inherited' } });
    fireEvent.click(screen.getByLabelText('functional'));
    fireEvent.change(screen.getByLabelText('Inheritance source'), { target: { value: JSON.stringify(['business_rule', 'br']) } });
    fireEvent.click(screen.getByRole('button', { name: 'Load source criteria' }));
    fireEvent.click(await screen.findByLabelText('Five failures lock access (ac-lock)'));
    fireEvent.change(screen.getByLabelText('Covered aspect'), { target: { value: 'Five attempts and blocking' } });
    fireEvent.click(screen.getByRole('button', { name: 'Use selected criteria' })); save();
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(api.updateSpecEntity.mock.calls[0][3].verification.inheritance).toEqual([{ source: { requirement_type: 'business_rule', requirement_id: 'br' }, source_digest: 'a'.repeat(64), criterion_ids: ['ac-lock'], covered_aspect: 'Five attempts and blocking' }]);
  });
  it('rejects a source snapshot that changed before selection', async () => {
    api.getRequirementVerification.mockResolvedValueOnce(response()).mockResolvedValueOnce(response({ spec_version: 9, items: [row({ requirement_type: 'business_rule', requirement_id: 'br' })] }));
    render(<RequirementVerificationPanel {...props()} />); await edit();
    fireEvent.change(screen.getByLabelText('Qualification mode'), { target: { value: 'inherited' } });
    fireEvent.change(screen.getByLabelText('Inheritance source'), { target: { value: JSON.stringify(['business_rule', 'br']) } });
    fireEvent.click(screen.getByRole('button', { name: 'Load source criteria' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Source changed or is unavailable');
    expect(api.updateSpecEntity).not.toHaveBeenCalled();
  });
  it('refuses incomplete drafts locally without manufacturing criterion selections', async () => {
    render(<RequirementVerificationPanel {...props()} />); await edit(); save();
    expect(screen.getByRole('alert')).toHaveTextContent('Choose the required profiles');
    expect(api.updateSpecEntity).not.toHaveBeenCalled();
  });
  it('keeps a refused edit and never calls refresh', async () => {
    api.updateSpecEntity.mockResolvedValue({ success: false, error_message: 'Version conflict' });
    render(<RequirementVerificationPanel {...props()} />); await edit(); useDefault(); save();
    expect(await screen.findByRole('alert')).toHaveTextContent('Version conflict');
    expect(screen.getByLabelText('functional')).toBeChecked();
    expect(onSaved).not.toHaveBeenCalled();
  });
  it('offers only reload after a confirmed save whose refresh failed', async () => {
    onSaved.mockRejectedValueOnce(new Error('offline'));
    render(<RequirementVerificationPanel {...props()} />); await edit(); useDefault(); save();
    expect(await screen.findByRole('alert')).toHaveTextContent('Saved. Refresh failed');
    expect(screen.getByRole('button', { name: 'Save qualification' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved qualification' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(2));
    expect(api.updateSpecEntity).toHaveBeenCalledOnce();
  });
  it('guards double click and ignores a confirmed response after unmount', async () => {
    let finish!: (value: unknown) => void;
    api.updateSpecEntity.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<RequirementVerificationPanel {...props()} />); await edit(); useDefault();
    const button = screen.getByRole('button', { name: 'Save qualification' });
    fireEvent.click(button); fireEvent.click(button);
    expect(api.updateSpecEntity).toHaveBeenCalledOnce(); view.unmount(); finish({ success: true });
    await Promise.resolve(); expect(onSaved).not.toHaveBeenCalled();
  });
});
