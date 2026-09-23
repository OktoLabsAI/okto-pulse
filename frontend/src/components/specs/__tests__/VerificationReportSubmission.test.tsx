import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { VerificationReportSubmission } from '../VerificationReportSubmission';
import { EvidenceBadge } from '../EvidenceBadge';
import { VerificationReportDetails } from '../VerificationReportDetails';
import type { TestScenario, TestScenarioEvidence } from '@/types';
const api = vi.hoisted(() => ({ admitTestVerificationReport: vi.fn(), updateTestScenarioStatus: vi.fn() }));
vi.mock('@/services/api', () => ({ useDashboardApi: () => api }));
const onSaved = vi.fn();
const onRejected = vi.fn();
const scenario: TestScenario = { id: 'ts', title: 'Inspect', verification_method: 'inspection', scenario_type: 'manual', status: 'ready', given: 'G', when: 'W', then: 'T', notes: null, linked_criteria: ['ac'], linked_task_ids: null };
const report = { method: 'inspection', result: 'passed', report_id: 'report-1', conclusion: 'Observed condition', observed_at: '2026-09-23T10:00:00Z', observations: [] };
const evidence: TestScenarioEvidence = { evidence_class: 'verification_report', execution_receipt: 'server-receipt', report_author_id: 'author', verification_report: { ...report, result: 'passed' } };
function props(extra = {}) { return { specId: 'spec', scenario, canSubmit: true, allowedResults: ['passed', 'failed'], onSaved, onRejected, ...extra }; }
function fill(value = report) { fireEvent.change(screen.getByLabelText('Verification report ts'), { target: { value: JSON.stringify(value) } }); }
function submit() { fireEvent.click(screen.getByRole('button', { name: 'Record verification report' })); }
beforeEach(() => { vi.clearAllMocks(); api.admitTestVerificationReport.mockResolvedValue({ evidence }); api.updateTestScenarioStatus.mockResolvedValue({}); onSaved.mockResolvedValue(undefined); onRejected.mockResolvedValue(false); });

it('records only the unchanged server-issued evidence through the scoped status writer', async () => {
  render(<VerificationReportSubmission {...props()} />); fill(); submit();
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(api.admitTestVerificationReport).toHaveBeenCalledExactlyOnceWith('spec', 'ts', report);
  expect(api.updateTestScenarioStatus).toHaveBeenCalledExactlyOnceWith('spec', 'ts', { status: 'passed', evidence });
  expect(screen.getByText(/Submission does not approve the Test Card/)).toBeInTheDocument();
});
it.each([{ canSubmit: false }, { allowedResults: [] }, { scenario: { ...scenario, verification_method: 'automated_test' } }])('does not expose unavailable or unauthorized writes %j', override => {
  render(<VerificationReportSubmission {...props(override)} />);
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});
it('rejects a mismatched method locally and never submits a forged result', async () => {
  render(<VerificationReportSubmission {...props()} />); fill({ ...report, method: 'static_analysis' }); submit();
  await screen.findByRole('alert'); expect(api.admitTestVerificationReport).not.toHaveBeenCalled();
  expect(api.updateTestScenarioStatus).not.toHaveBeenCalled();
});
it('keeps denied or stale submissions out of the status writer', async () => {
  api.admitTestVerificationReport.mockRejectedValue(new Error('verification_report_criterion_scope_mismatch'));
  render(<VerificationReportSubmission {...props()} />); fill(); submit();
  expect(await screen.findByRole('alert')).toHaveTextContent('criterion_scope_mismatch');
  expect(api.updateTestScenarioStatus).not.toHaveBeenCalled(); expect(onSaved).not.toHaveBeenCalled();
});
it('surfaces a final policy denial without treating admission as persistence', async () => {
  api.updateTestScenarioStatus.mockRejectedValue(new Error('policy_compliance_blocked'));
  render(<VerificationReportSubmission {...props()} />); fill(); submit();
  expect(await screen.findByRole('alert')).toHaveTextContent('policy_compliance_blocked');
  expect(onRejected).toHaveBeenCalled(); expect(onSaved).not.toHaveBeenCalled();
});
it('does not write a result after switching scope while admission is pending', async () => {
  let finish!: (value: { evidence: TestScenarioEvidence }) => void;
  api.admitTestVerificationReport.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<VerificationReportSubmission {...props()} />); fill(); submit();
  view.rerender(<VerificationReportSubmission {...props({ specId: 'other-spec' })} />);
  finish({ evidence }); await waitFor(() => expect(screen.getByLabelText('Verification report ts')).toHaveValue(''));
  expect(api.updateTestScenarioStatus).not.toHaveBeenCalled();
});
it('shows external authorship and observations without claiming replay or approval', () => {
  render(<><EvidenceBadge scenario={{ status: 'passed', evidence }} /><VerificationReportDetails evidence={evidence} /></>);
  expect(screen.getByTestId('evidence-badge-report')).toHaveAttribute('data-replayable', 'false');
  expect(screen.getByText('Submission author: author')).toBeInTheDocument();
  expect(screen.getByText(/Independent approval is separate/)).toBeInTheDocument();
  expect(screen.getByText(/Observed condition/)).toBeInTheDocument();
});
it('marks an unsigned historical report as unverified', () => {
  render(<EvidenceBadge scenario={{ status: 'passed', evidence: { ...evidence, execution_receipt: null } }} />);
  expect(screen.getByTestId('evidence-badge-report')).toHaveTextContent('unverified');
});
