import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import type { TestScenario } from '@/types';
import { ScenarioReceiptDetails } from '../ScenarioReceiptDetails';

afterEach(cleanup);
const scenario: TestScenario = { id: 'scenario', title: 'Capacity', status: 'failed', scenario_type: 'unit',
  verification_method: 'automated_test', given: 'G', when: 'W', then: 'T', notes: null, linked_criteria: [], linked_task_ids: [],
  evidence: { execution_receipt: 'opaque.receipt', evidence_class: 'run_log', output_snippet: '<script>Failure</script>',
    test_run_id: 'run-1', replay_command: 'pytest tests/test_capacity.py', execution_attestation: {
      schema_version: 2, run_id: 'run-1', executed_at: '2026-10-14T10:00:00Z', scenario_id: 'scenario', scenario_sha256: 'a'.repeat(64),
      outcome: 'failed', product_runtime_exercised: true, manifest_sha256: 'b'.repeat(64), attestation_sha256: 'c'.repeat(64),
      provenance: { producer: 'pytest', producer_version: '1', adapter: 'native', environment: 'fixture' },
      assertions: [{ name: 'capacity bound', expected: 2, observed: 3, status: 'failed', message: 'Over capacity' }],
    } } };

it('shows the exact stored run and structured assertions without executing replay or claiming verification', () => {
  const { container } = render(<ScenarioReceiptDetails scenarios={[scenario]} receipt="opaque.receipt" />);
  expect(screen.getByRole('article')).toHaveAttribute('data-readiness-ref', 'receipt:opaque.receipt');
  expect(screen.getByText(/independent approval remain separate/)).toBeInTheDocument();
  expect(screen.getByText('<script>Failure</script>')).toBeInTheDocument();
  expect(container.querySelector('script')).toBeNull();
  fireEvent.click(screen.getByText('Execution attestation'));
  fireEvent.click(screen.getByText('capacity bound · failed'));
  expect(screen.getByText('Over capacity')).toBeVisible();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});

it('drops details if the native pointer changes or becomes ambiguous', () => {
  const view = render(<ScenarioReceiptDetails scenarios={[scenario]} receipt="opaque.receipt" />);
  view.rerender(<ScenarioReceiptDetails scenarios={[{ ...scenario, evidence: { execution_receipt: 'new' } }]} receipt="opaque.receipt" />);
  expect(screen.getByRole('alert')).toHaveTextContent('unavailable');
  expect(screen.queryByText('<script>Failure</script>')).not.toBeInTheDocument();
  view.rerender(<ScenarioReceiptDetails scenarios={[scenario, { ...scenario, id: 'second' }]} receipt="opaque.receipt" />);
  expect(screen.getByRole('alert')).toHaveTextContent('ambiguous');
});
