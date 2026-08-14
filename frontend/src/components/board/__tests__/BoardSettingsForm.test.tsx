import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { BoardSettingsForm } from '../BoardSettingsForm';
import type { BoardSettings } from '@/types';

const baseSettings: BoardSettings = {
  max_scenarios_per_card: 2,
  skip_test_coverage_global: false,
  skip_rules_coverage_global: false,
  skip_trs_coverage_global: false,
  skip_contract_coverage_global: false,
  skip_ir_coverage_global: false,
  skip_or_coverage_global: false,
  skip_decisions_coverage_global: false,
  reviewer_separation_mode: 'enforce',
  require_task_validation: false,
  min_confidence: 70,
  min_completeness: 70,
  max_drift: 30,
};

describe('BoardSettingsForm — independent reviewer policy', () => {
  it('renders the persisted mode and offers all three policies', () => {
    render(<BoardSettingsForm settings={baseSettings} onChange={vi.fn()} />);

    const group = screen.getByTestId('reviewer-separation-mode');
    expect(group.querySelectorAll('button')).toHaveLength(3);
    expect(screen.getByTestId('reviewer-separation-mode-enforce')).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('patches reviewer_separation_mode when a policy is selected', () => {
    const onChange = vi.fn();
    render(<BoardSettingsForm settings={baseSettings} onChange={onChange} />);

    fireEvent.click(screen.getByTestId('reviewer-separation-mode-warn'));

    expect(onChange).toHaveBeenCalledWith({ reviewer_separation_mode: 'warn' });
  });

  it('projects an absent or unknown legacy value as off', () => {
    const { rerender } = render(
      <BoardSettingsForm
        settings={{ ...baseSettings, reviewer_separation_mode: undefined }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByTestId('reviewer-separation-mode-off')).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    rerender(
      <BoardSettingsForm
        settings={{ ...baseSettings, reviewer_separation_mode: 'invalid' as never }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByTestId('reviewer-separation-mode-off')).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });
});

describe('BoardSettingsForm — requirement lint languages', () => {
  it('renders the five supported languages with none selected by default', () => {
    render(<BoardSettingsForm settings={baseSettings} onChange={vi.fn()} />);
    const group = screen.getByTestId('lint-languages');
    expect(group.querySelectorAll('button')).toHaveLength(5);
    for (const code of ['pt-BR', 'en-US', 'es-ES', 'de-DE', 'fr-FR']) {
      expect(screen.getByTestId(`lint-language-${code}`)).toHaveAttribute(
        'aria-pressed',
        'false',
      );
    }
  });

  it('selecting a language patches lint_languages with the added code', () => {
    const onChange = vi.fn();
    render(<BoardSettingsForm settings={baseSettings} onChange={onChange} />);
    fireEvent.click(screen.getByTestId('lint-language-de-DE'));
    expect(onChange).toHaveBeenCalledWith({ lint_languages: ['de-DE'] });
  });

  it('deselecting a language removes only that code, preserving order', () => {
    const onChange = vi.fn();
    render(
      <BoardSettingsForm
        settings={{ ...baseSettings, lint_languages: ['pt-BR', 'de-DE'] }}
        onChange={onChange}
      />,
    );
    expect(screen.getByTestId('lint-language-pt-BR')).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    fireEvent.click(screen.getByTestId('lint-language-pt-BR'));
    expect(onChange).toHaveBeenCalledWith({ lint_languages: ['de-DE'] });
  });
});

describe('BoardSettingsForm — spec validation metric gates', () => {
  it('renders the five canonical thresholds without the legacy completeness threshold', () => {
    render(
      <BoardSettingsForm
        settings={{ ...baseSettings, require_spec_validation: true }}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByTestId('bsf-num-min_spec_confidence')).toHaveValue(70);
    expect(screen.getByTestId('bsf-num-min_spec_clarity')).toHaveValue(80);
    expect(screen.getByTestId('bsf-num-min_spec_assertiveness')).toHaveValue(80);
    expect(screen.getByTestId('bsf-num-min_spec_decidability')).toHaveValue(80);
    expect(screen.getByTestId('bsf-num-max_spec_ambiguity')).toHaveValue(30);
    expect(screen.queryByTestId('bsf-num-min_spec_completeness')).not.toBeInTheDocument();

    const thresholds = screen.getByTestId('spec-validation-thresholds');
    expect(thresholds.children).toHaveLength(5);
    for (const metric of [
      'min_spec_confidence',
      'min_spec_clarity',
      'min_spec_assertiveness',
      'min_spec_decidability',
      'max_spec_ambiguity',
    ]) {
      expect(screen.getByTestId(`bsf-row-${metric}`)).toContainElement(
        screen.getByTestId(`bsf-num-${metric}`),
      );
    }
  });

  it.each([
    ['bsf-num-min_spec_confidence', 'min_spec_confidence', 73],
    ['bsf-num-min_spec_clarity', 'min_spec_clarity', 81],
    ['bsf-num-min_spec_assertiveness', 'min_spec_assertiveness', 82],
    ['bsf-num-min_spec_decidability', 'min_spec_decidability', 83],
    ['bsf-num-max_spec_ambiguity', 'max_spec_ambiguity', 24],
  ] as const)('commits %s on blur', (testId, key, value) => {
    const onChange = vi.fn();
    render(
      <BoardSettingsForm
        settings={{ ...baseSettings, require_spec_validation: true }}
        onChange={onChange}
      />,
    );

    const input = screen.getByTestId(testId);
    fireEvent.change(input, { target: { value: String(value) } });
    fireEvent.blur(input);

    expect(onChange).toHaveBeenCalledWith({ [key]: value });
  });
});

describe('BoardSettingsForm — execution report evidence mode', () => {
  it('defaults to off and offers the three modes', () => {
    render(<BoardSettingsForm settings={baseSettings} onChange={vi.fn()} />);
    const group = screen.getByTestId('impact-evidence-mode');
    expect(group.querySelectorAll('button')).toHaveLength(3);
    expect(screen.getByTestId('impact-evidence-mode-off')).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('selecting a mode patches impact_evidence_mode', () => {
    const onChange = vi.fn();
    render(<BoardSettingsForm settings={baseSettings} onChange={onChange} />);
    fireEvent.click(screen.getByTestId('impact-evidence-mode-advisory'));
    expect(onChange).toHaveBeenCalledWith({ impact_evidence_mode: 'advisory' });
  });

  it('the shortcut toggle flips between require and off', () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <BoardSettingsForm settings={baseSettings} onChange={onChange} />,
    );
    fireEvent.click(screen.getByTestId('toggle-impact-evidence-gate'));
    expect(onChange).toHaveBeenCalledWith({ impact_evidence_mode: 'require' });

    rerender(
      <BoardSettingsForm
        settings={{ ...baseSettings, impact_evidence_mode: 'require' }}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByTestId('toggle-impact-evidence-gate'));
    expect(onChange).toHaveBeenLastCalledWith({ impact_evidence_mode: 'off' });
  });

  it('an unknown persisted value reads as off instead of breaking the screen', () => {
    render(
      <BoardSettingsForm
        settings={{ ...baseSettings, impact_evidence_mode: 'banana' as never }}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByTestId('impact-evidence-mode-off')).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });
});

describe('BoardSettingsForm — agent-mediated Code Traceability', () => {
  it('renders the permanent source-blind disclosure without repository controls', () => {
    render(<BoardSettingsForm settings={baseSettings} onChange={vi.fn()} />);

    expect(screen.getByText('Agent-mediated Code Traceability')).toBeInTheDocument();
    expect(screen.getByTestId('code-traceability-source-blind-disclosure')).toHaveTextContent(
      'Pulse does not access source code',
    );
    expect(screen.queryByLabelText(/repository|provider|checkout|filesystem/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /connect|sync|clone|probe|submit|check/i })).not.toBeInTheDocument();
  });

  it('offers only Advisory and Blocking, with Advisory as the safe default', () => {
    const onChange = vi.fn();
    render(<BoardSettingsForm settings={baseSettings} onChange={onChange} />);

    const mode = screen.getByLabelText<HTMLSelectElement>(
      'Code Traceability enforcement mode',
    );
    expect(mode).toHaveValue('advisory');
    expect(Array.from(mode.options, (option) => option.value)).toEqual([
      'advisory',
      'blocking',
    ]);
    expect(
      screen.getByTestId('code-traceability-enforcement-guidance'),
    ).toHaveTextContent(
      'Missing Technical Anchors or Code Evidence does not block applicable transitions',
    );
    expect(
      screen.getByTestId('code-traceability-enforcement-guidance'),
    ).toHaveTextContent(
      'repeat repository analysis after entity-version or source-head drift',
    );

    fireEvent.change(mode, { target: { value: 'blocking' } });

    expect(onChange).toHaveBeenCalledWith({
      code_traceability: expect.objectContaining({
        mode: 'blocking',
        evidence_attestation: 'preferred',
        target_resolution: 'advisory',
        accepted_attestor_policy: 'granular_permission',
        receipt_content: 'safe_excerpt',
      }),
    });
  });

  it('projects the retired Off value as Advisory', () => {
    render(
      <BoardSettingsForm
        settings={{
          ...baseSettings,
          code_traceability: {
            mode: 'off',
            evidence_attestation: 'preferred',
            target_resolution: 'advisory',
            accepted_attestor_policy: 'granular_permission',
            minimum_trust: 'single_attestation',
            preflight_freshness_seconds: 1800,
            overlap_policy: 'warn',
            observed_state_policy: 'allow_dirty_attestation',
            receipt_content: 'safe_excerpt',
          },
        }}
        onChange={vi.fn()}
      />,
    );

    expect(
      screen.getByLabelText('Code Traceability enforcement mode'),
    ).toHaveValue('advisory');
  });

  it('explains the current-coverage requirement in Blocking mode', () => {
    render(
      <BoardSettingsForm
        settings={{
          ...baseSettings,
          code_traceability: {
            mode: 'blocking',
            evidence_attestation: 'required',
            target_resolution: 'required_current_receipt',
            accepted_attestor_policy: 'granular_permission',
            minimum_trust: 'single_attestation',
            preflight_freshness_seconds: 1800,
            overlap_policy: 'warn',
            observed_state_policy: 'allow_dirty_attestation',
            receipt_content: 'safe_excerpt',
          },
        }}
        onChange={vi.fn()}
      />,
    );

    expect(
      screen.getByTestId('code-traceability-enforcement-guidance'),
    ).toHaveTextContent(
      'Missing requirements selected by the Code Evidence, Technical Anchor and attestation sub-policies become blockers',
    );
  });
});
