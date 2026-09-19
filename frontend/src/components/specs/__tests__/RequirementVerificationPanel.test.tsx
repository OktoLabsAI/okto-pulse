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
function props(extra = {}) { return { scope, canRead: true, canReadPlanning: true, canEdit: () => true, options, criteria: [{ id: 'ac-lock', text: 'Five failures lock access' }], onSaved, ...extra }; }
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
    expect(screen.getByText(/Implementation responsibilities, dependencies, semantic review and delivery evidence/)).toBeInTheDocument();
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
  it.each([true, false])('shows the global planning verdict without inferring proof (%s)', async complete => {
    api.getRequirementVerification.mockResolvedValue(response({ methods_evaluated: true,
      verification_work_evaluated: true, method_plan_complete: complete,
      verification_work_complete: complete, planning_population_complete: true,
      population_total: 30, total: 30, has_more: true, next_offset: 25,
    }));
    render(<RequirementVerificationPanel {...props()} />); open();
    expect(await screen.findByText(`Method planning: ${complete ? 'complete' : 'pending'} · Test Card planning: ${complete ? 'complete' : 'pending'}`)).toBeInTheDocument();
    expect(screen.getByText('Planning does not require a passing run and does not grant delivery credit.')).toBeInTheDocument();
  });
  it('keeps authorized qualification visible when planning reads are restricted', async () => {
    api.getRequirementVerification.mockResolvedValue(response({ planning_issues: [{ code: 'verification_planning_read_restricted' }] }));
    render(<RequirementVerificationPanel {...props()} />); open();
    expect(await screen.findByText(/requires scenario and Card read permissions/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit qualification fr' })).toBeInTheDocument();
    expect(screen.getByText('Method and Test Card planning is unavailable.')).toBeInTheDocument();
  });
  it('shows unsupported methods, missing Test Cards and bounded scenario details', async () => {
    api.getRequirementVerification.mockResolvedValue(response({ methods_evaluated: true,
      method_plan_complete: false, verification_work_complete: false, planning_population_complete: false,
      items: [row({ criteria_paths: [{ criterion_id: 'ac-lock', profile: 'functional', path: [], aspects: [], source_digests: [],
        scenario_count: 22, scenarios_truncated: true,
        scenario_plans: [{ scenario_id: 'ts', method: 'inspection', method_admitted: false, method_plan_complete: false,
          blockers: ['verification_method_unsupported'], work_blockers: ['verification_test_card_required'],
          test_card_ids: [], test_card_count: 0, test_cards_truncated: false }],
      }] })],
    }));
    render(<RequirementVerificationPanel {...props()} />); open();
    expect(await screen.findByText('This method has no supported evidence admission path.')).toBeInTheDocument();
    expect(screen.getByText('Assign this scenario to a Test Card.')).toBeInTheDocument();
    expect(screen.getByText(/planning considers all 22 scenarios/)).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('readiness is unknown');
  });
  it('discards loaded planning on loss of scenario or Card authority', async () => {
    api.getRequirementVerification.mockResolvedValue(response({ methods_evaluated: true,
      method_plan_complete: true, verification_work_complete: true, planning_population_complete: true }));
    const view = render(<RequirementVerificationPanel {...props()} />); open();
    expect(await screen.findByText('Method planning: complete · Test Card planning: complete')).toBeInTheDocument();
    view.rerender(<RequirementVerificationPanel {...props({ canReadPlanning: false })} />);
    expect(screen.queryByText(/Method planning: complete/)).not.toBeInTheDocument();
    open();
    expect(await screen.findByText('Method and Test Card planning is unavailable.')).toBeInTheDocument();
    expect(screen.queryByText(/Method planning: complete/)).not.toBeInTheDocument();
  });
  it('shows inherited responsibility only for its declared Card and preserves provenance', async () => {
    api.getRequirementVerification.mockResolvedValue(response({ implementation_plan_evaluated: true,
      implementation_plan_complete: true, implementation_population_complete: true,
      items: [row({ requirement_type: 'business_rule', requirement_id: 'br', contribution_count: 1,
        implementation_contributions: [{ card_id: 'authorization', origin: 'inherited', scope: 'selected_criteria',
          criterion_ids: ['ac-lock'], criterion_count: 1, criteria_truncated: false, summary: 'Authorization component',
          scope_sha256: 'a'.repeat(64), sources: [{ requirement_type: 'functional_requirement', requirement_id: 'fr-auth', scope_sha256: 'b'.repeat(64) }],
          source_count: 1, sources_truncated: false }],
      })],
    }));
    render(<RequirementVerificationPanel {...props()} />); open();
    expect(await screen.findByText('Implementation Card: authorization · inherited · selected criteria')).toBeInTheDocument();
    expect(screen.getByText('Inherited from: fr-auth')).toBeInTheDocument();
    expect(screen.getByText(/Contribution scope does not approve or complete the work/)).toBeInTheDocument();
    expect(screen.queryByText(/Implementation Card: ui/)).not.toBeInTheDocument();
  });
  it('reports ambiguous responsibility and summary truncation without a complete verdict', async () => {
    api.getRequirementVerification.mockResolvedValue(response({ implementation_plan_evaluated: true, implementation_plan_complete: false,
      items: [row({ contribution_blockers: ['implementation_inheritance_allocation_ambiguous'], contribution_count: 25, contributions_truncated: true })] }));
    render(<RequirementVerificationPanel {...props()} />); open();
    expect(await screen.findByText('Declared implementation scope: pending')).toBeInTheDocument();
    expect(screen.getByText(/Declare a direct allocation/)).toBeInTheDocument();
    expect(screen.getByText(/result considers all 25 contributions/)).toBeInTheDocument();
  });
  it('does not expose implementation facts without planning authority', async () => {
    api.getRequirementVerification.mockResolvedValue(response({ implementation_plan_evaluated: true, implementation_plan_complete: true,
      items: [row({ contribution_blockers: ['implementation_inheritance_allocation_ambiguous'] })] }));
    render(<RequirementVerificationPanel {...props({ canReadPlanning: false })} />); open();
    await screen.findByRole('button', { name: 'Edit qualification fr' });
    expect(screen.queryByText(/Declared implementation scope/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Declare a direct allocation/)).not.toBeInTheDocument();
  });
});
