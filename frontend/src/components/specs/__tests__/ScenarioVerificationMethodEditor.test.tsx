import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { ScenarioVerificationMethodEditor } from '../ScenarioVerificationMethodEditor';
import type { TestScenario } from '@/types';
import { AuthenticatedFetchError } from '@/lib/authFetch';
const api = vi.hoisted(() => ({ updateScenarioVerificationMethod: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const onSaved = vi.fn();
const scenario: TestScenario = { id: 'ts', title: 'Observe', scenario_type: 'manual', status: 'ready', given: 'G', when: 'W', then: 'T', notes: null, linked_criteria: ['ac'], linked_task_ids: null };
function props(extra = {}) { return { boardId: 'board', specId: 'spec', version: 7, scenario, canEdit: true, onSaved, ...extra }; }
function choose(value: string) { fireEvent.change(screen.getByLabelText('Verification method ts'), { target: { value } }); }
function save() { fireEvent.click(screen.getByRole('button', { name: 'Save verification method' })); }
beforeEach(() => { vi.clearAllMocks(); api.updateScenarioVerificationMethod.mockResolvedValue({ scenario_id: 'ts', evidence_invalidated: false, updated_fields: ['verification_method'] }); onSaved.mockResolvedValue(undefined); });

it('does not infer a method from a manual scenario and sends only the versioned method edit', async () => {
  render(<ScenarioVerificationMethodEditor {...props()} />);
  expect(screen.getByLabelText('Verification method ts')).toHaveValue('');
  expect(api.updateScenarioVerificationMethod).not.toHaveBeenCalled();
  choose('automated_test'); save();
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(api.updateScenarioVerificationMethod).toHaveBeenCalledExactlyOnceWith('board', 'spec', 'ts', 'automated_test', 7);
  expect(scenario.scenario_type).toBe('manual');
});
it.each(['static_analysis', 'inspection', 'demonstration'])('authors %s without claiming installed support or proof', async method => {
  render(<ScenarioVerificationMethodEditor {...props()} />); choose(method); save();
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(screen.getByText(/unsupported methods remain pending/)).toBeInTheDocument();
  expect(api.updateScenarioVerificationMethod.mock.calls[0][3]).toBe(method);
});
it('makes removal deliberate and warns about invalidating existing evidence', async () => {
  render(<ScenarioVerificationMethodEditor {...props({ scenario: { ...scenario, verification_method: 'automated_test', evidence: { test_run_id: 'old' } } })} />);
  expect(screen.getByText(/invalidates the current scenario evidence/)).toBeInTheDocument();
  choose(''); save(); await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(api.updateScenarioVerificationMethod.mock.calls[0][3]).toBeNull();
});
it('keeps unknown historic methods visible without silently rewriting them', () => {
  render(<ScenarioVerificationMethodEditor {...props({ scenario: { ...scenario, verification_method: 'old-method' } })} />);
  expect(screen.getByLabelText('Verification method ts')).toHaveValue('old-method');
  expect(screen.getByRole('button', { name: 'Save verification method' })).toBeDisabled();
});
it('preserves read-only visibility and removes authoring on permission loss', () => {
  const view = render(<ScenarioVerificationMethodEditor {...props()} />); choose('inspection');
  view.rerender(<ScenarioVerificationMethodEditor {...props({ canEdit: false })} />);
  expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  expect(screen.getByText('Verification method: Not defined')).toBeInTheDocument();
});
it('keeps the selection after a rejected stale write', async () => {
  api.updateScenarioVerificationMethod.mockRejectedValue(new Error('spec_version_conflict'));
  render(<ScenarioVerificationMethodEditor {...props()} />); choose('automated_test'); save();
  expect(await screen.findByRole('alert')).toHaveTextContent('spec_version_conflict');
  expect(screen.getByLabelText('Verification method ts')).toHaveValue('automated_test');
  expect(onSaved).not.toHaveBeenCalled();
});
it('never resubmits a confirmed mutation after reload fails', async () => {
  onSaved.mockRejectedValueOnce(new Error('offline'));
  render(<ScenarioVerificationMethodEditor {...props()} />); choose('automated_test'); save();
  expect(await screen.findByRole('alert')).toHaveTextContent('Saved. Reload');
  fireEvent.click(screen.getByRole('button', { name: 'Reload saved method' }));
  await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(2));
  expect(api.updateScenarioVerificationMethod).toHaveBeenCalledOnce();
});
it('prevents double writes and ignores a completion from another Spec', async () => {
  let finish!: (value: unknown) => void;
  api.updateScenarioVerificationMethod.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<ScenarioVerificationMethodEditor {...props()} />); choose('automated_test');
  const button = screen.getByRole('button', { name: 'Save verification method' }); fireEvent.click(button); fireEvent.click(button);
  expect(api.updateScenarioVerificationMethod).toHaveBeenCalledOnce();
  view.rerender(<ScenarioVerificationMethodEditor {...props({ specId: 'other' })} />); finish({ scenario_id: 'ts' });
  await Promise.resolve(); expect(onSaved).not.toHaveBeenCalled();
  expect(screen.getByLabelText('Verification method ts')).toHaveValue('');
});

it('preserves a selected method across a new version until the author reviews the base', async () => {
  const view = render(<ScenarioVerificationMethodEditor {...props()} />); choose('inspection');
  view.rerender(<ScenarioVerificationMethodEditor {...props({ version: 8, scenario: { ...scenario, verification_method: 'static_analysis' } })} />);
  expect(screen.getByLabelText('Verification method ts')).toHaveValue('inspection');
  expect(screen.getByRole('button', { name: 'Save verification method' })).toBeDisabled();
  expect(screen.getByText(/Current saved method: static_analysis/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Review method base' })); save();
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(api.updateScenarioVerificationMethod).toHaveBeenCalledExactlyOnceWith('board', 'spec', 'ts', 'inspection', 8);
});

it('keeps a native version conflict blocked until a newer valid read', async () => {
  api.updateScenarioVerificationMethod.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'spec_version_conflict' }));
  const view = render(<ScenarioVerificationMethodEditor {...props()} />); choose('inspection'); save();
  expect(await screen.findByRole('button', { name: 'Review method base' })).toBeDisabled();
  view.rerender(<ScenarioVerificationMethodEditor {...props({ version: 8, readCurrent: false })} />);
  expect(screen.getByLabelText('Verification method ts')).toHaveValue('inspection');
  expect(screen.getByRole('button', { name: 'Review method base' })).toBeDisabled();
  view.rerender(<ScenarioVerificationMethodEditor {...props({ version: 8 })} />);
  fireEvent.click(screen.getByRole('button', { name: 'Review method base' })); save();
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(api.updateScenarioVerificationMethod.mock.calls[1][4]).toBe(8);
});

it('does not confuse an unrelated 409 with a version conflict', async () => {
  api.updateScenarioVerificationMethod.mockRejectedValueOnce(new AuthenticatedFetchError({ status: 409, message: 'Scenario is locked' }));
  render(<ScenarioVerificationMethodEditor {...props()} />); choose('inspection'); save();
  expect(await screen.findByRole('alert')).toHaveTextContent('Scenario is locked');
  expect(screen.queryByRole('button', { name: 'Review method base' })).not.toBeInTheDocument();
  expect(screen.getByLabelText('Verification method ts')).toHaveValue('inspection');
});

it('confirms a completed write even when the same Spec receives another revision while it is pending', async () => {
  let finish!: (value: unknown) => void;
  api.updateScenarioVerificationMethod.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<ScenarioVerificationMethodEditor {...props()} />); choose('inspection'); save();
  view.rerender(<ScenarioVerificationMethodEditor {...props({ version: 8 })} />);
  await act(async () => finish({ scenario_id: 'ts' }));
  expect(onSaved).toHaveBeenCalledOnce();
  expect(screen.getByRole('button', { name: 'Save verification method' })).toBeDisabled();
  expect(api.updateScenarioVerificationMethod).toHaveBeenCalledOnce();
});

it('starts another edit only from a newly read saved base', async () => {
  const view = render(<ScenarioVerificationMethodEditor {...props()} />); choose('inspection'); save();
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(screen.getByRole('button', { name: 'Edit current method' })).toBeDisabled();
  view.rerender(<ScenarioVerificationMethodEditor {...props({ version: 8, scenario: { ...scenario, verification_method: 'inspection' } })} />);
  fireEvent.click(screen.getByRole('button', { name: 'Edit current method' }));
  expect(screen.getByLabelText('Verification method ts')).toHaveValue('inspection');
  choose('static_analysis'); save();
  await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(2));
  expect(api.updateScenarioVerificationMethod.mock.calls[1][4]).toBe(8);
});
