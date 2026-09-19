import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { ScenarioVerificationMethodEditor } from '../ScenarioVerificationMethodEditor';
import type { TestScenario } from '@/types';
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
it('prevents double writes and ignores a completion from an obsolete version', async () => {
  let finish!: (value: unknown) => void;
  api.updateScenarioVerificationMethod.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<ScenarioVerificationMethodEditor {...props()} />); choose('automated_test');
  const button = screen.getByRole('button', { name: 'Save verification method' }); fireEvent.click(button); fireEvent.click(button);
  expect(api.updateScenarioVerificationMethod).toHaveBeenCalledOnce();
  view.rerender(<ScenarioVerificationMethodEditor {...props({ version: 8 })} />); finish({ scenario_id: 'ts' });
  await Promise.resolve(); expect(onSaved).not.toHaveBeenCalled();
  expect(screen.getByLabelText('Verification method ts')).toHaveValue('');
});
