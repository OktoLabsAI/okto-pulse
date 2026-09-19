import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CriterionVerificationPanel, type VerificationRequirementOption } from '../CriterionVerificationPanel';

import { verificationRequirementOptions } from '../criterionVerificationOptions';

const api = vi.hoisted(() => ({ updateSpecEntity: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const options: VerificationRequirementOption[] = [
  { type: 'functional_requirement', id: 'fr_one', title: 'Lock after five attempts' },
  { type: 'business_rule', id: 'br_one', title: 'Five-attempt policy' },
];
const criterion = { id: 'ac_one', text: 'Five failed attempts block access', status: 'active' };
const onSaved = vi.fn();
function props(extra = {}) { return { specId: 'spec-one', version: 8, criteria: [criterion], options, canEdit: true, onSaved, ...extra }; }
function edit() { fireEvent.click(screen.getByRole('button', { name: 'Edit verification ac_one' })); }
function add(type: string, id: string) {
  fireEvent.change(screen.getByLabelText('Requirement to link'), { target: { value: JSON.stringify([type, id]) } });
  fireEvent.click(screen.getByRole('button', { name: 'Add link' }));
}

describe('criterion verification authoring', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.updateSpecEntity.mockResolvedValue({ success: true, spec_version: 9 });
    onSaved.mockResolvedValue(undefined);
  });
  it('sends one versioned patch with typed many-to-many links and explicit aspects', async () => {
    render(<CriterionVerificationPanel {...props()} />);
    expect(api.updateSpecEntity).not.toHaveBeenCalled();
    edit();
    expect(screen.getByLabelText('Verification profile')).toHaveValue('');
    fireEvent.change(screen.getByLabelText('Verification profile'), { target: { value: 'functional' } });
    add('functional_requirement', 'fr_one');
    add('business_rule', 'br_one');
    fireEvent.change(screen.getByLabelText('Covered aspect 2'), { target: { value: 'Five attempts and blocking' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save verification plan' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(api.updateSpecEntity).toHaveBeenCalledExactlyOnceWith('spec-one', 'acceptance_criterion', 'ac_one', {
      verification_profile: 'functional', requirement_links: [
        { requirement_type: 'functional_requirement', requirement_id: 'fr_one', aspect: null },
        { requirement_type: 'business_rule', requirement_id: 'br_one', aspect: 'Five attempts and blocking' },
      ],
    }, 8);
  });
  it('preserves an unavailable existing link when editing only the profile', async () => {
    render(<CriterionVerificationPanel {...props({ criteria: [{ ...criterion, requirement_links: [{ requirement_type: 'integration_requirement', requirement_id: 'ir_hidden', aspect: 'Timeout' }] }] })} />);
    expect(screen.getByText(/Requirement unavailable.*ir_hidden/)).toBeInTheDocument();
    edit();
    fireEvent.change(screen.getByLabelText('Verification profile'), { target: { value: 'integration' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save verification plan' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(api.updateSpecEntity.mock.calls[0][3].requirement_links).toEqual([{ requirement_type: 'integration_requirement', requirement_id: 'ir_hidden', aspect: 'Timeout' }]);
  });
  it('allows incomplete draft metadata without inventing a default', async () => {
    render(<CriterionVerificationPanel {...props()} />); edit();
    fireEvent.click(screen.getByRole('button', { name: 'Save verification plan' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
    expect(api.updateSpecEntity.mock.calls[0][3]).toEqual({ verification_profile: null, requirement_links: [] });
  });
  it('shows read-only metadata without edit permission', () => {
    render(<CriterionVerificationPanel {...props({ canEdit: false, criteria: [{ ...criterion, verification_profile: 'functional', requirement_links: [{ requirement_type: 'business_rule', requirement_id: 'br_one' }] }] })} />);
    expect(screen.getByText(/Five-attempt policy.*br_one/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Edit verification/ })).not.toBeInTheDocument();
    expect(api.updateSpecEntity).not.toHaveBeenCalled();
  });
  it('removes the editor when permission is lost', () => {
    const view = render(<CriterionVerificationPanel {...props()} />); edit();
    view.rerender(<CriterionVerificationPanel {...props({ canEdit: false })} />);
    expect(screen.queryByLabelText('Verification profile')).not.toBeInTheDocument();
  });
  it('avoids duplicate or ambiguous target choices', () => {
    render(<CriterionVerificationPanel {...props({ options: [...options, options[1]] })} />); edit();
    expect(screen.queryByRole('option', { name: /Five-attempt/ })).not.toBeInTheDocument();
    add('functional_requirement', 'fr_one');
    expect(screen.queryByRole('option', { name: /Lock after/ })).not.toBeInTheDocument();
  });
  it.each(['legacy text', { text: 'No ID' }, { ...criterion, verification_profile: 'unknown' }, { ...criterion, requirement_links: [{ requirement_type: 'functional_requirement', requirement_id: 'fr_one', verified: true }] }])('does not silently rewrite unsupported or legacy metadata', value => {
    render(<CriterionVerificationPanel {...props({ criteria: [value] })} />);
    expect(screen.queryByRole('button', { name: /Edit verification/ })).not.toBeInTheDocument();
    expect(api.updateSpecEntity).not.toHaveBeenCalled();
  });
  it('keeps edits after a refused write and does not reload', async () => {
    api.updateSpecEntity.mockResolvedValue({ success: false, error_message: 'Spec version conflict' });
    render(<CriterionVerificationPanel {...props()} />); edit();
    add('business_rule', 'br_one');
    fireEvent.click(screen.getByRole('button', { name: 'Save verification plan' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Spec version conflict');
    expect(screen.getByLabelText('Covered aspect 1')).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });
  it('reload failure after success cannot submit the write again', async () => {
    onSaved.mockRejectedValueOnce(new Error('network'));
    render(<CriterionVerificationPanel {...props()} />); edit();
    fireEvent.click(screen.getByRole('button', { name: 'Save verification plan' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Saved. Refresh failed');
    fireEvent.click(screen.getByRole('button', { name: 'Reload saved criterion' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(2));
    expect(api.updateSpecEntity).toHaveBeenCalledOnce();
  });
  it('suppresses double-click writes and old-context reloads', async () => {
    let finish!: (value: unknown) => void;
    api.updateSpecEntity.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const view = render(<CriterionVerificationPanel {...props()} />); edit();
    const save = screen.getByRole('button', { name: 'Save verification plan' });
    fireEvent.click(save); fireEvent.click(save);
    view.unmount();
    finish({ success: true });
    await waitFor(() => expect(api.updateSpecEntity).toHaveBeenCalledOnce());
    expect(onSaved).not.toHaveBeenCalled();
  });
  it('discards the old draft when the Spec version changes', () => {
    const view = render(<CriterionVerificationPanel {...props()} />); edit();
    add('functional_requirement', 'fr_one');
    view.rerender(<CriterionVerificationPanel {...props({ version: 9 })} />);
    expect(screen.queryByLabelText('Covered aspect 1')).not.toBeInTheDocument();
  });
  it('filters restricted, inactive and legacy requirement choices', () => {
    const collections = { functional_requirements: ['legacy', { id: 'fr', text: 'FR' }], business_rules: [{ id: 'br', status: 'revoked' }], integration_requirements: [{ id: 'ir', title: 'Secret IR' }], observability_requirements: [{ id: 'or', title: 'Secret OR' }] };
    expect(verificationRequirementOptions(collections, false, false)).toEqual([{ type: 'functional_requirement', id: 'fr', title: 'FR' }]);
    expect(verificationRequirementOptions(collections, true, true)).toHaveLength(3);
  });
});
