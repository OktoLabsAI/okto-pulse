/**
 * Tests for EvidenceBadge — Wave 2 frontend spec 5cb09dbc.
 *
 * Cobre TS6 (matrix de status x evidence presence) e TS7 (tooltip content).
 */

import { describe, expect, test } from 'vitest';
import { render, screen } from '@testing-library/react';

import { EvidenceBadge } from './EvidenceBadge';
import type { TestScenario } from '@/types';

function makeScenario(
  overrides: Partial<TestScenario> = {},
): Pick<TestScenario, 'status' | 'evidence'> {
  return {
    status: 'passed',
    evidence: null,
    ...overrides,
  };
}

const FULL_EVIDENCE = {
  test_file_path: 'tests/foo.py',
  test_function: 'test_bar',
  last_run_at: '2026-04-27T20:00:00',
  output_snippet: '1 passed',
  test_run_id: null,
};

describe('EvidenceBadge', () => {
  test('TS6 — passed com evidence renderiza badge verde "evidence"', () => {
    render(
      <EvidenceBadge
        scenario={makeScenario({ status: 'passed', evidence: FULL_EVIDENCE })}
      />,
    );
    expect(screen.getByTestId('evidence-badge-present')).toBeInTheDocument();
    expect(screen.getByText('evidence')).toBeInTheDocument();
  });

  test('TS6 — passed sem evidence renderiza badge cinza "no evidence"', () => {
    render(<EvidenceBadge scenario={makeScenario({ status: 'passed', evidence: null })} />);
    expect(screen.getByTestId('evidence-badge-missing')).toBeInTheDocument();
    expect(screen.getByText('no evidence')).toBeInTheDocument();
  });

  test('TS6 — automated com evidence renderiza badge verde', () => {
    render(
      <EvidenceBadge
        scenario={makeScenario({ status: 'automated', evidence: FULL_EVIDENCE })}
      />,
    );
    expect(screen.getByTestId('evidence-badge-present')).toBeInTheDocument();
  });

  test('TS6 — failed sem evidence renderiza badge cinza', () => {
    render(<EvidenceBadge scenario={makeScenario({ status: 'failed', evidence: null })} />);
    expect(screen.getByTestId('evidence-badge-missing')).toBeInTheDocument();
  });

  test('TS6 — ready não renderiza badge', () => {
    const { container } = render(
      <EvidenceBadge scenario={makeScenario({ status: 'ready', evidence: FULL_EVIDENCE })} />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId('evidence-badge-present')).not.toBeInTheDocument();
    expect(screen.queryByTestId('evidence-badge-missing')).not.toBeInTheDocument();
  });

  test('TS6 — draft não renderiza badge mesmo com evidence presente', () => {
    const { container } = render(
      <EvidenceBadge scenario={makeScenario({ status: 'draft', evidence: FULL_EVIDENCE })} />,
    );
    expect(container.firstChild).toBeNull();
  });

  test('TS6 — evidence object vazio (todos campos null) trata como ausente', () => {
    render(
      <EvidenceBadge
        scenario={makeScenario({
          status: 'passed',
          evidence: {
            test_file_path: null,
            test_function: null,
            last_run_at: null,
            output_snippet: null,
            test_run_id: null,
          },
        })}
      />,
    );
    expect(screen.getByTestId('evidence-badge-missing')).toBeInTheDocument();
  });

  test('TS7 — tooltip do badge verde inclui test_file_path, test_function e last_run_at', () => {
    render(
      <EvidenceBadge
        scenario={makeScenario({ status: 'passed', evidence: FULL_EVIDENCE })}
      />,
    );
    const badge = screen.getByTestId('evidence-badge-present');
    const tooltip = badge.getAttribute('title');
    expect(tooltip).toContain('tests/foo.py');
    expect(tooltip).toContain('test_bar');
    expect(tooltip).toContain('2026-04-27T20:00:00');
  });

  test('tooltip do badge cinza explica ausência', () => {
    render(<EvidenceBadge scenario={makeScenario({ status: 'passed', evidence: null })} />);
    const badge = screen.getByTestId('evidence-badge-missing');
    const tooltip = badge.getAttribute('title');
    expect(tooltip?.toLowerCase()).toContain('no evidence');
  });
});
